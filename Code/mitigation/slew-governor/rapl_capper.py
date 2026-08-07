#!/usr/bin/env python3
"""Reactive RAPL power capper -- shaves the PL2 turbo overshoot on spike onset.

Physics (measured; see characterize_overshoot.py / onset_leading_structure.py):
spike onset is a pure single-sample step (no forecasting possible at any rate),
then package power rides a turbo plateau ~+6.9% over sustained (~217 W vs
~205 W at test scale, ~434 vs ~410 W full scale) for a stable Tau = 1.6 s until
Intel's PL2->PL1 clamp steps it down by itself. The RF regime detector
(spike_daemon_rf.py -> InfluxDB measurement `power_spike_prediction`, field
`spike_risk`, bucket "Power") already sees the onset within ~1-2 s. So no
prediction is needed: on flag, write a lower package power limit into the RAPL
powercap sysfs for the turbo window, release on a timer, and the overshoot is
shaved. Feed-forward only -- the detector is NOT re-consulted inside the window.

Safety model (do NOT simplify these away):
  * FLOOR: refuses to start if the cap is below the PL1 sustained baseline
    (default floor = current constraint_0 value). Consequence: even the worst
    uncovered failure (SIGKILL / power loss mid-window leaves the kernel capped
    until manual restore or reboot) can only trim turbo, never throttle steady
    state, because the stuck cap is still >= PL1.
    EXCEPTION -- --allow-below-floor (2026-07-15): the floor IS the ceiling.
    mycroft's PL1 is 205 W x 2 sockets = a 410 W floor, ABOVE ai_load.sh's
    ~368 W busy plateau -- a floor-respecting cap can only trim turbo peaks
    and can never visibly smooth the plateau (five live demos proved it).
    With this flag a --cap-watts below PL1 lowers BOTH constraints (PL1+PL2)
    to the cap, clamping the plateau itself. This deliberately throttles
    steady state while capped; all other invariants (bit-exact restore,
    watchdog, fail-stop) still hold, and a stuck cap still only slows the
    box, never harms it. Supervised smoothing runs only.
  * Never-raise: only constraints currently ABOVE the cap are lowered;
    constraint_0 (PL1) is left untouched at the default cap level.
  * Release: unconditional timer at t_trigger + tau (main loop sleeps exactly
    until then; the cadence-1s poll only applies while idle).
  * Watchdog: an independent threading.Timer force-restores at t_trigger +
    max_cap_s even if the main loop is wedged or the release path never runs.
  * Restore-on-exit: all touched constraint_*_power_limit_uw values are
    snapshotted raw at startup and restored bit-exact on normal release, on
    SIGINT/SIGTERM, in main()'s finally, and via atexit. Every exit path also
    read-compare-writes the whole snapshot back (covers a crash mid-cap that
    left files dirty with the state flags confused).
  * Fail-stop: a failed restore write is retried in-call (3 attempts, short
    backoff) and again by every exit path; if the box still cannot be uncapped
    the process screams CRITICAL and exits nonzero (3) -- including --once --
    never a silent 0. A recovery by a later pass is logged (restore_recovered).

Triggers (pick one):
  (default)              poll the detector's local data/spike_risk.flag --
                         edge-triggered 0->1, stale mtime (>15 s) = no-signal.
                         Direct coupling; removes the 5.2 s InfluxDB publish
                         cadence from the latency budget (endeavor_summary §8).
  --watch-file PATH      same mechanism, explicit path
  --simulate "2,15,30"   scripted flag offsets in seconds (exits when consumed)
  --influx               legacy: poll InfluxDB power_spike_prediction for a
                         fresh spike_risk=true point (adds the detector's 5.2 s
                         publish cadence -- kept only for A/B comparison)
  --local-power          reactive: fires on a real power derivative crossing
                         POWER_DERIV_THRESHOLD_W_S, sampled directly off the
                         RAPL energy_uj counters at --poll-s (default 0.25 s).
                         No dependency on the ML scorer at all -- 2026-07-15
                         live demo found the scorer's ~5-7 s publish cadence
                         plus the fixed tau release meant caps armed on stale
                         flags and released via blind timer before the real
                         onset even began (see docs/prediction_engine.md).
                         Also switches release to feedback: tau becomes a
                         MINIMUM hold, and release only fires once measured
                         power actually drops back near the PL1 floor
                         (+RELEASE_MARGIN_W); the watchdog max_cap_s remains
                         the hard ceiling either way. Default --cooldown is
                         also short (0.1s, not the legacy 5s) -- a real
                         ai_load.sh burst ramps in two stages, and the 5s
                         flap-guard (meant for a noisy ML flag) was silencing
                         the legitimate second cap on the workload's second
                         stage, which lands well inside that window.

  --slew                 SLEW GOVERNOR (2026-07-15 redesign): continuous ramp
                         shaping instead of edge-triggered cap/hold/release.
                         Motivation: the flat deep clamp (130 W/socket) smooths
                         perfectly but costs 26-33% HPL Gflops (measured sweep,
                         N=20000: uncapped 1016, 180 W 747, 160 W 679); the
                         perf hit comes from throttling the WHOLE plateau when
                         only the transitions need shaping.
                         Up-ramps: the RAPL ceiling hugs measured power
                         (peak-hold over PEAK_HOLD_S + HEADROOM_W) and rises at
                         --slew-up W/s, so an onset becomes a controlled ramp;
                         a steady busy plateau runs entirely uncapped => ~0%
                         steady-state cost, only the onset ramp throttles
                         (~deltaP/slew seconds per onset; ai_load.sh's ~210 W
                         step at 75 W/s = 2.8 s of partial throttle per 18 s
                         busy phase, ~<=8% worst case -- validate live).
                         Down-ramps: a cap cannot stop power from FALLING; with
                         --ballast, SCHED_IDLE duty-cycled spinner processes
                         (ramp.c's worker pattern) fill the drop and decay at
                         --slew-down W/s. SCHED_IDLE only runs on otherwise
                         idle cycles, so ballast steals ~nothing from real
                         work. --ballast-w-per-core needs live calibration.
                         Implies --allow-below-floor (the ceiling sits near
                         idle between jobs). Snapshot/restore/fail-stop
                         invariants unchanged; a deadman thread force-restores
                         and exits 3 if the governor loop wedges.

Run:  .venv/bin/python rapl_capper.py --selfcheck        # offline proof, no root
      .venv/bin/python rapl_capper.py --dry-run          # live trigger, no writes
      sudo .venv/bin/python rapl_capper.py --secrets-dir /home/dlee/.secrets

Deferred LIVE validation plan (mycroft unreachable today; run in this order):
  1. READ-ONLY probe (inline -- there is no helper script; note BOTH sockets):
     ssh mycroft 'grep -H . /sys/class/powercap/intel-rapl:{0,1}/name \
         /sys/class/powercap/intel-rapl:{0,1}/constraint_*_{name,power_limit_uw,time_window_us} \
         && ls -l /sys/class/powercap/intel-rapl:{0,1}/constraint_*_power_limit_uw'
     -> expect constraint_0 long_term = 205000000 (PL1), constraint_1
        short_term = 246000000 (PL2), both -rw------- root (hence sudo).
  2. Dry-run beside the real detector through an ai_load.sh cycle (no root
     needed, writes nothing):  .venv/bin/python rapl_capper.py --dry-run
     (default trigger = the detector's local data/spike_risk.flag)
     -> compare data/capper_events.jsonl cap timestamps against the known
     scripted onsets; with the local-flag trigger the 5.2 s InfluxDB publish
     term is gone, so lag should be detector-cycle-bound (~1 s) + POLL_S.
  3. Root + live with the conservative default cap (= PL1): capture 10 Hz
     power with vs without the capper (overshoot plateau should flatten), and
     a paired job-runtime check -- runtime regression must stay < 10%.
"""
import argparse
import atexit
import glob as globmod
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

RAPL_ROOT = "/sys/class/powercap/intel-rapl:*"   # ALL package zones (2-socket fix); subzones (:N:M core/dram) filtered out
TAU_S = 1.6          # measured PL2 window (characterize_overshoot.py); stability across workloads unproven
MAX_CAP_S = 5.0      # independent watchdog force-release
COOLDOWN_S = 5.0     # after release, ignore triggers this long (clustered-flag flap guard)
POLL_S = 0.25        # idle trigger-poll cadence (was 1.0; §8 "leverage item 3")
MAX_FLAG_AGE_S = 15.0  # watch-file mtime freshness; older = detector down = no-signal (fail-open)
POWER_DERIV_THRESHOLD_W_S = 97.0  # same severe-tier derivative as the spike label (core/spike_labels.py)
RELEASE_MARGIN_W = 10.0           # --local-power: release once power <= floor + this margin
RELEASE_DEBOUNCE_S = 1.0          # --local-power: power must stay settled this long before
                                  # releasing -- ai_load.sh's real busy plateau is a rapid
                                  # train of ~100-300ms bursts every ~1.5-2.6s, not one clean
                                  # overshoot; releasing on the first settled sample between
                                  # bursts let every next burst's peak straight through
                                  # uncapped (2026-07-15 demo: mean dropped ~7%, max ~0%).
                                  # A real STOP/CONT dip is 3s, comfortably longer than this.
DEEP_RELEASE_HYSTERESIS_W = 40.0  # --allow-below-floor: clamped busy power sits AT the cap,
                                  # so "near the cap" means STILL BUSY -- release only once
                                  # power falls this far below the cap (toward idle). Pick a
                                  # deep cap >= idle + ~50 W or it never releases (the
                                  # watchdog max_cap_s is the backstop either way).
LOCAL_POWER_COOLDOWN_S = 0.1      # --local-power default: real ai_load.sh bursts ramp in two
                                  # stages (initial rise, then a further jump ~1.6-1.8s later,
                                  # right as tau/release fires) -- the 5s legacy cooldown (a
                                  # flap-guard for the noisy ML flag) was silencing the
                                  # legitimate second cap on that second stage. Release is
                                  # already power-gated here, so a long cooldown is redundant.
# --slew governor defaults (perf targets estimated offline; validate live)
SLEW_UP_W_S = 75.0        # ceiling rise rate: 210 W ai_load onset => 2.8 s ramp (<=~8% of an 18 s busy phase)
SLEW_DOWN_W_S = 75.0      # ceiling fall + ballast decay rate
HEADROOM_W = 25.0         # ceiling sits this far above the recent power peak (meter noise ~+-5 W)
PEAK_HOLD_S = 0.5         # (was 1.5; replay sweep 2026-07-17: every 0.1 s of
                          # hold is ~7.5 W of ceiling lag at the resume -- 1.5 s
                          # left resumes jumping +323 W/s vs +211 at 0.5 s, and
                          # peak-hold only acts while ENGAGED, so plateau safety
                          # is unaffected)
                          # ceiling tracks max power over this window -- brief inter-burst
                          # dips must not drag the ceiling down and re-throttle the plateau
                          # (ai_load plateau is 410 W +/- 7.5, so 1.5 s is safe), but a
                          # real >=3 s dip MUST start dragging it or post-dip resumes jump
                          # straight back to the old ceiling unshaped (2026-07-16: 3.0 s
                          # hold == the dip length meant zero resume shaping)
MIN_CEILING_W = 120.0     # absolute total-power floor for the ceiling (spurious ~0 W read guard)
GOV_POLL_S = 0.05         # governor control cadence. The reactive notch at
                          # every edge (power moves before the gate reacts) is
                          # ~2 ticks wide: 0.1 s polls left 384/-353 W/s spikes
                          # inside 0.5 s windows (2026-07-16 gov11).
JUMP_ENGAGE_W = 45.0      # single-tick |delta p| that engages IMMEDIATELY (one
                          # poll, no 0.3 s deriv-window wait). Plateau sample-to-
                          # sample noise is <~30 W; real ai_load edges are ~210 W.
                          # Replay predicted 60 was free (duty 18->9%, same
                          # ramps); LIVE 2026-07-17 (gov17) contradicted it:
                          # 60 ran 95% Gflops but the backstop cap engaged too
                          # late and edges roughened +189->+215 / -196->-208 W/s
                          # vs gov16. Smoothness is the goal and 45 already
                          # clears the 90% bar, so keep 45 (the crude replay
                          # undershoot model doesn't see late-engage edge slip).
ENGAGE_DERIV_W_S = 80.0   # |dP/dt| over DERIV_WIN_S that means "a transition is
                          # happening" -> engage the hug+slew. Plateau noise is
                          # ~7.5 W std (~40 W/s over 0.5 s), real ai_load edges
                          # are ~400 W/s. While quiet the caps sit fully STOCK:
                          # a permanently hugging cap costs 8-16% HPL whatever
                          # constraint it rides (2026-07-16 runs 2-7), because
                          # RAPL undershoots the written limit ~25-45 W.
DERIV_WIN_S = 0.5         # window for the gating derivative
DISENGAGE_STABLE_S = 1.0  # quiet (unpressed, |deriv| low, no ballast) this long
                          # -> release back to stock caps (was 2.0; replay sweep
                          # 2026-07-17: 1.0 cuts engage duty ~5 pp with identical
                          # ramp shaping -- less hug time is free perf)
SLEW_SHAPE_S = 3.0        # dynamic slope: spread the observed ceiling gap over
                          # ~this long, so a 210 W ai_load onset ramps ~70 W/s
                          # while a 40 W wiggle ramps gently -- slope follows
                          # the workload's own swing size. --slew-up/--slew-down
                          # stay the hard per-direction maxima.
SLEW_MIN_W_S = 15.0       # gentlest dynamic ramp (W/s)
BALLAST_DEADBAND_W = 30.0 # a fill only STARTS above this deficit: p_ref rides
                          # the noisy sample max while workload_p takes the
                          # noisy minimum, so meter noise fakes ~10-25 W
                          # deficits that twitch the pool at every plateau and
                          # reset the disengage timer forever (2026-07-16). An
                          # ongoing fill keeps tracking below the deadband so
                          # dip decays stay smooth. Real dips are 100-210 W.
RISK_ENGAGE_FRAC = 0.6    # risk flag pre-engages ONLY while power < this frac
                          # of top: pre-arming helps a coming UP-swing (cap is
                          # already low when it lands). At the busy plateau the
                          # regime detector reads "risky" the whole time, and
                          # honoring it there kept the hug engaged all run --
                          # that hug cost most of gov11's -8.3% Gflops while
                          # buying nothing (a cap can't mitigate a dip; ballast
                          # does, and it runs regardless).
PREBURN_WORKLOAD_FLOOR_FRAC = 0.45  # safety net for false detector flags: after
                          # the box has been flat idle for PREBURN_IDLE_SUPPRESS_S,
                          # risk pre-burn is ignored until real workload power is
                          # above an idle-ish floor, or measured power is rising.
                          # A recent detector lead still pre-burns, preserving
                          # gov16's led-onset behavior.
PREBURN_RISING_DERIV_W_S = 30.0
PREBURN_RISING_STEP_W = 20.0
PREBURN_IDLE_SUPPRESS_S = 30.0
PRESSED_ESCALATE_S = 1.0  # pressed this long = sustained throttle of real work
                          # (not a transient catch): climb at full slew_up. The
                          # dynamic slope alone wedged post-resume ceilings at
                          # gap/3s ~19 W/s for seconds of Gflops-costing deficit
                          # (gov13 live 2026-07-17: 87.0% vs the 90% bar); but
                          # escalating IMMEDIATELY stacks the fast climb into
                          # the same 0.5 s window as the engage catch and blows
                          # the worst-case ramp (replay: +260 W/s vs +238).
PREBURN_MAX_S = 15.0      # risk pre-burn: ramp ballast toward the full pool on a
                          # fresh risk-flag edge (while armed, see RISK_ENGAGE_FRAC)
                          # so a detector-led onset lands as a near-zero net step:
                          # SCHED_IDLE ballast yields watt-for-watt to the real
                          # work -- shaping by burn, not throttle. An episode burns
                          # at most this long (detector lead is ~3 s), then decays;
                          # a stuck-high flag can't re-arm until it drops. Trades
                          # deliberately wasted energy for zero Gflops cost
                          # (user decision 2026-07-17).
CONTACT_W = 40.0          # power within this of the ceiling = workload pressed against
                          # the cap (RAPL undershoots the written limit ~25 W live) ->
                          # climb toward stock at slew_up. Must exceed the undershoot or
                          # the loop wedges at ceiling = throttled_power + headroom and
                          # never satisfies demand (2026-07-16 live: whole ai_load run
                          # stuck ~180 W low). When uncapped (cap > demand) the ceiling
                          # saws gently in [peak+headroom, peak+contact] -- cap-only
                          # dither above actual power, invisible on the power trace.
BALLAST_W_PER_CORE = 1.3  # rough W burned per fully-busy spinner core (CALIBRATE LIVE;
                          # mycroft 2026-07-16: 3.26 W/core measured, 32-core SCHED_IDLE)
BALLAST_MAX_CORES = 32    # ballast worker pool cap
CEIL_DIVE_MAX_W_S = 300.0 # ceiling descent bound while engaged. A falling
                          # ceiling above falling power is NOT a power event
                          # (power is already below it), so the chase may run
                          # far faster than slew_down -- otherwise a 3 s dip
                          # ends with the ceiling still ~80 W high and the
                          # resume jumps to (ceiling - undershoot) unshaped
                          # (replay 2026-07-17: max_up +309 W/s from exactly
                          # this). slew_down keeps governing POWER shaping
                          # (ballast decay).
DEADMAN_S = 5.0           # governor loop stale this long => force-restore + exit 3
INFLUX_BUCKET = "Power"   # where spike_daemon_rf.py writes the flag (bucket name is not the secret org name)
DEFAULT_EVENTS = Path(__file__).resolve().parent / "data" / "capper_events.jsonl"
DEFAULT_FLAG = Path(__file__).resolve().parent.parent / "data" / "spike_risk.flag"

_EXIT_REASON = ["exit"]   # signal handler stamps e.g. "signal:SIGTERM" for the JSONL


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def die(msg, code=2):
    print(f"[{now_iso()}] FATAL: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def read_raw(path):
    with open(path) as fh:
        return fh.read()


def zone_key(p):
    """zone-qualified constraint name for logs -- bare p.name collides across sockets"""
    return f"{p.parent.name}/{p.name}"


class Capper:
    """Owns the snapshot and the cap/release/restore state machine.
    All mutation happens under one lock so the watchdog timer thread, the main
    loop, and the exit paths can all call release()/restore idempotently."""

    def __init__(self, files, originals, cap_uw, tau, max_cap_s, cooldown, dry_run, events_path):
        self.files = files            # [Path] every constraint_*_power_limit_uw we may touch
        self.originals = originals    # {Path: raw file content} -- restored bit-exact
        self.cap_uw = cap_uw
        self.tau = tau
        self.max_cap_s = max_cap_s
        self.cooldown = cooldown
        self.dry_run = dry_run
        self.events_path = Path(events_path)
        self.capped = False
        self.release_at = 0.0         # monotonic deadline for the tau release
        self.restore_failed = False   # fail-stop flag, checked by the main loop
        self.cooldown_until = 0.0
        self._cap_t0 = 0.0
        self._trigger_src = "?"
        self._watchdog = None
        self._lock = threading.Lock()
        self.settle_since = None      # --local-power: monotonic time power first read
                                       # settled since the current cap; None = not settled

    def _write(self, path, text):
        if self.dry_run:
            print(f"[{now_iso()}] DRY-RUN would write {text.strip()!r} -> {path}", flush=True)
            return
        with open(path, "w") as fh:
            fh.write(text)

    def _log(self, rec):
        rec = {"ts": now_iso(), "dry_run": self.dry_run, **rec}
        try:  # a broken log file must never block capping or restoring
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.events_path, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
        except OSError as e:
            print(f"[{now_iso()}] WARN cannot log event ({e}): {rec}", flush=True)

    def cap(self, source):
        """Apply the cap now. Returns True if applied, False if suppressed."""
        now = time.monotonic()
        with self._lock:
            if self.capped:
                return False
            if now < self.cooldown_until:
                print(f"[{now_iso()}] trigger ({source}) suppressed: cooldown for "
                      f"another {self.cooldown_until - now:.1f}s", flush=True)
                self._log({"event": "suppressed", "reason": "cooldown", "trigger": source})
                return False
            written = []
            try:
                for p in self.files:
                    if int(self.originals[p]) > self.cap_uw:   # never raise a limit
                        self._write(p, str(self.cap_uw))
                        written.append(zone_key(p))
            except OSError as e:
                print(f"[{now_iso()}] CRITICAL cap write failed ({e}); restoring", flush=True)
                self._restore_locked()
                self._log({"event": "cap_failed", "trigger": source, "error": str(e)})
                return False
            self.capped = True
            self._cap_t0 = now
            self._trigger_src = source
            self.release_at = now + self.tau
            self.settle_since = None
            self._watchdog = threading.Timer(self.max_cap_s, self.release, args=("watchdog",))
            self._watchdog.daemon = True
            self._watchdog.start()
            self._log({"event": "cap", "trigger": source, "cap_uw": self.cap_uw,
                       "original_uw": {zone_key(p): int(self.originals[p]) for p in self.files},
                       "written": written, "tau_s": self.tau, "max_cap_s": self.max_cap_s})
            print(f"[{now_iso()}] CAP ({source}): {', '.join(written)} -> "
                  f"{self.cap_uw} uW for {self.tau}s", flush=True)
            return True

    def _restore_locked(self):
        """Read-compare-write every snapshotted file back to its original raw
        bytes, then verify by readback; a failed pass is retried with a short
        backoff before giving up. Idempotent; call as often as you like.
        restore_failed reflects the LATEST attempt (a later successful pass
        clears it) so exit codes report the box's actual final state."""
        # 3 quick in-call attempts; beyond that the exit paths and a
        # human are the next layers -- no infinite loop next to a capped box
        last_err = "?"
        for delay in (0.0, 0.1, 0.4):
            if delay:
                time.sleep(delay)
            ok = True
            for p in self.files:
                try:
                    if read_raw(p) != self.originals[p]:
                        self._write(p, self.originals[p])
                        if not self.dry_run and read_raw(p) != self.originals[p]:
                            ok = False
                            last_err = f"readback mismatch on {zone_key(p)}"
                except OSError as e:
                    ok = False
                    last_err = str(e)
            if ok:
                self.restore_failed = False
                return True
            print(f"[{now_iso()}] CRITICAL restore attempt failed ({last_err}) "
                  "-- RAPL limits may still be capped; retrying", flush=True)
        self.restore_failed = True
        print(f"[{now_iso()}] CRITICAL restore FAILED after retries -- RAPL limits "
              "may still be capped, manual uncap needed", flush=True)
        return False

    def release(self, reason):
        """Restore originals + start cooldown. No-op if not capped (so the tau
        path, the watchdog, signals, finally, and atexit can all race safely)."""
        with self._lock:
            if not self.capped:
                return True
            held = time.monotonic() - self._cap_t0
            ok = self._restore_locked()
            self.capped = False
            self.cooldown_until = time.monotonic() + self.cooldown
            if self._watchdog is not None:
                self._watchdog.cancel()   # no-op when we ARE the watchdog callback
                self._watchdog = None
            self._log({"event": "release", "reason": reason, "trigger": self._trigger_src,
                       "held_s": round(held, 3), "tau_s": self.tau, "restore_ok": ok})
            print(f"[{now_iso()}] RELEASE ({reason}) after {held:.2f}s, restore_ok={ok}", flush=True)
            return ok

    def exit_restore(self):
        """Last line of defence; wired to atexit, finally, and (via SystemExit)
        the signal handlers. Second pass read-compare-writes unconditionally in
        case a crash mid-cap() left files dirty with self.capped still False."""
        self.release(_EXIT_REASON[0])
        with self._lock:
            was_failed = self.restore_failed
            if self._restore_locked() and was_failed:
                # a previously failed restore came good on the exit-path retry:
                # record it so post-hoc audit can tell "left capped" from "recovered"
                self._log({"event": "restore_recovered", "reason": _EXIT_REASON[0]})


def resolve_zones(pattern):
    """Expand --rapl-root (glob or literal dir) to package zones, dropping
    subzones: any match whose name extends another match's name with ':'
    (intel-rapl:0:0 is core/dram under intel-rapl:0 -- different limits,
    never package-capped)."""
    roots = sorted(Path(p) for p in globmod.glob(pattern))
    names = {r.name for r in roots}
    return [r for r in roots
            if not any(n != r.name and r.name.startswith(n + ":") for n in names)]


def setup_capper(args):
    """Snapshot the tree, resolve cap/floor, refuse unsafe configs."""
    roots = resolve_zones(args.rapl_root)
    if not roots:
        die(f"nothing matches --rapl-root {args.rapl_root!r}")
    files = sorted(p for root in roots for p in root.glob("constraint_*_power_limit_uw"))
    if not files:
        die(f"no constraint_*_power_limit_uw under {[str(r) for r in roots]} -- wrong --rapl-root?")
    originals = {}
    for p in files:   # refuse cleanly NOW; a bad file must never surface mid-cap
        try:
            raw = read_raw(p)
            int(raw)
        except OSError as e:
            die(f"cannot snapshot {p}: {e}")
        except ValueError:
            die(f"garbage in {p}: {raw!r} -- refusing to arm")
        originals[p] = raw
        print(f"[{now_iso()}] snapshot {zone_key(p)} = {int(raw)} uW", flush=True)

    c0s = [root / "constraint_0_power_limit_uw" for root in roots]
    if args.floor_watts is not None:
        floor_uw = int(args.floor_watts * 1e6)
    elif all(c0 in originals for c0 in c0s):
        # constraint_0 = PL1 sustained = the floor; with several sockets the
        # single cap must sit at or above EVERY zone's PL1 (max), so the worst
        # stuck-cap failure still never throttles any socket's steady state
        floor_uw = max(int(originals[c0]) for c0 in c0s)
    else:
        missing = [str(c0) for c0 in c0s if c0 not in originals]
        die(f"no constraint_0 to derive the PL1 floor from ({missing}); pass --floor-watts")
    if floor_uw <= 0:   # PL1 disabled/zero => derived "baseline" is nonsense
        die(f"floor {floor_uw} uW is not a sane PL1 baseline -- pass --floor-watts explicitly")
    cap_uw = int(args.cap_watts * 1e6) if args.cap_watts is not None else floor_uw

    if cap_uw < floor_uw:
        if not getattr(args, "allow_below_floor", False):
            die(f"REFUSED: cap {cap_uw} uW < PL1 floor {floor_uw} uW -- capping smooths "
                "the transient, it must never throttle steady state "
                "(pass --allow-below-floor to deep-cap the plateau itself)")
        print(f"[{now_iso()}] WARNING deep cap: {cap_uw} uW < PL1 floor {floor_uw} uW -- "
              "PL1 will be lowered too; steady state IS throttled while capped "
              "(--allow-below-floor)", flush=True)
    if cap_uw >= max(int(v) for v in originals.values()):
        die(f"cap {cap_uw} uW is not below any current limit -- nothing to clamp")
    if args.max_cap_s <= args.tau:
        die(f"--max-cap-s ({args.max_cap_s}) must exceed --tau ({args.tau}), "
            "else the watchdog preempts the planned release")
    if not args.dry_run:
        denied = [str(p) for p in files if not os.access(p, os.W_OK)]
        if denied:
            die("not writable (need root): " + ", ".join(denied))

    print(f"[{now_iso()}] armed: cap={cap_uw} uW floor={floor_uw} uW tau={args.tau}s "
          f"max_cap={args.max_cap_s}s cooldown={args.cooldown}s dry_run={args.dry_run}", flush=True)
    capper = Capper(files, originals, cap_uw, args.tau, args.max_cap_s, args.cooldown,
                    args.dry_run, args.events)
    capper.floor_uw = floor_uw   # main() needs it to pick the release-floor semantics
    return capper


# --- triggers: each factory returns (fire(now)->bool, finished()->bool) -------

def make_sim_trigger(spec):
    try:
        offsets = sorted(float(x) for x in spec.split(",") if x.strip())
    except ValueError:
        die(f'--simulate wants comma-separated seconds like "2,15,30", got {spec!r}')
    t0 = time.monotonic()

    def fire(now):
        if offsets and now - t0 >= offsets[0]:
            offsets.pop(0)
            return True
        return False

    return fire, (lambda: not offsets)


def make_watch_trigger(path, max_age_s=MAX_FLAG_AGE_S):
    # whole-file read each poll (it's a one-line flag file) and
    # edge-triggered 0->1 -- write "0" then "1" (or truncate+"1") to re-fire.
    # mtime freshness: the detector rewrites the flag every successful cycle
    # (core.telemetry.write_risk_flag), so a stale mtime means the detector is
    # down/wedged -- treat as no-signal (fail-open, never cap on a dead flag).
    state = [None]

    def fire(_now):
        try:
            if time.time() - os.stat(path).st_mtime > max_age_s:
                state[0] = None   # a fresh "1" after an outage counts as a new edge
                return False
            lines = [ln.strip() for ln in read_raw(path).splitlines() if ln.strip()]
        except OSError:
            state[0] = None
            return False
        cur = lines[-1] if lines else None
        fired = (cur == "1" and state[0] != "1")
        state[0] = cur
        return fired

    return fire, (lambda: False)


class PowerMeter:
    """Samples RAPL energy_uj counters directly -- real measured power, not the
    ML scorer's prediction -- so the local-power trigger/release never inherits
    the scorer's ~5-7 s publish cadence. One instance per zone set; `sample()`
    is stateful (needs a previous reading) so call it on a steady cadence."""

    def __init__(self, zones):
        self.energy_files = [z / "energy_uj" for z in zones]
        self.ranges = [int(read_raw(z / "max_energy_range_uj")) for z in zones]
        self._prev_e = [int(read_raw(p)) for p in self.energy_files]
        self._prev_t = time.monotonic()
        self._prev_power = None

    def sample(self):
        """Returns (power_w, deriv_w_s) vs the previous sample, or None if no
        time has passed (guards a busy-loop double-call)."""
        now = time.monotonic()
        dt = now - self._prev_t
        if dt <= 0:
            return None
        total_uj = 0
        for i, p in enumerate(self.energy_files):
            e = int(read_raw(p))
            delta = e - self._prev_e[i]
            if delta < 0:   # counter wrapped
                delta += self.ranges[i]
            total_uj += delta
            self._prev_e[i] = e
        power_w = (total_uj / 1e6) / dt
        deriv_w_s = 0.0 if self._prev_power is None else (power_w - self._prev_power) / dt
        self._prev_power = power_w
        self._prev_t = now
        return power_w, deriv_w_s


def make_power_trigger(meter, threshold_w_s=POWER_DERIV_THRESHOLD_W_S):
    """Fast local reactive trigger: fires the instant real measured power's
    derivative crosses threshold_w_s. Bypasses the ML scorer entirely -- it
    can't fire early on a stale/coarse flag because it isn't reading one."""
    def fire(_now):
        r = meter.sample()
        return r is not None and r[1] > threshold_w_s

    return fire, (lambda: False)


# --- slew governor (--slew): continuous ramp shaping ---------------------------

def rate_limit(prev, target, up_step, down_step):
    """Move prev toward target, bounded by up_step / down_step per call."""
    if target > prev:
        return min(prev + up_step, target)
    return max(prev - down_step, target)


def ballast_need(p_ref, p_now, w_per_core, max_cores):
    """Core-equivalents of SCHED_IDLE ballast needed to hold measured power at
    the decaying reference during a down-ramp. Float: the fractional part
    becomes a partial duty cycle on one extra worker."""
    if w_per_core <= 0:
        return 0.0
    deficit = p_ref - p_now
    if deficit <= 0:
        return 0.0
    return min(float(max_cores), deficit / w_per_core)


def _ballast_spin(core, duty, stop, cpu):
    """ramp.c's worker_thread in Python: pinned to one core, SCHED_IDLE (only
    runs on cycles nothing else wants -- ~zero cost to real work), 10 ms
    busy/sleep duty cycle read from a shared value. Exports its accumulated
    process_time via `cpu`: a starved worker accrues ~none, so the pool can
    measure ACHIEVED burn (calibration-free) -- package watts alone can't
    tell "idle + burning ballast" from "plateau + starved ballast"."""
    try:
        os.sched_setaffinity(0, {core})
    except OSError:
        pass
    try:
        os.sched_setscheduler(0, os.SCHED_IDLE, os.sched_param(0))
    except (AttributeError, OSError):
        pass   # without SCHED_IDLE it still works, just less polite
    cycle = 0.01
    while not stop.is_set():
        cpu.value = time.process_time()
        d = duty.value
        if d <= 0:
            time.sleep(0.02)  # parked: 50 no-op wakeups/s (invisible on the
                              # trace); 0.1 s here delayed the first dip-fill
            continue
        t0 = time.monotonic()
        busy_end = t0 + cycle * min(d, 100.0) / 100.0
        while time.monotonic() < busy_end:
            pass
        rem = t0 + cycle - time.monotonic()
        if rem > 0:
            time.sleep(rem)


class BallastPool:
    """Lazily-spawned pool of SCHED_IDLE spinner processes (subprocesses, not
    threads -- a Python thread can't burn more than one core past the GIL).
    Default cores are the HIGHEST-numbered ones, away from the monitoring pen
    on cores 0-1/64-65."""

    def __init__(self, max_cores=BALLAST_MAX_CORES, cores=None):
        import multiprocessing as mp
        self._mp = mp
        ncpu = os.cpu_count() or 1
        self.cores = list(cores) if cores else list(range(max(0, ncpu - max_cores), ncpu))
        self.workers = []           # [(Process, duty Value, cpu Value)]
        self.stop_evt = mp.Event()

    def prespawn(self):
        """Spawn the whole pool parked (duty 0: ~10 wakeups/s each, invisible
        on the power trace) so the first real fill has no spawn lag."""
        self.set(len(self.cores))
        for _p, duty, _c in self.workers:
            duty.value = 0.0

    def set(self, core_equiv):
        import math
        want = min(math.ceil(max(0.0, core_equiv)), len(self.cores))
        while len(self.workers) < want:
            duty = self._mp.Value("d", 0.0)
            cpu = self._mp.Value("d", 0.0, lock=False)   # single writer
            p = self._mp.Process(target=_ballast_spin,
                                 args=(self.cores[len(self.workers)], duty,
                                       self.stop_evt, cpu),
                                 daemon=True)
            p.start()
            self.workers.append((p, duty, cpu))
        whole = int(core_equiv)
        for i, (_p, duty, _c) in enumerate(self.workers):
            if i < whole:
                duty.value = 100.0
            elif i == whole:
                duty.value = (core_equiv - whole) * 100.0
            else:
                duty.value = 0.0

    def achieved_cores(self):
        """Core-equivalents the pool ACTUALLY burned since the last call:
        sum of worker process_time deltas over wall time. Starved SCHED_IDLE
        workers accrue ~nothing, so this reads ~0 the moment real work
        preempts the pool, regardless of what was requested."""
        now = time.monotonic()
        cpus = [c.value for _p, _d, c in self.workers]
        prev = getattr(self, "_ach_prev", None)
        self._ach_prev = (now, cpus)
        if prev is None or now - prev[0] < 0.02:
            return self._ach_last if hasattr(self, "_ach_last") else 0.0
        dt = now - prev[0]
        burned = sum(max(0.0, c - c0) for c, c0 in zip(cpus, prev[1]))
        # light EWMA: per-0.05s process_time sampling is ~10-20% noisy and
        # workload_p consumes this; 0.5 still collapses within ~2 ticks on
        # starvation (the park path needs speed more than polish)
        self._ach_last = 0.5 * getattr(self, "_ach_last", 0.0) + 0.5 * (burned / dt)
        return self._ach_last

    def shutdown(self):
        self.stop_evt.set()
        for p, *_ in self.workers:
            p.join(timeout=1)


class SlewGovernor:
    """Continuous ramp shaper. Every poll: ceiling moves toward
    (recent power peak + headroom) at bounded W/s rates and is written to
    every snapshotted constraint (never above its original -- so the ceiling
    tops out at the stock PL1 sum, keeping the PL2 turbo overshoot shaved);
    optionally a decaying SCHED_IDLE ballast fills abrupt power drops.
    Reuses the Capper snapshot: every existing exit path restores bit-exact.
    A deadman thread force-restores and exits 3 if this loop wedges."""

    def __init__(self, capper, meter, n_zones, slew_up=SLEW_UP_W_S,
                 slew_down=SLEW_DOWN_W_S, headroom_w=HEADROOM_W,
                 peak_hold_s=PEAK_HOLD_S, min_ceiling_w=MIN_CEILING_W,
                 poll_s=GOV_POLL_S, ballast=None,
                 ballast_w_per_core=BALLAST_W_PER_CORE, contact_w=CONTACT_W,
                 engage_deriv=ENGAGE_DERIV_W_S, shape_s=SLEW_SHAPE_S,
                 slew_min=SLEW_MIN_W_S, risk_file=None,
                 flag_max_age=MAX_FLAG_AGE_S, drop_risk_file=None):
        self.capper = capper
        self.meter = meter
        self.n_zones = n_zones
        self.slew_up = slew_up
        self.slew_down = slew_down
        self.headroom_w = headroom_w
        self.peak_hold_s = peak_hold_s
        self.min_ceiling_w = min_ceiling_w
        self.poll_s = poll_s
        self.ballast = ballast
        self.ballast_w_per_core = ballast_w_per_core
        self.contact_w = contact_w
        self.engage_deriv = engage_deriv
        self.shape_s = shape_s
        self.slew_min = slew_min
        self.risk_file = risk_file
        self.drop_risk_file = drop_risk_file
        self.flag_max_age = flag_max_age
        self.engaged = False
        self._stable_since = None
        self._phist = []                 # [(t, power_w)] for the gating deriv
        self._ballast_set = 0.0
        self.preburn = ballast is not None   # risk pre-burn needs a pool
        self._preburn_set = 0.0          # cores burning on detector lead
        self._preburn_t0 = None          # current episode start (None = armed)
        self._preburn_dir = 1            # +1 = pre-burn ahead of a rise, -1 ahead of a drop
        self._risk_prev = False
        self._flag_prev = False          # raw detector flag, for edge-triggering episodes
        self._idle_since = None          # long-idle suppressor for false risk edges
        self._pressed_since = None       # start of current cap-contact stretch
        self.ballast_achieved_fn = None  # replay injects; live uses the pool
        self._workload_w = 0.0           # p minus ACHIEVED ballast watts
        if ballast is not None:
            ballast.prespawn()   # parked workers cost ~nothing; spawning 32
                                 # procs lazily mid-dip lags the first fill
        # top = stock PL2 sum, not PL1: _apply min()s per file, so at the top
        # every constraint returns to its exact stock value and the busy
        # plateau runs at true baseline power. Topping at PL1 dragged PL2
        # 246->205 W/socket and throttled the WHOLE plateau ~12% (2026-07-16
        # live: 360 W vs 410 W baseline, +18.6% ai_load wall time). Onset and
        # resume overshoots still get shaved -- the ceiling is low exactly
        # when they happen.
        # write the ceiling to PL2 (constraint_1, the ms-window "short_term"
        # constraint) ONLY; PL1 stays stock and keeps governing the sustained
        # plateau. 2026-07-16 live findings behind this split: writing both
        # constraints to the ceiling dragged PL2 to ~PL1 at plateau and cost
        # 12-16% HPL (runs 2-4); riding PL2 at the stock PL2/PL1 ratio above
        # a contact-climbing ceiling meant the fast constraint never engaged
        # and ramps went entirely unshaped (run 5). PL2-only gives stock
        # plateau by construction AND fast bounded ramps.
        self.cap_files = [p for p in capper.files
                          if p.name == "constraint_1_power_limit_uw"]
        if not self.cap_files:          # zone with a single constraint
            self.cap_files = list(capper.files)
        by_zone = {}
        for p in self.cap_files:
            by_zone.setdefault(p.parent, []).append(int(capper.originals[p]))
        self.top_w = sum(max(v) for v in by_zone.values()) / 1e6
        self.ceiling_w = self.top_w      # start uncapped-equivalent; slews down by itself
        self.p_ref = 0.0
        self.peaks = []                  # [(t, power_w)] pruned to peak_hold_s
        self.heartbeat = time.monotonic()
        self._done = False
        self._written = {}
        self._last_t = None
        self._last_p = None
        self.last_deriv = 0.0
        self._engage_t0 = 0.0
        self._ballast_peak = 0.0
        self._ticks = 0
        self._eng_ticks = 0
        # tunables the replay harness sweeps (and future CLI flags can set);
        # defaults are the shipped constants
        self.jump_w = JUMP_ENGAGE_W
        self.disengage_s = DISENGAGE_STABLE_S
        self.engage_on_dips = True   # False: falling power never engages the
                                     # cap (ballast alone shapes dips); risk
                                     # and rising edges still engage
        self.ballast_cores_n = (len(ballast.cores) if ballast is not None
                                else BALLAST_MAX_CORES)   # sizing headroom;
                                     # replay overrides to match live pools

    def _deriv(self, now, p):
        """Power derivative over the last ~DERIV_WIN_S (W/s); 0 until the
        window has enough span/points to be meaningful. Least-squares slope,
        NOT endpoint difference: sampling jitter on a quantized energy counter
        makes endpoint diffs spike >80 W/s on a flat 60 W feed (measured
        2026-07-16: 8/64 samples false-fire; lsq peaks at 15 W/s)."""
        self._phist.append((now, p))
        self._phist = [(t, v) for t, v in self._phist if t >= now - DERIV_WIN_S]
        n = len(self._phist)
        if n < 3 or now - self._phist[0][0] < 0.6 * DERIV_WIN_S:
            return 0.0
        mt = sum(t for t, _ in self._phist) / n
        mp = sum(v for _, v in self._phist) / n
        den = sum((t - mt) ** 2 for t, _ in self._phist)
        return sum((t - mt) * (v - mp) for t, v in self._phist) / den if den else 0.0

    def _dyn_rates(self, target):
        """Dynamic slope: rate = ceiling gap spread over shape_s, floored at
        slew_min, hard-capped at the configured slew_up/slew_down maxima. Big
        workload swings ramp steeper (still bounded); small ones ramp gently."""
        r = max(abs(target - self.ceiling_w) / self.shape_s, self.slew_min)
        return min(r, self.slew_up), min(r, self.slew_down)

    def _risk_high(self):
        """Detector integration: level-read of spike_risk.flag. Fresh '1' =
        pre-engage the hug so a risky swing meets an already-shaped ceiling
        (the RF scorer leads real onsets by ~3 s). Stale/missing = fail-open."""
        if not self.risk_file:
            return False
        try:
            if time.time() - os.stat(self.risk_file).st_mtime > self.flag_max_age:
                return False
            lines = [ln.strip() for ln in read_raw(self.risk_file).splitlines()
                     if ln.strip()]
        except (OSError, ValueError):
            return False
        return bool(lines) and lines[-1] == "1"

    def _drop_risk_high(self):
        """Same level-read as _risk_high, on the FALLING-edge flag written by
        usage_edge --drop-risk-file. Stale/missing = fail-open."""
        if not self.drop_risk_file:
            return False
        try:
            if time.time() - os.stat(self.drop_risk_file).st_mtime > self.flag_max_age:
                return False
            lines = [ln.strip() for ln in read_raw(self.drop_risk_file).splitlines()
                     if ln.strip()]
        except (OSError, ValueError):
            return False
        return bool(lines) and lines[-1] == "1"

    def _drop_armed(self, p):
        """Mirror of _risk_armed. A predicted DROP is only actionable while
        power is still UP -- ballast has to already be burning when the load
        falls away, or there is nothing to fill the hole with. So the level
        gate is inverted relative to the up-swing case."""
        return self._drop_risk_high() and p >= RISK_ENGAGE_FRAC * self.top_w

    def _risk_armed(self, p):
        """Risk arms the hug only where a cap can help: below the busy
        plateau, anticipating an up-swing (see RISK_ENGAGE_FRAC). Honoring
        the regime detector's all-plateau 'risky' kept gov11 hugging the
        whole run for most of its -8.3% Gflops."""
        return self._risk_high() and p < RISK_ENGAGE_FRAC * self.top_w

    def _preburn_allowed(self, now, workload_w, deriv_w_s, d1_w):
        """Whether a fresh risk flag may start ballast pre-burn.

        Flat idle + a detector false positive was the 64 h burn failure mode.
        Preserve behavior for recent detector leads and once the box is loaded;
        suppress only long-flat-idle risk edges.
        """
        loaded = workload_w >= PREBURN_WORKLOAD_FLOOR_FRAC * self.top_w
        rising = deriv_w_s >= PREBURN_RISING_DERIV_W_S or d1_w >= PREBURN_RISING_STEP_W
        if loaded or rising:
            self._idle_since = None
            return True
        self._idle_since = self._idle_since or now
        return now - self._idle_since < PREBURN_IDLE_SUPPRESS_S

    def _achieved(self):
        """Cores the pool actually burned (achieved feedback). Fallback to
        the request when there's no pool and no injected signal -- the old
        (pre-feedback) estimate."""
        if self.ballast is not None:
            return self.ballast.achieved_cores()
        if self.ballast_achieved_fn is not None:
            return self.ballast_achieved_fn()
        return self._ballast_set

    def _ballast_tick(self, p, dt):
        """Feed-forward ballast sizing against WORKLOAD power (total minus the
        ballast's own estimated watts); p_ref decays from workload power too.
        Sizing against total power chases its own contribution (~1 Hz sawtooth,
        2026-07-16 dip traces); decaying p_ref from total self-sustains the
        fill (deficit stays == ballast watts -> pegged at 32 cores through a
        whole busy plateau, -12.7% HPL Gflops, same day)."""
        workload_p = p - self._achieved() * self.ballast_w_per_core
        self.p_ref = max(workload_p, self.p_ref - self.slew_down * dt)
        n = self.ballast_cores_n
        need = ballast_need(self.p_ref, workload_p, self.ballast_w_per_core, n)
        if self._ballast_set <= 0.0 and \
                self.p_ref - workload_p < BALLAST_DEADBAND_W:
            need = 0.0   # deadband: noise can't START a fill (see constant)
        self._ballast_set = need

    def _apply(self, ceiling_w):
        per_zone_uw = int(ceiling_w / self.n_zones * 1e6)
        for p in self.cap_files:
            val = min(per_zone_uw, int(self.capper.originals[p]))   # never raise
            if self._written.get(p) != val:
                self.capper._write(p, str(val))
                self._written[p] = val

    def _deadman(self):
        while not self._done:
            time.sleep(1.0)
            if not self._done and time.monotonic() - self.heartbeat > DEADMAN_S:
                print(f"[{now_iso()}] CRITICAL slew governor wedged >{DEADMAN_S}s "
                      "-- force-restoring and exiting", flush=True)
                self.capper.exit_restore()
                os._exit(3)

    def step(self, now, p):
        """One control tick at timestamp `now` (monotonic-like seconds) with
        measured power `p` (W). All governor logic lives here so the offline
        replay harness (actuators/replay_gov.py) can drive it with simulated
        time; run() supplies real time + the real meter. Returns the risk
        state for the status line."""
        dt = (now - self._last_t) if self._last_t is not None else self.poll_s
        self._last_t = now
        # a late poll must not become one big catch-up JUMP in the
        # ceiling -- that's the abruptness this governor exists to
        # prevent; bound the per-step budget instead of tracking
        # elapsed time exactly
        dt = min(dt, 3 * self.poll_s)

        self.peaks.append((now, p))
        self.peaks = [(t, v) for t, v in self.peaks if t >= now - self.peak_hold_s]
        peak = max(v for _t, v in self.peaks)
        deriv = self.last_deriv = self._deriv(now, p)

        # ENGAGE only while power is actually moving. A permanently
        # hugging cap costs 8-16% HPL no matter which constraint or
        # ratio it rides (2026-07-16 runs 2-7): RAPL undershoots the
        # written limit ~25-45 W, so any cap within headroom of the
        # plateau shaves the compute bursts. Stock caps when quiet,
        # hug+slew only across transitions.
        # workload estimate: total power minus what the pool ACTUALLY burned
        # (achieved feedback, not the request -- starved spinners draw no
        # watts, and subtracting nominal watts reads "plateau + starved pool"
        # as idle: package power alone cannot tell the two apart).
        self._workload_w = p - self._achieved() * self.ballast_w_per_core
        # arm on WORKLOAD power, not total: the pre-burn's own ballast watts
        # would otherwise climb until they disarm the trigger sustaining them
        # (replay: fill self-caps at 0.6*top - idle). The plateau rationale
        # (RISK_ENGAGE_FRAC) is about real work, so workload is the honest
        # signal for it anyway.
        d1 = (p - self._last_p) if self._last_p is not None else 0.0
        jump = abs(d1) >= self.jump_w
        self._last_p = p
        risk_raw = self._risk_armed(self._workload_w)
        preburn_ok = self._preburn_allowed(now, self._workload_w, deriv, d1)
        risk = risk_raw and (not self.preburn or preburn_ok)
        # Drop risk drives ballast ONLY. It deliberately never reaches the
        # ceiling logic below: a cap cannot mitigate a dip, and hugging on a
        # predicted fall would clamp the plateau we still want running.
        drop_risk = self._drop_armed(p)
        if not self.engaged:
            # the pre-burn ballast ramp itself runs at ~slew_up (75 W/s),
            # within lsq noise of ENGAGE_DERIV (80) -- don't self-engage on it
            deriv_ok = self._preburn_set <= 0.0
            trigger = jump or (deriv_ok and abs(deriv) >= self.engage_deriv)
            if not self.engage_on_dips:
                trigger = (jump and d1 > 0) or (deriv_ok and deriv >= self.engage_deriv)
            # with a ballast pool, risk pre-burns instead of pre-hugging: the
            # cap would clamp the ballast ramp it is trying to make room for
            if trigger or (risk and not self.preburn):
                self.engaged = True
                self._stable_since = None
                self._engage_t0 = now
                self._ballast_peak = 0.0
                # cap starts AT current power + headroom (a cap drop,
                # not a power step -- power is already below it)
                self.ceiling_w = min(max(p + self.headroom_w,
                                         self.min_ceiling_w), self.top_w)
                self.capper._log({"event": "engage",
                                  "reason": ("jump" if jump else
                                             "deriv" if abs(deriv) >= self.engage_deriv
                                             else "risk"),
                                  "power_w": round(p, 1),
                                  "deriv_w_s": round(deriv, 1),
                                  "ceiling_w": round(self.ceiling_w, 1)})
            else:
                self.ceiling_w = self.top_w   # stock (never-raise min()s
                                              # each file to its original)
            self._apply(self.ceiling_w)
        else:
            # contact rule: throttled power reads just under the written
            # ceiling (RAPL undershoot), so peak+headroom alone wedges at
            # ceiling = throttled_power + headroom -- pressed against the
            # cap means CLIMB toward stock, not hug (see CONTACT_W)
            pressed = peak >= self.ceiling_w - self.contact_w
            self._pressed_since = ((self._pressed_since or now) if pressed
                                   else None)
            if pressed:
                target = self.top_w
            else:
                # hug floor sits ABOVE the pressed threshold by more
                # than the meter noise, or the ceiling saws across it
                # (pressed toggles every few ticks, resetting the quiet
                # timer -> never disengages; live 2026-07-16 gov10
                # idled ENGAGED forever at ~242 W, and a +5 margin
                # re-sawed at the 0.05 s poll's noise). While ballast is
                # actively filling a dip, quiet is unreachable anyway
                # (ballast>0 blocks it), so hug tight -- every extra watt
                # of hug is a watt of unshaped step at the resume.
                hug = (self.headroom_w if self._ballast_set > 0.0
                       else max(self.headroom_w, self.contact_w + 15.0))
                target = min(max(peak + hug, self.min_ceiling_w),
                             self.top_w)
            up_r, down_r = self._dyn_rates(target)
            if pressed and now - self._pressed_since >= PRESSED_ESCALATE_S:
                up_r = self.slew_up   # sustained throttle: full design slope
                                      # (see PRESSED_ESCALATE_S)
            if target < self.ceiling_w:
                # ceiling chase-down: bounded by CEIL_DIVE_MAX, not slew_down
                # (see constant -- no power moves when the cap drops toward
                # power+hug, and a slow chase leaves resumes unshaped)
                gap = self.ceiling_w - target
                down_r = min(max(gap / max(self.shape_s / 3.0, 0.1), down_r),
                             CEIL_DIVE_MAX_W_S)
            self.ceiling_w = rate_limit(self.ceiling_w, target,
                                        up_r * dt, down_r * dt)
            self._apply(self.ceiling_w)
            quiet = (not pressed and abs(deriv) < self.engage_deriv
                     and self._ballast_set <= 0.0 and not risk)
            self._stable_since = (self._stable_since or now) if quiet else None
            if quiet and now - self._stable_since >= self.disengage_s:
                self.engaged = False
                self.ceiling_w = self.top_w
                self._apply(self.ceiling_w)
                self.capper._log({"event": "disengage",
                                  "held_s": round(now - self._engage_t0, 2),
                                  "ballast_peak_c": round(self._ballast_peak, 1),
                                  "power_w": round(p, 1)})

        if self.preburn:
            any_risk = risk or drop_risk
            # Edge-trigger on the raw DETECTOR FLAG, not on the level-gated
            # composite. Power hovering near RISK_ENGAGE_FRAC*top_w makes the
            # gate flicker, and taking the edge off it manufactured a "fresh"
            # episode every few ticks -- pilot 2026-07-27: 61 episodes for 17
            # real detector edges, each restarting the PREBURN_MAX_S window so
            # ballast burned continuously instead of in bounded episodes. The
            # gate still decides whether to burn; it no longer invents edges.
            raw_flag = self._risk_high() or self._drop_risk_high()
            if (raw_flag and not self._flag_prev and any_risk
                    and self._preburn_t0 is None):
                self._preburn_t0 = now       # fresh flag edge starts an episode
                self._preburn_dir = 1 if risk else -1
                self.capper._log({"event": "preburn", "power_w": round(p, 1),
                                  "dir": self._preburn_dir})
            self._flag_prev = raw_flag
            self._risk_prev = any_risk
            burn = (any_risk and self._preburn_t0 is not None
                    and now - self._preburn_t0 <= PREBURN_MAX_S)
            tgt = float(self.ballast_cores_n) if burn else 0.0
            # The instant-park branch is an UP-episode rule: it exists because
            # real work preempting the spinners makes parking free. On a DOWN
            # episode the plateau is exactly when the fill must stay burning,
            # so parking there would defeat the whole point.
            if self._preburn_dir > 0 and self._workload_w >= RISK_ENGAGE_FRAC * self.top_w:
                # the onset arrived: real work preempted the spinners, so
                # parking makes no power edge -- but leaving them RUNNABLE
                # steals scheduler/cache time from the work that swapped in
                # (gov14 live 2026-07-17: slew-decayed pre-burn kept the pool
                # runnable 75% of the leg, 81.6% Gflops). Park instantly;
                # the slew-limited decay below is only for false alarms,
                # where the power edge is real.
                self._preburn_set = 0.0
            else:
                per_core = self.slew_up / self.ballast_w_per_core * dt
                self._preburn_set = rate_limit(self._preburn_set, tgt, per_core,
                                               self.slew_down / self.ballast_w_per_core * dt)
            if not burn and self._preburn_set <= 0.0:
                self._preburn_t0 = None      # drained; next flag edge re-arms

        self._ballast_tick(p, dt)
        # during an onset the starved pre-burn still counts in
        # _ballast_tick's workload estimate for the ~3 s it takes to decay --
        # a dip landing inside that window gets a briefly undersized fill
        self._ballast_set = max(self._ballast_set, self._preburn_set)
        if self.ballast is not None:
            self.ballast.set(self._ballast_set)
        self._ballast_peak = max(self._ballast_peak, self._ballast_set)
        self._ticks += 1
        self._eng_ticks += self.engaged
        return risk

    def run(self, until_s=None):
        t_end = time.monotonic() + until_s if until_s is not None else None
        threading.Thread(target=self._deadman, daemon=True).start()
        last_status = 0.0
        try:
            while t_end is None or time.monotonic() < t_end:
                time.sleep(self.poll_s)
                now = time.monotonic()
                self.heartbeat = now
                r = self.meter.sample()
                if r is None:
                    continue
                p = r[0]
                risk = self.step(now, p)
                if now - last_status >= 5.0:
                    duty = 100.0 * self._eng_ticks / max(1, self._ticks)
                    print(f"[{now_iso()}] gov: power={p:.1f}W ceiling={self.ceiling_w:.1f}W "
                          f"ballast={self._ballast_set:.1f}c deriv={self.last_deriv:+.0f}W/s "
                          f"duty={duty:.0f}% "
                          f"{'ENGAGED' if self.engaged else 'stock'}"
                          f"{' RISK' if risk else ''}", flush=True)
                    last_status = now
        finally:
            self._done = True


def make_influx_trigger(args):
    """Fire on the newest power_spike_prediction spike_risk=true point that is
    newer than anything already handled (and than capper start, so stale flags
    never trigger). Measurement/field per spike_daemon_rf.py's write_flag;
    secrets layout copied from common.py -- org name never hardcoded."""
    # lazy import so --selfcheck/--simulate/--watch-file need no network stack
    from influxdb_client import InfluxDBClient

    secrets = Path(args.secrets_dir).expanduser()
    org = (secrets / "influx_org.txt").read_text().strip()
    token = (secrets / "influx_read_token.txt").read_text().strip()
    client = InfluxDBClient(url=args.influx_url, token=token, org=org, timeout=10_000)
    qapi = client.query_api()
    flux = (f'from(bucket: "{INFLUX_BUCKET}") |> range(start: -30s) '
            '|> filter(fn: (r) => r._measurement == "power_spike_prediction" '
            'and r._field == "spike_risk") |> last()')
    last_handled = [datetime.now(timezone.utc)]

    def fire(_now):
        try:
            tables = qapi.query(flux)
        except Exception as e:  # fail-safe direction is "no cap" -- warn, keep polling
            print(f"[{now_iso()}] WARN influx poll failed: {e}", flush=True)
            return False
        newest = None
        for tab in tables:
            for rec in tab.records:
                t = rec.get_time()
                if newest is None or t > newest[0]:
                    newest = (t, rec.get_value())
        if newest and bool(newest[1]) and newest[0] > last_handled[0]:
            last_handled[0] = newest[0]
            return True
        return False

    return fire, (lambda: False)


def resolve_release_floor(cap_uw, floor_uw, n_zones):
    """Total-power (all zones summed, W) release floor for --local-power.
    Normal cap (>= PL1): the workload settles back near PL1, so the floor is
    just the cap summed over zones. Deep cap (< PL1, --allow-below-floor):
    clamped busy power sits AT the cap, so "near the cap" means STILL BUSY --
    the floor drops DEEP_RELEASE_HYSTERESIS_W below the cap so release only
    fires once power heads toward idle."""
    total_w = cap_uw * n_zones / 1e6
    return total_w - DEEP_RELEASE_HYSTERESIS_W if cap_uw < floor_uw else total_w


def resolve_cooldown(cli_cooldown, local_power):
    """--cooldown default depends on trigger: an explicit --cooldown always
    wins; otherwise --local-power gets the short default (release is already
    power-gated, so a long flap-guard is redundant and blocks the legitimate
    re-cap of a workload's second ramp stage), other triggers keep the legacy
    5s ML-flag flap-guard."""
    if cli_cooldown is not None:
        return cli_cooldown
    return LOCAL_POWER_COOLDOWN_S if local_power else COOLDOWN_S


def run_loop(capper, fire, finished, source, poll_s, once,
             meter=None, release_floor_w=None, release_margin_w=RELEASE_MARGIN_W,
             release_debounce_s=RELEASE_DEBOUNCE_S):
    """Idle: poll the trigger every poll_s. Capped, no meter: sleep exactly
    until the tau release (legacy feed-forward behaviour, unchanged). Capped,
    with a meter (--local-power): tau becomes a MINIMUM hold -- after it
    elapses, keep polling real power and release only once it's stayed at or
    below release_floor_w + release_margin_w for a continuous
    release_debounce_s (a real ai_load.sh busy plateau is a rapid train of
    sub-second bursts, not one clean overshoot -- releasing on the first
    settled sample let every next burst's peak straight through uncapped).
    If power never comes back down, the independent watchdog (max_cap_s)
    still force-releases regardless."""
    while True:
        if capper.capped:
            if meter is None:
                time.sleep(max(0.0, capper.release_at - time.monotonic()))
                capper.release("tau")
            elif time.monotonic() < capper.release_at:
                time.sleep(min(poll_s, capper.release_at - time.monotonic()))
            else:
                # tau (minimum hold) has elapsed: check at a real poll_s
                # cadence, never faster -- a near-zero dt re-check would hit
                # RAPL's energy_uj read granularity and spuriously read ~0 W,
                # falsely satisfying "settled" within milliseconds of tau.
                r = meter.sample()
                now = time.monotonic()
                if r is not None and r[0] <= release_floor_w + release_margin_w:
                    if capper.settle_since is None:
                        capper.settle_since = now
                    elif now - capper.settle_since >= release_debounce_s:
                        capper.release("power-settled")
                else:
                    capper.settle_since = None   # still elevated -- reset the debounce clock
                if capper.capped:
                    time.sleep(poll_s)
            if once and not capper.capped:
                return
            continue
        if capper.restore_failed:
            die("restore failed -- exiting so a human uncaps the box", 3)
        if finished():
            return
        if fire(time.monotonic()) and capper.cap(source):
            continue   # skip the sleep; go straight to the capped branch
        time.sleep(poll_s)


def _install_signals():
    def handler(signum, _frame):
        _EXIT_REASON[0] = f"signal:{signal.Signals(signum).name}"
        raise SystemExit(128 + signum)   # unwinds through finally + atexit

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


# --- selfcheck ----------------------------------------------------------------

def build_fake_tree(base, pl1="205000000", pl2="250000000"):
    """Fake powercap package zone, named like the real interface. Defaults:
    PL1=205 W sustained / PL2=250 W with real-ish time windows."""
    root = Path(base)
    root.mkdir(parents=True)
    for name, val in [
        ("name", "package-0"),
        ("constraint_0_name", "long_term"),
        ("constraint_0_power_limit_uw", pl1),
        ("constraint_0_time_window_us", "27983872"),
        ("constraint_1_name", "short_term"),
        ("constraint_1_power_limit_uw", pl2),
        ("constraint_1_time_window_us", "2440"),
        ("energy_uj", "123456789"),
        ("max_energy_range_uj", "262143328850"),
        ("enabled", "1"),
    ]:
        (root / name).write_text(val + "\n")
    return root


def _ns(tree, events, **kw):
    base = dict(rapl_root=str(tree), cap_watts=None, floor_watts=None, tau=0.5,
                max_cap_s=3.0, cooldown=0.2, dry_run=False, events=str(events))
    base.update(kw)
    return argparse.Namespace(**base)


def selfcheck():
    results = []

    def check(name, cond, detail=""):
        results.append(bool(cond))
        line = f"{'PASS' if cond else 'FAIL'}: {name}"
        if detail and not cond:
            line += f"  [{detail}]"
        print(line, flush=True)

    with tempfile.TemporaryDirectory(prefix="rapl_capper_selfcheck_") as tmp:
        tmp = Path(tmp)
        orig_c0, orig_c1 = "205000000\n", "250000000\n"

        # 1. cap on flag / exact value / ~tau release / bit-exact restore
        tree = build_fake_tree(tmp / "t1")
        ev = tmp / "ev1.jsonl"
        capper = setup_capper(_ns(tree, ev))
        c0, c1 = tree / "constraint_0_power_limit_uw", tree / "constraint_1_power_limit_uw"
        samples, stop = [], threading.Event()

        def sample():
            while not stop.is_set():
                samples.append((read_raw(c0), read_raw(c1)))
                time.sleep(0.02)

        th = threading.Thread(target=sample, daemon=True)
        th.start()
        fire, finished = make_sim_trigger("0.1")
        run_loop(capper, fire, finished, "simulate", poll_s=0.05, once=False)
        stop.set()
        th.join(1)
        check("cap applied on flag; written value == requested cap (205000000 uW)",
              any(s[1] == "205000000" for s in samples))
        check("constraint_0 (PL1) never touched -- never-raise/never-lower-to-floor",
              all(s[0] == orig_c0 for s in samples))
        evs = [json.loads(ln) for ln in read_raw(ev).splitlines()]
        rels = [r for r in evs if r["event"] == "release"]
        check("released at ~tau (held 0.5s +/- 0.3s)",
              len(rels) == 1 and abs(rels[0]["held_s"] - 0.5) <= 0.3, str(rels))
        check("release reason 'tau', restore_ok true, JSONL has cap+release with required fields",
              rels and rels[0]["reason"] == "tau" and rels[0]["restore_ok"]
              and any(r["event"] == "cap" and r["cap_uw"] == 205000000
                      and r["original_uw"][f"{tree.name}/constraint_1_power_limit_uw"] == 250000000
                      and r["tau_s"] == 0.5 for r in evs))
        check("originals restored bit-exact after release",
              read_raw(c0) == orig_c0 and read_raw(c1) == orig_c1)

        # 2. floor violation refused
        tree2 = build_fake_tree(tmp / "t2")
        try:
            setup_capper(_ns(tree2, tmp / "ev2.jsonl", cap_watts=180.0))
            refused = False
        except SystemExit as e:
            refused = (e.code == 2)
        check("floor violation refused at startup (cap 180 W < PL1 floor 205 W)", refused)

        # 3. watchdog force-restores when the release path is blocked
        tree3 = build_fake_tree(tmp / "t3")
        ev3 = tmp / "ev3.jsonl"
        cap3 = setup_capper(_ns(tree3, ev3, tau=0.2, max_cap_s=0.6, cooldown=0.0))
        cap3.cap("selfcheck-blocked")   # deliberately never run the loop: tau release blocked
        c13 = tree3 / "constraint_1_power_limit_uw"
        check("cap in force while release path is blocked", read_raw(c13) == "205000000")
        time.sleep(1.2)                 # > max_cap_s
        evs3 = [json.loads(ln) for ln in read_raw(ev3).splitlines()]
        wd = [r for r in evs3 if r["event"] == "release" and r["reason"] == "watchdog"]
        check("watchdog force-restored at max_cap_s despite blocked release",
              len(wd) == 1 and not cap3.capped and read_raw(c13) == orig_c1, str(evs3))

        # 4. cooldown suppresses an immediate second trigger, allows a later one
        tree4 = build_fake_tree(tmp / "t4")
        ev4 = tmp / "ev4.jsonl"
        cap4 = setup_capper(_ns(tree4, ev4, tau=0.2, cooldown=1.0))
        fire4, fin4 = make_sim_trigger("0.05,0.3,1.9")
        run_loop(cap4, fire4, fin4, "simulate", poll_s=0.05, once=False)
        evs4 = [json.loads(ln) for ln in read_raw(ev4).splitlines()]
        n_cap = sum(r["event"] == "cap" for r in evs4)
        n_sup = sum(r["event"] == "suppressed" for r in evs4)
        check("cooldown suppressed the immediate second trigger", n_sup >= 1, str(evs4))
        check("later trigger capped again after cooldown expired (2 caps total)",
              n_cap == 2, str(evs4))
        check("tree restored after cooldown scenario",
              read_raw(tree4 / "constraint_1_power_limit_uw") == orig_c1)

        # 5. SIGTERM mid-cap restores originals (real process: handler+finally+atexit)
        tree5 = build_fake_tree(tmp / "t5")
        ev5 = tmp / "ev5.jsonl"
        c15 = tree5 / "constraint_1_power_limit_uw"
        cmd = [sys.executable, str(Path(__file__).resolve()), "--rapl-root", str(tree5),
               "--simulate", "0.1", "--tau", "30", "--max-cap-s", "60", "--cooldown", "0",
               "--poll-s", "0.05", "--events", str(ev5)]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and read_raw(c15) != "205000000":
            time.sleep(0.02)
        mid_cap = read_raw(c15) == "205000000"
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=10)
        check("subprocess reached capped state before SIGTERM", mid_cap, out[-300:])
        check("SIGTERM mid-cap restored originals bit-exact",
              read_raw(c15) == orig_c1 and read_raw(tree5 / "constraint_0_power_limit_uw") == orig_c0,
              out[-300:])
        check("SIGTERM exit code 143 (handler ran, not default kill)", proc.returncode == 143)
        # missing ev5 = the subprocess never capped (slow spawn under load) --
        # report the FAIL with its output instead of crashing the whole selfcheck
        evs5 = ([json.loads(ln) for ln in read_raw(ev5).splitlines()]
                if ev5.exists() else [])
        sig = [r for r in evs5 if r["event"] == "release" and r["reason"] == "signal:SIGTERM"]
        check("release logged with reason signal:SIGTERM, held_s>0, restore_ok",
              len(sig) == 1 and sig[0]["restore_ok"] and sig[0]["held_s"] > 0,
              str(evs5) + " | " + out[-200:])

        # 6. watch-file trigger is edge-triggered
        wf = tmp / "flag.txt"
        wf.write_text("0\n")
        fire6, _ = make_watch_trigger(str(wf))
        r1 = fire6(0)
        wf.write_text("1\n")
        r2 = fire6(0)
        r3 = fire6(0)
        wf.write_text("0\n")
        fire6(0)
        wf.write_text("1\n")
        r4 = fire6(0)
        check("watch-file edge trigger: fires on 0->1 only, no re-fire while high",
              (not r1) and r2 and (not r3) and r4)

        # 6b. stale flag (dead detector) is no-signal; a fresh "1" after the
        #     outage counts as a new edge
        wf2 = tmp / "flag_stale.txt"
        wf2.write_text("1\n")
        old = time.time() - 60
        os.utime(wf2, (old, old))               # detector "down" for 60 s
        fire6b, _ = make_watch_trigger(str(wf2), max_age_s=15.0)
        s1 = fire6b(0)
        wf2.write_text("1\n")                   # detector back, flag fresh + high
        s2 = fire6b(0)
        s3 = fire6b(0)
        check("stale-mtime flag ignored (fail-open); fresh '1' after outage fires once",
              (not s1) and s2 and (not s3))

        # 11. two-socket coverage: both package zones capped, subzone + PL1s
        #     untouched, per-zone floor = max(PL1s), restore bit-exact
        multi = tmp / "multi"
        z0 = build_fake_tree(multi / "intel-rapl:0")                     # PL1 205 / PL2 250
        z1 = build_fake_tree(multi / "intel-rapl:1", pl1="200000000",
                             pl2="246000000")                            # PL1 200 / PL2 246
        sub = build_fake_tree(multi / "intel-rapl:0:0", pl1="0",
                              pl2="999000000")                           # core/dram subzone
        cap11 = setup_capper(_ns(str(multi / "intel-rapl:*"), tmp / "ev11.jsonl",
                                 tau=0.2, max_cap_s=1.0, cooldown=0.0))
        check("2-zone floor is max of both PL1s (205 W, not zone1's 200 W)",
              cap11.cap_uw == 205000000)
        check("subzone intel-rapl:0:0 excluded from the snapshot",
              all("intel-rapl:0:0" not in str(p) for p in cap11.files))
        cap11.cap("selfcheck-2zone")
        c1z0, c1z1 = z0 / "constraint_1_power_limit_uw", z1 / "constraint_1_power_limit_uw"
        both_capped = read_raw(c1z0) == "205000000" and read_raw(c1z1) == "205000000"
        pl1s_kept = (read_raw(z0 / "constraint_0_power_limit_uw") == orig_c0
                     and read_raw(z1 / "constraint_0_power_limit_uw") == "200000000\n")
        sub_kept = read_raw(sub / "constraint_1_power_limit_uw") == "999000000\n"
        cap11.release("tau")
        check("both sockets' PL2 capped to 205 W (intel-rapl:1 no longer uncapped)",
              both_capped)
        check("PL1s never touched (incl. zone1's 200 W below the 205 W cap); subzone untouched",
              pl1s_kept and sub_kept)
        check("2-zone restore bit-exact",
              read_raw(c1z0) == orig_c1 and read_raw(c1z1) == "246000000\n"
              and read_raw(sub / "constraint_1_power_limit_uw") == "999000000\n")

        # 7. setup refuses garbage / unreadable constraint files and a zero floor
        def refused(tree, **kw):
            try:
                setup_capper(_ns(tree, tmp / "ev_refused.jsonl", **kw))
                return False
            except SystemExit as e:   # die() => clean refusal, not a traceback
                return e.code == 2

        tree7a = build_fake_tree(tmp / "t7a")
        (tree7a / "constraint_1_power_limit_uw").write_text("banana\n")
        check("garbage constraint file refused cleanly at setup", refused(tree7a))
        tree7b = build_fake_tree(tmp / "t7b")
        (tree7b / "constraint_2_power_limit_uw").mkdir()   # unreadable-as-file, even for root
        check("unreadable constraint file refused cleanly at setup", refused(tree7b))
        tree7c = build_fake_tree(tmp / "t7c")
        (tree7c / "constraint_0_power_limit_uw").write_text("0\n")
        check("constraint_0=0 (disabled PL1) refused -- demands explicit --floor-watts",
              refused(tree7c))

        # 8. restore write fails mid-cap: screams + restore_failed; exit-path retry
        #    recovers once writable again (permission-lost-mid-run drill)
        tree8 = build_fake_tree(tmp / "t8")
        ev8 = tmp / "ev8.jsonl"
        cap8 = setup_capper(_ns(tree8, ev8, tau=0.2, max_cap_s=5.0, cooldown=0.0))
        cap8.cap("selfcheck-restore-fail")
        c18 = tree8 / "constraint_1_power_limit_uw"
        c18.rename(tree8 / "hidden")   # root-proof failure injection (chmod won't stop root)
        c18.mkdir()                    # a directory: unread/unwritable as a file
        rel_ok = cap8.release("tau")
        failed_flagged = (not rel_ok) and cap8.restore_failed
        c18.rmdir()
        (tree8 / "hidden").rename(c18)   # "permission" returns; file still holds the cap
        cap8.exit_restore()
        evs8 = [json.loads(ln) for ln in read_raw(ev8).splitlines()]
        rel8 = [r for r in evs8 if r["event"] == "release"]
        check("restore failure mid-cap: release reports restore_ok=false and sets restore_failed",
              failed_flagged and len(rel8) == 1 and rel8[0]["restore_ok"] is False, str(evs8))
        check("exit-path retry recovers: file bit-exact, flag cleared, restore_recovered logged",
              read_raw(c18) == orig_c1 and not cap8.restore_failed
              and any(r["event"] == "restore_recovered" for r in evs8), str(evs8))

        # 9. --once whose restore is left failing must exit 3, never a silent 0
        tree9 = build_fake_tree(tmp / "t9")
        c19 = tree9 / "constraint_1_power_limit_uw"
        cmd9 = [sys.executable, str(Path(__file__).resolve()), "--rapl-root", str(tree9),
                "--simulate", "0.1", "--tau", "1.5", "--max-cap-s", "10", "--cooldown", "0",
                "--poll-s", "0.05", "--once", "--events", str(tmp / "ev9.jsonl")]
        proc9 = subprocess.Popen(cmd9, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and read_raw(c19) != "205000000":
            time.sleep(0.02)
        got_capped = read_raw(c19) == "205000000"
        c19.rename(tree9 / "hidden9")
        c19.mkdir()                    # tau release + all exit retries now fail
        out9, _ = proc9.communicate(timeout=30)
        check("--once with restore left failing screams CRITICAL and exits 3 (not 0)",
              got_capped and proc9.returncode == 3 and "CRITICAL" in out9,
              f"rc={proc9.returncode} {out9[-300:]}")
        c19.rmdir()
        (tree9 / "hidden9").rename(c19)

        # 11b. --local-power: fires on a real derivative crossing, tau is a
        # minimum (release withheld while power stays high), releases once
        # power settles back near the floor -- exercises the whole fix.
        tree12 = build_fake_tree(tmp / "t12", pl1="205000000", pl2="250000000")
        ev12 = tmp / "ev12.jsonl"
        cap12 = setup_capper(_ns(tree12, ev12, tau=0.2, max_cap_s=3.0, cooldown=0.0))
        meter12 = PowerMeter([tree12])
        e12 = tree12 / "energy_uj"

        def bump_energy(rate_w, dt, steps):
            # fake tree's energy_uj advances by rate_w*dt J per tick,
            # same units real hardware uses -- lets the meter "see" a chosen
            # instantaneous power without touching real sysfs.
            for _ in range(steps):
                cur = int(read_raw(e12))
                (e12).write_text(str(cur + int(rate_w * dt * 1e6)) + "\n")
                time.sleep(dt)

        # warm up: the first sample() anchors prev_power off the fake tree's
        # static default energy_uj (not a real idle rate), so the first real
        # delta would itself look like a spurious jump -- burn two idle ticks
        # before checking anything.
        meter12.sample()
        idle_thread = threading.Thread(target=bump_energy, args=(200.0, 0.05, 4))
        idle_thread.start(); idle_thread.join()
        meter12.sample()   # settles prev_power at the real ~200 W idle rate
        fire12, _ = make_power_trigger(meter12, threshold_w_s=97.0)
        idle_thread2 = threading.Thread(target=bump_energy, args=(200.0, 0.05, 2))
        idle_thread2.start(); idle_thread2.join()
        fired_idle = fire12(0)
        # onset: energy jumps as if power surged to ~400 W this tick
        (e12).write_text(str(int(read_raw(e12)) + int(400 * 0.05 * 1e6)) + "\n")
        fired_onset = fire12(0)
        check("local-power trigger: silent at idle rate, fires on a real >97 W/s jump",
              (not fired_idle) and fired_onset)

        # drive it through the REAL run_loop (not a manual bypass) -- this is
        # what caught the actual bug: once release_at was in the past, a
        # failed check degenerated the sleep to max(0.0, negative)=0, busy-
        # spinning meter.sample() at near-zero dt until RAPL's energy_uj read
        # granularity made power_w read spuriously ~0, releasing within ms of
        # tau regardless of real load. Fixed by a fixed poll_s cadence once
        # past tau; this guards the regression.
        run_stop = threading.Event()

        def feed(rate_w, duration_s, tick=0.05):
            end = time.monotonic() + duration_s
            while time.monotonic() < end:
                cur = int(read_raw(e12))
                e12.write_text(str(cur + int(rate_w * tick * 1e6)) + "\n")
                time.sleep(tick)

        cap12.cap("selfcheck-local-power")
        loop_th = threading.Thread(
            target=run_loop,
            args=(cap12, lambda now: False, lambda: run_stop.is_set(), "test", 0.05, False),
            kwargs=dict(meter=meter12, release_floor_w=cap12.cap_uw / 1e6,
                        release_margin_w=RELEASE_MARGIN_W, release_debounce_s=0.3),
            daemon=True)
        loop_th.start()
        feed(400.0, 0.6)   # busy well past tau=0.2s
        held_past_tau = cap12.capped
        # energy now flat (feed stopped) -- power reads ~0, should release soon
        deadline = time.monotonic() + 2.0
        while cap12.capped and time.monotonic() < deadline:
            time.sleep(0.05)
        released = not cap12.capped
        run_stop.set(); loop_th.join(timeout=1)
        check("feedback release: held past tau while power stayed high "
              "(busy-spin regression guard), released once power settled",
              held_past_tau and released)
        check("cooldown default: local-power short (legit re-cap on 2nd ramp "
              "stage), others keep the legacy 5s flap-guard, explicit wins",
              resolve_cooldown(None, False) == COOLDOWN_S
              and resolve_cooldown(None, True) == LOCAL_POWER_COOLDOWN_S
              and resolve_cooldown(2.5, True) == 2.5)

        # 11c. debounce bridges a brief inter-burst gap: a real busy plateau is
        # a rapid train of sub-second bursts, not one clean overshoot -- a gap
        # SHORTER than release_debounce_s must NOT release (the exact bug that
        # let every next burst's peak through uncapped on 2026-07-15); a gap
        # LONGER than the debounce (the real STOP/CONT dip) must still release.
        tree13 = build_fake_tree(tmp / "t13", pl1="205000000", pl2="250000000")
        ev13 = tmp / "ev13.jsonl"
        cap13 = setup_capper(_ns(tree13, ev13, tau=0.1, max_cap_s=5.0, cooldown=0.0))
        meter13 = PowerMeter([tree13])
        e13 = tree13 / "energy_uj"
        run_stop13 = threading.Event()

        def feed13(rate_w, duration_s, tick=0.05):
            end = time.monotonic() + duration_s
            while time.monotonic() < end:
                cur = int(read_raw(e13))
                e13.write_text(str(cur + int(rate_w * tick * 1e6)) + "\n")
                time.sleep(tick)

        cap13.cap("selfcheck-debounce")
        loop_th13 = threading.Thread(
            target=run_loop,
            args=(cap13, lambda now: False, lambda: run_stop13.is_set(), "test", 0.05, False),
            kwargs=dict(meter=meter13, release_floor_w=cap13.cap_uw / 1e6,
                        release_margin_w=RELEASE_MARGIN_W, release_debounce_s=0.5),
            daemon=True)
        loop_th13.start()
        feed13(400.0, 0.3)          # first burst, past tau=0.1s
        # brief inter-burst gap SHORTER than the 0.5s debounce -- no feed
        time.sleep(0.2)
        survived_short_gap = cap13.capped
        feed13(400.0, 0.3)          # next burst resumes -- must reset the debounce clock
        still_capped_after_second_burst = cap13.capped
        # now a gap LONGER than debounce (the real STOP/CONT dip) -- must release
        deadline13 = time.monotonic() + 2.0
        while cap13.capped and time.monotonic() < deadline13:
            time.sleep(0.05)
        released13 = not cap13.capped
        run_stop13.set(); loop_th13.join(timeout=1)
        check("debounce bridges a short inter-burst gap (stayed capped through "
              "it and the next burst), still releases after a genuinely long gap",
              survived_short_gap and still_capped_after_second_burst and released13,
              f"survived={survived_short_gap} still_capped={still_capped_after_second_burst} released={released13}")

        # 12. --allow-below-floor deep cap: refused without the flag (covered by
        # test 2), accepted with it, writes BOTH constraints (PL1 lowered too --
        # that's the whole point: the PL1 floor sits ABOVE the busy plateau, so
        # a floor-respecting cap can never clamp the plateau), restores both
        # bit-exact; release floor drops the hysteresis below the cap.
        tree14 = build_fake_tree(tmp / "t14")
        cap14 = setup_capper(_ns(tree14, tmp / "ev14.jsonl", cap_watts=130.0,
                                 allow_below_floor=True, tau=0.2, max_cap_s=1.0,
                                 cooldown=0.0))
        cap14.cap("selfcheck-deep")
        c014 = tree14 / "constraint_0_power_limit_uw"
        c114 = tree14 / "constraint_1_power_limit_uw"
        both_deep = read_raw(c014) == "130000000" and read_raw(c114) == "130000000"
        cap14.release("tau")
        check("deep cap lowers BOTH PL1 and PL2 to the cap (130 W < 205 W floor)",
              both_deep)
        check("deep cap restores both constraints bit-exact",
              read_raw(c014) == orig_c0 and read_raw(c114) == orig_c1)
        check("release floor: normal cap = cap*zones; deep cap = hysteresis below "
              "(clamped busy sits AT the cap, so 'near cap' means still busy)",
              resolve_release_floor(205000000, 205000000, 2) == 410.0
              and resolve_release_floor(130000000, 205000000, 2)
              == 260.0 - DEEP_RELEASE_HYSTERESIS_W)

        # 13. --slew governor: ceiling slews DOWN to idle+headroom at a bounded
        # rate, slews UP at a bounded rate on an onset, never exceeds any
        # original limit, restores bit-exact; helper math; ballast pool smoke.
        check("rate_limit: bounded rise, bounded fall, clamps at target",
              rate_limit(100.0, 200.0, 10.0, 10.0) == 110.0
              and rate_limit(100.0, 50.0, 10.0, 10.0) == 90.0
              and rate_limit(100.0, 103.0, 10.0, 10.0) == 103.0)
        # ballast must not self-sustain: steady workload + its own fill watts
        # in the measurement must decay the fill to zero within a few ticks
        gov_bt = SlewGovernor(cap14, PowerMeter([tree14]), 1, slew_down=75.0,
                              ballast=None, ballast_w_per_core=3.26)
        gov_bt._ballast_set, gov_bt.p_ref = 32.0, 404.3
        for _ in range(60):
            gov_bt._ballast_tick(300.0 + gov_bt._ballast_set * 3.26, 0.1)
        check("ballast sizing: steady workload decays the fill to zero "
              "(no self-sustaining feed-forward loop)", gov_bt._ballast_set == 0.0,
              f"ballast_set={gov_bt._ballast_set}")
        # ...and a real drop still gets filled: workload steps 300 -> 100
        gov_bt2 = SlewGovernor(cap14, PowerMeter([tree14]), 1, slew_down=75.0,
                               ballast=None, ballast_w_per_core=3.26)
        gov_bt2._ballast_set, gov_bt2.p_ref = 0.0, 300.0
        gov_bt2._ballast_tick(100.0, 0.1)
        check("ballast sizing: a power drop yields a positive decaying fill",
              gov_bt2._ballast_set > 30.0, f"ballast_set={gov_bt2._ballast_set}")
        # noise deadband: a sub-30 W phantom deficit must not START a fill
        # (meter noise twitched the pool all plateau + blocked disengage),
        # but an ONGOING fill keeps tracking through small deficits
        gov_bt3 = SlewGovernor(cap14, PowerMeter([tree14]), 1, slew_down=75.0,
                               ballast=None, ballast_w_per_core=3.26)
        gov_bt3._ballast_set, gov_bt3.p_ref = 0.0, 220.0
        gov_bt3._ballast_tick(200.0, 0.1)     # deficit 20 < deadband
        held_off = gov_bt3._ballast_set == 0.0
        gov_bt3._ballast_set, gov_bt3.p_ref = 10.0, 220.0
        gov_bt3._ballast_tick(200.0 + 10.0 * 3.26, 0.1)   # ongoing fill, deficit 20
        kept_tracking = gov_bt3._ballast_set > 0.0
        check("ballast deadband: noise can't start a fill; ongoing fill still "
              "tracks below the deadband", held_off and kept_tracking,
              f"held_off={held_off} kept={kept_tracking}")

        check("ballast_need: deficit/W-per-core, floor 0, capped at max cores",
              ballast_need(210.0, 200.0, 1.3, 32) == 10.0 / 1.3
              and ballast_need(190.0, 200.0, 1.3, 32) == 0.0
              and ballast_need(400.0, 200.0, 1.3, 32) == 32.0
              and ballast_need(210.0, 200.0, 0.0, 32) == 0.0)

        tree15 = build_fake_tree(tmp / "t15")   # PL1 205 / PL2 250
        cap15 = setup_capper(_ns(tree15, tmp / "ev15.jsonl", tau=0.2,
                                 max_cap_s=5.0, cooldown=0.0))
        meter15 = PowerMeter([tree15])
        e15 = tree15 / "energy_uj"
        c115 = tree15 / "constraint_1_power_limit_uw"   # the governor writes
                                                        # PL2 only; PL1 stays stock
        feed_stop15 = threading.Event()
        feed_rate15 = [60.0]   # W; single fake zone, so "idle" sits below PL1

        def feed15():
            # atomic replace: a plain write_text truncates first, and the
            # governor's PowerMeter racing that window reads '' and dies
            # (regular file, not sysfs -- sysfs writes are one syscall)
            while not feed_stop15.is_set():
                cur = int(read_raw(e15))
                nxt = e15.with_suffix(".next")
                nxt.write_text(str(cur + int(feed_rate15[0] * 0.01 * 1e6)) + "\n")
                os.replace(nxt, e15)
                time.sleep(0.01)

        gov15 = SlewGovernor(cap15, meter15, 1, slew_up=1000.0, slew_down=2000.0,
                             headroom_w=20.0, peak_hold_s=0.1, min_ceiling_w=50.0,
                             poll_s=0.02, ballast=None, contact_w=15.0,
                             shape_s=0.05)   # tiny shape => dyn rate always at the
                                             # configured maxima (old fixed-rate dynamics)
        # contact_w < headroom here: this case is OPEN loop (feed ignores the
        # cap), so keep the hug behavior tight to test rate bounds + settling;
        # the shipped contact default is exercised closed-loop in #14
        traj = []   # (t, written c1 value) sampled independently
        traj_stop = threading.Event()

        def sample15():
            while not traj_stop.is_set():
                try:
                    traj.append((time.monotonic(), int(read_raw(c115))))
                except ValueError:
                    pass   # raced _write's truncate on the fake tree (regular
                           # file, not sysfs -- sysfs writes are one syscall)
                time.sleep(0.005)

        threading.Thread(target=feed15, daemon=True).start()
        threading.Thread(target=sample15, daemon=True).start()
        gov_th = threading.Thread(target=gov15.run, kwargs=dict(until_s=1.2), daemon=True)
        gov_th.start()
        time.sleep(0.6)                # idle phase: ceiling should settle at 60+20=80 W
        settled = int(read_raw(c115))
        feed_rate15[0] = 180.0         # onset: target becomes 200 W
        gov_th.join(timeout=3)
        traj_stop.set(); feed_stop15.set()
        final = int(read_raw(c115))
        check("slew governor: caps stay fully STOCK while power is quiet "
              "(engage-gated; a permanent hug costs 8-16% HPL)",
              settled == int(orig_c1), f"settled={settled}")
        check("slew governor: onset engages and the cap hugs power (>120 W, "
              "below stock)", 120_000_000 < final < int(orig_c1),
              f"final={final}")
        # rate check over >=0.1s windows: the ceiling is a discrete staircase,
        # so instantaneous rate across one write is meaningless -- the promise
        # is bounded AVERAGE ramp over any real interval
        rates = []
        for i, (t1, v1) in enumerate(traj):
            for t2, v2 in traj[i + 1:]:
                if t2 - t1 >= 0.1:
                    rates.append((v2 - v1) / 1e6 / (t2 - t1))
                    break
        check("slew governor: average ceiling ramp over any 0.1s window within "
              "the slew bounds (rise <= ~1000 W/s, fall <= ~2000 W/s)",
              rates and all(-2000 * 1.3 <= r <= 1000 * 1.3 for r in rates),
              f"rates={sorted(rates)[:3]}...{sorted(rates)[-3:]}")
        check("slew governor: never wrote above any original limit",
              all(v <= 250_000_000 for _t, v in traj))
        cap15.exit_restore()
        check("slew governor: originals restored bit-exact after exit",
              read_raw(tree15 / "constraint_0_power_limit_uw") == orig_c0
              and read_raw(c115) == orig_c1)

        pool = BallastPool(cores=[0])
        pool.set(0.5)
        time.sleep(0.15)
        alive = pool.workers and pool.workers[0][0].is_alive()
        pool.set(0.0)
        pool.shutdown()
        gone = not pool.workers[0][0].is_alive()
        check("ballast pool: spawns a duty-cycled worker, parks at 0, joins on shutdown",
              bool(alive) and gone)

        # 14. slew governor CLOSED loop: unlike #13's open-loop feed, measured
        # power here depends on the written cap the way real RAPL does --
        # demand below the cap runs free, demand above it reads ~25 W UNDER
        # the cap (undershoot). With target = peak + headroom only, the
        # ceiling wedges at (throttled power + headroom) and never satisfies
        # demand (live 2026-07-16: whole ai_load run stuck ~180 W low); the
        # contact rule must let it escape.
        tree16 = build_fake_tree(tmp / "t16")   # PL1 205 / PL2 250
        cap16 = setup_capper(_ns(tree16, tmp / "ev16.jsonl", tau=0.2,
                                 max_cap_s=5.0, cooldown=0.0))
        meter16 = PowerMeter([tree16])
        e16 = tree16 / "energy_uj"
        c116 = tree16 / "constraint_1_power_limit_uw"
        stop16 = threading.Event()
        demand16 = [60.0]

        def feed16():
            while not stop16.is_set():
                try:
                    cap_w = int(read_raw(c116)) / 1e6
                except ValueError:   # raced _write's truncate (regular file,
                    continue         # not sysfs) -- same guard as sample15
                d = demand16[0]
                p = d if d <= cap_w else max(40.0, cap_w - 25.0)
                cur = int(read_raw(e16))
                nxt = e16.with_suffix(".next")
                nxt.write_text(str(cur + int(p * 0.01 * 1e6)) + "\n")
                os.replace(nxt, e16)
                time.sleep(0.01)

        gov16 = SlewGovernor(cap16, meter16, 1, slew_up=1000.0, slew_down=2000.0,
                             headroom_w=20.0, peak_hold_s=0.1, min_ceiling_w=50.0,
                             poll_s=0.02, ballast=None, shape_s=0.05)
        threading.Thread(target=feed16, daemon=True).start()
        gov_th16 = threading.Thread(target=gov16.run, kwargs=dict(until_s=1.5),
                                    daemon=True)
        gov_th16.start()
        time.sleep(0.5)                # settle near idle
        demand16[0] = 200.0            # onset: demand far above the ceiling
        gov_th16.join(timeout=4)
        stop16.set()
        final16 = int(read_raw(c116))
        check("slew governor closed-loop: ceiling escapes the peak+headroom "
              "equilibrium under RAPL undershoot and satisfies demand (>=195 W)",
              final16 >= 195_000_000, f"final={final16}")
        cap16.exit_restore()
        check("slew governor closed-loop: originals restored bit-exact",
              read_raw(tree16 / "constraint_0_power_limit_uw") == orig_c0
              and read_raw(c116) == orig_c1)

        # 17. dynamic slope: rate follows the ceiling gap, floored/capped
        gov16.ceiling_w, gov16.shape_s = 100.0, 3.0
        gov16.slew_up, gov16.slew_down, gov16.slew_min = 75.0, 75.0, 15.0
        check("dynamic slope: big gap capped at slew maxima, small gap floored "
              "at slew_min, mid gap = gap/shape_s",
              gov16._dyn_rates(400.0) == (75.0, 75.0)      # gap 300 -> 100 -> cap
              and gov16._dyn_rates(130.0) == (15.0, 15.0)  # gap 30 -> 10 -> floor
              and gov16._dyn_rates(190.0) == (30.0, 30.0)) # gap 90 -> 90/3

        # 18. LIVE-CADENCE engage: gov9 (2026-07-16) never engaged on mycroft
        # because it inherited the 0.25 s trigger poll and the 0.5 s deriv
        # window never held 3 samples. Prove the gate works at GOV_POLL_S with
        # the DEFAULT deriv constants.
        tree17 = build_fake_tree(tmp / "t17")
        cap17 = setup_capper(_ns(tree17, tmp / "ev17.jsonl", tau=0.2,
                                 max_cap_s=5.0, cooldown=0.0))
        c117 = tree17 / "constraint_1_power_limit_uw"
        e17 = tree17 / "energy_uj"
        stop17 = threading.Event()
        rate17 = [60.0]

        def feed17():
            while not stop17.is_set():
                try:
                    cur = int(read_raw(e17))
                except ValueError:
                    continue
                nxt = e17.with_suffix(".next")
                nxt.write_text(str(cur + int(rate17[0] * 0.01 * 1e6)) + "\n")
                os.replace(nxt, e17)
                time.sleep(0.01)

        gov17 = SlewGovernor(cap17, PowerMeter([tree17]), 1,
                             poll_s=GOV_POLL_S, ballast=None)   # all live defaults
        threading.Thread(target=feed17, daemon=True).start()
        th17 = threading.Thread(target=gov17.run, kwargs=dict(until_s=2.2), daemon=True)
        th17.start()
        time.sleep(1.0)
        rate17[0] = 150.0              # onset (+90 W -> lsq deriv ~+160 W/s);
                                       # stays below the tree's 250 W stock PL2
                                       # so the hug is distinguishable from stock
        th17.join(timeout=4)
        stop17.set()
        final17 = int(read_raw(c117))
        check("slew governor engages at the LIVE poll cadence + default deriv "
              "constants (gov9 regression: 0.25 s poll starved the window)",
              gov17.engaged and final17 < int(orig_c1), f"final={final17}")
        ev17_recs = [json.loads(ln) for ln in
                     read_raw(tmp / "ev17.jsonl").splitlines()] \
            if (tmp / "ev17.jsonl").exists() else []
        eng17 = [r for r in ev17_recs if r.get("event") == "engage"]
        check("engage logged to the events JSONL with a reason (observability: "
              "slew_events files used to stay empty)",
              eng17 and eng17[0].get("reason") in ("jump", "deriv", "risk"),
              str(ev17_recs[:3]))
        cap17.exit_restore()

        # 19. detector integration: a fresh risk flag pre-engages the hug on a
        # QUIET box (the RF scorer leads onsets ~3 s -- the swing arrives
        # against an already-shaped ceiling); clearing it releases to stock.
        tree18 = build_fake_tree(tmp / "t18")
        cap18 = setup_capper(_ns(tree18, tmp / "ev18.jsonl", tau=0.2,
                                 max_cap_s=5.0, cooldown=0.0))
        c118 = tree18 / "constraint_1_power_limit_uw"
        e18 = tree18 / "energy_uj"
        stop18 = threading.Event()
        rate18 = [60.0]

        def feed18():
            while not stop18.is_set():
                try:
                    cur = int(read_raw(e18))
                except ValueError:
                    continue
                nxt = e18.with_suffix(".next")
                nxt.write_text(str(cur + int(rate18[0] * 0.01 * 1e6)) + "\n")
                os.replace(nxt, e18)
                time.sleep(0.01)

        risk18 = tmp / "risk18.flag"
        risk18.write_text("1\n")
        gov18 = SlewGovernor(cap18, PowerMeter([tree18]), 1, poll_s=GOV_POLL_S,
                             ballast=None, risk_file=str(risk18),
                             min_ceiling_w=50.0)   # low floor => the hug rides
                             # peak+contact, exercising the pressed-saw disengage
                             # regression (gov10 idled ENGAGED forever at ~242 W)
        threading.Thread(target=feed18, daemon=True).start()
        th18 = threading.Thread(target=gov18.run, kwargs=dict(until_s=3.6), daemon=True)
        th18.start()
        time.sleep(0.8)
        mid18 = int(read_raw(c118))
        risk18.write_text("0\n")       # risk clears -> quiet -> stock after 2 s
        th18.join(timeout=6)
        stop18.set()
        final18 = int(read_raw(c118))
        check("risk flag pre-engages the hug on a quiet box (detector "
              "mitigation), cap hugging well below stock",
              mid18 < int(orig_c1) - 50_000_000, f"mid={mid18}")
        check("risk clear -> quiet disengage back to fully stock caps",
              final18 == int(orig_c1) and not gov18.engaged, f"final={final18}")
        ev18_recs = [json.loads(ln) for ln in
                     read_raw(tmp / "ev18.jsonl").splitlines()] \
            if (tmp / "ev18.jsonl").exists() else []
        check("risk engage + disengage pair logged with held_s",
              any(r.get("event") == "engage" and r.get("reason") == "risk"
                  for r in ev18_recs)
              and any(r.get("event") == "disengage" and r.get("held_s", 0) > 0
                      for r in ev18_recs), str(ev18_recs[:4]))
        risk18.write_text("1\n")
        check("risk arms the hug only below the plateau (RISK_ENGAGE_FRAC): "
              "a busy-regime flag must not hold the hug at full power",
              gov18._risk_armed(60.0) and not gov18._risk_armed(200.0))

        # Drop risk is the mirror: armed only NEAR the plateau, because ballast
        # has to already be burning when the load falls away. Inverted gate,
        # and it must never reach the ceiling path.
        drop18 = tmp / "drop18.flag"
        drop18.write_text("1\n")
        gov18.drop_risk_file = str(drop18)
        check("drop risk arms only AT the plateau (mirror of RISK_ENGAGE_FRAC)",
              gov18._drop_armed(200.0) and not gov18._drop_armed(60.0))
        drop18.write_text("0\n")
        check("drop flag low disarms", not gov18._drop_armed(200.0))
        stale18 = tmp / "stale18.flag"
        stale18.write_text("1\n")
        os.utime(stale18, (time.time() - 3600, time.time() - 3600))
        gov18.drop_risk_file = str(stale18)
        check("stale drop flag fails open (>flag_max_age = no signal)",
              not gov18._drop_armed(200.0))
        gov18.drop_risk_file = None
        check("no drop-risk file = feature off, never armed",
              not gov18._drop_armed(200.0))
        cap18.exit_restore()

        # 10. dry-run writes nothing
        tree7 = build_fake_tree(tmp / "t7")
        cap7 = setup_capper(_ns(tree7, tmp / "ev7.jsonl", tau=0.2, max_cap_s=1.0,
                                cooldown=0.0, dry_run=True))
        cap7.cap("selfcheck-dry")
        untouched = read_raw(tree7 / "constraint_1_power_limit_uw") == orig_c1
        cap7.release("tau")
        check("dry-run caps/releases without writing anything",
              untouched and read_raw(tree7 / "constraint_1_power_limit_uw") == orig_c1
              and not cap7.capped)

    ok = all(results)
    print(("SELFCHECK OK: " if ok else "SELFCHECK FAILED: ")
          + f"{sum(results)}/{len(results)} assertions passed", flush=True)
    return 0 if ok else 1


# --- main ----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rapl-root", default=RAPL_ROOT,
                    help="powercap package zone (point at a fake tree for testing)")
    ap.add_argument("--cap-watts", type=float, default=None,
                    help="cap level in W during the turbo window (default: the PL1 floor)")
    ap.add_argument("--floor-watts", type=float, default=None,
                    help="PL1 safety floor in W (default: current constraint_0 value)")
    ap.add_argument("--allow-below-floor", action="store_true",
                    help="permit --cap-watts below the PL1 floor: lowers PL1+PL2 to the "
                         "cap, clamping the busy plateau itself (throttles steady state "
                         "while capped -- supervised smoothing runs only)")
    ap.add_argument("--tau", type=float, default=TAU_S,
                    help=f"seconds to hold the cap (default {TAU_S}, the measured PL2 window)")
    ap.add_argument("--max-cap-s", type=float, default=MAX_CAP_S,
                    help="independent watchdog force-release (must exceed --tau)")
    ap.add_argument("--cooldown", type=float, default=None,
                    help="seconds after release during which triggers are ignored "
                         f"(default {COOLDOWN_S}, or {LOCAL_POWER_COOLDOWN_S} with --local-power)")
    ap.add_argument("--poll-s", type=float, default=POLL_S, help="idle trigger-poll cadence")
    ap.add_argument("--simulate", metavar="OFFSETS",
                    help='scripted trigger offsets in seconds, e.g. "2,15,30"; exits when consumed')
    ap.add_argument("--watch-file", metavar="PATH",
                    help=f"flag file to poll (default: the detector's {DEFAULT_FLAG})")
    ap.add_argument("--flag-max-age", type=float, default=MAX_FLAG_AGE_S,
                    help="watch-file mtime older than this = detector down = no-signal")
    ap.add_argument("--influx", action="store_true",
                    help="legacy trigger: poll InfluxDB instead of the local flag file "
                         "(re-adds the detector's ~5.2 s publish cadence)")
    ap.add_argument("--influx-url", default="http://localhost:8086",
                    help="InfluxDB URL for the --influx poll (the capper runs ON mycroft)")
    ap.add_argument("--secrets-dir", default="~/.secrets",
                    help="dir holding influx_org.txt / influx_read_token.txt "
                         "(root deployment: /home/dlee/.secrets)")
    ap.add_argument("--local-power", action="store_true",
                    help="reactive trigger off real RAPL energy counters + feedback "
                         "release (see module docstring); no ML scorer dependency")
    ap.add_argument("--power-deriv-threshold", type=float, default=POWER_DERIV_THRESHOLD_W_S,
                    help=f"--local-power fires above this W/s (default {POWER_DERIV_THRESHOLD_W_S})")
    ap.add_argument("--release-margin-w", type=float, default=RELEASE_MARGIN_W,
                    help=f"--local-power releases once power <= floor + this (default {RELEASE_MARGIN_W} W)")
    ap.add_argument("--release-debounce", type=float, default=RELEASE_DEBOUNCE_S,
                    help=f"--local-power: power must stay settled this long before releasing "
                         f"(default {RELEASE_DEBOUNCE_S}s; bridges gaps between a busy "
                         "plateau's individual sub-second bursts)")
    ap.add_argument("--slew", action="store_true",
                    help="continuous slew governor: ceiling hugs measured power, ramps "
                         "bounded to --slew-up/--slew-down W/s (see module docstring); "
                         "implies --allow-below-floor; not a trigger mode")
    ap.add_argument("--slew-up", type=float, default=SLEW_UP_W_S,
                    help=f"ceiling rise rate, W/s (default {SLEW_UP_W_S})")
    ap.add_argument("--slew-down", type=float, default=SLEW_DOWN_W_S,
                    help=f"ceiling fall + ballast decay rate, W/s (default {SLEW_DOWN_W_S})")
    ap.add_argument("--headroom-w", type=float, default=HEADROOM_W,
                    help=f"ceiling margin above the recent power peak (default {HEADROOM_W} W)")
    ap.add_argument("--slew-shape", type=float, default=SLEW_SHAPE_S,
                    help="dynamic slope: spread the ceiling gap over ~this long; "
                         f"slope scales with the workload swing (default {SLEW_SHAPE_S}s)")
    ap.add_argument("--slew-min", type=float, default=SLEW_MIN_W_S,
                    help=f"gentlest dynamic ramp, W/s (default {SLEW_MIN_W_S})")
    ap.add_argument("--no-engage-on-dips", action="store_true",
                    help="falling power never engages the cap: ballast alone "
                         "shapes dips, so resumes meet a STOCK ceiling "
                         "(replay 2026-07-17: same shaping, engage duty "
                         "25->18%%, no resume throttle)")
    ap.add_argument("--risk-file", default=str(DEFAULT_FLAG),
                    help="--slew: pre-engage the hug while this detector flag reads "
                         "fresh '1' (risky swing incoming); 'none' disables "
                         f"(default {DEFAULT_FLAG})")
    ap.add_argument("--drop-risk-file", default=None,
                    help="--slew: pre-burn ballast while this FALLING-edge flag "
                         "reads fresh '1' (usage_edge --drop-risk-file), so the "
                         "fill is already running when the load falls away. "
                         "Unset = react to drops only after they land.")
    ap.add_argument("--ballast", action="store_true",
                    help="--slew: fill abrupt power DROPS with decaying SCHED_IDLE "
                         "spinner processes (a cap cannot stop power falling)")
    ap.add_argument("--ballast-w-per-core", type=float, default=BALLAST_W_PER_CORE,
                    help=f"W one busy ballast core burns (default {BALLAST_W_PER_CORE}; calibrate live)")
    ap.add_argument("--ballast-max-cores", type=int, default=BALLAST_MAX_CORES,
                    help=f"ballast pool size cap (default {BALLAST_MAX_CORES})")
    ap.add_argument("--events", default=str(DEFAULT_EVENTS), help="JSONL event log path")
    ap.add_argument("--dry-run", action="store_true",
                    help="log what WOULD be written; write nothing")
    ap.add_argument("--once", action="store_true", help="one cap/release cycle, then exit")
    ap.add_argument("--selfcheck", action="store_true",
                    help="offline proof against a fake powercap tree (no root, no network)")
    args = ap.parse_args()
    args.cooldown = resolve_cooldown(args.cooldown, args.local_power)

    if args.selfcheck:
        sys.exit(selfcheck())
    if sum(map(bool, (args.simulate, args.watch_file, args.influx,
                      args.local_power, args.slew))) > 1:
        die("pick one mode: --simulate, --watch-file, --influx, --local-power, or --slew")
    if args.slew:
        args.allow_below_floor = True   # the ceiling lives near idle by design

    capper = setup_capper(args)
    if args.slew:
        zones = sorted({p.parent for p in capper.files}, key=str)
        pool = BallastPool(args.ballast_max_cores) if args.ballast else None
        # the governor MUST poll at GOV_POLL_S, not the trigger-mode --poll-s
        # (0.25 s): at 0.25 s the 0.5 s deriv window never holds 3 samples, the
        # gating deriv is stuck at 0, and the governor never engages (live
        # 2026-07-16 gov9: whole ai_load run at stock, ramps 489 W/s unshaped)
        risk_file = None if args.risk_file == "none" else args.risk_file
        gov = SlewGovernor(capper, PowerMeter(zones), len(zones),
                           slew_up=args.slew_up, slew_down=args.slew_down,
                           headroom_w=args.headroom_w,
                           poll_s=min(args.poll_s, GOV_POLL_S),
                           ballast=pool, ballast_w_per_core=args.ballast_w_per_core,
                           shape_s=args.slew_shape, slew_min=args.slew_min,
                           risk_file=risk_file, flag_max_age=args.flag_max_age,
                           drop_risk_file=args.drop_risk_file)
        gov.engage_on_dips = not args.no_engage_on_dips
        print(f"[{now_iso()}] slew governor: top={gov.top_w:.0f}W "
              f"max up/down={args.slew_up}/{args.slew_down}W/s "
              f"dyn slope=gap/{args.slew_shape}s floor {args.slew_min}W/s "
              f"headroom={args.headroom_w}W ballast={'on' if pool else 'off'} "
              f"risk_file={risk_file or 'off'} "
              f"drop_risk_file={args.drop_risk_file or 'off'}", flush=True)
        _install_signals()
        atexit.register(capper.exit_restore)
        try:
            gov.run()
        finally:
            # restore FIRST: it is the safety invariant, and pool.shutdown()
            # can take ~1 s/worker -- never queue the restore behind it
            capper.exit_restore()
            if pool is not None:
                pool.shutdown()
        if capper.restore_failed:
            die("restore still failing at exit -- RAPL limits may still be capped; "
                "verify constraint_*_power_limit_uw by hand", 3)
        return
    meter, release_floor_w = None, None
    if args.simulate:
        source, (fire, finished) = "simulate", make_sim_trigger(args.simulate)
    elif args.influx:
        source, (fire, finished) = "influx", make_influx_trigger(args)
    elif args.local_power:
        zones = sorted({p.parent for p in capper.files}, key=str)
        meter = PowerMeter(zones)
        source, (fire, finished) = "local-power", make_power_trigger(meter, args.power_deriv_threshold)
        # meter reads TOTAL power across all zones; cap_uw is applied PER zone
        # (each zone independently capped to the same value), so the release
        # floor must be summed across zones too, not left at the single-zone value.
        release_floor_w = resolve_release_floor(capper.cap_uw, capper.floor_uw, len(zones))
    else:
        flag = args.watch_file or str(DEFAULT_FLAG)
        source, (fire, finished) = "watch-file", make_watch_trigger(flag, args.flag_max_age)

    _install_signals()
    atexit.register(capper.exit_restore)
    try:
        run_loop(capper, fire, finished, source, args.poll_s, args.once,
                 meter=meter, release_floor_w=release_floor_w, release_margin_w=args.release_margin_w,
                 release_debounce_s=args.release_debounce)
    finally:
        capper.exit_restore()
    if capper.restore_failed:   # e.g. --once whose release failed: never exit 0 capped
        die("restore still failing at exit -- RAPL limits may still be capped; "
            "verify constraint_*_power_limit_uw by hand", 3)


if __name__ == "__main__":
    main()
