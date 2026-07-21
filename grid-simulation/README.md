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

## Layout

```
simulation/    MATLAB — fleet aggregation (readscript.m), model builder
               (build_microgrid.m), runner (run_simulation.m), and the phasor
               model (microgrid_phasor.slx)
analysis/      Python scoring — grid_metrics.py (per-run metrics),
               compare_pair.py (baseline vs smoother), plot_traces.py,
               and trim_rampc.py (trace prep, see below)
scripts/       build_model.sh, sim_trace.sh, score_all.sh, trim_rampc.py
data/traces/   input power traces, named <workload>_<method>.csv
data/comparisons/, data/plots/   scored comparisons and baseline/smoother overlays
results/<run>_worst/   simulation outputs (S_PCC, freq_dev, V_PCC) + metrics.json
SOURCES.md     citations for every threshold
```

## The runs

Three workloads — `hpl`, `aisim2`, `step` — each as a baseline and under three
mitigations: `rampc` (di/dt ramp shaping), `powersmoother` (standalone smoother
daemon), and `usagegov` (the slew governor). Twelve runs, nine
baseline-vs-mitigation comparisons.

## Reproduce with Python only (no MATLAB)

The simulation outputs are checked in under `results/`, so the metrics and
comparison tables regenerate with just Python and NumPy:

```bash
scripts/score_all.sh                                       # re-score every run
python3 analysis/compare_pair.py hpl_baseline hpl_rampc --out hpl_rampc
```

`compare_pair.py` prints cost (energy-area ratio of the two raw traces) and the
nine grid-risk metrics as baseline / smoother / percent change, and writes
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

```
RREI    ramp-rate exceedance events/yr (|dP/dt| over 0.333 MW/s = 20 MW/min)
NRS     integrated ramp severity above the limit, MW/yr
LOLE    loss-of-load-expectation proxy, days/yr (adequacy target 0.1)
CV      coefficient of variation of PCC power — flatness
P2M     peak-to-mean
nadir   frequency nadir, Hz (under-frequency floor 59.3 Hz)
ROCOF   maximum rate of change of frequency, Hz/s
Vsag    voltage sag events/yr and sag depth (pu), 0.95 pu threshold
```

Diff and count metrics run on a uniform 0.1 s resample of the variable-step
solver output, so they measure physical ramps rather than solver micro-steps.
Threshold values and citations are in `SOURCES.md`.

## Trace prep for ramp.c

`ramp.c` pre-ramps ballast before the real workload and ramps it down after,
padding its trace 2.1-2.4x. Because the metrics annualize event counts by trace
duration, that padding alone would deflate the risk scores. `trim_rampc.py`
trims each ramp.c trace to its real-workload window so it is duration-matched to
its baseline before scoring.
