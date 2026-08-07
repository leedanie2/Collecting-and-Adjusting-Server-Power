#!/usr/bin/env python3
"""
power_smoother.py
=================
Prevents di/dt spikes when a high-power primary task ends by replacing the
sudden power cliff with a smooth exponential ramp-down via a dummy FMA worker.

Phase overview
--------------
  Phase 1 — Primary task (numpy DGEMM loop) runs for PRIMARY_DURATION seconds.
  Phase 2 — Handoff: capture package power the instant the primary task stops.
  Phase 3 — Dummy worker (cpu_dummy_worker) spins up; intensity is calibrated
             so its power output matches the handoff power level.
  Phase 4 — Exponential ramp-down: intensity decays from 1.0 → TARGET_FRACTION
             over RAMPDOWN_SECS, updating the worker every CTRL_STEP seconds.

Decay function
--------------
  intensity(t) = exp(−λ · t)      λ = ln(1 / TARGET_FRACTION) / RAMPDOWN_SECS

  Why exponential over linear?
  • The rate of change at handoff is −λ·P₀ — bounded, never a cliff.
  • Rate scales with instantaneous level: gentle when power is already low,
    aggressive only when headroom is large.  This matches real VRM di/dt budgets.
  • A single parameter λ (or τ = 1/λ) controls both speed and smoothness.
  • Linear ramps have a constant di/dt throughout, so they don't ease the final
    transition to idle — just shift the cliff from start to end.

Power modulation
----------------
  Python writes a float in [0.0, 1.0] to /tmp/dummy_intensity.
  cpu_dummy_worker re-reads that file every CTRL_INTERVAL ticks (~200 ms) and
  scales its inner FMA loop count proportionally.

Prerequisites
-------------
  1. Build the C worker:
       gcc -O2 -march=native -o cpu_dummy_worker cpu_dummy_worker.c -lm
  2. Run as root (RAPL requires root or CAP_SYS_ADMIN):
       sudo python3 power_smoother.py
"""

import os
import sys
import math
import time
import json
import threading
import subprocess
import multiprocessing as mp
import numpy as np

from rapl_reader import RaplMonitor

# ── Tunables ──────────────────────────────────────────────────────────────────
PRIMARY_DURATION  = 3.0     # s — how long the primary task runs (2–4 s range)
RAMPDOWN_SECS     = 8.0     # s — time for intensity 1.0 → TARGET_FRACTION
TARGET_FRACTION   = 0.10    # stop dummy when power reaches this × peak
CTRL_STEP         = 0.25    # s — interval between intensity updates in Phase 4
CALIB_WINDOW      = 0.4     # s — RAPL averaging window used during calibration
MATRIX_SIZE       = 1024    # primary task: square matrix side length (DGEMM)
DUMMY_BASE_ITERS  = 2_000_000   # passed to cpu_dummy_worker as argv[1]
WORKER_BINARY     = "./cpu_dummy_worker"
INTENSITY_FILE    = "/tmp/dummy_intensity"
# ─────────────────────────────────────────────────────────────────────────────

# Decay rate constant: chosen so intensity(RAMPDOWN_SECS) == TARGET_FRACTION
LAMBDA = math.log(1.0 / TARGET_FRACTION) / RAMPDOWN_SECS   # ≈ ln(10) / 8


# ── Decay function ────────────────────────────────────────────────────────────

def decay_intensity(t: float) -> float:
    """
    Exponential decay envelope: I(t) = e^{−λt}.

    At t=0               → 1.0  (full dummy power)
    At t=RAMPDOWN_SECS   → TARGET_FRACTION  (≈ 0.10)

    The instantaneous di/dt is −λ · I(t), so it is always proportional to
    the current power level and never causes a hard step change.
    """
    return math.exp(-LAMBDA * t)


# ── Control-file I/O ──────────────────────────────────────────────────────────

def set_intensity(value: float) -> None:
    """
    Atomically write intensity [0.0, 1.0] to the worker's control file.
    Using rename prevents the worker from reading a partial write mid-update.
    """
    value = max(0.0, min(1.0, value))
    tmp = INTENSITY_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(f"{value:.6f}\n")
    os.replace(tmp, INTENSITY_FILE)


# ── Primary task ──────────────────────────────────────────────────────────────

def _primary_task_loop(duration: float) -> None:
    """
    Continuous float64 DGEMM for `duration` seconds.
    Targets ~80 % of package TDP by keeping both the FPUs and the L3/DRAM
    memory subsystem saturated.  The result feeds back into A each iteration
    to prevent dead-code elimination and to keep values numerically stable.
    """
    rng = np.random.default_rng(0)
    A = rng.random((MATRIX_SIZE, MATRIX_SIZE), dtype=np.float64)
    B = rng.random((MATRIX_SIZE, MATRIX_SIZE), dtype=np.float64)
    t_end = time.monotonic() + duration
    while time.monotonic() < t_end:
        C = A @ B
        A = C * (1.0 / (C.max() + 1e-12))


# ── Python fallback dummy worker ──────────────────────────────────────────────

def _py_worker_fn(intensity_val: mp.Value, stop_evt: mp.Event) -> None:  # type: ignore[type-arg]
    """
    Fallback used when cpu_dummy_worker binary is absent.
    Runs numpy GEMM in a separate process; scales matrix size with sqrt(intensity)
    so that active compute area (and thus power) tracks intensity linearly.
    """
    rng = np.random.default_rng(42)
    while not stop_evt.is_set():
        v = intensity_val.value
        if v < 0.01:
            time.sleep(0.005)
            continue
        sz = max(32, int(512 * math.sqrt(v)))
        A  = rng.random((sz, sz), dtype=np.float64)
        B  = rng.random((sz, sz), dtype=np.float64)
        _  = (A @ B).sum()


# ── Orchestrator ──────────────────────────────────────────────────────────────

class PowerSmoother:
    def __init__(self) -> None:
        self.rapl = RaplMonitor(interval=0.05)
        self._worker_proc:  subprocess.Popen | None = None
        self._py_proc:      mp.Process        | None = None
        self._py_intensity: mp.Value          | None = None
        self._py_stop:      mp.Event          | None = None

    # ── Worker helpers ────────────────────────────────────────────────────────

    def _launch_worker(self, initial_intensity: float) -> None:
        """Start the dummy worker — C binary preferred, Python numpy fallback."""
        set_intensity(initial_intensity)

        if os.path.exists(WORKER_BINARY):
            self._worker_proc = subprocess.Popen(
                [WORKER_BINARY, str(DUMMY_BASE_ITERS)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            print(f"[Phase 3] C worker PID {self._worker_proc.pid} started "
                  f"(intensity={initial_intensity:.3f})")
        else:
            print(f"[Phase 3] {WORKER_BINARY!r} not found — "
                  "using Python numpy fallback worker")
            self._py_intensity = mp.Value("d", initial_intensity)
            self._py_stop      = mp.Event()
            self._py_proc      = mp.Process(
                target=_py_worker_fn,
                args=(self._py_intensity, self._py_stop),
                daemon=True,
            )
            self._py_proc.start()
            print(f"[Phase 3] Python worker PID {self._py_proc.pid} started "
                  f"(intensity={initial_intensity:.3f})")

    def _set_worker_intensity(self, value: float) -> None:
        if self._worker_proc is not None:
            set_intensity(value)
        elif self._py_intensity is not None:
            self._py_intensity.value = value

    def _stop_worker(self) -> None:
        if self._worker_proc:
            self._worker_proc.terminate()
            try:
                self._worker_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._worker_proc.kill()
            self._worker_proc = None
        if self._py_proc and self._py_stop:
            self._py_stop.set()
            self._py_proc.join(timeout=3)
            self._py_proc = None

    # ── Main run ──────────────────────────────────────────────────────────────

    def run(self) -> list[tuple[float, float]]:
        """
        Execute all four phases and return the full list of RAPL samples as
        [(t_relative_s, watts), ...] for offline analysis or plotting.
        """
        print("=" * 64)
        print("  Power Smoother — exponential di/dt spike prevention")
        print("=" * 64)

        # ── Phase 1: Primary task ─────────────────────────────────────────────
        print(f"\n[Phase 1] Primary task — {MATRIX_SIZE}×{MATRIX_SIZE} DGEMM "
              f"for {PRIMARY_DURATION:.1f} s")
        self.rapl.start()
        time.sleep(0.3)     # let RAPL accumulate its first derivative sample

        primary_thread = threading.Thread(
            target=_primary_task_loop, args=(PRIMARY_DURATION,), daemon=True
        )
        primary_thread.start()
        primary_thread.join()   # blocks until primary task finishes

        # ── Phase 2: Handoff measurement ──────────────────────────────────────
        handoff_power = self.rapl.snapshot_watts(window=CALIB_WINDOW)
        print(f"\n[Phase 2] Primary stopped.  "
              f"Handoff power ({int(CALIB_WINDOW*1000)} ms avg): "
              f"{handoff_power:.2f} W")

        # ── Phase 3: Launch dummy worker & calibrate starting intensity ───────
        self._launch_worker(initial_intensity=1.0)
        time.sleep(0.5)     # let worker reach thermal/frequency steady state

        dummy_full_power = self.rapl.snapshot_watts(window=CALIB_WINDOW)

        if dummy_full_power < 1.0:
            print("[Phase 3] WARNING: dummy reference power < 1 W — "
                  "RAPL unavailable; intensity uncalibrated (using 1.0)")
            calibrated_start = 1.0
        else:
            # ── Calibration: scale intensity so dummy ≈ handoff_power ─────────
            # If dummy at 1.0 draws D watts and we want H watts:
            #   intensity_start = H / D   (clamped to [0, 1])
            calibrated_start = min(1.0, handoff_power / dummy_full_power)
            self._set_worker_intensity(calibrated_start)
            print(f"[Phase 3] Dummy @ full load: {dummy_full_power:.2f} W  →  "
                  f"calibrated start intensity: {calibrated_start:.4f}")

        # ── Phase 4: Exponential ramp-down ────────────────────────────────────
        print(f"\n[Phase 4] Ramp-down over {RAMPDOWN_SECS:.1f} s  "
              f"(λ={LAMBDA:.4f} s⁻¹, τ={1/LAMBDA:.2f} s, "
              f"target={TARGET_FRACTION*100:.0f}% of peak)\n")
        print(f"  {'t (s)':>8}  {'intensity':>10}  {'RAPL (W)':>10}")
        print(f"  {'─'*8}  {'─'*10}  {'─'*10}")

        ramp_start = time.monotonic()
        while True:
            elapsed = time.monotonic() - ramp_start
            if elapsed >= RAMPDOWN_SECS:
                break

            # Apply decay relative to calibrated starting point
            new_intensity = calibrated_start * decay_intensity(elapsed)
            new_intensity = max(0.0, min(1.0, new_intensity))
            self._set_worker_intensity(new_intensity)

            rapl_now = self.rapl.current_watts
            print(f"  {elapsed:8.2f}s  {new_intensity:10.4f}  {rapl_now:10.2f}")
            time.sleep(CTRL_STEP)

        # ── Shutdown ──────────────────────────────────────────────────────────
        self._set_worker_intensity(0.0)
        time.sleep(0.3)
        self._stop_worker()
        self.rapl.stop()

        peak  = max(handoff_power, dummy_full_power if dummy_full_power > 1.0 else 0.0)
        final = self.rapl.snapshot_watts(window=0.5)
        print(f"\n[Done]  Peak ≈ {peak:.2f} W  |  "
              f"10% target ≈ {peak * TARGET_FRACTION:.2f} W  |  "
              f"Final ≈ {final:.2f} W")

        return self.rapl.all_samples()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    smoother = PowerSmoother()
    samples  = smoother.run()

    out = "power_samples.json"
    with open(out, "w") as f:
        json.dump(samples, f, indent=2)
    print(f"\n[Saved] {len(samples)} RAPL samples → {out}")
    print("        Run simulate_power_profile.py to visualise the trace.")
