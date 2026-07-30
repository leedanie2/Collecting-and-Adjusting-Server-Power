# Figures

Presentation figures, regenerated from checked-in data with Python + matplotlib
(no MATLAB): `python3 figures/make_figures.py`.

| File | What it shows |
|---|---|
| `scoreboard.png` | The headline. Each mitigation's effect on CV, peak-to-mean, runtime, and energy vs baseline, with n=4 run-to-run error bars. ramp.c is the only one that flattens the load (−69% CV). |
| `tradeoff.png` | One-glance summary: cost (energy) vs benefit (CV reduction) scatter. ramp.c alone sits in the high-benefit corner; the other two are at/below zero benefit. |
| `all_metrics.png` | **Every** metric (all 12 grid + cost) × 3 mitigations in one view, grouped by trust tier. Y-axis inverted so lower-is-better reads as **up = better**; outliers past ±200% clipped and labelled. The detector tier is annotated with the pooled scores from `../analysis/score_detector.py` rather than percent-change bars — those four are absolute quantities and apply to `usagegov` alone. |
| `reproducibility.png` | Each of the 4 runs as a dot per mitigation (CV / runtime / energy) — CV clusters tight, cost carries the spread. Robustness evidence. |
| `distribution.png` | Single-node power histograms, baseline (spread) vs ramp.c plateau (tight peak) — the mechanism behind the CV number. |
| `pcc_timeseries.png` | Fleet PCC power (post-UPS, 10,000 servers) over time, baseline (rippling) vs ramp.c (flat) — what the grid actually sees. |
| `per_workload.png` | The same effect split by workload — the averages hide that usage-governor nearly doubles aisim2 variability and that ramp.c's energy cost is ~2× on the bursty aisim2 load. |
| `overlay_<mitigation>.png` | Raw single-node power, baseline vs one mitigation, three workload rows. For ramp.c the trace is **untrimmed and time-aligned** so its plateau starts where baseline starts — the ramp flanks sit in negative time / past the baseline end, and the shaded band is the scored plateau. |
| `pipeline.png` | The methods schematic: one measured server → 10,000-server fleet → UPS → phasor microgrid → grid-risk metrics. |

Numbers come from `../data/summary/`; traces in `overlays.png` are from
`../data/runs/run1/`.

## Experiment and instrument figures

`make_experiment_figs.py` produces the rest — the ones describing the setup
rather than the result:

| File | What it shows |
|---|---|
| `experiment_schematic.png`, `run_matrix.png` | The procedure: three workloads × four conditions, collected four times on a quiesced server. |
| `wl_hpl.png`, `wl_aisim2.png`, `wl_step.png` | Each workload's own power profile — the cyclic duty cycle, the inference-style cliff, and the programmable staircase. |
| `rampc_profile.png`, `powersmoother_profile.png`, `usagegov_profile.png` | Each mitigation acting on a trace, one per figure. |
| `rapl_clamp.png`, `rapl_rate.png` | The PL2→PL1 turbo clamp, and why sampling faster than 10 Hz reports power that never happened. |
| `observer_cost.png` | What the monitoring stack itself costs in CPU and watts — the reason a collection quiesces the box first. |
| `simulink_model.png` | The phasor microgrid model. |
