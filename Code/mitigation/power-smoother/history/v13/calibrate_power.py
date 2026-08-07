#!/usr/bin/env python3
"""
calibrate_power.py  (windowed time-slice rewrite)
==================
Profiles this machine's RAPL package power using the exact windowed duty-cycle
structure used by power_smoother_11 workers:

    Each worker runs exactly N math iterations inside a hard 10 ms time-slice
    window, then sleeps for the remaining window time.  Changing N continuously
    scales power from baseline idle up to the full-blast ceiling.

The calibration uses all N_WORKERS processes simultaneously (matching the
daemon exactly) and maps:

    iterations_per_worker_per_10ms_window  →  total RAPL package power (W)

The inverse — watts_to_iters() — is what the daemon queries at every
CTRL_STEP during rampdown to set workers to the exact target wattage.

Run as root:
    sudo python3 calibrate_power.py [options]

Flags:
    --workers N      worker count (default: min(cpu_count, 16))
    --iter-step S    iteration step size (default: 500)
    --max-watts W    hard power ceiling — sweep stops if RAPL >= W

Outputs:
    calib_profile.csv  — raw sweep: iterations, baseline_w, power_w, delta_w
    calib_lookup.py    — auto-generated module: lookup table + watts_to_iters()
"""

from __future__ import annotations

import argparse
import csv
import math
import multiprocessing as mp
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rapl_reader import RaplMonitor

# ── Constants ─────────────────────────────────────────────────────────────────
WINDOW_SEC        = 0.010   # 10 ms — must match power_smoother_11
DEFAULT_WORKERS   = min(mp.cpu_count(), 16)
DEFAULT_ITER_STEP = 500

TEST_DURATION  = 3.0    # s  — total measurement time per data point
WARMUP_SECS    = 1.0    # s  — discarded at start of each test (transient noise)
COOLDOWN_SECS  = 1.5    # s  — idle gap between tests (thermal settle)
BASELINE_SECS  = 5.0    # s  — idle measurement before the sweep
RAPL_INTERVAL  = 0.05   # s  — RaplMonitor sampling interval

CALIB_CSV       = "calib_profile.csv"
CALIB_LOOKUP_PY = "calib_lookup.py"


# ── Window ceiling detection ──────────────────────────────────────────────────

def _find_window_max_iters(step: int) -> int:
    """
    Time the math kernel single-threaded to find the largest iteration count
    that completes within 90% of WINDOW_SEC.  Provides a safe ceiling for the
    calibration sweep without requiring the user to guess.
    """
    x = 1.000001
    iters = step
    while True:
        t = time.monotonic()
        for _ in range(iters):
            x = math.sqrt(x * x + 1.0) - math.sqrt(x * x - 1.0 + 1e-15)
            x = math.sin(x) * math.cos(x) + math.exp(-x * x) + 1.0
        if time.monotonic() - t >= WINDOW_SEC * 0.90:
            return max(step, iters - step)
        iters += step


# ── Calibration worker ────────────────────────────────────────────────────────

def _calib_worker(
    iterations: int,
    ready_evt: "mp.Event[bool]",
    stop_evt:  "mp.Event[bool]",
) -> None:
    """
    Windowed duty-cycle worker — identical loop structure to power_smoother_11.
    Runs exactly `iterations` math ops per WINDOW_SEC, sleeping the remainder.
    """
    x = 1.000001
    ready_evt.set()
    while not stop_evt.is_set():
        t_start = time.monotonic()
        for _ in range(iterations):
            x = math.sqrt(x * x + 1.0) - math.sqrt(x * x - 1.0 + 1e-15)
            x = math.sin(x) * math.cos(x) + math.exp(-x * x) + 1.0
        remaining = WINDOW_SEC - (time.monotonic() - t_start)
        if remaining > 0.0005:
            time.sleep(remaining)


# ── Measurement helpers ───────────────────────────────────────────────────────

def _measure_idle(mon: RaplMonitor, duration: float) -> float:
    t0 = time.monotonic()
    samples: list[float] = []
    while time.monotonic() - t0 < duration:
        samples.append(mon.current_watts)
        time.sleep(RAPL_INTERVAL)
    return sum(samples) / len(samples) if samples else 0.0


def _run_test(
    mon: RaplMonitor,
    n_procs: int,
    iterations: int,
    max_watts: float | None,
) -> tuple[float, bool]:
    """
    Spawn n_procs windowed workers, wait for all to enter their loop, then
    collect power samples for TEST_DURATION (discarding WARMUP_SECS).

    Returns (mean_watts, limit_hit).  If RAPL crosses max_watts at any sample,
    workers are killed immediately and limit_hit=True is returned.
    """
    ready_evts = [mp.Event() for _ in range(n_procs)]
    stop_evt   = mp.Event()
    procs = [
        mp.Process(
            target=_calib_worker,
            args=(iterations, ready_evts[i], stop_evt),
            daemon=True,
        )
        for i in range(n_procs)
    ]
    for p in procs:
        p.start()
    for evt in ready_evts:
        evt.wait(timeout=5.0)

    def _kill_all() -> None:
        stop_evt.set()
        for p in procs:
            p.join(timeout=3)
            if p.is_alive():
                p.terminate()

    t0 = time.monotonic()
    samples: list[float] = []
    while time.monotonic() - t0 < TEST_DURATION:
        w = mon.current_watts
        if max_watts is not None and w >= max_watts:
            _kill_all()
            return (sum(samples) / len(samples) if samples else w), True
        if time.monotonic() - t0 >= WARMUP_SECS:
            samples.append(w)
        time.sleep(RAPL_INTERVAL)

    _kill_all()
    return (sum(samples) / len(samples) if samples else 0.0), False


# ── Lookup-table generator ────────────────────────────────────────────────────

def _write_lookup(rows: list[dict], baseline_w: float, n_workers: int) -> None:
    table_lines = [
        f"    ({r['iterations']}, {r['power_w']:.3f}),"
        for r in rows
    ]

    code = f'''\
#!/usr/bin/env python3
"""
calib_lookup.py
===============
Auto-generated by calibrate_power.py — do not edit by hand.
Re-run calibrate_power.py to refresh after hardware changes.

Maps iterations_per_worker_per_{WINDOW_SEC*1000:.0f}ms_window <-> RAPL package power (W).
Calibrated with {n_workers} workers, {WINDOW_SEC*1000:.0f} ms time-slice window.

Baseline idle power (no workers): {baseline_w:.3f} W
"""

from __future__ import annotations

WINDOW_SEC : float = {WINDOW_SEC}
N_WORKERS  : int   = {n_workers}
BASELINE_W : float = {baseline_w:.3f}

# [(iterations_per_window, mean_package_watts)]  sorted by iterations ascending
CALIB_TABLE: list[tuple[int, float]] = [
{chr(10).join(table_lines)}
]


def watts_to_iters(target_w: float) -> int:
    """
    Return the iterations_per_window value whose calibrated power is closest
    to target_w.  Called by the daemon at every CTRL_STEP during rampdown
    to set the workers' exact iteration target.
    """
    if not CALIB_TABLE:
        return 0
    return min(CALIB_TABLE, key=lambda row: abs(row[1] - target_w))[0]


def iters_to_watts(iters: int) -> float:
    """Return the calibrated power for an exact iteration count, or -1.0."""
    for i, w in CALIB_TABLE:
        if i == iters:
            return w
    return -1.0


def max_iters() -> int:
    """Highest calibrated iteration count (full-blast ceiling)."""
    return CALIB_TABLE[-1][0] if CALIB_TABLE else 0


def max_watts() -> float:
    """Highest calibrated power reading."""
    return max(w for _, w in CALIB_TABLE) if CALIB_TABLE else 0.0
'''

    with open(CALIB_LOOKUP_PY, "w") as f:
        f.write(code)
    print(f"[Saved] {CALIB_LOOKUP_PY}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if os.geteuid() != 0:
        print("Error: RAPL requires root.  Run: sudo python3 calibrate_power.py")
        raise SystemExit(1)

    parser = argparse.ArgumentParser(
        description="Sweep windowed iteration counts and record RAPL package power."
    )
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS,
        help=f"Worker count (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--iter-step", type=int, default=DEFAULT_ITER_STEP,
        help=f"Iteration step size (default: {DEFAULT_ITER_STEP})",
    )
    parser.add_argument(
        "--max-watts", type=float, default=None,
        help="Hard power ceiling (W) — sweep stops if RAPL >= this value",
    )
    args = parser.parse_args()

    print("[Calibrate] Detecting window iteration ceiling …")
    max_iters_window = _find_window_max_iters(args.iter_step)
    iter_values      = list(range(0, max_iters_window + 1, args.iter_step))

    cap_note = f"{args.max_watts:.0f} W hard cap" if args.max_watts else "no cap"
    est_min  = len(iter_values) * (TEST_DURATION + COOLDOWN_SECS) / 60.0
    print(f"[Calibrate] Window ceiling : {max_iters_window:,} iters/window")
    print(f"[Calibrate] Sweep          : {len(iter_values)} points  "
          f"step={args.iter_step}  workers={args.workers}  {cap_note}")
    print(f"[Calibrate] Est. time      : {est_min:.1f} min\n")

    mon = RaplMonitor(interval=RAPL_INTERVAL)
    mon.start()
    time.sleep(0.2)

    print(f"[Calibrate] Measuring idle baseline for {BASELINE_SECS:.1f} s …")
    baseline_w = _measure_idle(mon, BASELINE_SECS)
    print(f"[Calibrate] Baseline = {baseline_w:.2f} W\n")

    rows: list[dict] = []
    total = len(iter_values)

    with open(CALIB_CSV, "w", newline="") as csvfile:
        writer = csv.DictWriter(
            csvfile,
            fieldnames=["iterations", "baseline_w", "power_w", "delta_w"],
        )
        writer.writeheader()

        for idx, iters in enumerate(iter_values, 1):
            print(
                f"  [{idx:3d}/{total}]  iters={iters:>6,}  … ",
                end="", flush=True,
            )

            avg_w, limit_hit = _run_test(mon, args.workers, iters, args.max_watts)
            delta_w = avg_w - baseline_w

            if limit_hit:
                print(f"{avg_w:7.2f} W  *** HIT {args.max_watts:.0f} W LIMIT — stopping ***")
            else:
                print(f"{avg_w:7.2f} W  (Δ{delta_w:+6.2f} W above idle)")

            row = {
                "iterations": iters,
                "baseline_w": round(baseline_w, 3),
                "power_w":    round(avg_w, 3),
                "delta_w":    round(delta_w, 3),
            }
            writer.writerow(row)
            rows.append(row)
            csvfile.flush()

            if limit_hit:
                break

            time.sleep(COOLDOWN_SECS)

    mon.stop()
    print(f"\n[Saved] {CALIB_CSV}  ({len(rows)} rows)")
    _write_lookup(rows, baseline_w, args.workers)
    print("\n[Calibrate] Done.  Run power_smoother_11.py — it will import calib_lookup automatically.")


if __name__ == "__main__":
    main()
