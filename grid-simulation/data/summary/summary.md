# Which mitigation wins? (n=4, mean ± SD across runs)

| rank | mitigation | CV | peak | runtime | energy | verdict |
|---|---|---|---|---|---|---|
| 1 | **rampc** | -68.8 ± 0.9% | -5.1 ± 0.3% | +121.1 ± 5.7% | +161.0 ± 6.7% | strong smoothing: 69% (+/-1) less variability |
| 2 | **powersmoother** | +0.6 ± 1.9% | -0.4% | +3.5 ± 0.2% | +7.4 ± 0.6% | no measurable smoothing (+1% (+/-2)) |
| 3 | **usagegov** | +16.0 ± 4.9% | -0.8 ± 0.2% | +1.1 ± 1.8% | +11.6 ± 0.7% | WORSENS variability by 16% (+/-5) |

Each cell: mean ± standard deviation across four repeat collections of the full 3×4 matrix (quiesced server, aisim2 schedule seed pinned). Negative = mitigation lower (better) for CV/peak/runtime; energy is a cost either way. CV and peak are on the simulated PCC power (post 15 s UPS filter, plateau only); **runtime and energy are the full untrimmed trace** — ramp.c's ramp-up/down ballast is real time and energy the mitigation costs.

Ranked by CV reduction — the flatness a smoother exists to deliver. The ± is run-to-run spread; where it rivals the mean (runtime, energy), a single collection cannot be trusted, which is why three were taken. Per-cell numbers: `matrix.csv` (run × workload × mitigation).

