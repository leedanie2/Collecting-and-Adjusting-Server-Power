# Champion–challenger retraining: notes on why it did not work

Read against `data/trainer_metrics.jsonl` (20 cycles, 2026-07-07 → 2026-07-23)
and `data/rf_continual_metrics.jsonl` (the retired on-box loop). The design
itself is documented in [`README.md`](README.md#off-box-retraining-championchallenger);
this file is only the post-mortem.

Short version: the loop failed on **variance under nonstationarity**, not on
overfitting. There was one genuine leak, it was found and fixed, and fixing it
did not rescue the loop — because the deeper problem is that a 6 h window is not
enough data to estimate this model, and the gate judging it saw 1–4 events.

## It trained on new data only

Worth stating plainly, because it shapes everything below. Each cycle fits a
**fresh forest on a single 6 h slice** and discards everything else:

- `trainer.py:360` pulls 72 h, but `select_viable_gate_data` (`trainer.py:172`)
  returns `X_train` from **one** fold. Hence `train_samples: 21570` in most log
  rows — 6 h at 1 Hz is 21600 samples.
- `train_rf` (`continual_detector.py:87`) calls `rf.new_forest()` and `.fit()`
  from scratch. No warm start, no accumulated rows, no decayed history. The
  other 66 h is used only for fold construction, the advisory rise/drop heads,
  and onset timestamps.
- The only channel through which the past enters is `hard_example_weights`
  (`continual_detector.py:107`), and even that scores the incumbent on *the
  current window's* rows and upweights its errors 4×. It reweights new data; it
  never revisits old data.

So this is a sliding-window refit with a no-regression gate, not continual
learning in the accumulating sense. With a base rate this unstable, that choice
makes things worse, not better: there is no history to average the regime shift
against.

## The one real overfitting instance, and why it isn't the answer

It existed and it is documented in the code. Before 2026-07-08 the gate scored
on `folds[-1][0]` — the last fold's **train** window, 4 of whose 6 hours lay
inside the challenger's own training data, so the gate "largely measured
memorization and was biased toward promoting challengers"
(`trainer.py:141-149`). Textbook leakage.

But model capacity does not support overfitting as the root cause.
`new_forest()` is `n_estimators=200, min_samples_leaf=20, class_weight="balanced"`
with OOB scoring — a leaf floor of 20 on 21k rows is well regularised. And the
promotion record after the fix is the real tell: promotions *continued* for
another week, but on nothing. 2026-07-13 promoted a challenger at
`pr_auc 0.00406` over a champion at `0.00391`, both with `recall: 0.0` — a
0.00015 PR-AUC margin in a window where neither model detected anything. The
two 07-15 promotions cleared margins of 0.019 and 0.005 against a champion
scoring `recall: 0.0`.

Those are all the PR-AUC fallback branch of `beats()`. Once event-level metrics
entered the log and became the primary criterion (2026-07-20 onward), the loop
promoted **zero times in nine cycles**. Eight promotions in week one, none
after.

## What actually broke it

**1. The gate judges on almost nothing.** `gate_events` across every cycle that
logs it: 13, 1, 2, 2, 2, 2, 4. `beats()` compares `event_recall` first
(`continual_detector.py:161`). Deciding promotion on one or two physical onsets
is a coin flip with extra steps.

**2. The threshold moves more than the model does.** Within 13 minutes on
2026-07-20 the calibrated threshold ran 0.737 → 0.603 → 0.598 → 0.483, and by
07-23 it sat on the `MIN_THRESHOLD = 0.35` floor. The *pre*-calibration
`base_threshold` was itself jumping (0.737, 0.759, 0.729, 0.734, 0.461), so
this is per-window OOB scale drift, not a policy responding to real change.
Champion and challenger are therefore compared at independently recalibrated
operating points — the gate is measuring calibration noise as much as model
quality. It shows: challenger `alert_duty: 0.0` / `event_recall: 0.0` against
champion `alert_duty: 0.14–0.38` and 84–160 false alerts/hour. Those are not
two models, they are two thresholds.

**3. The label is wildly nonstationary between windows.** `train_positives` by
cycle: ~35–41 through 07-13, then **1425**, then 595, 632, 224, 129, 70, ~190.
`eval_positives`: 37, 35, then **1601**. A 40× swing in base rate across
adjacent 6 h slices means each refit is fitting a materially different problem,
and a threshold calibrated on one window is meaningless on the next.

## The gate rejected its best challenger

The 2026-07-23 cycle is worth singling out, because it is not a variance story
— it is a policy bug.

| | challenger | champion |
|---|---|---|
| event recall | 1.0 (4/4) | 1.0 (4/4) |
| alert duty | 0.027 | 0.379 |
| false alerts/h | 9.0 | 159.6 |
| event latency | −2.5 s | −5.0 s |

`promoted: false`. Trace `beats()`: recall ties, `duty_better` is true, but
`lat_not_worse` requires `clat <= plat + 0.5` — i.e. `−2.5 <= −4.5` — which
fails, and latency vetoes the promotion. A challenger that cut false alarms
**18×** at identical event recall was rejected over 2.5 s of nominal lead time.

And that lead time is an artifact. The champion is alarmed 38% of the time; at
that duty it is "early" to almost everything by accident. The latency
comparison is not duty-normalised, so near-continuous alarming reads to the
gate as lead time and is then protected by the no-regression rule. This is the
clearest evidence that the loop's later rejections were not all the gate
correctly refusing bad models.

## Verdict

A fresh 200-tree forest fit on 6 hours holding 35–200 positive rows and a
handful of onsets, thresholded by OOB calibration on that same idiosyncratic
window, is a high-variance estimate. The gate then adjudicates it on 1–4
events, at two different operating points, with a latency criterion that
rewards constant alarming. Nothing in that pipeline had the statistical power
to detect a real improvement, so the safe outcome — keep the champion — is the
one it converged to.

The gate did do its job in the narrow sense: it went strictly causal, it
stopped rubber-stamping, and it never deployed a model that had been shown
worse. It just could not manufacture a good one, which is consistent with the
larger finding in [`README.md`](README.md) — the shipped forest was ultimately
beaten by `usage_edge`, two hand-set thresholds and no training at all,
responding in ~0.1 s against the forest's 10–20 s.

## If it were picked up again

- Accumulate, or exponentially decay, prior windows instead of refitting on 6 h
  in isolation. The base-rate swing is the argument for this, not against it.
- Hold the operating point fixed across champion and challenger when gating.
  Compare models, not thresholds.
- Gate on pooled events across many cycles, not per-cycle *n* = 1–4. Nothing
  else fixes the sample size.
- Duty-normalise the latency criterion, or drop it in favour of a single
  cost-weighted score. As written it protects the noisiest incumbent available.

## The on-box loop failed differently

`detectors/random_forest/continual_detector.py` is retired for a physical
reason, not a statistical one: retraining on the instrumented host burned
enough CPU to create power spikes of its own, which then fed back into the
detector's training data. Moving training off-box fixed *that*. It fixed none
of the above.
