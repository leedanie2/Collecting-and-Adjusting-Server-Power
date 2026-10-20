# Original paper metric ranking — all four runs

> Historical grid scores. Use [POSTER_DATA.md](../../POSTER_DATA.md) and [corrected tables](rescored/) for the poster: CV reductions 91.4%, 8.2%, and 21.2% for ramp & fill, power smoother, and governor. Full-trace cost and separate detector scores remain applicable.

% change vs baseline, mean ± SD across 4 runs (lower = better everywhere). **Winner** is the best mitigation for that metric. The *trust* column records the original diagnostic categories; it does not validate the superseded grid conclusions.

| metric | trust | rampc | powersmoother | usagegov | winner |
|---|---|---|---|---|---|
| _direct_ | |  |  |  | |
| CV | direct | **-68.8±0.9** | +0.6±1.9 | +15.4±7.6 | rampc |
| peak-to-mean | direct | **-5.1±0.3** | -0.4±0.0 | -0.8±0.3 | rampc |
| _perf_ | |  |  |  | |
| runtime | perf | +121.1±5.7 | +3.5±0.2 | **+1.1±1.9** | usagegov |
| energy | perf | +161.0±6.7 | **+7.4±0.6** | +11.6±1.5 | powersmoother |
| _coldstart_ | |  |  |  | |
| ROCOF | coldstart | +81.3±2.4 | +6.0±1.4 | **-1.1±2.3** | usagegov |
| NRS | coldstart | +59.5±0.8 | +1.8±1.3 | **-1.9±3.2** | usagegov |
| freq nadir dev | coldstart | +75.3±2.4 | +5.7±1.4 | **-1.3±2.0** | usagegov |
| volt sag depth | coldstart | **-15.2±1.2** | +2.5±1.5 | -2.5±2.2 | rampc |
| _degenerate_ | |  |  |  | |
| RREI | degenerate | **-6.1±0.9** | -3.2±0.2 | -1.0±1.7 | rampc |
| LOLE proxy | degenerate | **-2.2±0.4** | -1.2±0.8 | +0.8±2.3 | rampc |
| under-freq events | degenerate | +80.8±5.8 | +13.2±3.8 | **-3.4±6.1** | usagegov |
| _cliff_ | |  |  |  | |
| volt sag events | cliff | **-100.0** | +1670.9±2504.4 | +1461.0±2207.6 | rampc |
| _detector_ | |  |  |  | |
| event recall | detector | _n/a_ | _n/a_ | 0.33 | _n/a_ |
| event latency (s) | detector | _n/a_ | _n/a_ | 0.23 | _n/a_ |
| lead time (s) | detector | _n/a_ | _n/a_ | -0.23 | _n/a_ |
| alert precision | detector | _n/a_ | _n/a_ | 0.19 | _n/a_ |

### Trust tags

- **direct** — computed from original PCC signals; grid conclusions superseded by corrected poster analysis
- **perf** — real cost (full trace)
- **coldstart** — affected by startup and the fixed operating point; trimming alone does not correct both
- **degenerate** — no signal — one t=0 exceedance per run makes it ~constant
- **cliff** — count across a threshold both sides sit ~equal distance from
- **detector** — ABSOLUTE values, not % vs baseline, and only usagegov has a detector — see analysis/score_detector.py. aisim2 only: hpl and step emit no phase log to score against

The original `scoreboard.csv` ranks on the four `direct`/`perf` rows. A warm-up alone does not remove the fixed operating-point error. The poster uses `analysis/rescore_warm.py` with a per-trace operating point and 30 s warm-up (45 s sensitivity check). Detector values come from a separate collection with ±0.5 s phase-anchor uncertainty; they are absolute quantities, not percent changes.

