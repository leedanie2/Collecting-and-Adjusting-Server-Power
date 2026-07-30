#!/usr/bin/env python3
"""
power_smoother_9.py
===================
Background daemon v9: monitors CPU package power via RAPL and smooths V-dips.

Improvements over v8
--------------------
  State-machine lockout  : derivative detection is completely bypassed during
                           RAMPDOWN and COOLDOWN — eliminates phantom triggering.
  Memory-only intensity  : file-based /tmp/dummy_intensity removed; workers share
                           an mp.Value so intensity writes are sub-microsecond.
  Baseline-aware ramp    : measures ambient idle power from a rolling history
                           window and targets that floor instead of absolute zero.
  Back-off logic         : during RAMPDOWN, if a rapid power *increase* is
                           detected (workload resumed early), workers instantly
                           drop to 0.0 and skip to COOLDOWN to avoid fighting
                           the main task.
  OpenBLAS-free workers  : pure-Python floating-point math — no numpy, no BLAS
                           thread explosion on many-core machines.

Fixed experimental parameters
------------------------------
  detection threshold  : -3000 W/s  (-3 W/ms)
  back-off threshold   :  +1500 W/s  (power rise during ramp → abort)
  ramp-down window     :  6.0 s (linear decay, may abort early)
  cooldown             :  2.0 s after every event before re-arming

Run as root in the background:
  sudo python3 power_smoother_9.py &

Kill cleanly when done:
  sudo kill $(cat /tmp/smoother9.pid)

Output files
------------
  power_samples_9.csv  — full RAPL trace (written on exit)
  events_9.csv         — one row per event (written in real time)
"""

from __future__ import annotations

import collections
import csv
import math
import multiprocessing as mp
import os
import signal
import time

from rapl_reader import RaplMonitor

# ── Fixed experimental parameters ────────────────────────────────────────────
DERIV_THRESHOLD   = -3000.0   # W/s  drop trigger
BACKOFF_THRESHOLD = +1500.0   # W/s  power increase during ramp → abort immediately
RAMPDOWN_SECS     = 6.0       # s    max linear ramp duration
DETECT_POLL_SECS  = 0.002     # s    2 ms high-frequency poll (IDLE only)
CTRL_STEP         = 0.25      # s    intensity update interval during ramp
CALIB_WINDOW      = 0.4       # s    RAPL snapshot window for logging snapshots
COOLDOWN_SECS     = 2.0       # s    quiet period after each event before re-arm

BASELINE_FALLBACK = 400.0     # W    fallback when history is too short
BASELINE_WINDOW   = 60.0      # s    look-back window for idle power estimation
BASELINE_MARGIN   = 20.0      # W    finish ramp early if within this of baseline

# Cap workers — enough to fill a power gap without swamping a many-core machine
N_WORKERS   = min(mp.cpu_count(), 16)
PID_FILE    = "/tmp/smoother9.pid"
SAMPLES_CSV = "power_samples_9.csv"
EVENTS_CSV  = "events_9.csv"

EVENTS_FIELDS = [
    "event_num", "detected_at_s", "rate_w_per_s",
    "power_at_detect_w", "baseline_w", "handoff_w",
    "final_w", "ramp_duration_s", "abort_reason",
]


# ── Pure-Python worker (no numpy — avoids OpenBLAS thread explosion) ─────────

def _worker_fn(intensity: mp.Value, stop: mp.Event) -> None:  # type: ignore[type-arg]
    """Burn CPU proportionally to intensity [0, 1] using pure FP math."""
    x = 1.000001
    while not stop.is_set():
        v = intensity.value
        if v < 0.01:
            time.sleep(0.0001)   # 100 µs idle — stays responsive to intensity changes
            continue
        iters = max(200, int(60_000 * v))
        for _ in range(iters):
            x = math.sqrt(x * x + 1.0) - math.sqrt(x * x - 1.0 + 1e-15)
            x = math.sin(x) * math.cos(x) + math.exp(-x * x) + 1.0


# ── Baseline estimator ────────────────────────────────────────────────────────

def _estimate_baseline(
    history: collections.deque,  # type: ignore[type-arg]
    now: float,
) -> float:
    """
    10th-percentile of readings within BASELINE_WINDOW.
    Outliers from the workload plateau land in the upper percentiles;
    the idle floor survives in the lower tail.
    """
    cutoff  = now - BASELINE_WINDOW
    samples = sorted(w for t, w in history if t >= cutoff)
    if len(samples) < 20:
        return BASELINE_FALLBACK
    return samples[max(0, len(samples) // 10)]


# ── CSV helpers ───────────────────────────────────────────────────────────────

def _write_samples(samples: list[tuple[float, float]]) -> None:
    with open(SAMPLES_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp_s", "power_w"])
        w.writerows(samples)
    print(f"[Saved] {len(samples)} samples → {SAMPLES_CSV}")


def _append_event(row: dict) -> None:
    exists = os.path.exists(EVENTS_CSV)
    with open(EVENTS_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=EVENTS_FIELDS)
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
        print("Error: RAPL requires root.  Run: sudo python3 power_smoother_9.py")
        raise SystemExit(1)

    signal.signal(signal.SIGINT,  _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    with open(PID_FILE, "w") as f:
        f.write(f"{os.getpid()}\n")

    # ── Spawn workers in hot-standby at intensity=0 ───────────────────────────
    intensity = mp.Value("d", 0.0)   # shared-memory double — no file I/O
    stop_evt  = mp.Event()
    workers   = [
        mp.Process(target=_worker_fn, args=(intensity, stop_evt), daemon=True)
        for _ in range(N_WORKERS)
    ]
    for w in workers:
        w.start()
    pids = [w.pid for w in workers]

    rapl = RaplMonitor(interval=0.05)
    rapl.start()
    time.sleep(0.2)   # let RAPL settle and workers reach their idle-poll loop

    # ── One-time calibration: filler package power at full intensity ──────────
    # Lets handoff enter at the primary's level (start_intensity below) instead
    # of slamming to 1.0, which on a many-core box draws more than the primary
    # left off and overshoots. Daemon starts before the workload, so the machine
    # is idle here and the reading is clean.
    intensity.value = 1.0
    time.sleep(1.0)
    dummy_full_power = rapl.snapshot_watts(window=0.5)
    intensity.value = 0.0
    time.sleep(0.5)
    print(f"[Daemon] Calibrated filler full-intensity power ≈ {dummy_full_power:.1f} W")

    if os.path.exists(EVENTS_CSV):
        os.remove(EVENTS_CSV)

    print(f"[Daemon] v9  drop={DERIV_THRESHOLD:.0f} W/s  "
          f"backoff={BACKOFF_THRESHOLD:+.0f} W/s  ramp={RAMPDOWN_SECS:.1f} s  "
          f"cooldown={COOLDOWN_SECS:.1f} s")
    print(f"[Daemon] {N_WORKERS} workers (hot-standby, mp.Value)  PIDs: {pids}")
    print(f"[Daemon] PID {os.getpid()} → {PID_FILE}  |  silent until event fires\n")

    # ── State machine ─────────────────────────────────────────────────────────
    #
    #   IDLE ──(drop detected)──► RAMPDOWN ──(done/backoff/baseline)──► COOLDOWN
    #    ▲                                                                    │
    #    └─────────────────────────────────────────────────────────────────────┘
    #
    state   = "IDLE"
    t_origin = time.monotonic()
    event_num = 0

    # Rolling power history for baseline estimation (IDLE only)
    history: collections.deque[tuple[float, float]] = collections.deque()

    # IDLE derivative state
    last_w: float | None = None
    last_t: float | None = None

    # RAMPDOWN state
    ramp_start      = 0.0
    ramp_last_w: float | None = None
    ramp_last_t: float | None = None

    # Per-event logged values
    event_detect_t = 0.0
    event_detect_w = 0.0
    event_rate     = 0.0
    handoff_w      = 0.0
    baseline_w     = BASELINE_FALLBACK
    start_intensity = 1.0   # set per-event from handoff_w / dummy_full_power

    while _running:
        t_now = time.monotonic()
        w_now = rapl.current_watts

        # ── IDLE ──────────────────────────────────────────────────────────────
        # Derivative check is ONLY active here — completely absent in all other
        # states, which is what prevents phantom re-triggering.
        if state == "IDLE":
            history.append((t_now, w_now))
            while history and t_now - history[0][0] > BASELINE_WINDOW:
                history.popleft()

            # Derivative must use the RAPL sample's OWN timestamp, not the poll
            # gap: current_watts only refreshes every rapl.interval (~50 ms), so
            # dividing by the 2 ms poll inflated dP/dt ~25× and caused false fires.
            s_t, s_w = rapl.current
            if last_t is not None and s_t > last_t:        # only on a fresh sample
                rate = (s_w - last_w) / (s_t - last_t)      # true ~50 ms dt
                if rate <= DERIV_THRESHOLD:
                    baseline_w = _estimate_baseline(history, t_now)
                    handoff_w  = rapl.snapshot_watts(window=CALIB_WINDOW)
                    # Enter at the level the primary left off, not full blast —
                    # slamming to 1.0 overshoots and makes the di/dt spike worse.
                    start_intensity = (
                        1.0 if dummy_full_power <= 1.0
                        else min(1.0, handoff_w / dummy_full_power)
                    )
                    intensity.value = start_intensity
                    ramp_start      = time.monotonic()
                    event_num      += 1
                    event_detect_t  = t_now - t_origin
                    event_detect_w  = s_w
                    event_rate      = rate

                    print(f"[Event {event_num}] t={event_detect_t:.3f} s  "
                          f"dP/dt={rate:+.0f} W/s  power={s_w:.1f} W  "
                          f"baseline≈{baseline_w:.1f} W  start={start_intensity:.2f} "
                          f"→ workers activated")

                    state       = "RAMPDOWN"
                    last_w = last_t = None
                    ramp_last_w = ramp_last_t = None
                    time.sleep(CTRL_STEP)
                    continue

            if last_t is None or s_t > last_t:
                last_w, last_t = s_w, s_t
            time.sleep(DETECT_POLL_SECS)

        # ── RAMPDOWN ──────────────────────────────────────────────────────────
        # No derivative check here — only back-off and completion checks.
        elif state == "RAMPDOWN":
            elapsed = time.monotonic() - ramp_start
            frac    = max(0.0, 1.0 - elapsed / RAMPDOWN_SECS)
            intensity.value = start_intensity * frac   # decay from handoff level, not 1.0

            abort_reason: str | None = None

            # 1. Back-off: rapid power increase means workload resumed early
            if ramp_last_w is not None and ramp_last_t is not None:
                dt_r = t_now - ramp_last_t
                if dt_r > 0 and (w_now - ramp_last_w) / dt_r >= BACKOFF_THRESHOLD:
                    intensity.value = 0.0   # release CPU immediately
                    abort_reason    = "backoff"

            # 2. Baseline reached ahead of schedule
            if abort_reason is None and w_now <= baseline_w + BASELINE_MARGIN:
                intensity.value = 0.0
                abort_reason    = "baseline_reached"

            # 3. Normal full-duration completion
            if abort_reason is None and elapsed >= RAMPDOWN_SECS:
                intensity.value = 0.0
                abort_reason    = "normal"

            if abort_reason is not None:
                final_w     = rapl.snapshot_watts(window=CALIB_WINDOW)
                actual_secs = round(time.monotonic() - ramp_start, 2)
                print(f"[Event {event_num}] Ramp done ({abort_reason})  "
                      f"handoff={handoff_w:.1f} W  final≈{final_w:.1f} W  "
                      f"baseline={baseline_w:.1f} W  elapsed={actual_secs:.2f} s")
                _append_event({
                    "event_num":         event_num,
                    "detected_at_s":     round(event_detect_t, 4),
                    "rate_w_per_s":      round(event_rate, 1),
                    "power_at_detect_w": round(event_detect_w, 2),
                    "baseline_w":        round(baseline_w, 2),
                    "handoff_w":         round(handoff_w, 2),
                    "final_w":           round(final_w, 2),
                    "ramp_duration_s":   actual_secs,
                    "abort_reason":      abort_reason,
                })
                state = "COOLDOWN"
                time.sleep(CTRL_STEP)
                continue

            ramp_last_w = w_now
            ramp_last_t = t_now
            time.sleep(CTRL_STEP)

        # ── COOLDOWN ──────────────────────────────────────────────────────────
        # Completely deaf to power signals — just wait out the quiet period.
        elif state == "COOLDOWN":
            time.sleep(COOLDOWN_SECS)
            last_w = None   # discard stale derivative seed before re-arming
            last_t = None
            state  = "IDLE"

    # ── Graceful shutdown ─────────────────────────────────────────────────────
    print("\n[Daemon] Shutting down …")
    intensity.value = 0.0
    time.sleep(0.3)
    stop_evt.set()
    for w in workers:
        w.join(timeout=3)
    rapl.stop()

    try:
        os.remove(PID_FILE)
    except OSError:
        pass

    _write_samples(rapl.all_samples())
    print(f"[Daemon] Events log → {EVENTS_CSV}")


if __name__ == "__main__":
    main()
