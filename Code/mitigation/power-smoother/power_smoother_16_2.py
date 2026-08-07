#!/usr/bin/env python3
"""
power_smoother_16_2.py
=======================
Background daemon v16.2 (server variant): hybrid CPU-utilisation + RAPL
detection, retuned for a 128-core / ~225-400 W server ("mycroft") instead of
the small local dev machine v16 was tuned on.

Hardware profile this build targets
------------------------------------
  Cores                : 128
  Idle baseline power   : ~225 W
  Full-workload peak    : ~400 W  (workload swing ≈ 175 W)
  Expected cliff rate   : ~3 W/ms (-3000 W/s)

No detection/control *logic* changed from power_smoother_16.py — every
change here is either a tunable constant rescaled for the bigger power
envelope, or a filename/PID path so this can run alongside the original
without colliding. Rescaling method:
  - HARDWARE_IDLE_FLOOR_W / BASELINE_FALLBACK: set directly to the known
    ~225 W idle baseline (these represent the absolute floor itself, not a
    margin off of it).
  - Rate constants (DERIV_THRESHOLD, FASTPASS_RATE_THRESHOLD,
    RAMPDOWN_NEG_RATE_ABORT_W_S, BACKOFF_THRESHOLD): each set safely below
    (smaller magnitude than) the expected ~3000 W/s cliff rate, preserving
    the original file's relative strictness ordering between the four, so a
    genuine cliff reliably trips them with margin instead of just barely
    grazing the threshold.
  - Watt-margin constants (NOISE_GUARD_W, DELTA_THRESHOLD, DEADBAND_W,
    WORKLOAD_ENGAGE_W, DROP_TO_BASELINE_MARGIN_W, LATE_TRIGGER_GUARD_W,
    RESUMPTION_MARGIN_W, RAMPDOWN_NEG_DELTA_ABORT_W, FASTPASS_DELTA_W,
    HYBRID_RAPL_MIN_DROP_W, GATE_B_MIN_DROP_W, VAR_QUIESCENT_W2): scaled by
    the workload-swing ratio, server ≈175 W ÷ assumed local ≈30 W ≈ 5.83×
    (VAR_QUIESCENT_W2 by 5.83² since it's in W²). The local ~30 W swing is
    an assumption inferred from the original file's own constants, not a
    measured figure — treat these as a starting point and validate/retune
    against real traces from this server before trusting them unattended.
  - Timing/fraction constants (RAMP_STEP, cooldowns, CPU_* percentages,
    calibration windows, etc.) are hardware-scale-agnostic and unchanged.

Background daemon v16: hybrid CPU-utilisation + RAPL detection.

Core improvement over v15
--------------------------
v15 relies entirely on RAPL power readings for both detection and filtering.
RAPL has a hardware refresh latency of ~20-50 ms on Intel platforms, so even
the fast-pass single-step check must wait one full polling interval before the
drop is measurable.

v16 adds a parallel CPU-utilisation monitor that reads /proc/stat at 10 ms
cadence.  Kernel CPU accounting updates the moment a process exits or sleeps,
so a workload cliff appears in utilisation 20-50 ms before the equivalent RAPL
drop is visible.  The hybrid gate uses this early signal to arm the daemon and
then uses RAPL to confirm before committing workers — giving fast detection
without sacrificing false-positive immunity.

Hybrid Dual-Gate Architecture (v16)
-------------------------------------
  Gate A — CPU utilisation early-tripwire (fast, lightweight):
    A background thread (CpuMonitor) polls /proc/stat every
    CPU_POLL_INTERVAL_S (10 ms) and maintains a rolling deque of
    (timestamp, utilisation) pairs.  During IDLE the main loop checks
    whether aggregate utilisation has fallen by ≥ CPU_DROP_THRESHOLD (65 pp)
    within a CPU_DROP_WINDOW_S (120 ms) window — comparing the window peak
    against a CPU_TREND_TRAIL_S (12 ms) trailing average rather than a
    single instantaneous sample — AND the pre-drop peak was ≥ CPU_ACTIVE_MIN
    (80 %).  If both conditions hold, Gate A fires and the daemon enters the
    transient POTENTIAL_CLIFF state.

  Gate B — immediate RAPL confirmation (definitive):
    Once in POTENTIAL_CLIFF the daemon checks the RAPL delta every
    DETECT_POLL (2 ms) interval.  If RAPL has fallen ≥ HYBRID_RAPL_MIN_DROP_W
    (110 W) since the Gate A trip moment AND the drop persists for
    CLIFF_DWELL_S (120 ms — a micro-stall recovers inside the dwell, a real
    cliff stays down), Gate B confirms and RAMP_START is issued — bypassing
    noise Layers 2, 3, and 4 (which depend on historical context that hasn't
    had time to accumulate).  Layer 1 (pre-trip elevation check) is retained
    as a minimal sanity gate.
    If RAPL does not confirm within HYBRID_VERIFY_TIMEOUT_S (250 ms) plus
    the dwell, Gate A disarms and the daemon returns to IDLE silently.

  Why only L1 is retained in the Gate B path:
    L2 (quiescent timeout) — CPU utilisation evidence substitutes.
    L3 (variance floor)    — an 80 ms window is too short to accumulate the
                             ~40 idle samples the variance deque needs.
    L4 (adaptive history)  — history hasn't captured the pre-cliff peak yet.
    L1 is cheap and guards against the one real risk: triggering when RAPL
    happens to dip 110 W during genuine deep idle (baseline drift).

  Fallback — classic sliding-window RAPL path:
    Retained unchanged from v15.  Catches workloads that shed power without
    a sharp CPU utilisation cliff (e.g., memory-bandwidth-bound phases that
    stall threads while the OS still counts them as "busy" in /proc/stat).

State machine
-------------
  IDLE ──[Gate A trips]──► POTENTIAL_CLIFF ──[Gate B confirms]──► RAMPDOWN
    │                            │[timeout / L1 fail]                  │
    │                            └──────────────────► IDLE             │
    ├──[fast-pass RAPL]───────────────────────────────────────► RAMPDOWN
    ├──[sliding-window RAPL, all 5 layers]───────────────────► RAMPDOWN
    │                                                               │
    └────────────────────── COOLDOWN ◄─────────────────────────────┘

Event types logged in smoother_events_2.csv
--------------------------------------------
  RAMP_START       — drop detected (hybrid / fast-pass / sliding-window)
  RAMP_END         — full ramp completed normally
  RAMP_INTERRUPTED — primary workload returned mid-ramp (level check)
  RAMP_BACKOFF     — primary workload returned mid-ramp (rate check)

Run as root:
  cd /home/eldoyle/Desktop/Power_Project/InstructionEnergy/Power_Smoother_16
  sudo python3 power_smoother_16_2.py &

Kill cleanly:
  sudo kill $(cat /tmp/smoother16_2.pid)
"""

from __future__ import annotations

import collections
import csv
import math
import multiprocessing as mp
import os
import signal
import threading
import time

from rapl_reader import RaplMonitor

# ── Tunable parameters ────────────────────────────────────────────────────────

# Median despike filter (v16.2 noise hardening — mycroft idle false triggers)
# Real mycroft idle traces (2026-07-09, 45 s with the daemon armed and no
# workload) showed 7 false RAMP_STARTs. Root cause: RAPL read-timing jitter
# produces single-sample spikes up to ±150 W at idle — LARGER than several
# detection thresholds, and comparable to the real ~185 W workload cliff, so
# no pure threshold separates them. But the artifacts are 1-2 samples wide
# while a real cliff persists, so a median over the last MEDIAN_FILTER_N
# DISTINCT RAPL refreshes removes them almost entirely (measured: worst
# idle single-step drop -124 W raw → -14 W filtered) at a detection latency
# cost of ~2 refreshes (~50 ms). All detection/decision logic below runs on
# the filtered value; the raw hardware value is still what feed_rapl() and
# the samples CSV see. (Back to 5 from 3: at the 400-600 W active operating
# point, multi-burst scheduler noise needs the wider ~100-125 ms smoothing
# window before the decision logic sees the trace.)
MEDIAN_FILTER_N = 5          # distinct RAPL refreshes in the despike median

# Detection (v16.2 retune: thresholds re-derived from measured post-filter
# mycroft idle noise — short-window wiggle ≤ ~40 W — instead of the original
# local-machine swing-ratio guesses)
SAMPLE_INTERVAL_S = 0.025    # s   — RAPL polling cadence (25 ms)
MEDIAN_TRANSIT_S  = MEDIAN_FILTER_N * SAMPLE_INTERVAL_S
                              # s   — worst-case time for a step change to fully
                              #       transit the despike median. Any RAMPDOWN
                              #       check that judges "trajectory" must not
                              #       trust the filtered trace younger than this:
                              #       for the first MEDIAN_TRANSIT_S after
                              #       RAMP_START the filter is still replaying
                              #       the very cliff that was just confirmed.
DERIV_WINDOW_N    = 12       # samples in the sliding dP/dt window (300 ms total)
DERIV_THRESHOLD   = -2400.0  # W/s — fall faster than this → trigger (rate arm)
DELTA_THRESHOLD   = 120.0    # W   — peak-to-now drop → trigger (delta arm)
                              #       (was 100: PREFILL micro-stalls on the
                              #       128-core box dip ~100 W without being a
                              #       real cliff; real cliff ≈ 185 W)
DEADBAND_W        = 60.0     # W   — minimum absolute drop to arm either trigger
BACKOFF_THRESHOLD = +3000.0  # W/s — fast rise during ramp → RAMP_BACKOFF abort
                              #       (was +4500: back off earlier on a surge to
                              #       avoid stacking on a resuming workload)
BACKOFF_DEBOUNCE_N = 2       # consecutive RAMP_STEP hits ≥ BACKOFF_THRESHOLD
                              #       required to abort (v16.3): our own worker
                              #       handoff landing produces a one-step rise
                              #       well past the threshold on the filtered
                              #       trace — self-signal, not resumption. A
                              #       genuine resumption sustains the rise.
RAMP_TARGET_CLAMP_W = 410.0  # W   — never COMMAND total power above this (the
                              #       hard TDP constraint): the ramp target is
                              #       clamped here, so our own contribution can
                              #       never push commanded draw past 410 W
RAMP_ABORT_ABS_W    = 470.0  # W   — measured-total abort ceiling (v16.3 split,
                              #       was 410): run-8 showed the workload ITSELF
                              #       peaks at ~406-445 W, so a 410 W abort on
                              #       measured total killed every genuine ramp
                              #       whose handoff sat near the pre-cliff level
                              #       (events 2 and 4). 470 W sits above the
                              #       workload's own peak — only genuine
                              #       stacking (workload back + our dummy load)
                              #       can cross it → abort instantly

# Negative-gradient fast-abort (v16.1 fix — 4 s-late RAMP_INTERRUPTED)
# BACKOFF_THRESHOLD only catches the real workload coming back ON (a rise).
# It has no symmetric check for the real workload dropping *further* mid-ramp
# — e.g. a false/early RAMP_START mid-PREFILL, followed 0.28 s later by the
# real PREFILL→REST cliff. That case used to be invisible until the level-
# based resumption check eventually (and only coincidentally) fired ~4 s
# later. A further single-step drop this steep means our ramp's assumed
# trajectory is simply wrong (a real cliff is happening *right now*) —
# abort immediately. Runs unconditionally, NOT gated by RAMP_BLANK_S: unlike
# the level-based resumption check (which needs the blank window to avoid
# handoff noise), an unambiguous sharp further drop is real signal even in
# the first few ms of a ramp.
RAMPDOWN_NEG_RATE_ABORT_W_S = -2000.0 # W/s — single-step rate this negative aborts
RAMPDOWN_NEG_DELTA_ABORT_W  = 150.0   # W   — and the absolute step must be at least this,
                                       #       so ordinary RAPL sample jitter can't trip it
                                       #       (was -1200 / 100: an active PREFILL burst
                                       #       on the 128-core box naturally swings >100 W
                                       #       in one 50 ms step, so a false ramp aborted
                                       #       via "negative gradient" ~100 ms in, cooled
                                       #       down, and immediately re-triggered — a
                                       #       spikey abort/re-arm storm. A real cliff is
                                       #       steeper and larger than active-phase swing.)

# Ramp control (unchanged from v15)
RAMPDOWN_SECS = 6.0          # s   — maximum linear ramp-down duration
RAMP_STEP     = 0.05         # s   — 50 ms control steps (120 steps over 6 s)
DETECT_POLL   = 0.002        # s   — 2 ms poll interval in IDLE / POTENTIAL_CLIFF

# Closed-loop intensity control (v16.4 — run-9 fix)
# The environment after a genuine cliff is NOT the idle floor: this sim's
# REST phases hold ~330-370 W (128 threads waking every 2 ms), so the old
# open-loop command — sized against the floor — stacked 490-540 W totals
# and the guardrail correctly killed every genuine ramp (run-9 events
# 5-7). The loop now commands only what's MISSING: intensity is derived
# from (target - observed environment), with the environment estimate
# EMA-smoothed and intensity increases slew-limited so measurement noise
# can't chatter the control. Decreases apply instantly — shedding load is
# always safe; adding it is what can stack.
W_ENV_EMA_ALPHA   = 0.35     # frac/step — env-estimate EMA gain (τ ≈ 0.15 s)
INTENSITY_SLEW_UP = 0.15     # max intensity increase per RAMP_STEP

# Cooldowns
COOLDOWN_SECS       = 2.0    # s   — quiet period after a normal ramp completion
BACKOFF_COOLDOWN    = 1.0    # s   — re-arm delay after a rate-based backoff abort
                              #       (was 0.5: re-arm must comfortably outlast the
                              #       abort's own shed transient — dropping intensity
                              #       to 0 sheds hundreds of watts in one step, and
                              #       that self-cliff needs to flush through the
                              #       median filter + DERIV window before detection
                              #       can be trusted again; see 15-event abort/re-arm
                              #       storm in the 2026-07-14 mycroft run)

# Interrupted-ramp cooldown (v16.1: adaptive, split by how fast the interrupt
# fired). How confused the daemon was is proportional to how long it took to
# notice the ramp was wrong, *not* simply "was it interrupted at all":
#   - A ramp interrupted within FAST_INTERRUPT_S of RAMP_START means whatever
#     mechanism caught it (resumption margin, or the negative-gradient abort
#     below) resolved cleanly and immediately — the system is not confused,
#     so re-arm fast (FAST_INTERRUPT_COOLDOWN_S) to stay sensitive to a
#     follow-on cliff.
#   - A ramp that drags past FAST_INTERRUPT_S before getting interrupted
#     means the detection was genuinely uncertain/noisy for a while — give
#     it the longer SLOW_INTERRUPT_COOLDOWN_S to let the signal settle.
FAST_INTERRUPT_S           = 1.0  # s   — elapsed-since-RAMP_START boundary between tiers
FAST_INTERRUPT_COOLDOWN_S  = 1.0  # s   — re-arm after a clean, fast interrupt
                                   #      (was 0.4: same self-cliff flush requirement
                                   #      as BACKOFF_COOLDOWN — 0.4 s re-armed while
                                   #      the abort's own shed transient was still
                                   #      inside the detection windows)
SLOW_INTERRUPT_COOLDOWN_S  = 3.0  # s   — settle time after a slow/messy interrupt

# Baseline estimation (unchanged from v15)
BASELINE_WINDOW   = 60.0     # s   — look-back window for idle floor estimation
BASELINE_FALLBACK = 225.0    # W   — used until 20 samples accumulate
CALIB_WINDOW      = 0.4      # s   — RAPL snapshot window for event logging

# Hardware idle floor clamp (v16 fix — RAMP_END drop)
# The 10th-percentile baseline estimator assumes the trailing BASELINE_WINDOW
# is ≥90% idle. When workload duty-cycle rises above that (frequent/long
# PREFILL bursts within the 60 s lookback), the percentile itself gets
# dragged upward and ev_baseline_w overestimates the true hardware idle
# floor. HARDWARE_IDLE_FLOOR_W is the known true floor (matches the
# simulator's REST-state power) and is used both to clamp the ramp-down
# target and as a hard lower bound when ev_baseline_w is decayed during
# RAMPDOWN (see RAMPDOWN state below).
HARDWARE_IDLE_FLOOR_W = 225.0 # W   — true idle hardware floor
BASELINE_CLAMP_MAX_W  = HARDWARE_IDLE_FLOOR_W + 60.0
                              # W   — ceiling on the estimated idle baseline
                              #       (v16.3): the 10th-percentile estimator
                              #       can be dragged to ~400 W by a long burst
                              #       dominating its window — see
                              #       _estimate_baseline's docstring
BASELINE_DECAY_ALPHA  = 0.15 # frac — EMA rate pulling ev_baseline_w down
                              #        toward observed low readings mid-ramp

# ── Noise-filter / hysteresis parameters (unchanged from v15) ─────────────────

# Layer 1: pre-drop elevation gate
NOISE_GUARD_W = 60.0         # W   — pre-drop mean must exceed baseline by this much
                              #       (was 35: mycroft filtered idle wiggles ~+40 W
                              #       over baseline; PREFILL sits ~+120 W)

# Layer 2: quiescent timeout gate
WORKLOAD_ENGAGE_W   = 50.0   # W   — power above baseline counted as "workload active"
QUIESCENT_TIMEOUT_S = 20.0   # s   — no high-power obs for this long → suppress trigger

# Layer 3: rolling-variance floor
VARIANCE_WIN_N   = 40        # samples (~1 s at 25 ms poll cadence)
VAR_QUIESCENT_W2 = 150.0     # W²  — variance below this → deep-idle, suppress trigger
                              #       (was 27: measured post-filter idle rolling
                              #       variance is ~18 W² median — 27 never
                              #       suppressed anything on mycroft; a real cliff
                              #       inside the window measures thousands of W²)

# Layer 4: adaptive pre-drop elevation gate (history-aware)
RECENT_PEAK_WINDOW_S = 8.0   # s   — look-back into history for the workload envelope
LAYER4_PEAK_FRAC     = 0.10  # frac — older-half mean must be ≥ this fraction of swing

# Layer 5: drop-floor gate
DROP_TO_BASELINE_MARGIN_W = 60.0  # W — w_now must be within this of baseline

# Late-trigger guard (v16 fix — lagged detection → ghost RAMP_START)
# Applied at every commit-to-RAMPDOWN point (fastpass, sliding-window,
# hybrid). If detection lags the real cliff enough that w_raw has already
# settled back to within LATE_TRIGGER_GUARD_W of the current baseline by
# the time we're about to start a ramp, the real cliff already fully
# happened — there is nothing left to smooth. Starting a ramp here just
# spins workers up against an already-idle system, which promptly reads as
# "excess" power and self-interrupts, flushing history and re-arming into a
# second, flat, unramped ghost RAMP_START.
LATE_TRIGGER_GUARD_W = 40.0  # W — w_raw must be > baseline + this to allow a ramp
STALE_CLIFF_S        = 0.35  # s — v16.2: the guard only suppresses when power has
                              #     ALSO been quiescent (below baseline +
                              #     WORKLOAD_ENGAGE_W) at least this long. With the
                              #     median despike filter, a genuine 1-refresh cliff
                              #     is only *seen* ~50-75 ms after it lands — by
                              #     which time the filtered reading is already at
                              #     the floor, and a pure level check would suppress
                              #     every legitimate detection. Power elevated
                              #     <0.35 s ago ⇒ the cliff JUST happened ⇒ ramp is
                              #     still worth starting; quiescent ≥0.35 s ⇒ truly
                              #     stale ⇒ suppress the ghost.

# Fast-pass trigger
FASTPASS_RATE_THRESHOLD = -1800.0  # W/s — single RAPL-interval rate threshold
FASTPASS_DELTA_W        = 160.0   # W   — minimum single-step absolute drop
                                    #      (was 180: sat at/above the actual
                                    #      ~175-185 W mycroft cliff and missed it;
                                    #      160 sits under the cliff but above the
                                    #      ~100-150 W micro-stall dips, and the
                                    #      utilisation veto below now rejects
                                    #      busy-core artifacts this floor alone
                                    #      used to have to absorb)

# ── Workload-resumption detection in RAMPDOWN (unchanged from v15) ────────────

RESUMPTION_MARGIN_W   = 90.0 # W   — excess above expected dummy level
RAMP_BLANK_S          = 0.30 # s   — blanking window after RAMP_START
RESUMPTION_DEBOUNCE_N = 5    # consecutive RAMP_STEP hits before interrupting

# ── Hybrid CPU-utilisation + RAPL detection (v16 new) ────────────────────────

# CPU utilisation monitor
CPU_POLL_INTERVAL_S = 0.010  # s    — /proc/stat polling cadence (10 ms)
CPU_HISTORY_S       = 0.5    # s    — rolling retention window for util samples

# Gate A — utilisation early-tripwire
CPU_DROP_WINDOW_S  = 0.120   # s    — look-back window for drop measurement (120 ms).
                              #        Rewidened again (45 → 80 → 120 ms): measured
                              #        PREFILL micro-stalls on the 128-core server run
                              #        up to ~100 ms, so an 80 ms window still saw them
                              #        as full cliffs (11 false RAMP_STARTs inside
                              #        Prefill 1 alone). 120 ms outlasts them, so only
                              #        a sustained utilisation collapse arms Gate A.
                              #        The added latency is covered by
                              #        LATE_TRIGGER_GUARD_W / STALE_CLIFF_S.
CPU_TREND_TRAIL_S  = 0.012   # s    — trailing sub-window averaged for "current"
                              #        utilisation instead of a single last sample,
                              #        smoothing out one-off /proc/stat noise; kept
                              #        short relative to the 120 ms window so it still
                              #        leaves room to see the drop within the window
CPU_DROP_THRESHOLD = 0.65    # frac — absolute utilisation drop (pp) that arms Gate A;
                              #        e.g. 0.65 means a drop from 90% → 25% qualifies
                              #        (was 0.40: PREFILL micro-stalls on 128 cores
                              #        routinely shed 40-60 pp without ending the phase)
CPU_ACTIVE_MIN     = 0.80    # frac — peak util in window must exceed this to confirm
                              #        genuine workload was present (not scheduling noise)
                              #        (was 0.50: REST-phase scheduler/background churn
                              #        on the idle 128-core box still armed Gate A 19
                              #        times in one run; the real sim runs ~100% util,
                              #        so 0.80 excludes idle wiggle without risk)

# Gate B — RAPL confirmation after Gate A trips (v16.1: adaptive/confidence-scaled)
# A flat threshold+timeout forces a choice between "sensitive" (catches real
# cliffs fast, but also confirms dense-math noise) and "strict" (immune to
# noise, but a genuine cliff can silently fail to confirm if RAPL's ~25-50 ms
# hardware refresh just barely misses the window — Gate A times out, disarms
# *silently*, and nothing is ever logged). Real trace analysis showed exactly
# this: full PREFILL→REST cliffs going completely undetected.
#
# Fix: scale the required drop and the patience by how strong the Gate A
# evidence was. A CPU utilisation drop right at CPU_DROP_THRESHOLD is weak
# evidence (could be dense-math noise) → stay strict. A drop far above
# threshold (e.g. ~100%→~0%, a real full-workload cliff) is strong
# evidence → become lenient and patient, because there's very little
# chance this is noise and every reason to wait out RAPL's refresh lag.
# The timeouts absorb the median filter's ~50-75 ms confirmation lag (a
# real cliff needs 3 of the 5 median samples to change before the
# filtered value moves).
#
# v16.3 rebase + dwell: the 150/130 floors from the previous round were
# tuned for a 400-600 W operating range this box doesn't have — mycroft's
# ai_sim profile runs ~225 W idle → ~400 W burst, so the real cliff is
# only ~175-185 W. Floors that high silently missed genuine cliffs
# (PREFILL 2's end in the 2026-07-15 run produced no RAMP_START at all),
# while mid-burst micro-stall dips run ~100-150 W — the watt gap between
# noise and signal is ~30 W, too small for thresholds alone. So the
# floors are rebased BELOW the real cliff (110/90) and the noise/signal
# separation now comes from CLIFF_DWELL_S: a micro-stall recovers within
# ~100 ms, a real cliff stays down, so the confirmed drop must HOLD for
# the dwell before RAMP_START commits. Timeouts stretched to make room
# for filter transit + dwell inside the confirmation budget.
HYBRID_RAPL_MIN_DROP_W  = 110.0  # W — required RAPL drop at MINIMUM confidence
GATE_B_MIN_DROP_W       = 90.0   # W — required RAPL drop at MAXIMUM confidence
HYBRID_VERIFY_TIMEOUT_S = 0.250  # s — confirmation patience at MINIMUM confidence
GATE_B_MAX_TIMEOUT_S    = 0.400  # s — confirmation patience at MAXIMUM confidence
CLIFF_DWELL_S           = 0.12   # s — a floor-crossing drop must persist this long
                                  #     before any RAMP_START commits (Gate B and the
                                  #     sliding path); resets if power recovers

# ── Online OLS calibration for RAPL estimation (v16 enhancement) ─────────────
CAL_WINDOW_S      = 1.5     # s    — rolling window of (util, rapl) training pairs
MIN_CAL_SAMPLES   = 30      # pairs — model invalid until this many exist in the window
MIN_UTIL_VARIANCE = 0.005   # frac² — require real utilisation spread; blocks idle-only fit
RAPL_STALE_S      = 0.020   # s    — RAPL older than this starts blending toward model

# Low-intensity worker contention relief (v16 fix — RAMPDOWN tail noise)
# Near the tail of a ramp, workers wake every WINDOW (10 ms) to do a tiny
# burst — many short wake-ups contend for scheduling against ai_sim_2.py's
# own REST-phase threads (which sleep 2 ms per iteration), adding utilisation
# and RAPL noise right when the daemon is trying to glide smoothly to idle.
LOW_INTENSITY_THRESHOLD  = 0.15  # frac — below this, coalesce into fewer, longer cycles
LOW_INTENSITY_COALESCE_N = 4     # windows coalesced into one cycle when below threshold

N_WORKERS   = mp.cpu_count()  # server profile: no local 16-core cap
PID_FILE    = "/tmp/smoother16_2.pid"
SAMPLES_CSV = "power_samples_16_2.csv"
SMOOTHER_EVENTS_CSV    = "smoother_events_2.csv"
SMOOTHER_EVENTS_FIELDS = ["event_num", "event_type", "timestamp_s"]


# ── CPU utilisation monitor ───────────────────────────────────────────────────

class CpuMonitor:
    """
    Background thread that polls /proc/stat every CPU_POLL_INTERVAL_S and
    maintains a rolling deque of (timestamp, utilisation) pairs.  Utilisation
    is the aggregate busy fraction across all logical CPUs.

    OLS calibration enhancement
    ----------------------------
    While running, the thread maintains a CAL_WINDOW_S rolling window of
    (util, rapl_watts) training pairs and fits the model:

        P_estimated = P_idle + alpha * utilisation

    using O(1)-update rolling ordinary least squares (running sums: Σu, Σp,
    Σu², Σup).  The main loop feeds raw RAPL readings via feed_rapl() and
    retrieves the blended estimate via estimated_watts().

    When the RAPL register is fresh (changed within RAPL_STALE_S), raw RAPL
    is returned unchanged.  As the register ages, estimated_watts() linearly
    blends toward the model prediction — bridging the 25 ms hardware gap with
    10 ms utilisation resolution.
    """

    def __init__(self, poll_interval: float, history_s: float) -> None:
        self._interval  = poll_interval
        self._history_s = history_s
        self._lock      = threading.Lock()

        # Gate A history (unchanged)
        self._util: float = 0.0
        self._history: collections.deque[tuple[float, float]] = collections.deque()

        # OLS calibration window: (timestamp, util, rapl_w) triples
        self._cal_win: collections.deque[tuple[float, float, float]] = collections.deque()

        # Running sums for O(1) rolling OLS
        self._ols_n:   int   = 0
        self._ols_Su:  float = 0.0   # Σu
        self._ols_Sp:  float = 0.0   # Σp
        self._ols_Su2: float = 0.0   # Σu²
        self._ols_Sup: float = 0.0   # Σup

        # Fitted coefficients (updated under _lock)
        self._p_idle:      float = 0.0
        self._alpha:       float = 0.0
        self._model_valid: bool  = False

        # Latest RAPL reading — fed by main loop via feed_rapl()
        self._rapl_w:           float = 0.0
        self._rapl_prev_w:      float = 0.0
        self._rapl_last_change: float = time.monotonic()

        self._stop   = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    # ── public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    @property
    def utilization(self) -> float:
        with self._lock:
            return self._util

    @property
    def is_model_valid(self) -> bool:
        with self._lock:
            return self._model_valid

    def recent(self, window_s: float) -> list[tuple[float, float]]:
        """Return (timestamp, util) pairs from the last window_s seconds."""
        cutoff = time.monotonic() - window_s
        with self._lock:
            return [(t, u) for t, u in self._history if t >= cutoff]

    def feed_rapl(self, w: float) -> None:
        """
        Called by the main loop every iteration with rapl.current_watts BEFORE
        calling estimated_watts().  Detects when the RAPL register actually
        refreshed (value changed) so estimated_watts() knows how stale it is.
        Does not add calibration samples — _run() does that with fresh util.
        """
        with self._lock:
            if w != self._rapl_prev_w:
                self._rapl_last_change = time.monotonic()
                self._rapl_prev_w = w
            self._rapl_w = w

    def estimated_watts(self, current_rapl_w: float) -> float:
        """
        Return the best real-time power estimate.

        When RAPL changed within RAPL_STALE_S seconds, returns current_rapl_w
        unchanged — the hardware reading is fresh.  As the register ages past
        RAPL_STALE_S, linearly blends toward the OLS model prediction:

            blend = 0.0  at  stale_s == RAPL_STALE_S      (pure raw RAPL)
            blend = 1.0  at  stale_s == 2 × RAPL_STALE_S  (pure model)

        Returns current_rapl_w unchanged if the model is not yet valid (too few
        samples or no workload variation), preserving v16 baseline behaviour.
        """
        with self._lock:
            if not self._model_valid:
                return current_rapl_w
            stale_s = time.monotonic() - self._rapl_last_change
            if stale_s <= RAPL_STALE_S:
                return current_rapl_w
            blend   = min(1.0, (stale_s - RAPL_STALE_S) / RAPL_STALE_S)
            p_model = max(0.0, self._p_idle + self._alpha * self._util)
            return current_rapl_w * (1.0 - blend) + p_model * blend

    # ── internal ──────────────────────────────────────────────────────────────

    def _refit(self) -> None:
        """Recompute OLS coefficients from running sums.  Called under lock."""
        n = self._ols_n
        if n < MIN_CAL_SAMPLES:
            self._model_valid = False
            return
        # Require genuine utilisation spread — blocks fitting on idle-only data
        # where all u ≈ 0 and the denominator collapses.
        util_var = (self._ols_Su2 - self._ols_Su ** 2 / n) / n
        if util_var < MIN_UTIL_VARIANCE:
            self._model_valid = False
            return
        denom = n * self._ols_Su2 - self._ols_Su ** 2
        if abs(denom) < 1e-12:
            self._model_valid = False
            return
        self._alpha  = (n * self._ols_Sup - self._ols_Su * self._ols_Sp) / denom
        self._p_idle = (self._ols_Sp - self._alpha * self._ols_Su) / n
        self._model_valid = True

    def _add_cal_sample(self, t: float, u: float, p: float) -> None:
        """
        Append (t, u, p), evict samples older than CAL_WINDOW_S, update running
        sums in O(1), and refit.  Called under lock from _run().
        """
        cutoff = t - CAL_WINDOW_S
        while self._cal_win and self._cal_win[0][0] < cutoff:
            _, u_old, p_old = self._cal_win.popleft()
            self._ols_n   -= 1
            self._ols_Su  -= u_old
            self._ols_Sp  -= p_old
            self._ols_Su2 -= u_old * u_old
            self._ols_Sup -= u_old * p_old
        self._cal_win.append((t, u, p))
        self._ols_n   += 1
        self._ols_Su  += u
        self._ols_Sp  += p
        self._ols_Su2 += u * u
        self._ols_Sup += u * p
        self._refit()

    @staticmethod
    def _read_stat() -> tuple[int, int]:
        """Return (busy_jiffies, total_jiffies) from /proc/stat aggregate line."""
        with open("/proc/stat") as f:
            parts = f.readline().split()
        vals  = [int(v) for v in parts[1:]]
        idle  = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals)
        return total - idle, total

    def _run(self) -> None:
        prev_busy, prev_total = self._read_stat()
        while not self._stop.is_set():
            self._stop.wait(self._interval)
            busy, total = self._read_stat()
            d_total = total - prev_total
            util    = (busy - prev_busy) / d_total if d_total > 0 else 0.0
            prev_busy, prev_total = busy, total
            t_now  = time.monotonic()
            cutoff = t_now - self._history_s
            with self._lock:
                self._util = max(0.0, min(1.0, util))
                self._history.append((t_now, self._util))
                while self._history and self._history[0][0] < cutoff:
                    self._history.popleft()
                # Calibrate: pair this util sample with the latest RAPL value.
                # self._rapl_w is at most one main-loop poll (2 ms) stale —
                # negligible relative to the 25 ms RAPL refresh cadence.
                if self._rapl_w > 0.0:
                    self._add_cal_sample(t_now, self._util, self._rapl_w)


# ── Worker ────────────────────────────────────────────────────────────────────

def _worker_fn(intensity: mp.Value, stop_evt: mp.Event) -> None:  # type: ignore[type-arg]
    """
    Burn CPU proportional to intensity [0.0, 1.0] using pure-Python FP math.
    Each 10 ms window splits into work_time = intensity × 10 ms and
    sleep_time = (1 − intensity) × 10 ms.

    Below LOW_INTENSITY_THRESHOLD (v16 fix), windows are coalesced into
    LOW_INTENSITY_COALESCE_N-times-longer cycles and the process yields
    explicitly after each burst — same average duty cycle, far fewer wake-ups,
    which cuts scheduler contention noise during the low-intensity tail of a
    ramp-down.
    """
    x = 1.000001
    WINDOW = 0.010

    while not stop_evt.is_set():
        v = intensity.value
        if v < 0.01:
            # v16.2 fix: was 0.0001 s — 128 workers each waking 10 000×/s put
            # ~1.3 M wakeups/s of scheduler churn on an otherwise idle box,
            # measured as ~78 W of extra idle draw (302 W with the daemon armed
            # vs 224 W without) plus CPU-utilisation noise that repeatedly
            # armed Gate A. 5 ms still reacts an order of magnitude faster
            # than the 50 ms ramp step.
            time.sleep(0.005)
            continue

        low_intensity = v < LOW_INTENSITY_THRESHOLD
        window        = WINDOW * LOW_INTENSITY_COALESCE_N if low_intensity else WINDOW

        t0      = time.monotonic()
        work_s  = v * window
        sleep_s = (1.0 - v) * window

        while time.monotonic() - t0 < work_s:
            for _ in range(1_000):
                x = math.sqrt(x * x + 1.0) - math.sqrt(x * x - 1.0 + 1e-15)
                x = math.sin(x) * math.cos(x) + math.exp(-x * x) + 1.0

        if low_intensity:
            os.sched_yield()

        if sleep_s > 0.0005:
            time.sleep(sleep_s)


# ── Baseline estimator ────────────────────────────────────────────────────────

def _estimate_baseline(
    history: collections.deque,  # type: ignore[type-arg]
    now: float,
) -> float:
    """
    10th-percentile of RAPL readings within BASELINE_WINDOW, clamped to
    BASELINE_CLAMP_MAX_W.

    The clamp (v16.3) fixes contamination inversion: after a long burst
    (run-8's 26 s PREFILL 2), the 60 s window is dominated by ~400 W
    samples and the raw percentile rises to ~401 W on a box whose true
    idle is ~204 W. Every "is power near baseline?" comparison then
    inverts — the late-trigger guard suppressed a genuine cliff commit
    because 230 W looked "already idle" against a 401 W baseline. The
    hardware idle floor is known and calibrated at startup, so no
    baseline estimate should ever meaningfully exceed it.
    """
    cutoff  = now - BASELINE_WINDOW
    samples = sorted(w for t, w in history if t >= cutoff)
    if len(samples) < 20:
        return BASELINE_FALLBACK
    return min(samples[max(0, len(samples) // 10)], BASELINE_CLAMP_MAX_W)


def _cliff_already_resolved(w_raw: float, baseline_w: float, quiescent_s: float) -> bool:
    """
    True when w_raw has already settled back near the idle baseline — i.e.
    the real workload cliff fully happened before detection caught up, and
    there is no elevated power left to smooth. Starting a ramp here would
    just spin workers up against an already-idle system (v16 fix).

    v16.2: also requires power to have been quiescent ≥ STALE_CLIFF_S. The
    median despike filter means a genuine cliff is only visible ~50-75 ms
    after it lands, by which point the filtered reading is already at the
    floor — a pure level check would therefore suppress every legitimate
    detection. quiescent_s is time since power was last elevated: small ⇒
    the cliff just happened and the ramp is still worth starting.
    """
    return (
        w_raw <= baseline_w + LATE_TRIGGER_GUARD_W
        and quiescent_s >= STALE_CLIFF_S
    )


# ── Context piercing: external phase-boundary override (v16.1 new) ───────────

class PhaseSignal:
    """
    Thread-safe "context piercing" channel. Lets an out-of-band source — a
    co-located workload orchestrator, or a real inference server's own
    phase hooks — push authoritative phase-boundary notifications straight
    into the state machine, bypassing every gradient/gate/cooldown
    computation entirely.

    This is strictly additive: with nothing calling notify(), the daemon
    behaves exactly as it does today, purely on RAPL + CPU-utilisation
    inference. Wiring something to it is optional.

    Two event kinds are understood by the main loop:
      WORKLOAD_END   — the real primary workload just stopped (or is about
                        to). Forces an immediate RAMP_START from IDLE,
                        POTENTIAL_CLIFF, or COOLDOWN, flushing history first.
      WORKLOAD_START — the real primary workload just resumed. Forces an
                        immediate RAMP_INTERRUPTED from RAMPDOWN.

    Timestamps are in the same time.monotonic() domain as the rest of the
    daemon. If you're notifying "live" as things happen, omit timestamp_s
    and the call time is used.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: collections.deque[tuple[float, str]] = collections.deque()

    def notify(self, kind: str, timestamp_s: float | None = None) -> None:
        if kind not in ("WORKLOAD_END", "WORKLOAD_START"):
            raise ValueError(f"Unknown PhaseSignal kind: {kind!r}")
        t = timestamp_s if timestamp_s is not None else time.monotonic()
        with self._lock:
            self._queue.append((t, kind))

    def poll_ready(self, now: float) -> list[str]:
        """Pop and return the kinds of all queued events due by *now*, oldest first."""
        ready: list[str] = []
        with self._lock:
            while self._queue and self._queue[0][0] <= now:
                _, kind = self._queue.popleft()
                ready.append(kind)
        return ready


# ── CSV helpers ───────────────────────────────────────────────────────────────

def _write_samples(samples: list[tuple[float, float]]) -> None:
    with open(SAMPLES_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp_s", "power_w"])
        w.writerows(samples)
    print(f"[Saved] {len(samples)} samples → {SAMPLES_CSV}")


def _write_smoother_event(row: dict) -> None:
    exists = os.path.exists(SMOOTHER_EVENTS_CSV)
    with open(SMOOTHER_EVENTS_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SMOOTHER_EVENTS_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow(row)


# ── Signal handler ────────────────────────────────────────────────────────────

_running = True


def _on_signal(sig: int, frame) -> None:  # type: ignore[type-arg]
    global _running
    _running = False


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    global _running

    if os.geteuid() != 0:
        print("Error: RAPL requires root.  Run: sudo python3 power_smoother_16_2.py")
        raise SystemExit(1)

    signal.signal(signal.SIGINT,  _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    with open(PID_FILE, "w") as f:
        f.write(f"{os.getpid()}\n")

    # ── Spawn dummy workers ───────────────────────────────────────────────────
    intensity = mp.Value("d", 0.0)
    stop_evt  = mp.Event()
    workers   = [
        mp.Process(target=_worker_fn, args=(intensity, stop_evt), daemon=True)
        for _ in range(N_WORKERS)
    ]
    for w in workers:
        w.start()
    pids = [w.pid for w in workers]

    # ── Start CPU utilisation monitor (v16) ───────────────────────────────────
    cpu_mon = CpuMonitor(CPU_POLL_INTERVAL_S, CPU_HISTORY_S)
    cpu_mon.start()

    # ── Context-piercing external phase signal (v16.1, optional) ─────────────
    # Nothing feeds this unless a caller wires it up (see the PhaseSignal
    # docstring). Polled every loop iteration below regardless.
    phase_signal = PhaseSignal()

    rapl = RaplMonitor(interval=SAMPLE_INTERVAL_S)
    rapl.start()
    time.sleep(0.5)

    if os.path.exists(SMOOTHER_EVENTS_CSV):
        os.remove(SMOOTHER_EVENTS_CSV)

    # ── Self-calibration ──────────────────────────────────────────────────────
    print("[Daemon] Calibrating idle baseline (1.0 s) …")
    time.sleep(1.0)
    baseline_w_startup = rapl.snapshot_watts(window=1.0)
    print(f"[Daemon] Idle baseline   : {baseline_w_startup:.2f} W")

    print("[Daemon] Calibrating worker peak power (0.5 s) …")
    intensity.value = 1.0
    time.sleep(0.5)
    worker_peak_total = rapl.snapshot_watts(window=0.5)
    intensity.value = 0.0
    time.sleep(0.5)
    worker_peak_w = max(1.0, worker_peak_total - baseline_w_startup)
    print(f"[Daemon] Worker peak     : {worker_peak_total:.2f} W total  "
          f"({worker_peak_w:.2f} W above baseline)")

    print(f"\n[Daemon] v16 (hybrid)  "
          f"median_filter={MEDIAN_FILTER_N} refreshes  "
          f"sample={SAMPLE_INTERVAL_S*1000:.0f} ms  "
          f"deriv_window={DERIV_WINDOW_N} samples ({DERIV_WINDOW_N*SAMPLE_INTERVAL_S*1000:.0f} ms)  "
          f"drop_thresh={DERIV_THRESHOLD:.0f} W/s  "
          f"delta_thresh={DELTA_THRESHOLD:.1f} W  "
          f"deadband={DEADBAND_W:.1f} W  "
          f"ramp={RAMPDOWN_SECS:.1f} s  step={RAMP_STEP*1000:.0f} ms")
    print(f"[Daemon] noise_guard={NOISE_GUARD_W:.1f} W  "
          f"quiescent_timeout={QUIESCENT_TIMEOUT_S:.0f} s  "
          f"variance_win={VARIANCE_WIN_N} samples  "
          f"var_floor={VAR_QUIESCENT_W2:.2f} W²")
    print(f"[Daemon] drop_floor={DROP_TO_BASELINE_MARGIN_W:.1f} W above baseline  "
          f"recent_peak_win={RECENT_PEAK_WINDOW_S:.0f} s  "
          f"L4_peak_frac={LAYER4_PEAK_FRAC:.0%}")
    print(f"[Daemon] hw_idle_floor={HARDWARE_IDLE_FLOOR_W:.1f} W  "
          f"baseline_decay_alpha={BASELINE_DECAY_ALPHA:.2f}")
    print(f"[Daemon] fastpass: rate≤{FASTPASS_RATE_THRESHOLD:+.0f} W/s  "
          f"delta≥{FASTPASS_DELTA_W:.0f} W")
    print(f"[Daemon] hybrid gate_a: cpu_poll={CPU_POLL_INTERVAL_S*1000:.0f} ms  "
          f"window={CPU_DROP_WINDOW_S*1000:.0f} ms  "
          f"trend_trail={CPU_TREND_TRAIL_S*1000:.0f} ms  "
          f"drop≥{CPU_DROP_THRESHOLD:.0%}  "
          f"active_min={CPU_ACTIVE_MIN:.0%}")
    print(f"[Daemon] hybrid gate_b: rapl_min_drop={HYBRID_RAPL_MIN_DROP_W:.1f} W  "
          f"timeout={HYBRID_VERIFY_TIMEOUT_S*1000:.0f} ms")
    print(f"[Daemon] late_trigger_guard={LATE_TRIGGER_GUARD_W:.1f} W  "
          f"low_intensity_threshold={LOW_INTENSITY_THRESHOLD:.2f}  "
          f"coalesce_n={LOW_INTENSITY_COALESCE_N}")
    print(f"[Daemon] ols_cal: window={CAL_WINDOW_S:.1f} s  "
          f"min_samples={MIN_CAL_SAMPLES}  "
          f"min_util_var={MIN_UTIL_VARIANCE:.3f}  "
          f"rapl_stale={RAPL_STALE_S*1000:.0f} ms")
    print(f"[Daemon] resumption_margin={RESUMPTION_MARGIN_W:.1f} W  "
          f"blank={RAMP_BLANK_S*1000:.0f} ms  "
          f"debounce={RESUMPTION_DEBOUNCE_N} steps ({RESUMPTION_DEBOUNCE_N*RAMP_STEP*1000:.0f} ms)")
    print(f"[Daemon] closed-loop: env_ema_alpha={W_ENV_EMA_ALPHA:.2f}/step  "
          f"slew_up={INTENSITY_SLEW_UP:.2f}/step  "
          f"target_clamp={RAMP_TARGET_CLAMP_W:.0f} W  "
          f"abort_ceiling={RAMP_ABORT_ABS_W:.0f} W")
    print(f"[Daemon] cooldown={COOLDOWN_SECS:.1f} s (normal)  "
          f"{FAST_INTERRUPT_COOLDOWN_S:.1f}/{SLOW_INTERRUPT_COOLDOWN_S:.1f} s "
          f"(interrupted fast/slow, split at {FAST_INTERRUPT_S:.1f} s)  "
          f"{BACKOFF_COOLDOWN:.1f} s (backoff)")
    print(f"[Daemon] neg_rate_abort={RAMPDOWN_NEG_RATE_ABORT_W_S:+.0f} W/s  "
          f"neg_delta_abort={RAMPDOWN_NEG_DELTA_ABORT_W:.1f} W  "
          f"(unconditional, not RAMP_BLANK_S-gated)")
    print(f"[Daemon] {N_WORKERS} workers  PIDs: {pids}")
    print(f"[Daemon] PID {os.getpid()} → {PID_FILE}  |  silent until event fires\n")

    # ── State-machine variables ───────────────────────────────────────────────
    state     = "IDLE"
    t_origin  = time.monotonic()
    event_num = 0

    history: collections.deque[tuple[float, float]] = collections.deque()

    deriv_win: collections.deque[tuple[float, float]] = collections.deque(
        maxlen=DERIV_WINDOW_N
    )

    variance_win: collections.deque[float] = collections.deque(maxlen=VARIANCE_WIN_N)
    last_high_power_t: float = time.monotonic()

    ramp_start          = 0.0
    ramp_initial_intens = 1.0
    ramp_last_w: float | None = None
    ramp_last_t: float | None = None
    resumption_hits     = 0

    # Hybrid detection state (v16)
    potential_cliff_t:   float = 0.0   # monotonic time when Gate A fired
    hybrid_rapl_at_trip: float = 0.0   # RAPL reading at the moment Gate A fired
    _last_gate_a_drop:   float = 0.0   # stored for Gate B log line
    gate_a_confidence:   float = 0.0   # 0..1 — how far the util drop exceeded
                                        # CPU_DROP_THRESHOLD; scales Gate B's
                                        # required drop + patience (v16.1)

    # Per-event log values
    ev_detect_t   = 0.0
    ev_detect_w   = 0.0
    ev_rate       = 0.0
    ev_pre_drop_w = 0.0
    ev_init_int   = 1.0
    ev_handoff_w  = 0.0
    ev_baseline_w = baseline_w_startup

    cooldown_dur = COOLDOWN_SECS

    # Median despike filter state (v16.2 — see MEDIAN_FILTER_N). The window
    # holds the last N *distinct* RAPL refreshes — the main loop polls every
    # 2 ms but the register only refreshes every ~25 ms, so appending every
    # iteration would flood the window with duplicates and defeat the filter.
    med_win: collections.deque[float] = collections.deque(maxlen=MEDIAN_FILTER_N)
    med_last_refresh: float | None = None
    backoff_hits = 0                     # v16.3 — see BACKOFF_DEBOUNCE_N

    # v16.4 closed-loop state (see W_ENV_EMA_ALPHA); re-seeded each ramp
    w_env_ema: float | None = None

    # v16.3 dwell state (see CLIFF_DWELL_S)
    gate_b_cross_t: float | None = None  # when Gate B's drop first crossed its floor
    sliding_pend_t: float | None = None  # when the sliding path armed (None = idle)
    sliding_pend_ref_w  = 0.0            # pre-drop peak at arm time
    sliding_pend_peak_t = 0.0            # timestamp of that peak (lag compensation)
    sliding_pend_reason = ""             # "rate" / "delta" for logging
    sliding_pend_rate   = 0.0            # dP/dt at arm time for logging
    sliding_pend_quiesc = 0.0            # quiescent_s at arm time for logging

    while _running:
        t_now  = time.monotonic()
        w_hw   = rapl.current_watts              # true instantaneous hardware reading
        cpu_mon.feed_rapl(w_hw)                  # staleness tracker + calibration want raw

        # Despike: w_raw everywhere below is the median of the last
        # MEDIAN_FILTER_N distinct hardware refreshes. Single-sample RAPL
        # read-jitter spikes (±150 W at idle on mycroft) vanish; a real
        # cliff comes through intact one-to-two refreshes (~25-50 ms) later.
        if w_hw != med_last_refresh:
            med_win.append(w_hw)
            med_last_refresh = w_hw
        w_raw = sorted(med_win)[len(med_win) // 2]

        w_now  = cpu_mon.estimated_watts(w_raw)  # blended: filtered when fresh, model when stale

        # ── Context piercing: external phase override (v16.1) ─────────────────
        # Checked before the state dispatch so it can override *any* state.
        # An authoritative external signal skips every gate/gradient/cooldown
        # computation below — that's the point of piercing.
        pierced = phase_signal.poll_ready(t_now)

        if "WORKLOAD_END" in pierced and state in ("IDLE", "POTENTIAL_CLIFF", "COOLDOWN"):
            history.append((t_now, w_raw))
            ev_baseline_w = _estimate_baseline(history, t_now)
            t_recent      = t_now - RECENT_PEAK_WINDOW_S
            recent_peak_w = max((w for t_, w in history if t_ >= t_recent), default=w_raw)
            pierce_init_int = min(1.0, max(0.0, (recent_peak_w - ev_baseline_w) / worker_peak_w))

            ev_pre_drop_w       = recent_peak_w
            ev_init_int         = pierce_init_int
            intensity.value     = pierce_init_int
            ramp_start          = time.monotonic()
            ramp_initial_intens = pierce_init_int
            ev_handoff_w        = rapl.snapshot_watts(window=CALIB_WINDOW)
            event_num          += 1
            ev_detect_t         = t_now - t_origin
            ev_detect_w         = w_now
            ev_rate             = 0.0

            print(
                f"[Pierce] External WORKLOAD_END  t={ev_detect_t:.3f} s  "
                f"w_raw={w_raw:.1f} W  recent_peak={recent_peak_w:.1f} W  "
                f"baseline≈{ev_baseline_w:.1f} W  init_intensity={pierce_init_int:.2f}  "
                f"(overrode state={state})"
            )

            state = "RAMPDOWN"
            _write_smoother_event({
                "event_num":   event_num,
                "event_type":  "RAMP_START",
                "timestamp_s": round(ev_detect_t, 4),
            })
            deriv_win.clear()
            variance_win.clear()
            ramp_last_w     = None
            ramp_last_t     = None
            resumption_hits = 0
            time.sleep(RAMP_STEP)
            continue

        if "WORKLOAD_START" in pierced and state == "RAMPDOWN":
            intensity.value = 0.0
            ramp_end_t      = t_now - t_origin
            final_w         = rapl.snapshot_watts(window=CALIB_WINDOW)

            print(
                f"[Pierce] External WORKLOAD_START  t={ramp_end_t:.3f} s  "
                f"w_raw={w_raw:.1f} W  final≈{final_w:.1f} W  "
                f"— forcing immediate RAMP_INTERRUPTED"
            )

            _write_smoother_event({
                "event_num":   event_num,
                "event_type":  "RAMP_INTERRUPTED",
                "timestamp_s": round(ramp_end_t, 4),
            })
            cooldown_dur = FAST_INTERRUPT_COOLDOWN_S
            state        = "COOLDOWN"
            time.sleep(RAMP_STEP)
            continue

        # ── IDLE ──────────────────────────────────────────────────────────────
        if state == "IDLE":
            # Rolling history for 10th-percentile baseline estimation — always raw
            # so the idle floor reflects real hardware measurements, not model output
            history.append((t_now, w_raw))
            while history and t_now - history[0][0] > BASELINE_WINDOW:
                history.popleft()

            # Layer 3 — rolling variance window (raw: variance must reflect reality)
            variance_win.append(w_raw)

            # Layer 2 — keep last_high_power_t fresh while workload is active (raw)
            current_baseline = _estimate_baseline(history, t_now)
            if w_raw > current_baseline + WORKLOAD_ENGAGE_W:
                last_high_power_t = t_now

            # Derivative detection window — estimated: faster cliff detection
            deriv_win.append((t_now, w_now))

            # ── v16.3 sliding-path dwell: pending commit / cancel ─────────────
            # The sliding trigger below no longer commits directly — it arms
            # this pending check, which cancels if power recovers (micro-
            # stall) or commits once the drop has held for CLIFF_DWELL_S.
            # Layers 1-5 already passed at arm time, while the pre-drop
            # evidence was still inside the derivative window.
            if sliding_pend_t is not None:
                if w_now > sliding_pend_ref_w - DEADBAND_W:
                    print(
                        f"[Guard] Sliding arm cancelled (power recovered)  "
                        f"w_now={w_now:.1f} W  ref_peak={sliding_pend_ref_w:.1f} W  "
                        f"held={(t_now - sliding_pend_t)*1000:.0f} ms  — micro-stall"
                    )
                    sliding_pend_t = None
                elif t_now - sliding_pend_t >= CLIFF_DWELL_S:
                    ev_baseline_w  = _estimate_baseline(history, t_now)
                    lag_s          = max(0.0, t_now - sliding_pend_peak_t)
                    lag_frac       = max(0.0, 1.0 - lag_s / RAMPDOWN_SECS)
                    peak_intensity = min(1.0, max(0.0,
                        (sliding_pend_ref_w - ev_baseline_w) / worker_peak_w
                    ))
                    ev_init_int   = peak_intensity * lag_frac
                    ev_pre_drop_w = sliding_pend_ref_w

                    intensity.value     = ev_init_int
                    ramp_start          = time.monotonic()
                    ramp_initial_intens = ev_init_int
                    ev_handoff_w        = rapl.snapshot_watts(window=CALIB_WINDOW)
                    event_num          += 1
                    ev_detect_t         = t_now - t_origin
                    ev_detect_w         = w_now
                    ev_rate             = sliding_pend_rate

                    print(
                        f"[Event {event_num}]  t={ev_detect_t:.3f} s  "
                        f"trigger={sliding_pend_reason}+dwell  "
                        f"dP/dt={sliding_pend_rate:+.0f} W/s  "
                        f"delta={sliding_pend_ref_w - w_now:.1f} W  "
                        f"power={w_now:.1f} W  "
                        f"peak={sliding_pend_ref_w:.1f} W  "
                        f"lag={lag_s*1000:.0f} ms  "
                        f"init_intensity={ev_init_int:.2f}  "
                        f"baseline≈{ev_baseline_w:.1f} W  "
                        f"quiescent={sliding_pend_quiesc:.1f} s"
                    )

                    state = "RAMPDOWN"
                    _write_smoother_event({
                        "event_num":   event_num,
                        "event_type":  "RAMP_START",
                        "timestamp_s": round(ev_detect_t, 4),
                    })
                    deriv_win.clear()
                    variance_win.clear()
                    ramp_last_w     = None
                    ramp_last_t     = None
                    resumption_hits = 0
                    sliding_pend_t  = None
                    time.sleep(RAMP_STEP)
                    continue

            # ── Gate A: CPU utilisation early-tripwire (v16) ──────────────────
            # Runs every DETECT_POLL (2 ms) — much faster than the RAPL window
            # can fill.  If aggregate utilisation drops sharply within the last
            # CPU_DROP_WINDOW_S from a genuine workload level, arm Gate B.
            util_hist = cpu_mon.recent(CPU_DROP_WINDOW_S)
            if len(util_hist) >= 2:
                peak_u = max(u for _, u in util_hist)
                # "Current" utilisation is the trailing-average over the last
                # CPU_TREND_TRAIL_S, not a single last sample — a sustained
                # trend rather than a hyper-sensitive instantaneous delta.
                trail_cutoff  = t_now - CPU_TREND_TRAIL_S
                trail_samples = [u for t_, u in util_hist if t_ >= trail_cutoff]
                curr_u        = (
                    sum(trail_samples) / len(trail_samples)
                    if trail_samples else util_hist[-1][1]
                )
                gate_a_drop = peak_u - curr_u
                if gate_a_drop >= CPU_DROP_THRESHOLD and peak_u >= CPU_ACTIVE_MIN:
                    _last_gate_a_drop   = gate_a_drop
                    hybrid_rapl_at_trip = w_now
                    potential_cliff_t   = t_now
                    gate_b_cross_t      = None   # v16.3 dwell — fresh arm
                    # v16.1: how far past the minimum threshold did the drop
                    # land? 0.0 at exactly CPU_DROP_THRESHOLD (weak evidence),
                    # 1.0 at 2x threshold or more (strong evidence, e.g. a
                    # near-total utilisation collapse). Drives Gate B below.
                    gate_a_confidence = min(1.0, max(0.0,
                        (gate_a_drop - CPU_DROP_THRESHOLD) / CPU_DROP_THRESHOLD
                    ))
                    print(
                        f"[Hybrid] Gate A armed  t={t_now - t_origin:.3f} s  "
                        f"util_drop={gate_a_drop:.0%}  confidence={gate_a_confidence:.2f}  "
                        f"peak={peak_u:.0%}  curr={curr_u:.0%}  "
                        f"rapl={w_now:.1f} W"
                    )
                    state = "POTENTIAL_CLIFF"
                    time.sleep(DETECT_POLL)
                    continue

            if len(deriv_win) >= 2:
                # ── Fast-pass: severe single-step RAPL cliff ──────────────────
                fp_prev_t, fp_prev_w = deriv_win[-2]
                fp_dt = t_now - fp_prev_t
                if fp_dt > 0:
                    fp_rate  = (w_now - fp_prev_w) / fp_dt
                    fp_delta = fp_prev_w - w_now
                    if (
                        fp_rate <= FASTPASS_RATE_THRESHOLD
                        and fp_delta >= FASTPASS_DELTA_W
                        and sliding_pend_t is None
                    ):
                        # v16.3 utilisation veto: a genuine end-of-workload
                        # cliff always collapses aggregate utilisation. If
                        # cores are still busy, this RAPL step is a phase-
                        # boundary trough / turbo droop / read artifact —
                        # not a workload ending. (Trade-off: this weakens
                        # detection of busy-but-shedding memory-stall
                        # phases; on this workload those don't occur.)
                        veto_samples = cpu_mon.recent(CPU_TREND_TRAIL_S)
                        if veto_samples and (
                            sum(u for _, u in veto_samples) / len(veto_samples)
                            >= CPU_ACTIVE_MIN
                        ):
                            print(
                                f"[Guard] Fastpass vetoed (cores still busy)  "
                                f"delta={fp_delta:.1f} W  "
                                f"util≈{sum(u for _, u in veto_samples) / len(veto_samples):.0%} "
                                f"≥ {CPU_ACTIVE_MIN:.0%}  — staying in IDLE"
                            )
                            deriv_win.clear()
                            variance_win.clear()
                            time.sleep(DETECT_POLL)
                            continue

                        fp_baseline = _estimate_baseline(history, t_now)

                        # Late-trigger guard (v16 fix): if w_raw has already
                        # settled back near baseline, the cliff already fully
                        # resolved before we got here — don't ramp against a
                        # system that's already idle. Flush the contaminated
                        # windows instead of letting them seed a ghost trigger.
                        if _cliff_already_resolved(w_raw, fp_baseline,
                                                   t_now - last_high_power_t):
                            print(
                                f"[Guard] Late-trigger suppressed (fastpass)  "
                                f"w_raw={w_raw:.1f} W  baseline={fp_baseline:.1f} W  "
                                f"Δ={w_raw - fp_baseline:.1f} W ≤ {LATE_TRIGGER_GUARD_W:.1f} W  "
                                f"— already idle, staying in IDLE"
                            )
                            deriv_win.clear()
                            variance_win.clear()
                            time.sleep(DETECT_POLL)
                            continue

                        if fp_prev_w > fp_baseline + NOISE_GUARD_W:
                            # v16.3: fastpass no longer commits directly —
                            # run-8's Event 5 committed on a -59,785 W/s
                            # filtered spike that had recovered within one
                            # refresh. Arm the same pending dwell the
                            # sliding path uses; a real cliff stays down
                            # and commits ~120 ms later.
                            sliding_pend_t      = t_now
                            sliding_pend_ref_w  = fp_prev_w
                            sliding_pend_peak_t = fp_prev_t
                            sliding_pend_reason = "fastpass"
                            sliding_pend_rate   = fp_rate
                            sliding_pend_quiesc = t_now - last_high_power_t
                            print(
                                f"[Fastpass] armed  "
                                f"pre_step={fp_prev_w:.1f} W  "
                                f"w_now={w_now:.1f} W  "
                                f"delta={fp_delta:.1f} W  "
                                f"dP/dt={fp_rate:+.0f} W/s  "
                                f"— dwelling {CLIFF_DWELL_S*1000:.0f} ms before commit"
                            )
                            time.sleep(DETECT_POLL)
                            continue

                # ── Normal sliding-window path (fallback, all 5 layers) ────────
                dt = deriv_win[-1][0] - deriv_win[0][0]
                dw = deriv_win[-1][1] - deriv_win[0][1]
                if dt > 0:
                    rate      = dw / dt
                    peak_w    = max(w for _, w in deriv_win)
                    drop_w    = peak_w - w_now
                    rate_arm  = rate <= DERIV_THRESHOLD
                    delta_arm = drop_w > DELTA_THRESHOLD

                    if (rate_arm or delta_arm) and drop_w > DEADBAND_W and sliding_pend_t is None:
                        trigger_reason = "rate" if rate_arm else "delta"

                        # v16.3 utilisation veto — same reasoning as the
                        # fastpass veto above: a genuine end-of-workload
                        # cliff collapses utilisation; busy cores mean this
                        # drop is a boundary trough / turbo droop artifact.
                        veto_samples = cpu_mon.recent(CPU_TREND_TRAIL_S)
                        if veto_samples and (
                            sum(u for _, u in veto_samples) / len(veto_samples)
                            >= CPU_ACTIVE_MIN
                        ):
                            print(
                                f"[Guard] Sliding arm vetoed (cores still busy)  "
                                f"drop={drop_w:.1f} W  "
                                f"util≈{sum(u for _, u in veto_samples) / len(veto_samples):.0%} "
                                f"≥ {CPU_ACTIVE_MIN:.0%}  — staying in IDLE"
                            )
                            deriv_win.clear()
                            variance_win.clear()
                            time.sleep(DETECT_POLL)
                            continue

                        ev_baseline_w = _estimate_baseline(history, t_now)
                        ev_pre_drop_w = sum(w for _, w in deriv_win) / len(deriv_win)

                        # Late-trigger guard (v16 fix): same reasoning as the
                        # fastpass path above — if w_raw is already back near
                        # baseline, the cliff already fully resolved.
                        if _cliff_already_resolved(w_raw, ev_baseline_w,
                                                   t_now - last_high_power_t):
                            print(
                                f"[Guard] Late-trigger suppressed ({trigger_reason})  "
                                f"w_raw={w_raw:.1f} W  baseline={ev_baseline_w:.1f} W  "
                                f"Δ={w_raw - ev_baseline_w:.1f} W ≤ {LATE_TRIGGER_GUARD_W:.1f} W  "
                                f"— already idle, staying in IDLE"
                            )
                            deriv_win.clear()
                            variance_win.clear()
                            time.sleep(DETECT_POLL)
                            continue

                        t_recent      = t_now - RECENT_PEAK_WINDOW_S
                        recent_peak_w = max(
                            (w for t_, w in history if t_ >= t_recent),
                            default=peak_w,
                        )

                        # Layer 1
                        window_elevated  = ev_pre_drop_w > ev_baseline_w + NOISE_GUARD_W
                        history_elevated = (
                            recent_peak_w > ev_baseline_w + NOISE_GUARD_W + WORKLOAD_ENGAGE_W
                        )
                        if not window_elevated and not history_elevated:
                            time.sleep(DETECT_POLL)
                            continue

                        # Layer 5
                        if w_now >= ev_baseline_w + DROP_TO_BASELINE_MARGIN_W:
                            time.sleep(DETECT_POLL)
                            continue

                        # Layer 2
                        quiescent_s = t_now - last_high_power_t
                        if quiescent_s > QUIESCENT_TIMEOUT_S:
                            if ev_pre_drop_w > ev_baseline_w + WORKLOAD_ENGAGE_W:
                                last_high_power_t = t_now
                            else:
                                time.sleep(DETECT_POLL)
                                continue

                        # Layer 3
                        if len(variance_win) >= VARIANCE_WIN_N // 2:
                            mean_v = sum(variance_win) / len(variance_win)
                            var_v  = sum((x - mean_v) ** 2 for x in variance_win) / len(variance_win)
                            if var_v < VAR_QUIESCENT_W2:
                                time.sleep(DETECT_POLL)
                                continue

                        # Layer 4
                        effective_peak_w = max(peak_w, recent_peak_w)
                        workload_swing_w = max(1.0, effective_peak_w - ev_baseline_w)
                        adaptive_floor_w = ev_baseline_w + LAYER4_PEAK_FRAC * workload_swing_w
                        win_list         = list(deriv_win)
                        half             = max(1, len(win_list) // 2)
                        older            = win_list[:half]
                        older_mean_w     = sum(w for _, w in older) / half
                        if older_mean_w < adaptive_floor_w:
                            time.sleep(DETECT_POLL)
                            continue

                        # All gates passed — v16.3: don't commit yet. Arm the
                        # pending dwell (evaluated at the top of IDLE every
                        # iteration): the drop must persist for CLIFF_DWELL_S
                        # before RAMP_START. Snapshot the handoff inputs now,
                        # while the pre-drop peak is still in the window.
                        t_at_peak, _        = max(deriv_win, key=lambda x: x[1])
                        sliding_pend_t      = t_now
                        sliding_pend_ref_w  = peak_w
                        sliding_pend_peak_t = t_at_peak
                        sliding_pend_reason = trigger_reason
                        sliding_pend_rate   = rate
                        sliding_pend_quiesc = quiescent_s
                        print(
                            f"[Sliding] armed ({trigger_reason})  "
                            f"peak={peak_w:.1f} W  w_now={w_now:.1f} W  "
                            f"drop={drop_w:.1f} W  "
                            f"— dwelling {CLIFF_DWELL_S*1000:.0f} ms before commit"
                        )
                        time.sleep(DETECT_POLL)
                        continue

            time.sleep(DETECT_POLL)

        # ── POTENTIAL_CLIFF ───────────────────────────────────────────────────
        # Transient holding state entered when Gate A fires.  Waits for RAPL
        # to confirm the drop (Gate B) before committing to RAMP_START.
        # Maximum dwell is adaptive — see gate_b_timeout_s below (v16.1).
        elif state == "POTENTIAL_CLIFF":
            # Keep history current so baseline estimation stays accurate if
            # we fall back to IDLE after a timeout (raw only).
            history.append((t_now, w_raw))
            while history and t_now - history[0][0] > BASELINE_WINDOW:
                history.popleft()

            # Keep quiescent timer current so Layer 2 re-arms immediately
            # in IDLE if Gate A was a false alarm (raw).
            current_baseline = _estimate_baseline(history, t_now)
            if w_raw > current_baseline + WORKLOAD_ENGAGE_W:
                last_high_power_t = t_now

            elapsed_since_trip = t_now - potential_cliff_t
            rapl_drop = hybrid_rapl_at_trip - w_now   # positive when RAPL is falling

            # v16.1: adaptive Gate B — scale required drop and patience by how
            # strong the Gate A evidence was (gate_a_confidence, 0..1).
            gate_b_required_drop_w = (
                HYBRID_RAPL_MIN_DROP_W
                - gate_a_confidence * (HYBRID_RAPL_MIN_DROP_W - GATE_B_MIN_DROP_W)
            )
            gate_b_timeout_s = (
                HYBRID_VERIFY_TIMEOUT_S
                + gate_a_confidence * (GATE_B_MAX_TIMEOUT_S - HYBRID_VERIFY_TIMEOUT_S)
            )

            # ── Gate B: RAPL confirms the drop ────────────────────────────────
            # v16.3 dwell: crossing the floor once is not enough — at this
            # operating point a PREFILL micro-stall dips nearly as deep as a
            # real cliff (~100-150 W vs ~175-185 W), but it RECOVERS within
            # ~100 ms while a real cliff stays down. The drop must therefore
            # hold continuously for CLIFF_DWELL_S before RAMP_START commits;
            # the dwell timer resets the moment the drop un-crosses the floor.
            if rapl_drop >= gate_b_required_drop_w and gate_b_cross_t is None:
                gate_b_cross_t = t_now
                print(
                    f"[Hybrid] Gate B floor crossed  "
                    f"rapl_drop={rapl_drop:.1f} W ≥ {gate_b_required_drop_w:.1f} W  "
                    f"— dwelling {CLIFF_DWELL_S*1000:.0f} ms before commit"
                )
            elif rapl_drop < gate_b_required_drop_w and gate_b_cross_t is not None:
                print(
                    f"[Hybrid] Gate B drop recovered mid-dwell  "
                    f"rapl_drop={rapl_drop:.1f} W < {gate_b_required_drop_w:.1f} W  "
                    f"— micro-stall, dwell reset"
                )
                gate_b_cross_t = None

            if (
                gate_b_cross_t is not None
                and rapl_drop >= gate_b_required_drop_w
                and t_now - gate_b_cross_t >= CLIFF_DWELL_S
            ):
                ev_baseline_w = _estimate_baseline(history, t_now)

                # Late-trigger guard (v16 fix): if w_raw is already back near
                # baseline by the time Gate B confirms, the cliff already
                # fully resolved before we got here — drop straight back to
                # IDLE instead of committing a ramp against an idle system.
                if _cliff_already_resolved(w_raw, ev_baseline_w,
                                           t_now - last_high_power_t):
                    print(
                        f"[Guard] Late-trigger suppressed (hybrid)  "
                        f"w_raw={w_raw:.1f} W  baseline={ev_baseline_w:.1f} W  "
                        f"Δ={w_raw - ev_baseline_w:.1f} W ≤ {LATE_TRIGGER_GUARD_W:.1f} W  "
                        f"— already idle, returning to IDLE"
                    )
                    state = "IDLE"
                    time.sleep(DETECT_POLL)
                    continue

                # Minimal Layer 1: was the pre-cliff RAPL reading genuinely
                # elevated?  This blocks the rare case where RAPL drifts 2 W
                # downward during true deep idle and Gate B would otherwise
                # fire spuriously.
                if hybrid_rapl_at_trip > ev_baseline_w + NOISE_GUARD_W:
                    lag_s    = max(0.0, elapsed_since_trip)
                    lag_frac = max(0.0, 1.0 - lag_s / RAMPDOWN_SECS)

                    hybrid_init_int = min(1.0, max(0.0,
                        (hybrid_rapl_at_trip - ev_baseline_w) / worker_peak_w
                    )) * lag_frac

                    ev_pre_drop_w       = hybrid_rapl_at_trip
                    ev_init_int         = hybrid_init_int
                    intensity.value     = hybrid_init_int
                    ramp_start          = time.monotonic()
                    ramp_initial_intens = hybrid_init_int
                    ev_handoff_w        = rapl.snapshot_watts(window=CALIB_WINDOW)
                    event_num          += 1
                    ev_detect_t         = t_now - t_origin
                    ev_detect_w         = w_now
                    ev_rate             = (
                        (w_now - hybrid_rapl_at_trip) / max(0.001, elapsed_since_trip)
                    )

                    print(
                        f"[Event {event_num}]  t={ev_detect_t:.3f} s  "
                        f"trigger=hybrid  "
                        f"util_drop={_last_gate_a_drop:.0%}  confidence={gate_a_confidence:.2f}  "
                        f"rapl_drop={rapl_drop:.1f} W (required≥{gate_b_required_drop_w:.1f} W)  "
                        f"rapl_at_trip={hybrid_rapl_at_trip:.1f} W  "
                        f"w_now={w_now:.1f} W  "
                        f"gate_b_latency={elapsed_since_trip*1000:.0f} ms "
                        f"(timeout={gate_b_timeout_s*1000:.0f} ms)  "
                        f"init_intensity={hybrid_init_int:.2f}  "
                        f"baseline≈{ev_baseline_w:.1f} W"
                    )

                    state = "RAMPDOWN"
                    _write_smoother_event({
                        "event_num":   event_num,
                        "event_type":  "RAMP_START",
                        "timestamp_s": round(ev_detect_t, 4),
                    })
                    deriv_win.clear()
                    variance_win.clear()
                    ramp_last_w     = None
                    ramp_last_t     = None
                    resumption_hits = 0
                    time.sleep(RAMP_STEP)
                    continue

                else:
                    # RAPL moved but L1 failed: pre-trip power wasn't elevated,
                    # so the utilisation drop was probably a scheduler artifact.
                    print(
                        f"[Hybrid] Gate B disarmed (L1 fail)  "
                        f"rapl_at_trip={hybrid_rapl_at_trip:.1f} W  "
                        f"baseline={ev_baseline_w:.1f} W  "
                        f"guard={NOISE_GUARD_W:.1f} W  "
                        f"— returning to IDLE"
                    )
                    state = "IDLE"
                    time.sleep(DETECT_POLL)
                    continue

            # ── Gate A timeout ────────────────────────────────────────────────
            # RAPL did not confirm (and hold through the dwell) within the
            # adaptive gate_b_timeout_s plus the dwell budget.  The CPU
            # utilisation drop was likely a brief scheduler pause or a
            # memory-stall phase where cores appear idle to /proc/stat but
            # power has not actually dropped.  Disarm silently.
            if elapsed_since_trip > gate_b_timeout_s + CLIFF_DWELL_S:
                print(
                    f"[Hybrid] Gate A timeout ({elapsed_since_trip*1000:.0f} ms "
                    f"> {(gate_b_timeout_s + CLIFF_DWELL_S)*1000:.0f} ms)  "
                    f"rapl_drop={rapl_drop:.1f} W < {gate_b_required_drop_w:.1f} W  "
                    f"— disarmed, returning to IDLE"
                )
                state = "IDLE"

            time.sleep(DETECT_POLL)

        # ── RAMPDOWN ──────────────────────────────────────────────────────────
        elif state == "RAMPDOWN":
            elapsed = time.monotonic() - ramp_start
            frac    = max(0.0, 1.0 - elapsed / RAMPDOWN_SECS)

            # v16.2 fix (RAMP_END 30 W plateau): w_raw is *total* package
            # power — it includes whatever our own dummy workers are still
            # drawing this instant, not just the real environment. Every
            # check below that asks "has the environment actually gone
            # idle?" was feeding it raw w_raw, which makes the estimator
            # self-referential: as long as intensity.value > 0, our own
            # synthetic load props up the reading, so it never looks idle,
            # so the floor/baseline never converges, so intensity never
            # drops — a feedback loop that stalls the ramp above the true
            # floor instead of gliding down to it. We know our own
            # contribution exactly (we set intensity.value ourselves last
            # step, and that's what RAPL just measured), so subtract it out
            # before judging environmental state. This is an *estimate* of
            # the hardware-only reading, not a substitute for w_raw in
            # contexts that need the true total (e.g. the level-based
            # resumption check below, which deliberately compares the raw
            # total against an expected total).
            dummy_contribution_w = intensity.value * worker_peak_w
            w_env = max(0.0, w_raw - dummy_contribution_w)

            # v16.4 closed-loop state: EMA of the environment estimate.
            # ramp_last_w is None exactly on the first RAMPDOWN iteration,
            # so the EMA re-seeds itself at the start of every ramp.
            if w_env_ema is None or ramp_last_w is None:
                w_env_ema = w_env
            else:
                w_env_ema += W_ENV_EMA_ALPHA * (w_env - w_env_ema)

            # v16 floor-clamp fix (RAMP_END drop): ev_baseline_w was frozen
            # at detection time from a 10th-percentile lookback that can be
            # elevated above the true hardware idle floor whenever the
            # workload duty-cycle eats into the ≥90%-idle assumption behind
            # that estimator. Decay it toward freshly observed low
            # *environmental* readings (w_env, not raw w_raw — see v16.2
            # fix above) as the ramp progresses, so it converges on reality
            # instead of holding onto a stale, elevated snapshot — and never
            # let it (or the ramp target) fall below the known true floor.
            if w_env < ev_baseline_w:
                ev_baseline_w = max(
                    HARDWARE_IDLE_FLOOR_W,
                    ev_baseline_w + BASELINE_DECAY_ALPHA * (w_env - ev_baseline_w),
                )
            ramp_floor_w = max(ev_baseline_w, HARDWARE_IDLE_FLOOR_W)

            # Ramp the *target power* smoothly from the handoff level down to
            # ramp_floor_w, then derive intensity from that — instead of
            # driving intensity to 0 against a target that may sit above the
            # true floor, which is what produced the sharp RAMP_END cliff.
            target_power_w  = ramp_floor_w + (ev_handoff_w - ramp_floor_w) * frac
            # Hard TDP constraint: never *command* total power above the
            # clamp — a ramp whose handoff level is above RAMP_TARGET_CLAMP_W
            # glides along the ceiling until the linear trajectory falls
            # under it, instead of starting above it. The abort guardrail
            # below (RAMP_ABORT_ABS_W, higher) stays as the backstop for
            # genuine stacking in the measured total.
            target_power_w  = min(target_power_w, RAMP_TARGET_CLAMP_W)

            # v16.4 closed-loop command: add only the power that's MISSING
            # from the target, judged against the observed environment —
            # not against the idle floor (see W_ENV_EMA_ALPHA above). When
            # the environment alone meets or exceeds the target (e.g. a
            # ~350 W REST phase under a descending trajectory), the command
            # glides to 0 and the ramp finishes as a no-op instead of
            # stacking. Increases are slew-limited; decreases are instant.
            intensity_cmd = max(0.0, min(
                1.0, (target_power_w - w_env_ema) / worker_peak_w
            ))
            if intensity_cmd > intensity.value:
                intensity.value = min(intensity_cmd,
                                      intensity.value + INTENSITY_SLEW_UP)
            else:
                intensity.value = intensity_cmd

            # v16.2 fix: same self-contamination applies here — "is the real
            # workload still engaged" should be judged from the environment
            # reading, not the total that includes our own dummy load, or
            # this re-arms last_high_power_t on essentially every ramp step.
            if w_env > ev_baseline_w + WORKLOAD_ENGAGE_W:
                last_high_power_t = t_now

            abort_reason: str | None = None
            backoff_kind = "rate"

            # Overlap / power-stacking guardrail: absolute ceiling on total
            # package draw during a ramp. If the primary workload surges
            # back (or never actually left) while our dummy load is still
            # engaged, the two stack — cut the workers instantly the moment
            # total power crosses RAMP_ABORT_ABS_W, rather than waiting for
            # a rate signal, so a hardware-scale transient never forms.
            # Checked FIRST but gated by RAMP_BLANK_S: the filtered RAPL
            # value still reflects the pre-cliff level for the first ~2-3
            # refreshes after a genuine RAMP_START (median lag), so an
            # ungated check could abort a legitimate high-power ramp at
            # step one on a lag artifact. During the blank window the
            # target clamp above already caps our own commanded power at
            # RAMP_TARGET_CLAMP_W; past the blank, measured total above
            # RAMP_ABORT_ABS_W (which sits above the workload's own ~445 W
            # peak) really does mean stacking.
            if elapsed > RAMP_BLANK_S and w_raw >= RAMP_ABORT_ABS_W:
                intensity_at_abort = intensity.value
                intensity.value    = 0.0
                abort_reason       = "backoff"
                backoff_kind       = "abs-guardrail"
                print(
                    f"[Event {event_num}]  RAMP_BACKOFF (absolute guardrail)  "
                    f"t={t_now - t_origin:.3f} s  "
                    f"w_raw={w_raw:.1f} W ≥ {RAMP_ABORT_ABS_W:.0f} W  "
                    f"intensity={intensity_at_abort:.2f}  "
                    f"elapsed={elapsed:.2f} s"
                )

            # Single-step rate/delta off the raw RAPL trace — shared by the
            # negative-gradient abort, the level-resumption check, and the
            # backoff check below, so all three agree on what the
            # instantaneous trajectory is actually doing.
            step_rate:  float | None = None
            step_delta: float | None = None
            if ramp_last_w is not None and ramp_last_t is not None:
                dt_r = t_now - ramp_last_t
                if dt_r > 0:
                    step_rate  = (w_raw - ramp_last_w) / dt_r
                    step_delta = ramp_last_w - w_raw

            # v16.2 fix (false RAMP_INTERRUPTED on genuine cliffs): a steep
            # negative step is only a *conflicting* signal once w_raw has
            # actually come down near the idle floor — at that point there's
            # nothing left for the ramp's own trajectory to explain, so a
            # further sharp drop means something else is going on (e.g. a
            # second, distinct cliff arriving mid-ramp). Far above the floor,
            # an aggressive negative rate right after RAMP_START simply *is*
            # the real cliff still landing — RAPL's refresh lag means our
            # linear ramp target hasn't caught up to the hardware yet. That's
            # the trajectory we're trying to smooth, not a conflict with it,
            # so keep workers engaged and ride the cliff out.
            near_floor = w_raw <= ramp_floor_w + RESUMPTION_MARGIN_W

            # v16.1 fast abort on negative gradient — deliberately checked
            # FIRST and not gated by the full RAMP_BLANK_S. BACKOFF_THRESHOLD
            # below only catches the real workload coming back ON (a rise);
            # this is the symmetric case where it drops *further* than our
            # own ramp explains — e.g. a false/early RAMP_START mid-PREFILL
            # followed shortly after by the real PREFILL→REST cliff. That
            # combination used to take ~4 s to resolve via the level-based
            # check below; a steep further drop is real signal early — but
            # only once (a) it's actually near the floor (see near_floor
            # above) and (b) the ramp is older than MEDIAN_TRANSIT_S
            # (v16.3): for the first N×refresh after RAMP_START the median
            # filter is still replaying the just-confirmed cliff itself, so
            # a "steep further drop" in that window is the detection's own
            # lagged image, not a second cliff — this exact artifact
            # produced the ~0.10 s RAMP_INTERRUPTED storm in the 2026-07-14
            # mycroft run (events 5, 11, 13).
            if (
                abort_reason is None
                and near_floor
                and elapsed >= MEDIAN_TRANSIT_S
                and step_rate is not None
                and step_delta is not None
                and step_rate <= RAMPDOWN_NEG_RATE_ABORT_W_S
                and step_delta >= RAMPDOWN_NEG_DELTA_ABORT_W
            ):
                intensity_at_abort = intensity.value
                intensity.value    = 0.0
                abort_reason       = "interrupted"
                resumption_hits    = 0
                print(
                    f"[Event {event_num}]  RAMP_INTERRUPTED (negative gradient)  "
                    f"t={t_now - t_origin:.3f} s  "
                    f"step_rate={step_rate:+.0f} W/s  "
                    f"step_delta={step_delta:.1f} W  "
                    f"intensity={intensity_at_abort:.2f}  "
                    f"elapsed={elapsed:.2f} s"
                )

            # Level-based resumption check. v16.2 fix: excess-power hits
            # only accumulate toward the debounce when the raw trajectory is
            # actually consistent with resumption — i.e. power has
            # stabilized or is rising, not still actively falling. A
            # strongly negative single-step rate means the primary workload
            # is still shedding power *right now*; mistaking that transient
            # excess (target_power_w lagging a steep real drop) for "the
            # workload came back" is exactly the false-interrupt bug. So
            # bypass the debounce counter entirely — reset it — whenever the
            # gradient is still strongly negative, no matter how far above
            # the trailing target w_raw momentarily reads.
            if abort_reason is None and elapsed > RAMP_BLANK_S:
                # v16.4: expected total is the closed-loop model — observed
                # environment plus our commanded contribution — not floor +
                # command. An elevated-but-stable environment (REST phase)
                # is absorbed by the loop itself (intensity falls), so
                # excess here now means what it claims: power beyond what
                # the model can explain, i.e. genuine stacking / the real
                # workload surging back faster than the EMA can track.
                expected_w = w_env_ema + intensity.value * worker_peak_w
                excess_w   = w_raw - expected_w

                still_dropping = (
                    step_rate is not None
                    and step_rate <= RAMPDOWN_NEG_RATE_ABORT_W_S
                )

                if still_dropping:
                    resumption_hits = 0
                elif excess_w > RESUMPTION_MARGIN_W:
                    resumption_hits += 1
                else:
                    resumption_hits = 0

                if resumption_hits >= RESUMPTION_DEBOUNCE_N:
                    intensity_at_abort = intensity.value
                    intensity.value    = 0.0
                    abort_reason       = "interrupted"
                    resumption_hits    = 0
                    print(
                        f"[Event {event_num}]  RAMP_INTERRUPTED (level)  "
                        f"t={t_now - t_origin:.3f} s  "
                        f"w_raw={w_raw:.1f} W  "
                        f"expected={expected_w:.1f} W  "
                        f"excess={excess_w:.1f} W  "
                        f"hits={RESUMPTION_DEBOUNCE_N}  "
                        f"intensity={intensity_at_abort:.2f}  "
                        f"elapsed={elapsed:.2f} s"
                    )

            # Rate-based backoff — the real workload surging back ON.
            # v16.3 hardening: blank-gated and debounced. Our own worker
            # handoff landing (and the filter's V-shaped detect-then-handoff
            # replay) produces a single-step rise well past the threshold in
            # the first ramp steps — self-signal, not resumption. Requiring
            # BACKOFF_DEBOUNCE_N consecutive hits past RAMP_BLANK_S costs
            # ~100 ms of reaction time on a genuine resumption, which the
            # absolute guardrail above already bounds in watts.
            if abort_reason is None and step_rate is not None and step_rate >= BACKOFF_THRESHOLD:
                if elapsed > RAMP_BLANK_S:
                    backoff_hits += 1
                    if backoff_hits >= BACKOFF_DEBOUNCE_N:
                        intensity.value = 0.0
                        abort_reason    = "backoff"
            else:
                backoff_hits = 0

            if abort_reason is None and elapsed >= RAMPDOWN_SECS:
                intensity.value = 0.0
                abort_reason    = "normal"

            if abort_reason is not None:
                ramp_end_t  = time.monotonic() - t_origin
                final_w     = rapl.snapshot_watts(window=CALIB_WINDOW)
                actual_secs = round(time.monotonic() - ramp_start, 2)

                # v16.1: RAMP_INTERRUPTED cooldown is now adaptive on how
                # long the ramp survived before being interrupted (`elapsed`
                # at abort time), not a flat value. A fast interrupt (either
                # the negative-gradient check above, or the level check
                # resolving within FAST_INTERRUPT_S) means detection was
                # confident and quick — the system isn't confused, so
                # re-arm fast. A slow interrupt means the signal was
                # genuinely ambiguous for a while — give it more time to
                # settle and let history reaccumulate clean samples. (And
                # since history is no longer wiped every COOLDOWN — see the
                # COOLDOWN state below — that reaccumulation starts from a
                # real baseline instead of from scratch.)
                if abort_reason == "normal":
                    cooldown_dur = COOLDOWN_SECS
                elif abort_reason == "interrupted":
                    cooldown_dur = (
                        FAST_INTERRUPT_COOLDOWN_S if elapsed < FAST_INTERRUPT_S
                        else SLOW_INTERRUPT_COOLDOWN_S
                    )
                else:
                    cooldown_dur = BACKOFF_COOLDOWN

                event_type = {
                    "normal":      "RAMP_END",
                    "interrupted": "RAMP_INTERRUPTED",
                    "backoff":     "RAMP_BACKOFF",
                }[abort_reason]

                if abort_reason == "normal":
                    print(
                        f"[Event {event_num}]  Ramp done (normal)  "
                        f"init={ramp_initial_intens:.2f}  "
                        f"handoff={ev_handoff_w:.1f} W  "
                        f"final≈{final_w:.1f} W  "
                        f"baseline={ev_baseline_w:.1f} W  "
                        f"elapsed={actual_secs:.2f} s  "
                        f"next_cooldown={cooldown_dur:.1f} s"
                    )
                elif abort_reason == "backoff":
                    print(
                        f"[Event {event_num}]  Ramp backoff ({backoff_kind})  "
                        f"handoff={ev_handoff_w:.1f} W  "
                        f"final≈{final_w:.1f} W  "
                        f"elapsed={actual_secs:.2f} s  "
                        f"next_cooldown={cooldown_dur:.1f} s"
                    )

                _write_smoother_event({
                    "event_num":   event_num,
                    "event_type":  event_type,
                    "timestamp_s": round(ramp_end_t, 4),
                })
                state = "COOLDOWN"
                time.sleep(RAMP_STEP)
                continue

            ramp_last_w = w_raw
            ramp_last_t = t_now
            time.sleep(RAMP_STEP)

        # ── COOLDOWN ──────────────────────────────────────────────────────────
        # Blind spot for gating purposes — deriv_win/variance_win (the
        # short-term trend detectors) are reset so no stale trend carries
        # into the new arm cycle. They're already empty by construction here
        # (every RAMP_START commit point clears them, and nothing appends to
        # them during RAMPDOWN), so this is a defensive no-op, not the fix.
        #
        # v16.3 echo fix: med_win, however, is NOT empty here — it holds the
        # last N filtered-input samples from the ramp, i.e. dummy-elevated
        # power levels. Left in place, the first few IDLE iterations after
        # cooldown displace them with fresh idle-level refreshes, and the
        # *filtered* value walks down 150-300 W in ~2-3 refreshes — a
        # textbook fastpass cliff signature manufactured entirely by the
        # daemon's own abort. That echo re-triggered a new RAMP_START
        # ~(cooldown + 0.1 s) after almost every abort in the 2026-07-14
        # mycroft run (events 3, 7, 9, 14, 15). Flush it, then re-prime it
        # with fresh post-cooldown refreshes so IDLE never runs on a thin
        # (spike-transparent) median window either.
        #
        # v16.1 fix: history is deliberately NOT cleared here anymore.
        # RAMPDOWN never appends to `history` either (only IDLE/
        # POTENTIAL_CLIFF do), so wiping it bought no protection against
        # ramp-decay contamination — there was nothing in it to contaminate.
        # What it *did* do: discard up to BASELINE_WINDOW (60 s) of genuine
        # pre-ramp observations. If the daemon comes back from COOLDOWN
        # while real power happens to be elevated (a second, unrelated
        # workload burst — exactly what happened between Event 5 and the
        # ~110 s cliff in the traced run), a freshly-emptied history has
        # nothing low to compare against: _estimate_baseline() gets pulled
        # up to match whatever's elevated *right now*, and every "is this
        # actually elevated over baseline" gate (Layer 1, and Gate B's
        # minimal L1) then fails against the real cliff that follows —
        # exactly the silent, multi-second blind spot this bug report
        # describes. Keeping history intact across the cooldown boundary
        # means baseline estimation stays anchored to real history the
        # moment IDLE resumes.
        elif state == "COOLDOWN":
            deriv_win.clear()
            variance_win.clear()
            med_win.clear()
            med_last_refresh = None
            sliding_pend_t   = None   # v16.3 — stale pending arm dies with the ramp
            time.sleep(cooldown_dur)
            # Re-prime the despike median (~MEDIAN_TRANSIT_S) before arming.
            while _running and len(med_win) < MEDIAN_FILTER_N:
                w_hw = rapl.current_watts
                cpu_mon.feed_rapl(w_hw)
                if w_hw != med_last_refresh:
                    med_win.append(w_hw)
                    med_last_refresh = w_hw
                time.sleep(DETECT_POLL)
            state = "IDLE"

    # ── Graceful shutdown ─────────────────────────────────────────────────────
    print("\n[Daemon] Shutting down …")
    intensity.value = 0.0
    time.sleep(0.3)
    stop_evt.set()
    for w in workers:
        w.join(timeout=3)
    cpu_mon.stop()
    rapl.stop()

    try:
        os.remove(PID_FILE)
    except OSError:
        pass

    _write_samples(rapl.all_samples())
    print(f"[Daemon] Events log → {SMOOTHER_EVENTS_CSV}")


if __name__ == "__main__":
    main()
