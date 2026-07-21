# Mitigation (adjust)

Three ways to smooth a server's power draw, all reducing the di/dt the grid
sees.

## slew-governor/

`rapl_capper.py --slew --ballast` is the shipped governor. It stays fully stock
while power is quiet; on a detected transition — a single-tick jump, a
least-squares derivative over threshold, or the risk flag firing below the
plateau — it shapes the edge two ways:

- The RAPL PL2 cap is hugged to current power and released at a bounded slope,
  as the rising-edge backstop.
- SCHED_IDLE ballast (spinner threads) fills dips so power descends at a bounded
  slope, and pre-burns ahead of a risk-flagged onset so real work displaces the
  spinners watt-for-watt with near-zero net step.

Ballast accounting is calibration-free: each spinner's achieved burn is read
from its CPU time, so a power-starved spinner contending with the real job is
not counted as delivered load. In live runs the governor kept at least 90% of
HPL Gflops while holding edges near +/-190 W/s versus +/-450 W/s uncontrolled.

`usage_edge.py` is the CPU-usage-edge detector variant. `replay_gov.py` replays
a recorded trace through the governor offline for tuning.

`deploy/` holds the systemd user unit and the exact sudoers command (RAPL writes
need root). These are the units as deployed on the measurement host, so the
paths and user are site-specific.

## ramp/

`ramp.c` — di/dt slew-rate shaping applied as a workload ramps down, optionally
gated on the risk flag. Standalone C, one thread per core.

## power-smoother/

`power_smoother_16_2.py` — a standalone daemon with its own dual-gate onset
detector, so it needs no external detector. It reads RAPL directly and ramps a
target power level through each transition.
