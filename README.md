# Collecting and Adjusting Server Power

Companion code for a study of software techniques that measure and smooth the
power swings a datacenter CPU imposes on the electrical grid.

A single server's CPU package power is measured under realistic workloads;
transient onsets are detected from the power signal and upstream OS-scheduler
telemetry; actuators (a RAPL cap with SCHED_IDLE ballast, and di/dt ramp
shaping) smooth the draw; and the resulting fleet-scale load is pushed through a
phasor microgrid model to score grid impact — ramp-rate exceedances, frequency
nadir and ROCOF, and voltage sag — against NERC and IEEE thresholds.

Paper: (link to be added)

## Layout

| Directory | Role |
|---|---|
| `measurement/` | Collect — RAPL and system telemetry samplers into InfluxDB |
| `prediction/` | Detect — a random-forest + Kalman regime detector that flags imminent transients |
| `mitigation/` | Adjust — the slew governor, di/dt ramp shaper, and standalone power smoother |
| `grid-simulation/` | Fleet aggregation, a Simulink phasor microgrid, and grid-risk scoring |
| `workloads/` | The benchmarks run on the server (HPL, an AI-style load, a stepped load) |
| `cluster-profiling/` | Fleet duty-cycle clustering visualization |

## What runs where

Measurement, prediction, and mitigation ran on the instrumented server — a
128-thread dual-socket Xeon with per-socket RAPL (PL1 205 W, PL2 246 W) and a
live InfluxDB. They are included as the method of record; reproducing them needs
that hardware.

The grid simulation runs offline from the power traces under
`grid-simulation/data/`, and reproduces here in full.

## Reproduce the grid-risk results

The published risk and cost numbers regenerate from the checked-in simulation
outputs with only Python and NumPy — no MATLAB license needed:

```bash
cd grid-simulation
scripts/score_all.sh                                       # re-score every run
python3 analysis/compare_pair.py hpl_baseline hpl_rampc --out hpl_rampc
```

`compare_pair.py` prints cost and the nine grid-risk metrics as
baseline / smoother / percent change. Regenerating the simulation outputs
themselves from the raw traces needs MATLAB R2025a with Simulink and Simscape
Electrical; see `grid-simulation/README.md`.

## Workloads

The stepped load generator (`workloads/load.c`) and the AI-style workload
(`workloads/ai_sim_2.py`) were contributed by collaborators.

## License

MIT — see `LICENSE`.
