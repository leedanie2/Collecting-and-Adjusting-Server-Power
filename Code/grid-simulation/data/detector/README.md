# Detector scoring inputs

`analysis/score_detector.py` scores what the governor actually *did* against the
workload's own ground truth. It needs two files per run, and both are here:

```
aisim2_usagegov.log            the workload's phase log — ground truth
aisim2_usagegov.events.jsonl   the governor's event log — what it did
```

## Why this is a separate directory from `../runs/`

These come from an earlier quiesced collection of the `usagegov` arm than the
one in `../runs/`, and the two are **not interchangeable**. Detector scoring
matches events to transitions on a wall clock, so a log from one session paired
with events from another produces zero matches — the timestamps simply do not
overlap. Keeping the pair together in its own directory is what makes the
scoring reproducible.

The governor event logs for the `../runs/` collection were not retained, which
is why the scoring runs against this one. The grid and cost metrics in
`../summary/` come from `../runs/` and are unaffected.

## Ground truth

Ground truth is the workload schedule, not a power-derived label. `ai_sim_2.py`
prints its own phase transitions (`PREFILL START (req N) t=...`), and those are
causally upstream of the power step, so they are the honest reference for a
lead-time claim. Only `aisim2` is scored: HPL and the step workload carry no
phase log.

Alerts are the governor's `events.jsonl` — `preburn dir=+1` for ballast ramped
ahead of a predicted rise, `dir=-1` ahead of a predicted fall, `engage` for the
RAPL ceiling taking hold. Scoring actuation rather than the detector's own
output is the stricter reading: a detector edge that never moved the actuator
did nothing for the grid.

## Timing precision

`events.jsonl` stamps UTC to the millisecond; the workload log stamps local wall
clock to the second, plus a workload-relative `t` to the millisecond. Every
onset within a run therefore shares one anchor uncertainty of about ±0.5 s,
while relative spacing is exact. **Lead times below ~1 s should not be read as
precise** — which covers nearly all of them.

## Reproducing

```bash
python3 analysis/score_detector.py             # writes data/summary/detector_scores.csv
python3 analysis/score_detector.py --selfcheck
```
