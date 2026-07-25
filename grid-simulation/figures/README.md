# Figures

Presentation figures, regenerated from checked-in data with Python + matplotlib
(no MATLAB): `python3 figures/make_figures.py`.

| File | What it shows |
|---|---|
| `scoreboard.png` | The headline. Each mitigation's effect on CV, peak-to-mean, runtime, and energy vs baseline, with n=4 run-to-run error bars. ramp.c is the only one that flattens the load (−69% CV). |
| `tradeoff.png` | One-glance summary: cost (energy) vs benefit (CV reduction) scatter. ramp.c alone sits in the high-benefit corner; the other two are at/below zero benefit. |
| `all_metrics.png` | **Every** metric (all 12 grid + cost) × 3 mitigations in one view, grouped by trust tier. Y-axis inverted so lower-is-better reads as **up = better**; outliers past ±200% clipped and labelled. The detector tier names its four metrics and is marked PENDING (not computable from power traces — see below). |
| `reproducibility.png` | Each of the 4 runs as a dot per mitigation (CV / runtime / energy) — CV clusters tight, cost carries the spread. Robustness evidence. |
| `distribution.png` | Single-node power histograms, baseline (spread) vs ramp.c plateau (tight peak) — the mechanism behind the CV number. |
| `pcc_timeseries.png` | Fleet PCC power (post-UPS, 10,000 servers) over time, baseline (rippling) vs ramp.c (flat) — what the grid actually sees. |
| `per_workload.png` | The same effect split by workload — the averages hide that usage-governor nearly doubles aisim2 variability and that ramp.c's energy cost is ~2× on the bursty aisim2 load. |
| `overlay_<mitigation>.png` | Raw single-node power, baseline vs one mitigation, three workload rows. For ramp.c the trace is **untrimmed and time-aligned** so its plateau starts where baseline starts — the ramp flanks sit in negative time / past the baseline end, and the shaded band is the scored plateau. |
| `pipeline.png` | The methods schematic: one measured server → 10,000-server fleet → UPS → phasor microgrid → grid-risk metrics. |

Numbers come from `../data/summary/`; traces in `overlays.png` are from
`../data/runs/run1/`.
