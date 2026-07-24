# Figures

Presentation figures, regenerated from checked-in data with Python + matplotlib
(no MATLAB): `python3 figures/make_figures.py`.

| File | What it shows |
|---|---|
| `scoreboard.png` | The headline. Each mitigation's effect on CV, peak-to-mean, runtime, and energy vs baseline, with n=4 run-to-run error bars. ramp.c is the only one that flattens the load (−69% CV). |
| `per_workload.png` | The same effect split by workload — the averages hide that usage-governor nearly doubles aisim2 variability and that ramp.c's energy cost is ~2× on the bursty aisim2 load. |
| `overlays.png` | Raw single-node power, baseline vs each mitigation, all 9 pairs. ramp.c is shown **untrimmed** so the full di/dt ramp-up/down is visible; the shaded band is the scored plateau window. |
| `pipeline.png` | The methods schematic: one measured server → 10,000-server fleet → UPS → phasor microgrid → grid-risk metrics. |

Numbers come from `../data/summary/`; traces in `overlays.png` are from
`../data/runs/run1/`.
