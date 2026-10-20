# Corrected poster grid results

These tables support the October 2026 poster. From `Code/grid-simulation/`, run
`python3 analysis/rescore_warm.py` to regenerate the CSVs and parameters.
See [POSTER_DATA.md](../../../POSTER_DATA.md) for full definitions and cost data.

| File | Contents |
|---|---|
| [scoreboard.csv](scoreboard.csv) | Per-workload and overall CV changes at 30 s and 45 s warm-up |
| [matrix.csv](matrix.csv) | 36 workload/run/mitigation comparisons per warm-up setting |
| [voltage_swing.csv](voltage_swing.csv) | Peak-to-trough voltage and mean per-trace minima at 10,000 servers, 30 s warm-up |
| [sweep.csv](sweep.csv) | Worst baseline voltage/frequency and maximum ROCOF across three workloads and four runs at each fleet size; mean MW is averaged |
| [params.json](params.json) | Warm-up, UPS time constant, and original fitted Simulink operating point used in validation |

For the headline CV values, select `warmup_s=30` and `workload=mean`.
`cv_pct_mean` is a signed change; negate it to express reduction:
91.4 ± 1.4% (ramp & fill), 8.2 ± 3.5% (power smoother), and
21.2 ± 12.7% (governor). Workloads are averaged within each run before computing
mean and sample SD over the four runs.

Voltage swings are 1.411 ± 0.026% of nominal at baseline, 0.344 ± 0.005% for
ramp & fill, 1.366 ± 0.026% for the smoother, and 1.193 ± 0.047% for the
governor. The voltage table averages per-trace minima; the sweep instead takes
the worst baseline minima, which need not occur in the same trace for voltage
and frequency. These summaries should not be interchanged.

The original `../scoreboard.csv` still supplies full-trace runtime and CPU-energy
costs. Its grid columns, and the other original tables in the parent directory,
retain the earlier model artifacts.
