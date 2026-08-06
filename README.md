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
| `measurement/` | Collect — RAPL, system, BMC, and PDU telemetry into InfluxDB |
| `prediction/` | Detect — the forecasting attempts, and the detector that replaced them |
| `mitigation/` | Adjust — di/dt ramp shaping, the standalone power smoother, and the slew governor |
| `grid-simulation/` | Fleet aggregation, a Simulink phasor microgrid, and grid-risk scoring |
| `workloads/` | The benchmarks run on the server (HPL, an AI-style load, a stepped load) |
| `cluster-profiling/` | Fleet duty-cycle clustering visualization |

## The result

Three mitigations, three workloads, four repeat collections on a quiesced
server. Only one of the three flattens the grid-facing load, and it is the
expensive one:

| mitigation | CV (flatness) | runtime | energy |
|---|---|---|---|
| **di/dt ramp shaping** | **−68.8 ± 0.9%** | +121 ± 6% | +161 ± 7% |
| standalone smoother | +0.6 ± 1.9% | +3.5 ± 0.2% | +7.4 ± 0.6% |
| slew governor | +15.4 ± 7.6% | +1.1 ± 1.9% | +11.6 ± 1.5% |

Mean ± SD across the four runs, on simulated PCC power. The two cheap
mitigations are cheap because they are reactive: both wait for a transition to
be detected, so neither can remove its leading edge, and both perturb the signal
they detect on. Scored directly, the governor's actuator moves a median 0.23 s
*after* the transition it is responding to.

## What reproduces, and where

**Python only, from a clone** — the grid and cost results, including every
figure. Simulation outputs are checked in, so no MATLAB is needed:

```bash
cd grid-simulation
python3 analysis/summarize_n4.py         # the n=4 scoreboard
python3 analysis/rank_all.py             # all thirteen metrics
python3 analysis/cost_edges.py           # ramp.c's cost split: ballast legs vs workload
python3 analysis/summarize_sweep.py      # the same matrix at 1k/5k/10k/20k servers
python3 analysis/score_detector.py       # detection quality
python3 figures/make_figures.py          # every figure
python3 figures/make_experiment_figs.py  # the workload / profile / schematic figures
python3 analysis/paper_data.py           # PAPER_DATA.md — every published number, sourced
```

`grid-simulation/PAPER_DATA.md` is the sheet to read alongside the paper: each
number in it is derived from a checked-in CSV or `metrics.json` and cites the
file it came from. Several of the analysis scripts take `--selfcheck`, which
re-derives a published table and asserts it matches the committed one.

**MATLAB R2025a + Simscape Electrical** — regenerating the simulation outputs
from raw traces. The model uses `powerlib`, removed in R2026a, so the version is
pinned.

**The instrumented server** — everything under `measurement/`, `prediction/`,
and `mitigation/`. These need root for RAPL, a live InfluxDB, a BMC and a
networked PDU, and a 128-thread dual-socket host with per-socket RAPL
(PL1 205 W, PL2 246 W). They are included as the method of record. Most modules
still offer a `--selfcheck` that exercises their logic against synthetic input.
Everything under `measurement/`, `mitigation/` and `grid-simulation/` passes it
from a clean clone; under `prediction/` only the six listed in
`prediction/README.md` do, because the rest score against the telemetry caches
that are too large to commit.

## Reading the repo against the paper

| Paper section | Code |
|---|---|
| RAPL, sampling rate, false spikes | `measurement/collect_rapl.py`, `rapl_hf_sampler.c` |
| Cost of observation | the five daemons in `measurement/` |
| PL1/PL2/tau clamp | `prediction/exploration/characterize_overshoot.py` |
| Regression baseline | `prediction/exploration/regression.py`, `prediction/detectors/ols/` |
| Precursor heuristics screen | `prediction/exploration/extended_precursor_screen.py` |
| Hawkes / run-queue modelling | `prediction/core/features.py`, `exploration/confirmation_backtest.py` |
| Random forest, continual retraining | `prediction/detectors/random_forest/` |
| Detector comparison | `mitigation/slew-governor/usage_edge.py`, `prediction/validation/` |
| The three mitigations | `mitigation/` |
| Workloads | `workloads/` |
| Experiment procedure | `grid-simulation/scripts/quiesce.sh`, `recollect.sh` |
| Grid model and metrics | `grid-simulation/simulation/`, `analysis/` |

Each directory's README covers its own part in more detail, including what was
tried and abandoned. The failed approaches are kept as code rather than
described, because the final design is mostly a consequence of them.

## Not included

- **Telemetry caches** (~216 MB) and dated training pickles. The exported model
  of record is committed; `prediction/telemetry/` regenerates the caches from
  InfluxDB.
- **Governor event logs for the main run matrix**, which were not retained. The
  earlier collection that the detector scores are computed from is committed in
  full — see `grid-simulation/data/detector/README.md`.
- A lightweight learned Kalman variant that was attempted and left no code worth
  keeping. The classical adaptive filter in `prediction/models/kalman/` is what
  was used.

## Contributors

The AI-style workload (`workloads/ai_sim_2.py`), the stepped load generator
(`workloads/load.c`), and `composite_ramp.c` were contributed by collaborators.
The deployment units under `*/deploy/` are checked in verbatim as deployed, so
their paths and usernames are site-specific — a sudoers entry in particular has
to match the deployed command exactly.

## License

MIT — see `LICENSE`.
