# Mitigation (adjust)

Three ways to smooth a server's power draw, all reducing the di/dt the grid
sees. They make different bets, and the results reflect that: shaping the ramp
directly removes most of the volatility but costs a great deal of runtime and
energy, while the two reactive approaches are nearly free and do much less.

Nothing here predicts. Both reactive designs act only after a transition is
detected, which bounds how much of the initial edge they can remove, and both
perturb the very signal they detect on.

## ramp/

`ramp.c` — di/dt slew-rate shaping applied as a workload ramps up and down,
optionally gated on the risk flag. Standalone C, one thread per core, and the
most effective of the three at flattening power: it cut fleet PCC volatility by
roughly two thirds. It is also the most expensive, roughly doubling runtime on
HPL once the ramps are counted, which is the tradeoff the results are about.

The core list accepts comma lists, ranges, and mixtures (`0:123`, `0-3,96`).
`./ramp --selfcheck` exercises the risk-file edge, the fail-open paths, the
usage edge, and the core-list parser without root or a workload.

## composite-ramp/

`composite_ramp.c` extends `ramp.c` with per-core filling and transition
handling. Its source is not in this repository yet — see the README there.

## power-smoother/

`power_smoother_16_2.py` — a standalone daemon with its own dual-gate onset
detector, so it needs no external detector. It reads RAPL directly and ramps a
target power level through each transition, burning power in Python worker
processes to guide the server down to idle rather than letting it fall.

Gate A watches CPU utilization, which moves 20–50 ms before RAPL registers the
same event; Gate B confirms against power before committing. The layered noise
suppression, the late-trigger guard, and the cooldown states all exist because
earlier versions failed in specific ways.

`history/` keeps three of those earlier versions, because the design is mostly
a record of what went wrong:

```
v1/   pure RAPL, exponential decay, handoff calibrated to match the power
      level the primary task left off at. The overshoot on that handoff is
      what motivated everything after it. calibrate_power.py (in v13/) is the
      study of how much power a given dummy workload actually draws.
v9/   adds a state-machine lockout, after successful ramp-downs were observed
      to end fast enough to re-trigger the smoother on their own tail, and a
      baseline-aware target so the ramp aims at measured idle rather than zero.
v13/  adds a rolling-window derivative. Two-sample dP/dt at 2 ms let a single
      noisy RAPL read manufacture a 10,000 W/s edge and fire a false ramp.
```

Each version keeps its contemporaneous `rapl_reader.py` and dummy worker, so
each directory runs as the snapshot it was. The shipped version replaced the C
dummy worker with Python worker processes.

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
A ceiling cannot stop a fall, which is why the ballast half exists at all.

`usage_edge.py` is the detector that drives it, and the one that replaced the
machine-learning work in `../prediction/`: two hand-set thresholds on
`/proc/stat`, no training, ~0.1 s to respond. It emits signed edges — a fall is
a rise with the sign flipped, and what differs is the actuator (ceiling for a
rise, ballast for a fall), not the detection.

`replay_gov.py` replays a recorded trace through the governor offline for
tuning. `deploy/` holds the systemd user unit and the exact sudoers command
(RAPL writes need root); those are the units as deployed, so the paths and user
are site-specific.

`rapl_capper.py --selfcheck` runs 60 assertions against a fake powercap tree
and needs neither root nor real hardware. `usage_edge.py --selfcheck` covers
the pulse, the risk-flag writer, and the evaluator.
