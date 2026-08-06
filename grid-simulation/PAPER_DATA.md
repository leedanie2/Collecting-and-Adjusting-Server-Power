# PAPER_DATA.md — the numbers, with sources

One place to look when writing a results paragraph. Every value below is derived from a checked-in CSV / `metrics.json`, never hand-copied. Regenerate: `python3 analysis/paper_data.py`.

**Study in one line:** one measured server's CPU power (RAPL) → 10,000-server worst-case (all in-phase) fleet aggregation → 15 s UPS low-pass → phasor microgrid (swing equation, 50 MW base, H=2.5) → grid-risk metrics. 3 workloads × 4 conditions × **n=4** repeat collections = 48 simulations at N=10000, plus 144 more across the fleet-size sweep.

All ± are **run-to-run SD across the 4 repeat collections** (system variance), not within-run noise and not a confidence interval. n=4, so an SD is coarse; treat |mean| < 2·SD as indistinguishable from zero.

## 0. Study constants (what the numbers assume)

| constant | value | where set |
|---|---|---|
| fleet size `N_servers` | 10,000 (sweep: 1k/5k/10k/20k) | `simulation/readscript.m:47` |
| aggregation mode | `worst_case` — all N run the measured trace in phase | `readscript.m:58` |
| non-CPU draw per server | 300 W | `readscript.m:49` `P_base_hardware` |
| PUE | 1.60 idle → 1.15 full, linear in IT fraction | `readscript.m:51-67` |
| UPS model | single-pole low-pass, `tau_ups` = 15 s | `readscript.m:80` |
| grid base `S_base_grid` | 50 MW (weak / islanded microgrid) | `readscript.m:118` |
| system inertia `H_sys` | 2.5 s (baked into the .slx at build time) | `readscript.m:119` |
| PCC nominal | 25 kV LL, 60 Hz, SCL 100 MVA, X/R 7 | `readscript.m:108-111` |
| droop / damping / gov | R = 0.05, D = 1, `tau_gov` = 20 s | `readscript.m` (unchanged since 2026-07-13 retune) |
| ramp limit | 0.333 MW/s (= 20 MW/min, Southern Company large-load cap) | `analysis/grid_metrics.py:31` |
| LOLE target | 0.1 days/yr (NERC 1-in-10) | `grid_metrics.py:33` |
| under-freq threshold | 59.3 Hz (−0.7 Hz, NERC BAL-003 relay) | `grid_metrics.py:34` |
| voltage sag threshold | 0.95 pu (IEEE 1159) | `grid_metrics.py:35` |
| metric resample | uniform 0.1 s (matches RAPL cadence) | `grid_metrics.py:60` |
| warm-up discarded | **0 s** — every number here includes the t=0 fleet cold start | `grid_metrics.py` `WARMUP_S=0.0` |
| aisim2 schedule seed | 3555822270 (pinned, so repeats measure system variance) | `README.md` §setup |
| trace provenance | `data/runs/` — server quiesced (`scripts/quiesce.sh`) | `data/README.md` |

**Cost vs risk use different traces on purpose.** Runtime and energy are the **full untrimmed** single-node trace (ramp.c's ballast flanks are real wall-time and real energy). CV / peak / all grid metrics are the **plateau-trimmed** trace pushed through the sim (`scripts/trim_auto.py`), because there the ballast is an annualization artifact. Source: `analysis/summarize_n4.py` docstring.

## 1. Headline numbers

Source: `data/summary/scoreboard.csv` (← `analysis/summarize_n4.py`); per-cell backing `data/summary/matrix.csv`. % vs the same workload's baseline, averaged over 3 workloads within a run, then mean ± SD over the 4 runs.

| mitigation | CV (flatness) | peak-to-mean | runtime | energy | survives scrutiny? |
|---|---|---|---|---|---|
| **ramp.c** | -68.8 ± 0.9% | -5.1 ± 0.3% | +121.1 ± 5.7% | +161.0 ± 6.7% | **CV: real** (76× its SD, same sign in all 3 workloads and all 4 fleet sizes). peak/runtime/energy: real. |
| **power smoother** | +0.6 ± 1.9% | -0.4 ± 0.0% | +3.5 ± 0.2% | +7.4 ± 0.6% | **CV: within-noise** (+0.6 ± 1.9 — do not report as a reduction *or* an increase). peak −0.4%: real but negligible. cost: real, small. |
| **usage governor** | +15.4 ± 7.6% | -0.8 ± 0.3% | +1.1 ± 1.9% | +11.6 ± 1.5% | **CV: worsens at warm-up 0, improves at warm-up 45 s.** The +15% is dominated by the ballast pool's t=0 spin-up; excluding 45 s it becomes −28.1 ± 12.2%. Do not quote either number without saying which. cost: real, small. |

Negative is better for CV / peak / runtime; energy is a cost either way. A fifth cost dimension — **delivered HPL throughput** — is in §4: ramp.c also gives up 15.5% of sustained Gflops.

### Absolute values behind the percentages (baseline, N=10000, n=4)

Source: `results/<workload>_<cond>_r<1..4>_worst/metrics.json`. Use these to write "CV falls from X to Y" instead of a bare percentage.

| workload | cond | PCC mean (MW) | PCC peak (MW) | CV | peak-to-mean | min V (pu) | nadir (Hz) |
|---|---|---|---|---|---|---|---|
| hpl | baseline | 7.918 ± 0.031 | 8.216 ± 0.014 | 0.0456 ± 0.0031 | 1.0376 ± 0.0037 | 0.9502 ± 0.0002 | 59.150 ± 0.010 |
| hpl | rampc | 8.300 ± 0.003 | 8.368 ± 0.052 | 0.0109 ± 0.0007 | 1.0083 ± 0.0063 | 0.9580 ± 0.0001 | 58.438 ± 0.019 |
| hpl | powersmoother | 8.137 ± 0.060 | 8.412 ± 0.069 | 0.0437 ± 0.0027 | 1.0337 ± 0.0029 | 0.9482 ± 0.0009 | 59.093 ± 0.021 |
| hpl | usagegov | 7.959 ± 0.108 | 8.219 ± 0.118 | 0.0415 ± 0.0013 | 1.0327 ± 0.0011 | 0.9511 ± 0.0018 | 59.160 ± 0.020 |
| aisim2 | baseline | 7.050 ± 0.117 | 7.674 ± 0.144 | 0.0407 ± 0.0006 | 1.0886 ± 0.0024 | 0.9512 ± 0.0023 | 59.154 ± 0.048 |
| aisim2 | rampc | 8.081 ± 0.008 | 8.191 ± 0.008 | 0.0146 ± 0.0001 | 1.0135 ± 0.0003 | 0.9588 ± 0.0002 | 58.579 ± 0.004 |
| aisim2 | powersmoother | 7.176 ± 0.013 | 7.776 ± 0.016 | 0.0436 ± 0.0017 | 1.0836 ± 0.0012 | 0.9515 ± 0.0012 | 59.126 ± 0.014 |
| aisim2 | usagegov | 7.502 ± 0.044 | 8.048 ± 0.030 | 0.0625 ± 0.0079 | 1.0728 ± 0.0093 | 0.9540 ± 0.0007 | 59.175 ± 0.004 |
| step | baseline | 7.114 ± 0.005 | 7.556 ± 0.002 | 0.0426 ± 0.0003 | 1.0621 ± 0.0005 | 0.9577 ± 0.0001 | 59.217 ± 0.006 |
| step | rampc | 7.853 ± 0.004 | 7.870 ± 0.004 | 0.0143 ± 0.0002 | 1.0021 ± 0.0002 | 0.9639 ± 0.0001 | 58.640 ± 0.003 |
| step | powersmoother | 7.310 ± 0.116 | 7.733 ± 0.120 | 0.0420 ± 0.0015 | 1.0578 ± 0.0006 | 0.9560 ± 0.0019 | 59.166 ± 0.021 |
| step | usagegov | 7.167 ± 0.002 | 7.586 ± 0.004 | 0.0431 ± 0.0005 | 1.0584 ± 0.0004 | 0.9577 ± 0.0001 | 59.221 ± 0.008 |

### Single-node trace (what was actually measured, before any fleet model)

Source: `data/runs/run{1..4}/<workload>_<cond>.csv`, full untrimmed.

| workload | cond | duration (s) | mean (W) | min (W) | max (W) | energy (kJ) |
|---|---|---|---|---|---|---|
| hpl | baseline | 213.8 ± 11.0 | 367.1 ± 8.0 | 191.6 ± 0.9 | 452.1 ± 3.7 | 78.4 ± 2.4 |
| hpl | rampc | 403.1 ± 23.1 | 371.5 ± 1.8 | 196.3 ± 1.4 | 449.5 ± 1.3 | 149.8 ± 9.2 |
| hpl | powersmoother | 236.0 ± 10.9 | 376.6 ± 5.1 | 196.3 ± 1.0 | 490.2 ± 14.9 | 88.9 ± 3.3 |
| hpl | usagegov | 220.2 ± 0.1 | 371.5 ± 1.2 | 195.9 ± 1.2 | 452.4 ± 34.0 | 81.8 ± 0.3 |
| aisim2 | baseline | 121.0 ± 0.0 | 254.5 ± 0.3 | 93.4 ± 17.7 | 499.9 ± 51.5 | 30.8 ± 0.0 |
| aisim2 | rampc | 285.9 ± 25.7 | 343.7 ± 5.9 | 191.4 ± 1.0 | 445.8 ± 2.0 | 98.1 ± 7.0 |
| aisim2 | powersmoother | 121.0 ± 0.0 | 267.6 ± 2.1 | 130.6 ± 17.4 | 498.9 ± 5.2 | 32.4 ± 0.3 |
| aisim2 | usagegov | 121.0 ± 0.0 | 325.4 ± 7.6 | 189.7 ± 6.7 | 458.0 ± 9.2 | 39.4 ± 0.9 |
| step | baseline | 110.8 ± 0.0 | 301.8 ± 0.5 | 194.6 ± 0.3 | 378.4 ± 0.2 | 33.5 ± 0.0 |
| step | rampc | 264.2 ± 0.9 | 346.1 ± 0.5 | 196.8 ± 1.3 | 388.0 ± 0.7 | 91.5 ± 0.2 |
| step | powersmoother | 110.9 ± 0.0 | 313.5 ± 2.5 | 186.0 ± 3.7 | 409.4 ± 30.4 | 34.8 ± 0.3 |
| step | usagegov | 110.9 ± 0.0 | 309.2 ± 0.6 | 195.7 ± 0.3 | 379.3 ± 1.0 | 34.3 ± 0.1 |

## 2. Full metric table (all 12, by trust tier)

Source: `data/summary/full_ranking.csv` (← `analysis/rank_all.py`); definitions from `analysis/grid_metrics.py`. All columns are % change vs baseline, mean ± SD, n=4; **lower = better on every row** by construction.

| metric | unit | dir | plain English | tier | ramp.c | power smoother | slew gov | use it? |
|---|---|---|---|---|---|---|---|---|
| CV | dimensionless | lower | SD/mean of fleet PCC power — flatness | `direct` | -68.8 ± 0.9 | +0.6 ± 1.9 | +15.4 ± 7.6 | **YES — the headline metric** |
| peak-to-mean | dimensionless | lower | max/mean of PCC power — headroom the utility must hold | `direct` | -5.1 ± 0.3 | -0.4 ± 0.0 | -0.8 ± 0.3 | yes |
| runtime | s | lower | wall-clock of the full untrimmed trace | `perf` | +121.1 ± 5.7 | +3.5 ± 0.2 | +1.1 ± 1.9 | yes (cost) |
| energy | J | lower | ∫P dt over the full untrimmed trace | `perf` | +161.0 ± 6.7 | +7.4 ± 0.6 | +11.6 ± 1.5 | yes (cost) |
| ROCOF | Hz/s | lower | max \|df/dt\| from the swing equation | `coldstart` | +81.3 ± 2.4 | +6.0 ± 1.4 | -1.1 ± 2.3 | **NO as-is** — t=0 artifact |
| NRS | MW·s/yr | lower | annualized ramp-limit exceedance area (severity, not count) | `coldstart` | +59.5 ± 0.8 | +1.8 ± 1.3 | -1.9 ± 3.2 | **NO as-is** — t=0 artifact |
| freq nadir dev | Hz | lower | deepest frequency excursion below 60 Hz | `coldstart` | +75.3 ± 2.4 | +5.7 ± 1.4 | -1.3 ± 2.0 | **NO as-is** — t=0 artifact |
| volt sag depth | pu | lower | 1 − min PCC voltage (graded companion to the sag count) | `coldstart` | -15.2 ± 1.2 | +2.5 ± 1.5 | -2.5 ± 2.2 | directionally only |
| RREI | events/yr | lower | annualized count of 0.333 MW/s ramp-limit exceedances | `degenerate` | -6.1 ± 0.9 | -3.2 ± 0.2 | -1.0 ± 1.7 | **NO — degenerate** |
| LOLE proxy | days/yr | lower | RREI × (peak/10 GW regional × 2) / 24 | `degenerate` | -2.2 ± 0.4 | -1.2 ± 0.8 | +0.8 ± 2.3 | **NO — RREI-derived** |
| under-freq events | events/yr | lower | annualized samples below 59.3 Hz | `degenerate` | +80.8 ± 5.8 | +13.2 ± 3.8 | -3.4 ± 6.1 | **NO — t=0 dominated** |
| volt sag events | events/yr | lower | annualized samples below 0.95 pu | `cliff` | -100.0 ± 0.0 | +1670.9 ± 2504.4 | +1461.0 ± 2207.6 | **NO — threshold cliff** |
| event recall | — | — | detection quality of `usage_edge` (see §6) | `detector` | n/a | n/a | 0.333 | **absolute, aisim2 only** |
| event latency (s) | — | — | detection quality of `usage_edge` (see §6) | `detector` | n/a | n/a | 0.233 | **absolute, aisim2 only** |
| lead time (s) | — | — | detection quality of `usage_edge` (see §6) | `detector` | n/a | n/a | -0.233 | **absolute, aisim2 only** |
| alert precision | — | — | detection quality of `usage_edge` (see §6) | `detector` | n/a | n/a | 0.19 | **absolute, aisim2 only** |

### Why the bottom three tiers are not results

- **`degenerate` (RREI, LOLE, under-freq).** Verified over all 48 N=10000 runs (0 violations): every run has **exactly one** ramp-limit exceedance — the t=0 fleet cold start, when the model starts all 10,000 servers instantaneously at the trace's first power value. So `RREI ≡ SECONDS_PER_YEAR / trace_duration` identically, and any RREI "improvement" is a **trace-length ratio, not efficacy**. LOLE is RREI × a constant, so it inherits this.
  - The historical **−53% RREI for ramp.c was a pure trace-length artifact** (untrimmed ramp.c traces ran 2.1–2.4× longer). After duration-matching (`scripts/trim_auto.py`) it collapses to -6.1 ± 0.9%, essentially the residual duration mismatch. **Do not write the −53% number.**
- **`coldstart` (ROCOF, NRS, nadir dev, sag depth).** Same t=0 step. A trimmed ramp.c trace *starts at its plateau* (~420 W) where a baseline starts near idle (~194 W), so ramp.c's cold-start step is ~2× larger and ROCOF/NRS read as a large **regression that is entirely startup**. That is why `full_ranking.csv` shows ramp.c ROCOF 81.3% and NRS 59.5%. Prior warm-up sweeps showed ramp.c *reduces* ROCOF 62–94% once t=0 is excluded (`README.md` §6.2) — but that rescore has **not been run**, so neither the regression nor the reduction is citable today.
- **`cliff` (volt sag events).** A count across the 0.95 pu threshold that the baselines straddle: baseline min V is 0.9502–0.9577 pu, i.e. as little as 0.0002 pu from the line. It reads -100.0% / +1671 ± 2504% depending on which side a run happens to land. Meaningless; use `volt sag depth` or absolute `min V`.

## 3. Fleet-size sweep (N = 1k / 5k / 10k / 20k)

Source: `data/summary/sweep/grid_vs_count.csv`, `scoreboard_N*.csv`, `sweep/summary.md`. Same n=4 clean traces, worst-case aggregation, pushed through the **fixed 50 MW** grid at each fleet size via the `N_servers` override.

### Baseline grid stress vs fleet size

| N | mean load (MW) | ≈ pu of 50 MW | peak (MW) | min V (pu) | nadir (Hz) | ROCOF (Hz/s) | CV |
|---|---|---|---|---|---|---|---|
| 1,000 | 0.7388 | 0.01 | 4.1040 | 0.9731 | 59.6708 | 0.9062 | 0.1306 |
| 5,000 | 3.6818 | 0.07 | 4.1054 | 0.9731 | 59.8508 | 0.2928 | 0.0415 |
| 10,000 | 7.3606 | 0.15 | 7.8153 | 0.9530 | 59.1739 | 0.4737 | 0.0430 |
| 20,000 | 14.7181 | 0.29 | 15.6304 | 0.8924 ⚠ <0.95 | 56.5610 | 2.0072 | 0.0456 |
| 50,000 | ~37 | ~0.74 | — | **solver diverged** | — | — | — |
| 100,000 | ~74 | ~1.47 | — | **solver diverged** | — | — | — |

**Constraint, state it as one:** the model has a hard upper bound at **N ≈ 20,000 servers on a 50 MW grid**. At N=50,000 (~37 MW) and N=100,000 (~74 MW) the phasor solver fails at t ≈ 0.001 s — the weak grid physically cannot host the fleet, this is not "degraded metrics". Voltage crosses the 0.95 pu NERC/IEEE sag limit by N=10,000 and craters to 0.8924 pu at N=20,000; frequency nadir falls to 56.56 Hz, far below the 59.3 Hz under-frequency relay threshold.

Load scaling itself is exact (mean MW ∝ N):

| N | mean MW | MW per 1000 servers |
|---|---|---|
| 1,000 | 0.7388 | 0.7388 |
| 5,000 | 3.6818 | 0.7364 |
| 10,000 | 7.3606 | 0.7361 |
| 20,000 | 14.7181 | 0.7359 |

All N-dependence above is nonlinear **grid response**, as intended.

### Does ramp.c still win at every scale? (CV %, n=4)

| mitigation | N=1,000 | N=5,000 | N=10,000 | N=20,000 |
|---|---|---|---|---|
| **ramp.c** | -18.7 ± 1.2% | -91.8 ± 0.9% | -68.8 ± 0.9% | -56.6 ± 1.1% |
| **power smoother** | -3.9 ± 0.7% | +0.4 ± 1.9% | +0.6 ± 1.9% | +0.3 ± 1.6% |
| **usage governor** | +14.8 ± 2.3% | +27.1 ± 4.1% | +15.4 ± 7.6% | +32.5 ± 4.3% |

ramp.c wins CV at **every solvable scale** — the ranking is robust to fleet size. The *magnitude* is not: it swings −18.7% (1k) → −91.8% (5k) → −68.8% (10k) → −56.6% (20k). Quote **−68.8 ± 0.9% at the paper's N=10,000**, and cite the range as a scale-sensitivity note, not as four independent results.

**Bounding artifact — do not read below ~10k as load-driven.** peak_MW ≈ 4.10 MW at *both* N=1,000 and N=5,000 (see the table above: 4.104 vs 4.1054, a 0.03% difference across a 5× fleet). That is a fixed model-startup inrush, not load, so CV / nadir / ROCOF wobble non-monotonically below ~10k. The clean, load-driven regime is **10k–20k**: below it the startup transient dominates, above it the grid diverges.

**RREI is flat at 231,135.7/yr for every N** (`grid_vs_count.csv`) — it is the t=0 exceedance annualized over the same trace duration, unchanged by fleet size. Confirms §2's degeneracy argument from a second direction.

## 4. Per-workload breakdown

Source: `data/summary/matrix.csv` (48 rows: 4 runs × 3 workloads × 3 mitigations), re-aggregated here to mean ± SD over the 4 runs **within** each workload. This is where the averages in §1 hide structure.

Workloads: **hpl** = dense AVX-512 LINPACK (flat plateau); **aisim2** = bursty AI-training envelope (idle↔full REST/PREFILL, seed-pinned); **step** = duty-cycled scalar load.

### CV (% vs baseline)

| mitigation | hpl | aisim2 | step | 3-workload mean (§1) |
|---|---|---|---|---|
| **ramp.c** | -76.0 ± 3.2 | -64.1 ± 0.8 | -66.5 ± 0.4 | -68.8 ± 0.9 |
| **power smoother** | -3.9 ± 4.9 | +7.0 ± 3.8 | -1.3 ± 3.9 | +0.6 ± 1.9 |
| **usage governor** | -8.7 ± 6.4 | +53.6 ± 19.1 | +1.3 ± 0.9 | +15.4 ± 7.6 |

### energy (% vs baseline)

| mitigation | hpl | aisim2 | step | 3-workload mean (§1) |
|---|---|---|---|---|
| **ramp.c** | +90.9 ± 7.2 | +218.8 ± 23.0 | +173.3 ± 0.3 | +161.0 ± 6.7 |
| **power smoother** | +13.3 ± 1.0 | +5.1 ± 0.8 | +3.9 ± 0.9 | +7.4 ± 0.6 |
| **usage governor** | +4.4 ± 3.7 | +27.9 ± 3.0 | +2.4 ± 0.3 | +11.6 ± 1.5 |

### runtime (% vs baseline)

| mitigation | hpl | aisim2 | step | 3-workload mean (§1) |
|---|---|---|---|---|
| **ramp.c** | +88.6 ± 5.2 | +136.3 ± 21.2 | +138.3 ± 0.8 | +121.1 ± 5.7 |
| **power smoother** | +10.4 ± 0.6 | -0.0 ± 0.1 | +0.0 ± 0.0 | +3.5 ± 0.2 |
| **usage governor** | +3.2 ± 5.6 | -0.0 ± 0.0 | +0.0 ± 0.0 | +1.1 ± 1.9 |

### peak-to-mean (% vs baseline)

| mitigation | hpl | aisim2 | step | 3-workload mean (§1) |
|---|---|---|---|---|
| **ramp.c** | -2.8 ± 0.9 | -6.9 ± 0.2 | -5.6 ± 0.0 | -5.1 ± 0.3 |
| **power smoother** | -0.4 ± 0.2 | -0.5 ± 0.3 | -0.4 ± 0.1 | -0.4 ± 0.0 |
| **usage governor** | -0.5 ± 0.3 | -1.4 ± 0.8 | -0.3 ± 0.1 | -0.8 ± 0.3 |

**What to say about each row:**

- **ramp.c flattens everything** — CV -76.0 ± 3.2% (hpl), -64.1 ± 0.8% (aisim2), -66.5 ± 0.4% (step). Not workload-specific; the headline is a real average, not an artifact of one cell.
- **The usage governor's +15% CV is one cell.** aisim2 +53.6 ± 19.1% vs hpl -8.7 ± 6.4% and step +1.3 ± 0.9%. It reacts badly to sharp bursts. Report the cell, not the average.
- **The power smoother does not act.** CV -3.9 ± 4.9% / +7.0 ± 3.8% / -1.3 ± 3.9% — every cell within ~2 SD of zero. (README.md §3 says "≈0% on all three"; the aisim2 cell is nominally +7.0 ± 3.8%, which is ≈2 SD — call it 'no measurable smoothing', not 'exactly zero'.)
- **ramp.c's cost is worst where the baseline idles most.** aisim2 energy +218.8 ± 23.0% vs hpl +90.9 ± 7.2% — ramp.c holds ballast at 100% through aisim2's idle dips.

### Delivered throughput — sustained HPL Gflops (hpl only, n=4)

Source: `data/runs/run{1..4}/hpl_*.log`, the HPL `WR*` summary line, parsed by this script. **This number appears in no other table in the repo** (`data/sweep_meta.csv` does not exist, so `score_all.sh` never passed `--gflops` for the clean matrix). It is the only *useful-work* metric available: runtime and energy say what the mitigation spends, this says what it delivers.

| condition | HPL solve (s) | sustained Gflops | % vs baseline |
|---|---|---|---|
| baseline | 187.1 ± 1.6 | 1824.1 ± 15.2 | — |
| rampc | 222.9 ± 20.3 | 1540.6 ± 141.3 | -15.5% |
| powersmoother | 208.1 ± 2.3 | 1640.2 ± 18.4 | -10.1% |
| usagegov | 199.0 ± 1.0 | 1715.3 ± 8.8 | -6.0% |

**ramp.c costs 15.5% of HPL throughput** on top of its +121% runtime and +161% energy — its core-affinity ballast takes cores away from the real workload. Its run-to-run SD (±141 Gflops, 9%) is much larger than the baseline's (0.8%), so quote it as a range, not a point. Only hpl reports a FLOP rate; aisim2 and step have no equivalent.

### Per-workload cold-start metrics (for completeness — still artifacts)

Same aggregation, from `metrics.json`. Included so nobody re-derives them and mistakes them for results. See §2.

**RREI** (% vs baseline)

| mitigation | hpl | aisim2 | step |
|---|---|---|---|
| ramp.c | -6.7 ± 5.9 | -10.3 ± 1.7 | -1.4 ± 2.0 |
| power smoother | -9.4 ± 0.5 | +0.0 ± 0.0 | -0.0 ± 0.1 |
| usage governor | -2.9 ± 5.0 | +0.0 ± 0.0 | +0.0 ± 0.1 |

**ROCOF** (% vs baseline)

| mitigation | hpl | aisim2 | step |
|---|---|---|---|
| ramp.c | +92.1 ± 2.7 | +69.9 ± 9.7 | +81.8 ± 2.0 |
| power smoother | +7.5 ± 4.3 | +3.8 ± 5.9 | +6.7 ± 3.7 |
| usage governor | -0.4 ± 2.5 | -2.6 ± 5.3 | -0.4 ± 0.7 |

**NRS** (% vs baseline)

| mitigation | hpl | aisim2 | step |
|---|---|---|---|
| ramp.c | +67.3 ± 12.5 | +43.9 ± 9.3 | +67.3 ± 4.0 |
| power smoother | -3.6 ± 3.7 | +3.3 ± 5.1 | +5.7 ± 3.2 |
| usage governor | -3.2 ± 6.6 | -2.3 ± 4.6 | -0.3 ± 0.5 |

**RREI is exactly the inverse duration ratio — verified, not asserted.** Because every run has one exceedance, `RREI_% ≡ (dur_baseline/dur_mitigation − 1)·100`. Side by side:

| cell | RREI % change | duration % change | predicted RREI % from duration alone |
|---|---|---|---|
| hpl × ramp.c | -6.7 ± 5.9 | +7.5 ± 6.7 | -7.0 |
| hpl × power smoother | -9.4 ± 0.5 | +10.4 ± 0.6 | -9.4 |
| hpl × usage governor | -2.9 ± 5.0 | +3.2 ± 5.6 | -3.1 |
| aisim2 × ramp.c | -10.3 ± 1.7 | +11.5 ± 2.1 | -10.3 |
| aisim2 × power smoother | +0.0 ± 0.0 | +0.0 ± 0.0 | +0.0 |
| aisim2 × usage governor | +0.0 ± 0.0 | +0.0 ± 0.0 | +0.0 |
| step × ramp.c | -1.4 ± 2.0 | +1.4 ± 2.0 | -1.4 |
| step × power smoother | -0.0 ± 0.1 | +0.0 ± 0.1 | -0.0 |
| step × usage governor | +0.0 ± 0.1 | +0.0 ± 0.1 | -0.0 |

The residual RREI "improvements" are entirely leftover duration mismatch after `trim_auto.py`: aisim2×ramp.c (-10.3 ± 1.7%) is the worst-matched pair (+11.5% longer), step×ramp.c (-1.4 ± 2.0%) the best-matched. Nothing here is ramp-risk reduction. Note this also means the **power smoother's hpl RREI -9.4 ± 0.5%** is not a safety improvement — it is the smoother making HPL run 10% longer.

## 5. Figure index

All in `figures/` (regenerate: `python3 figures/make_figures.py`). One claim each; the numbers cross-reference §1.

| figure | the one claim it supports | sentence-level takeaway | numbers (§1/§4) |
|---|---|---|---|
| `pipeline.png` | The method is a chain, not a scaling law. | One measured server's RAPL trace is aggregated to 10,000 in phase, low-passed by a 15 s UPS, and solved through a swing-equation microgrid — voltage and frequency come from the model, not from multiplication. | §0 constants |
| `overlay_rampc.png` | ramp.c changes the single-node power shape. | Against baseline on all three workloads, ramp.c replaces the workload's excursions with a held plateau, bracketed by the di/dt ramp legs (shown untrimmed, plateau-aligned). | single-node table §1; runtime +121.1 ± 5.7% |
| `distribution.png` | The flatness is distributional, not a smoothed line. | Single-node power histograms: baseline is spread, ramp.c collapses to a tight peak — this is the mechanism behind the CV number. | CV -68.8 ± 0.9% |
| `pcc_timeseries.png` | The flatness survives the fleet model. | Fleet PCC power (post-UPS, 10,000 servers) ripples under baseline and is flat under ramp.c — what the grid actually sees. | §1 absolute table, PCC mean/peak MW |
| `scoreboard.png` | The headline result, quantified with error bars. | Four believable metrics × 3 mitigations, n=4 error bars: ramp.c is the only mitigation that reduces CV. | §1, all cells |
| `tradeoff.png` | Flatness is bought, not free. | Cost (energy) vs benefit (CV reduction): ramp.c sits alone in the high-benefit corner at high cost; the other two sit at or below zero benefit. | CV -68.8 ± 0.9% vs energy +161.0 ± 6.7% |
| `per_workload.png` | The averages hide structure. | CV and energy split per workload — exposes the slew governor's aisim2 blowup and ramp.c's ~3.2× aisim2 energy that the 3-workload mean flattens. | §4 tables |
| `reproducibility.png` | The claim does not rest on a lucky run. | Each of the 4 runs as a dot for CV / runtime / energy: CV clusters tightly, cost carries the spread. | §1 ± columns; §6 note on run 3 |
| `all_metrics.png` | Nothing is hidden — but not everything is trustworthy. | All 12 grid+cost metrics grouped by trust tier (y-axis inverted, up = better), with the detector tier explicitly marked not-yet-scored. | §2 full table |
| `overlay_powersmoother.png` | The power smoother barely acts. | Baseline vs power smoother on the raw trace — the two curves are nearly coincident, which is why its CV is within noise. | CV +0.6 ± 1.9% |
| `overlay_usagegov.png` | The usage governor is asymmetric by design. | Baseline vs usage governor: it limits ramp-up but fills falls with ballast (ship config `--no-engage-on-dips`), which is why it adds energy without flattening at warm-up 0. | energy +11.6 ± 1.5%; CV +15.4 ± 7.6% |

Suggested paper order: **pipeline** → **overlay_rampc / distribution** (one node) → **pcc_timeseries** (the fleet/grid) → **scoreboard / tradeoff** (the result) → **per_workload / reproducibility / all_metrics** (depth, robustness, completeness).

No figure exists for the fleet-size sweep (§3) — that data is table-only (`data/summary/sweep/`).

## 6. What is still open / not measured

| # | gap | why it matters | what would close it |
|---|---|---|---|
| 1 | **Detector metrics — SCORED 2026-07-28, on aisim2 only.** hpl and step emit no phase log, so ground truth exists for 1 of 3 workloads: 12 rises and 12 drops total. | The measured answer is that the detector does not lead — recall 33%, precision 19%, median lead −0.23 s, i.e. after the transition and inside the ±0.5 s anchor uncertainty, so the detector adds nothing the actuator was not already doing. | Emit phase markers from hpl/step, and add a timestamp field to `usage_edge`'s JSONL so the detector can be scored separately from the actuator. |
| 2 | **Warm-up rescore not run.** Every number here is `WARMUP_S=0`. | ROCOF / NRS / nadir / sag depth (4 of 12 metrics) are t=0-dominated and currently unusable in either direction. Prior sweeps suggest ramp.c *reduces* ROCOF 62–94% once t=0 is excluded — a potentially major, currently uncitable result. | `WARMUP_S=45 scripts/score_all.sh` + a supplementary table/figure. |
| 3 | **One aisim2 schedule seed** (3555822270), pinned across all 4 runs. | The n=4 bars measure *system* variance only. Generalization beyond one schedule is unmeasured. | A deliberate seed sweep, reported as a separate spread. |
| 4 | **Diverse-fleet aggregation not in the headline.** All results are `worst_case` (100% in-phase). | It is an upper bound. A partial-synchrony (`sync_fraction`) break-even sweep was prototyped earlier but the n=4 matrix was never run through it, so `readscript.m` here is worst-case only and the machinery is not in this repo. | Restore the sync_fraction path and re-run the matrix at sf = 0/0.25/0.5/0.75/1.0. |
| 5 | **N > 20,000 is unreachable**, not merely unmeasured. | Caps the claim: results are for a datacenter that fits inside a 50 MW weak grid. A 100k-server claim needs a larger `S_base_grid`, which changes the whole regime. | Re-tune `GRID.S_base_grid` / `H_sys` and rebuild the .slx (`scripts/build_model.sh`). |
| 6 | **No dollar cost / tariff analysis.** `analysis/cost_analysis.py` was deleted (0008af4) and nothing regenerates `cost_analysis.png`. | Energy % is a physical cost, not a bill. Demand-charge framing is absent. | Re-add a tariff model, or drop the economic framing from the paper. |
| 7 | **Gflops/W (Green500) is unscored** — but raw Gflops is now recovered (§4). `data/sweep_meta.csv` **does not exist** in this repo, so `score_all.sh` never passed `--gflops` and no `metrics.json` has an `efficiency` block. | Gflops/W is the natural "is the mitigation worth it" ratio and the field's standard efficiency unit; without it the cost story is watts and seconds only. Half-closed: §4 now has sustained Gflops per condition. | Feed the §4 Gflops into `grid_metrics.py --gflops` and rescore, or write the missing `data/sweep_meta.csv`. |
| 8 | **aisim2 load-generator jitter** (scalar Python `math.*` across 128 procs). | Its PREFILL bursts are jagged by construction, which inflates baseline CV and hence every aisim2 percentage. | A/B `ai_sim_2.py --kernel numpy` on mycroft before adopting. |

**Known outlier, kept:** run 3 is a mild outlier (its `hpl` governor cell flips sign). It was kept rather than cherry-picked; dropping it tightens every bar and changes no conclusion (ramp.c CV → −69.3 ± 0.4). Source: `README.md` §5; the per-cell values are in `matrix.csv`.

## 7. Discrepancies found between docs and data

Checked while deriving the tables above. Nothing here was silently resolved.

| # | where | doc says | CSV says | resolution |
|---|---|---|---|---|
| D1 | `data/summary/sweep/scoreboard_N*.csv`, `runtime` / `energy` columns | — | ramp.c runtime **+6.8%**, energy **+37.6%**, identical at all four N | **FIXED 2026-07-27 — cost moved to `data/summary/sweep/cost.csv`, one row per smoother, and the stale per-N columns removed.** The old columns held the *trimmed-trace* numbers `README.md` §2 retracts as "misleadingly cheap +7% / +38%"; `cost.csv` carries the current full-trace values with SDs (**+121.1 ± 5.7%**, **+161.0 ± 6.7%**), derived from `scoreboard.csv`. Verified in git: commit 8f0b03c changed `scoreboard.csv` `6.8,1.0,37.6,1.4` → `121.1,5.7,161.0,6.7` while *adding* the sweep CSVs with the old `6.8 / 37.6` still in them. Only ramp.c was affected (only it has ballast flanks). Stored once rather than per-N because runtime and energy are single-node **trace** properties that cannot vary with fleet size — the four identical columns were one measurement copied four times. `scoreboard_N*.csv` now carry grid metrics only. |
| D2 | `README.md` §3 | "The power smoother is flat everywhere (≈0% CV on all three)" | hpl −3.9 ± 4.9%, aisim2 **+7.0 ± 3.8%**, step −1.3 ± 3.9% | **FIXED 2026-07-27.** Was defensible but loose — the aisim2 cell is ~1.8 SD from zero, not ≈0. §3 now reads "moves nothing measurable: no cell is distinguishable from zero at n=4", with all three cells and their SDs inline. |
| D3 | `scripts/nrun_pipeline.sh` | header and loops said **3 runs** (`for r in 1 2 3`, `results/*_r[123]_worst`, "n=3 scoreboard") | four runs exist (`data/runs/run{1,2,3,4}`, 48 `_r[1-4]_worst` results dirs) and `summarize_n4.py` sets `RUNS=[1,2,3,4]` | **FIXED 2026-07-27.** Loop → `1 2 3 4`, glob → `_r[1234]_worst` (matches all 48 dirs), header → 4 runs / 48 traces; `analysis/summarize_n3.py` renamed to `summarize_n4.py` (it already had `RUNS=[1,2,3,4]` inside — only the name lied) and all references updated. The committed script now reproduces the committed n=4 results; run 4 had been added by hand. |
| D4 | `data/README.md` "Known gaps" | hpl clean durations "196 / 240 / 209 / 219 s for baseline / powersmoother / rampc / slewgov", "the ordering tracks intervention strength" | Those are the **n=1** `data/runs/run<N>/hpl_*.csv` set (197.3 / 241.6 / 209.8 / 220.1 s), and the 209.8 s rampc figure is the **trimmed** trace while the other three are untrimmed. Untrimmed hpl_rampc is **372.5 s**. n=4 full-trace means: baseline 213.8 ± 11.0 s, powersmoother 236.0 ± 10.9 s, rampc 403.1 ± 23.1 s, usagegov 220.2 ± 0.1 s | Two problems: (a) n=1, and the quoted baseline (196 s) is run 3, the outlier — the other three runs are ~219 s, n=4 mean 213.8 ± 11.0 s; (b) the ordering claim is built from a mixed trimmed/untrimmed comparison. On like-for-like full traces the ordering is baseline < usagegov < powersmoother ≪ rampc, which *does* track intervention strength — but by 1.9×, not the 1.06× the README implies. **FIXED 2026-07-27** — `data/README.md` now quotes the n=4 full-trace means with SDs and the 1.9× ordering, and records the old n=1 figures as superseded. |
| D5 | `README.md` §4 / `figures/README.md` | "The pipeline emits **12** grid + cost metrics" / "**All 12**" | `rank_all.py` `METRICS` has 12 scored rows **+ 4 detector rows** = 16 rows in `full_ranking.csv` | Consistent once you read "12 scored + 4 pending". **FIXED 2026-07-27** — both files now say "12 scored", and `README.md` §4 names the 4 pending detector rows and the 16-row total explicitly. |
| D6 | `README.md` TL;DR | ramp.c costs "~2.2× wall-time (+121%) and ~2.6× energy (+161%)" | +121.1 ± 5.7% → 2.21×; +161.0 ± 6.7% → 2.61× | ✅ agrees. |
| D7 | `README.md` §2 per-workload cost table | hpl +89% / +91%, aisim2 +136% / +219%, step +138% / +173% | hpl +88.6 ± 5.2 / +90.9 ± 7.2, aisim2 +136.3 ± 21.2 / +218.8 ± 23.0, step +138.3 ± 0.8 / +173.3 ± 0.3 | ✅ agrees (rounding only). Note aisim2 runtime carries a ±21.2% SD — the widest bar in the study; do not quote +136% without it. |
| D8 | `data/summary/sweep/summary.md` | "peak_MW≈4.1 MW at both 1k and 5k is the fixed startup inrush" | 4.1040 (1k) vs 4.1054 (5k) | ✅ agrees, and it is a strong artifact signal: a 5× fleet moves peak by 0.03%. |

### Numbers that are *consistent* across every source checked

- ramp.c CV **−68.8 ± 0.9%** at N=10,000: `scoreboard.csv`, `full_ranking.csv`, `sweep/scoreboard_N10000.csv`, `summary.md`, `sweep/summary.md`, `README.md`.
- `n_exceedances_in_trace == 1` and `RREI == YEAR/duration` in **48/48** N=10,000 runs (0 violations) — recomputed here, not taken on faith.
- Fleet-size non-convergence at N=50,000 / 100,000: `sweep/summary.md` only (no CSV rows exist, by construction — the solver produced no output).

---

Generated by `analysis/paper_data.py`. Do not hand-edit — edit the script.
