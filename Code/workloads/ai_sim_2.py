#!/usr/bin/env python3
"""
ai_sim_mycroft.py
=================
Server variant of ai_sim_2.py — same simulator, retuned to drive 128 worker
processes (one per core) instead of 8, for use against power_smoother_16_2.py
on the 128-core / ~225-400 W server. Only N_WORKERS and the output CSV paths
differ from ai_sim_2.py; the phase/schedule/worker logic is unchanged.

Note: POWER_IDLE / POWER_PREFILL below are still the original 6 W / 28 W
annotation labels written to the timeline CSV — they're descriptive labels
only (not used for control), but if you want the timeline CSV's
target_power_w column to reflect the real ~225 W / ~400 W server profile,
update those two constants too.

Simulates the power signature of an AI LLM inference server across two
distinct CPU states:

  REST    — workers sleep; baseline power (~6 W)
  PREFILL — all workers hammer dense FP math at constant maximum CPU load;
            power holds flat until the phase ends, then cuts off ABRUPTLY
            back to idle (no step-down, no decay).

Schedule
--------
  partition_time() pre-calculates the full timeline before the loop:
    (n_req + 1) REST phases interleaved with n_req PREFILL bursts.
  Every PREFILL runs at sustained full load for its entire duration; the
  cliff is produced by a single state-flag write at the end.

Outputs
-------
  ai_benchmark_log_2.csv      — one row per phase (start/stop timestamps)
  ai_benchmark_timeline_2.csv — high-resolution state log every TICK_MS ms

Run (no root needed):
  python3 ai_sim_mycroft.py
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import math
import multiprocessing as mp
import os
import random
import time
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TOTAL_SECS = 120.0  # s  — total benchmark window
NUM_REQ    = 3      # number of PREFILL bursts
MIN_SEG    = 5.0    # s  — minimum duration for any single phase
N_WORKERS  = 128    # persistent worker processes — one per logical CPU
TICK_MS    = 50     # ms — timeline CSV sampling interval

# Target power annotations (W) — written to the timeline CSV only
POWER_IDLE    = 6.0
POWER_PREFILL = 28.0

# Worker state codes
S_IDLE    = 0
S_PREFILL = 1
S_QUIT    = 99

_DIR         = os.path.dirname(os.path.abspath(__file__))
LOG_CSV      = os.path.join(_DIR, "ai_benchmark_log_2.csv")
TIMELINE_CSV = os.path.join(_DIR, "ai_benchmark_timeline_2.csv")


# ---------------------------------------------------------------------------
# Phase definition
# ---------------------------------------------------------------------------

@dataclass
class Phase:
    name:           str
    duration_s:     float
    state_code:     int
    target_power_w: float
    request_id:     int   # 0 = REST; ≥1 = PREFILL burst


# ---------------------------------------------------------------------------
# Schedule generator
# ---------------------------------------------------------------------------

def partition_time(total: float, n_req: int, min_seg: float) -> list[Phase]:
    """
    Randomly partition `total` seconds into:
        REST, PREFILL, REST, PREFILL, ..., REST
    Every segment is >= min_seg.  Returns a list of Phase objects.
    """
    n_segs    = n_req + (n_req + 1)
    remainder = total - n_segs * min_seg
    if remainder < 0:
        raise ValueError(
            f"Cannot fit {n_segs} segments of >= {min_seg} s into {total} s total."
        )

    cuts   = sorted(random.uniform(0, remainder) for _ in range(n_segs - 1))
    extras = (
        [cuts[0]]
        + [cuts[i] - cuts[i - 1] for i in range(1, len(cuts))]
        + [remainder - cuts[-1]]
    )
    durations = [e + min_seg for e in extras]

    phases:  list[Phase] = []
    seg_idx = 0

    for req in range(n_req + 1):
        phases.append(Phase(
            name="REST",
            duration_s=durations[seg_idx],
            state_code=S_IDLE,
            target_power_w=POWER_IDLE,
            request_id=0,
        ))
        seg_idx += 1

        if req < n_req:
            phases.append(Phase(
                name="PREFILL",
                duration_s=durations[seg_idx],
                state_code=S_PREFILL,
                target_power_w=POWER_PREFILL,
                request_id=req + 1,
            ))
            seg_idx += 1

    return phases


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def _worker(worker_id: int, state: mp.Value, quit_evt: mp.Event) -> None:  # type: ignore[type-arg]
    """
    Persistent worker.

    PREFILL: runs an unbroken inner loop of dense FP math — CPU stays pegged
             at maximum until the state flag changes, producing a perfectly
             flat power profile for the duration of the phase.
    REST:    sleeps 2 ms per iteration — minimal CPU load.

    The inner loop re-checks the state flag every 5 000 iterations (~2-5 ms)
    so the drop to idle is near-instantaneous when the phase ends.
    """
    x = 1.000001 + worker_id * 1e-7

    while not quit_evt.is_set():
        s = state.value

        if s == S_IDLE:
            time.sleep(0.002)

        elif s == S_PREFILL:
            # Tight inner loop — keeps CPU fully loaded with no gaps.
            # Short block size (5 000) means we respond to a state change
            # within ~2-5 ms, giving a crisp cliff at phase end.
            while state.value == S_PREFILL:
                for _ in range(5_000):
                    x = math.sqrt(x * x + 1.0) - math.sqrt(x * x - 1.0 + 1e-15)
                    x = math.sin(x) * math.cos(x) + math.exp(-x * x) + 1.0

        elif s == S_QUIT:
            break

        else:
            time.sleep(0.001)


# ---------------------------------------------------------------------------
# Timeline recorder
# ---------------------------------------------------------------------------

def _tick_loop(
    rows: list[dict],
    phase_name: str,
    target_power_w: float,
    duration_s: float,
    t_origin: float,
) -> None:
    """Sleep in TICK_MS increments, appending a timeline row each tick."""
    tick     = TICK_MS / 1000.0
    deadline = time.monotonic() + duration_s
    while True:
        now = time.monotonic()
        rows.append({
            "timestamp_s":    round(now - t_origin, 4),
            "target_power_w": target_power_w,
            "phase_name":     phase_name,
        })
        remaining = deadline - now
        if remaining <= 0:
            break
        time.sleep(min(tick, remaining))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--seed", type=int, default=None,
        help="RNG seed for the REST/PREFILL schedule. Omit for a random "
             "schedule (the seed used is printed so you can pass it back "
             "with --seed to reproduce the exact same schedule, e.g. to "
             "compare baseline vs. smoother-on runs on identical timing).",
    )
    args = parser.parse_args()

    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2**32)
    random.seed(seed)
    print(f"[AI Sim 2] Schedule seed: {seed}  (rerun with --seed {seed} to reproduce)")

    phases = partition_time(TOTAL_SECS, NUM_REQ, MIN_SEG)

    print(f"[AI Sim 2] {TOTAL_SECS:.0f} s window  |  {NUM_REQ} PREFILL bursts  "
          f"|  {N_WORKERS} workers  |  min_seg={MIN_SEG:.0f} s")
    print(f"[AI Sim 2] Schedule ({len(phases)} phases):")
    cumulative = 0.0
    for ph in phases:
        tag = f"  req={ph.request_id}" if ph.request_id else "        "
        print(f"           t={cumulative:6.2f} s  {ph.name:<8}{tag}  "
              f"{ph.duration_s:.2f} s  →  {ph.target_power_w:.0f} W")
        cumulative += ph.duration_s
    print(f"           t={cumulative:.2f} s  (total)\n")

    state_flag = mp.Value(ctypes.c_int, S_IDLE)
    quit_evt   = mp.Event()

    workers = [
        mp.Process(target=_worker, args=(i, state_flag, quit_evt), daemon=True)
        for i in range(N_WORKERS)
    ]
    for w in workers:
        w.start()

    t_origin      = time.monotonic()
    log_rows:      list[dict] = []
    timeline_rows: list[dict] = []

    try:
        for ph in phases:
            state_flag.value = ph.state_code
            t_start = time.monotonic()

            tag = f"(req {ph.request_id})" if ph.request_id else "        "
            print(f"[{time.strftime('%H:%M:%S')}]  {ph.name:<8}  START  {tag}"
                  f"  t={t_start - t_origin:.3f} s  planned {ph.duration_s:.2f} s")

            _tick_loop(timeline_rows, ph.name, ph.target_power_w, ph.duration_s, t_origin)

            state_flag.value = S_IDLE   # abrupt cliff — single atomic write
            t_stop  = time.monotonic()
            actual  = t_stop - t_start

            print(f"[{time.strftime('%H:%M:%S')}]  {ph.name:<8}  STOP   {tag}"
                  f"  t={t_stop - t_origin:.3f} s  actual  {actual:.3f} s")

            log_rows.append({
                "request_id": ph.request_id,
                "phase":      ph.name,
                "start_s":    round(t_start - t_origin, 4),
                "stop_s":     round(t_stop  - t_origin, 4),
                "duration_s": round(actual,             4),
            })

            if ph is not phases[-1]:
                print()

    finally:
        state_flag.value = S_QUIT
        quit_evt.set()
        for w in workers:
            w.join(timeout=3)

    t_end = time.monotonic()
    print(f"\n[{time.strftime('%H:%M:%S')}]  Done.  "
          f"Total elapsed: {t_end - t_origin:.3f} s  (target {TOTAL_SECS:.0f} s)")

    with open(LOG_CSV, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["request_id", "phase", "start_s", "stop_s", "duration_s"]
        )
        writer.writeheader()
        writer.writerows(log_rows)
    print(f"[AI Sim 2] Phase log      → {LOG_CSV}")

    with open(TIMELINE_CSV, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["timestamp_s", "target_power_w", "phase_name"]
        )
        writer.writeheader()
        writer.writerows(timeline_rows)
    print(f"[AI Sim 2] Timeline CSV   → {TIMELINE_CSV}")


if __name__ == "__main__":
    main()
