# Full metric ranking — every grid metric, all four runs

% change vs baseline, mean ± SD across 4 runs (lower = better everywhere). **Winner** is the best mitigation for that metric. The *trust* column says whether the ranking means anything — only `direct`/`perf` rows are safe to rank on as-is.

| metric | trust | rampc | powersmoother | usagegov | winner |
|---|---|---|---|---|---|
| _direct_ | |  |  |  | |
| CV | direct | **-68.8±0.9** | +0.6±1.9 | +16.0±4.9 | rampc |
| peak-to-mean | direct | **-5.1±0.3** | -0.4±0.0 | -0.8±0.2 | rampc |
| _perf_ | |  |  |  | |
| runtime | perf | +121.1±5.7 | +3.5±0.2 | **+1.1±1.8** | usagegov |
| energy | perf | +161.0±6.7 | **+7.4±0.6** | +11.6±0.7 | powersmoother |
| _coldstart_ | |  |  |  | |
| ROCOF | coldstart | +81.3±2.4 | +6.0±1.4 | **-0.3±2.1** | usagegov |
| NRS | coldstart | +59.5±0.8 | +1.8±1.3 | **-1.2±3.1** | usagegov |
| freq nadir dev | coldstart | +75.3±2.4 | +5.7±1.4 | **-0.6±2.1** | usagegov |
| volt sag depth | coldstart | **-15.2±1.2** | +2.5±1.5 | -3.1±1.2 | rampc |
| _degenerate_ | |  |  |  | |
| RREI | degenerate | **-6.1±0.9** | -3.2±0.2 | -1.0±1.6 | rampc |
| LOLE proxy | degenerate | **-2.2±0.4** | -1.2±0.8 | +0.7±2.1 | rampc |
| under-freq events | degenerate | +80.8±5.8 | +13.2±3.8 | **-0.5±6.5** | usagegov |
| _cliff_ | |  |  |  | |
| volt sag events | cliff | **-100.0** | +1670.9±2504.4 | -100.0 | rampc |

### Trust tags

- **direct** — computed straight from the signal — believe the ranking
- **perf** — real cost (full trace)
- **coldstart** — dominated by the model's t=0 cold start; needs WARMUP_S=45 to mean anything
- **degenerate** — no signal — one t=0 exceedance per run makes it ~constant
- **cliff** — count across a threshold both sides sit ~equal distance from

The headline `scoreboard.csv` ranks on the four `direct`/`perf` rows only. The `coldstart` rows become meaningful with `WARMUP_S=45 scripts/score_all.sh`; `degenerate`/`cliff` rows carry no mitigation signal at worst-case aggregation.

