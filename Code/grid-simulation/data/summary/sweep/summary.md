# Original Simulink fleet-size sweep (n=4)

> Historical model outputs. Use [POSTER_DATA.md](../../../POSTER_DATA.md)
> and [corrected sweep](../rescored/sweep.csv) for the poster. The corrected
> worst baseline voltage is 0.9488 pu at 10,000 servers and 0.8826 pu at
> 20,000; the frequency nadir at 20,000 is 59.654 Hz. Voltage crosses the
> study threshold first.

Same clean n=4 traces, worst-case aggregation, pushed through the **fixed 50 MW** grid at each fleet size (`N_servers` override; exact-equivalent to trace-scaling). Load scales linearly with N; grid stress does not.

## Baseline grid stress vs fleet size

| N | mean MW (≈pu) | min V (pu) | nadir (Hz) | ROCOF (Hz/s) |
|---|---|---|---|---|
| 1000 | 0.7388 (0.01) | 0.9731 | 59.6708 | 0.9062 |
| 5000 | 3.6818 (0.07) | 0.9731 | 59.8508 | 0.2928 |
| 10000 | 7.3606 (0.15) | 0.953 | 59.1739 | 0.4737 |
| 20000 | 14.7181 (0.29) | 0.8924 ⚠ | 56.561 | 2.0072 |
| 50000 | ~37 (>0.7) | **solver diverged** | — | — |
| 100000 | ~74 (>1.5) | **solver diverged** | — | — |

**50,000 and 100,000-server runs did not converge** in the original phasor
model near t = 0.001 s. This is a solver limitation; it does not by itself
establish a physical hosting limit. The table above averages baseline metrics
over workloads and runs and includes model artifacts. The corrected sweep
reports worst-case minima, so its aggregation also differs from this table.

## Does rampc still win on CV at every scale?

- **N=1000**: top smoother = **rampc** (CV -18.7% ± 1.2)
- **N=5000**: top smoother = **rampc** (CV -91.8% ± 0.9)
- **N=10000**: top smoother = **rampc** (CV -68.8% ± 0.9)
- **N=20000**: top smoother = **rampc** (CV -56.6% ± 1.1)

Ramp & fill ranks first in this original-model sweep. These values retain
startup effects and do not establish the corrected benefit at every fleet size.
The poster reports 91.4 ± 1.4% CV reduction at 10,000 servers.

## Cost (N-independent — `cost.csv`)

| smoother | runtime % | energy % |
|---|---|---|
| rampc | +121.1 ± 5.7 | +161.0 ± 6.7 |
| powersmoother | +3.5 ± 0.2 | +7.4 ± 0.6 |
| usagegov | +1.1 ± 1.9 | +11.6 ± 1.5 |

Runtime and energy are properties of the single-node **trace**, so they do not
vary with `N_servers` — one table, not one per fleet size. Values mirror
`../scoreboard.csv` (full untrimmed trace, n=4). Hand-maintained: `summarize_sweep.py`
writes the CSVs, not this prose. The `scoreboard_N*.csv` files
carry grid metrics only.

## Model artifacts
- **RREI is flat (~231k/yr) at every N** — it's the t=0 cold-start exceedance annualized (1 exceedance × year/duration), not a load signal. Ignore RREI across this sweep.
- **Low-N grid metrics are transient-dominated**: peak_MW≈4.1 MW at both 1k and 5k is the fixed startup inrush, not the load — so CV/nadir wobble non-monotonically below ~10k. The fixed operating point also biases 10k–20k; those runs are not an artifact-free range.
- **Load scaling itself is exact** (mean MW ∝ N to 3 sig figs); the original grid metrics additionally reflect startup and operating-point errors.
