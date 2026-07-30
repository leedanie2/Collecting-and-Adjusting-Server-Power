# Grid simulation

Estimates the electrical-grid stress of a 10,000-server datacenter from one
server's measured CPU power:

```
power trace (one server)
  -> fleet aggregation (10,000 servers, worst-case in-phase) + non-CPU load
  -> double-conversion UPS (15 s low-pass)
  -> Simulink phasor microgrid (swing-equation governor, PCC voltage)
  -> grid-risk metrics vs NERC/IEEE thresholds
```

Worst-case aggregation is the upper bound: every server runs the same trace in
phase, with no diversity cancellation.

## Result

The full 3×4 matrix was collected four times on a quiesced server, so every
number below carries a run-to-run error bar (`data/summary/summary.md`):

| rank | mitigation | CV (flatness) | peak | runtime | energy | verdict |
|---|---|---|---|---|---|---|
| 1 | **rampc** | **−68.8 ± 0.9%** | −5.1 ± 0.3% | +121 ± 6% | +161 ± 7% | strong smoothing, high cost |
| 2 | powersmoother | +0.6 ± 1.9% | −0.4% | +3.5 ± 0.2% | +7.4 ± 0.6% | no measurable smoothing |
| 3 | usagegov | +16.0 ± 4.9% | −0.8 ± 0.2% | +1.1 ± 1.8% | +11.6 ± 0.7% | worsens variability |

Mean ± SD across the four runs. Negative is better for CV/peak/runtime; energy
is a cost. **di/dt ramp shaping (`rampc`) is the only mitigation that flattens
the grid-facing load** — it cuts the coefficient of variation of PCC power by
69%, but at a real cost: **~2.2× wall-time (+121%) and ~2.6× energy (+161%)**,
because it wraps the workload in ramp-up/down ballast. The standalone power
smoother does nothing measurable at this timescale, and the slew governor makes
variability worse. CV is on the trimmed plateau; **runtime and energy are the
full untrimmed trace**, so the ramp flanks count as the real cost they are (the
plateau-only figures — +7% runtime, +38% energy — describe only the workload
once ramped, and understate the mitigation). The CV bar is tight (±0.9%) even
though raw per-cell runtimes swung 10–19% between runs — the grid metric is
stable; wall-clock time is the noisy one, which is why four collections were taken.

Regenerate with `python3 analysis/summarize_n3.py` (Python only — the per-run
metrics are checked in).

## Layout

```
simulation/    MATLAB — fleet aggregation (readscript.m), model builder
               (build_microgrid.m), runner (run_simulation.m), and the phasor
               model (microgrid_phasor.slx)
analysis/      Python scoring — grid_metrics.py (per-run metrics),
               compare_pair.py (baseline vs smoother), summarize_n3.py (the
               n=4 scoreboard), rank_all.py (all thirteen metrics),
               score_detector.py (detection quality), plot_traces.py
scripts/       quiesce.sh / recollect.sh (collect the matrix, see below),
               build_model.sh, sim_trace.sh, score_all.sh, nrun_pipeline.sh
               (regenerate the n=4 result), trim_auto.py / trim_rampc.py (trace
               prep, see below)
figures/       make_figures.py (every figure from the checked-in summaries),
               make_experiment_figs.py (workload and schematic figures)
data/traces/   input power traces, named <workload>_<method>.csv (one run)
data/runs/     run{1,2,3,4}/  the four repeat collections (raw traces + logs)
data/detector/ run{1,2,3,4}/  workload phase logs + governor event logs, the
               inputs for detection scoring (see the README there — these are
               NOT from the same collection as data/runs/)
data/summary/  the n=4 scoreboard: scoreboard.csv, matrix.csv, summary.md,
               full_ranking.{csv,md}, detector_scores.csv
data/comparisons/, data/plots/   scored comparisons and baseline/smoother overlays
results/<run>_worst/       one-run simulation outputs (S_PCC, freq_dev, V_PCC) + metrics.json
results/<run>_r<N>_worst/  per-run metrics.json for the n=4 scoreboard
SOURCES.md     citations for every threshold
```

## The runs

Three workloads — `hpl`, `aisim2`, `step` — each as a baseline and under three
mitigations: `rampc` (di/dt ramp shaping), `powersmoother` (standalone smoother
daemon), and `usagegov` (the slew governor). Twelve cells, nine
baseline-vs-mitigation comparisons. The whole matrix was collected four times
(`data/runs/run{1,2,3,4}/`) so the headline scoreboard reports mean ± SD, not a
single draw. `data/traces/` holds one representative run for the per-pair tables
and plots.

### How a collection was taken

The server's own observability stack is loud enough to matter: the time-series
database alone runs continuously at most of a core and bursts every 5 s, which
is a ~15 W ripple sitting inside every trace. So each collection quiesces the
box first, and traces go straight to CSV rather than through the database that
would otherwise be one of the things being measured.

```bash
sudo scripts/quiesce.sh dry-run    # what would be stopped; touches nothing
sudo scripts/quiesce.sh stop       # tear down, recording what was running
sudo scripts/recollect.sh          # the 12 cells
sudo scripts/quiesce.sh start      # put it all back
```

`quiesce.sh` records what it stopped so `start` restores exactly that, and
refuses to hand-restart anything under a systemd unit — a supervised process
comes back on its own, and a hand-restored copy becomes a duplicate.
`recollect.sh` resumes rather than restarting from zero if a cell fails, and
pins the AI simulation's schedule seed so a mitigation's effect is never
confounded with schedule luck. Both need the instrumented server.

### Detection quality

`analysis/score_detector.py` scores what the governor actually did against the
workload's own phase log:

```bash
python3 analysis/score_detector.py    # -> data/summary/detector_scores.csv
```

Pooled over four runs it recalls 8 of 24 transitions (33%) at 19% precision,
with a median lead of −0.23 s — that is, the actuator moves *after* the
transition, not before. This is the quantitative form of the reactive-design
limitation: the detector fires on a change that has already begun. Read the
lead times with the ±0.5 s anchor uncertainty documented in
`data/detector/README.md`; nearly all of them are inside it.

## Reproduce with Python only (no MATLAB)

The simulation outputs are checked in under `results/`, so the metrics and
comparison tables regenerate with just Python and NumPy:

```bash
python3 analysis/summarize_n3.py                           # the n=4 scoreboard
scripts/score_all.sh                                       # re-score every run
python3 analysis/compare_pair.py hpl_baseline hpl_rampc --out hpl_rampc
```

`summarize_n3.py` reads the checked-in per-run metrics and writes
`data/summary/` — the headline table above. `compare_pair.py` drills into one
baseline-vs-mitigation pair, printing cost (energy-area ratio) and all thirteen
metrics as baseline / smoother / percent change into
`data/comparisons/<name>_comparison.csv`.

## Reproduce the full chain (MATLAB R2025a + Simscape Electrical)

The model uses `powerlib` (Specialized Power Systems), which was removed in
R2026a — pin R2025a. Build the model once, then run any trace:

```bash
scripts/build_model.sh                        # bakes the grid regime into the .slx
scripts/sim_trace.sh hpl_baseline.csv         # -> results/hpl_baseline_worst/
scripts/score_all.sh hpl_baseline_worst
```

## Metrics

The pipeline computes thirteen metrics, but only four **discriminate** between
mitigations at this fleet scale, and the scoreboard ranks on those alone:

```
CV       coefficient of variation of PCC power — flatness (the target)
P2M      peak-to-mean of PCC power
runtime  trace duration — time-to-completion cost
energy   integral P dt over the raw trace — energy cost
```

CV and peak are taken from the simulated PCC power, after the 15 s UPS
low-pass — that filter is precisely what separates a mitigation that shapes the
grid-facing draw from one that only reshuffles sub-filter wiggles.

The other nine are reported by `compare_pair.py` for completeness but are **not
ranked**, because at 10,000 servers they do not carry a mitigation signal:

```
RREI, LOLE          degenerate. Every worst-case run has exactly one ramp-rate
                    exceedance — the model's t=0 fleet cold start — so RREI ==
                    seconds_per_year / trace_duration by construction, and LOLE
                    tracks it. Drop the cold start and there are zero
                    exceedances anywhere: the 15 s UPS filter keeps every real
                    transient under the 0.333 MW/s limit.
NRS, ROCOF, nadir,  cold-start dominated. All are driven by that same t=0 step,
sag depth           so at warm-up 0 the sign can even invert (ramp.c's ROCOF
                    reads +100% at 0 s but −84% once the cold start is excluded).
                    Score with a warm-up past ~2 UPS time constants before
                    reading them.
Vsag (events)       a count across a 0.95 pu threshold that both baseline and
                    mitigation sit thousandths of a pu from — a cliff, not a
                    gradient. The graded sag depth is the honest version.
```

Diff and count metrics run on a uniform 0.1 s resample of the variable-step
solver output, so they measure physical ramps rather than solver micro-steps.
Threshold values and citations are in `SOURCES.md`.

## Trace prep for ramp.c

`ramp.c` pre-ramps ballast before the real workload and ramps it down after,
padding its trace 2.1-2.4x. Because the metrics annualize event counts by trace
duration, that padding alone would deflate the risk scores, so each ramp.c trace
is trimmed to its real-workload window (the high-power plateau) before scoring.
`trim_auto.py` detects that window straight from the power trace (first and last
crossing of a threshold just under the plateau, so a mid-workload dip survives)
and is what `summarize_n3.py` and `nrun_pipeline.sh` use. `trim_rampc.py` is the
older tool that instead takes hand-supplied offsets read from ramp.c's tick log.
