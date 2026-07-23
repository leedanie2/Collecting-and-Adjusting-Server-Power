# Which mitigation wins? (n=3, mean ± SD across runs)

| rank | mitigation | CV | peak | runtime | energy | verdict |
|---|---|---|---|---|---|---|
| 1 | **rampc** | -68.8 ± 1.1% | -5.1 ± 0.4% | +6.7 ± 1.1% | +37.4 ± 1.7% | strong smoothing: 69% (+/-1) less variability |
| 2 | **powersmoother** | +0.8 ± 2.2% | -0.4% | +3.5 ± 0.2% | +7.5 ± 0.7% | no measurable smoothing (+1% (+/-2)) |
| 3 | **usagegov** | +16.3 ± 5.9% | -0.7 ± 0.2% | +1.4 ± 2.1% | +11.7 ± 0.8% | WORSENS variability by 16% (+/-6) |

Each cell: mean ± standard deviation across three repeat collections of the full 3×4 matrix (quiesced server, aisim2 schedule seed pinned). Negative = mitigation lower (better) for CV/peak/runtime; energy is a cost either way. CV and peak are on the simulated PCC power (post 15 s UPS filter); runtime and energy are direct from the RAPL trace.

Ranked by CV reduction — the flatness a smoother exists to deliver. The ± is run-to-run spread; where it rivals the mean (runtime, energy), a single collection cannot be trusted, which is why three were taken. Per-cell numbers: `matrix.csv` (run × workload × mitigation).

