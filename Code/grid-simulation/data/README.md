# data/ layout

Everything here was collected on a quiesced server (`scripts/quiesce.sh` stops
the observability stack first — see "Why quiesced" below).

```
data/
  runs/        run{1,2,3,4}/   four repeat collections: raw traces + workload logs
  traces/      one reference trace per cell, for the single-run tooling
  summary/     scoreboard.csv, matrix.csv, full_ranking.csv, cost_edges.csv, sweep/
  plots/       plot_traces.py overlays, one per (workload, mitigation)
  comparisons/ compare_pair.py tables, one per (workload, mitigation)
  detector/    governor event logs + phase logs (a separate, earlier collection)
  load_signals.csv
```

A **cell** is `<workload>_<condition>` where workload ∈ {hpl, aisim2, step} and
condition ∈ {baseline, rampc, powersmoother, usagegov}.

## Two collections, on purpose

`runs/` and `traces/` are **not** the same data, and neither is a subset of the
other.

- `runs/run<N>/<cell>.csv` — the 3×4 matrix, collected four times. This is what
  every published number comes from: `summarize_n4.py`, `rank_all.py`,
  `cost_edges.py` and the sweep all read `runs/` plus the matching
  `results/<cell>_r<N>_worst/metrics.json`. The aisim2 schedule seed is pinned
  across the four runs, so the repeats measure system variance rather than
  workload luck.
- `traces/<cell>.csv` — one earlier reference trace per cell, kept because the
  single-run tools (`plot_traces.py`, `compare_pair.py`, `sim_trace.sh`) take a
  bare filename and resolve it here, and because `results/<cell>_worst/` holds
  the full `S_PCC`/`V_PCC`/`freq_dev` time series for those runs (the run-repeat
  result dirs keep only `metrics.json`, to stay small). Do not mix a `traces/`
  number into an n=4 table — the two sets were collected weeks apart.

`detector/` is a third, earlier collection, and the reason is written up in
`detector/README.md`: the governor event logs for the main run matrix were not
retained, so detector quality is scored on the collection that still has them.

## Why quiesced

The observability stack in `measurement/` costs real power on the box it
measures — `influxd` alone raises idle package power ~6 W and shows a ~5 s
compaction ripple, which lands squarely in the volatility signal these runs
exist to measure. `scripts/quiesce.sh` stops it before a collection.

## Reading the results

Start at `summary/summary.md` — one ranked table, "which mitigation wins".
`summary/scoreboard.csv` is the same thing machine-readable, `matrix.csv` is the
per-cell backing (48 rows = 4 runs × 3 workloads × 3 mitigations), and
`full_ranking.csv` carries all thirteen metrics with a trust tag on each.
`../PAPER_DATA.md` derives every published number from these files and cites the
file each one came from.

## Not included

Telemetry caches (~216 MB) and dated training pickles; see the top-level README.
