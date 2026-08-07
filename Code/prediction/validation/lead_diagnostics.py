#!/usr/bin/env python3
"""Checkpoint 4 -- lead-time diagnosis: is RF's skill FORECASTING or NOWCASTING?

Checkpoint 3 showed RF PR-AUC 0.68 but a lone |dP/dt| feature already scores
0.53, and lead-time is flat at 2 s. This decides whether genuine multi-second
precursor structure exists at 1 Hz, or the skill is just persistence (the
pre-spike second already looks like the spike). Three decisive tests:

  1. PR-AUC-vs-horizon: RF vs trivial |dP/dt| on IDENTICAL walk-forward test
     rows. If RF's margin over |dP/dt| holds/grows as horizon lengthens ->
     real precursor. If both collapse toward random together -> nowcasting.
  2. Isolated-onset lift: re-score RF against only isolated spike onsets (no
     spike in the prior GAP s). Skill survives -> foresight; collapses ->
     persistence.
  3. Per-event lead: fraction of spike onsets that get >=1 flag BEFORE onset,
     and the lead distribution (reactive |dP/dt| baseline = 0 s lead always).

read-only analysis on the existing procs cache, reuses lane_a +
precursor_screen. No model changes, no new deps.
"""
import sys

import numpy as np
import pandas as pd

from validation import evaluation as ev
from core import features as la
from core.spike_labels import (
    DEFAULT_DERIV_THRESHOLD_W as DTHR,
    spike_events,
    spike_in_horizon,
)

HORIZONS = (1, 2, 3, 5, 8, 12, 20)
ISO_GAP_S = 10  # an onset is "isolated" if no spike in the prior ISO_GAP_S s
TEST_H = 12     # coarse folds (~6) -> few refits per horizon; trend, not 3rd decimal


def _trivial_score(df):
    """Reactive detector signal known at time t: last second's |dP/dt|."""
    return df["power_watts"].diff().abs()


def _sweep(df, horizon):
    """One RF walk-forward at a given horizon -> (yt(float), ys, ts)."""
    res = la.wf_classify(df, lane_a=False, kind="rf", horizon=horizon, test_h=TEST_H)
    if res is None:
        return None
    yt, ys, ts = res
    return yt.astype(float), ys, ts


def horizon_curve(df, cache):
    """RF vs trivial |dP/dt|, same test rows, across horizons. Reports PR-AUC and
    lift over the random floor (= label rate) so different horizons compare.
    Stores each sweep in `cache` so the later tests reuse the horizon-5 fit."""
    triv = _trivial_score(df)
    print(f"{'H(s)':>4} {'rate':>7} {'triv_PA':>8} {'triv_lift':>10} "
          f"{'RF_PA':>7} {'RF_lift':>8} {'RF-triv':>8}")
    for H in HORIZONS:
        res = _sweep(df, H)
        cache[H] = res
        if res is None:
            print(f"{H:>4}  (no folds)"); continue
        yt, ys, ts = res
        rate = yt.mean()
        rf = ev._pr_auc(yt, ys)
        tv = triv.reindex(pd.DatetimeIndex(ts)).to_numpy()
        ok = np.isfinite(tv)
        tp = ev._pr_auc(yt[ok], tv[ok])
        print(f"{H:>4} {rate:>7.4f} {tp:>8.4f} {tp/rate:>10.2f} "
              f"{rf:>7.4f} {rf/rate:>8.2f} {rf-tp:>8.4f}")


def isolated_onsets(power, gap=ISO_GAP_S):
    """Boolean Series: spike onsets with no spike in the prior `gap` seconds."""
    ev_flags = spike_events(power, DTHR)
    prior = ev_flags.shift(1).rolling(gap, min_periods=1).max().fillna(0) > 0
    return ev_flags & ~prior


def isolated_lift(df, sweep, horizon=la.HORIZON_S, gap=ISO_GAP_S):
    """RF (trained on the normal label) re-scored against the isolated-onset
    label. If lift collapses vs the all-spike label, the skill was persistence."""
    yt, ys, ts = sweep
    ts_idx = pd.DatetimeIndex(ts)

    all_lbl = yt.astype(float)
    iso = isolated_onsets(df["power_watts"], gap)
    iso_lbl_full = (spike_in_horizon(iso, horizon) > 0).astype(int)
    iso_lbl = iso_lbl_full.reindex(ts_idx).fillna(0).to_numpy().astype(float)

    triv = _trivial_score(df).reindex(ts_idx).to_numpy()
    ok = np.isfinite(triv)

    def line(name, lbl):
        rate = lbl.mean()
        rf = ev._pr_auc(lbl, ys)
        tp = ev._pr_auc(lbl[ok], triv[ok])
        print(f"  {name:<20} rate={rate:.4f}  RF_PA={rf:.4f} (lift {rf/rate:.2f})"
              f"  triv_PA={tp:.4f} (lift {tp/rate:.2f})")

    print(f"Isolated-onset test (horizon={horizon}s, gap={gap}s):")
    line("all spikes", all_lbl)
    line("isolated onsets", iso_lbl)


def per_event_lead(df, sweep, horizon=la.HORIZON_S, gap=ISO_GAP_S):
    """For each spike onset, did RF flag in [t-horizon, t-1]? Lead = onset minus
    earliest pre-onset flag. Reactive baseline lead = 0 s by definition."""
    yt, ys, ts = sweep
    ts_idx = pd.DatetimeIndex(ts)

    # operating point = equal alarm budget (top-K = #positives), as in lane_a
    k = int(yt.sum())
    thr = np.sort(ys)[-k] if k > 0 else np.inf
    flagged = ts_idx[ys >= thr].sort_values()

    onset = isolated_onsets(df["power_watts"], gap)
    onset_ts = onset.index[onset]
    # only onsets whose pre-window is inside test coverage
    lo, hi = ts_idx.min(), ts_idx.max()
    onset_ts = onset_ts[(onset_ts > lo + pd.Timedelta(seconds=horizon)) & (onset_ts <= hi)]

    leads, warned = [], 0
    fa = flagged.to_numpy()
    for t in onset_ts:
        w0 = t - pd.Timedelta(seconds=horizon)
        i = flagged.searchsorted(w0, side="left")
        j = flagged.searchsorted(t, side="left")  # strictly before onset
        if j > i:
            warned += 1
            first = pd.Timestamp(fa[i])
            leads.append((t - first).total_seconds())
    n = len(onset_ts)
    print(f"Per-event lead (isolated onsets, horizon={horizon}s):")
    print(f"  onsets evaluated      {n}")
    print(f"  warned before onset   {warned} ({warned/n:.1%})" if n else "  no onsets")
    if leads:
        print(f"  lead median / max     {np.median(leads):.1f}s / {max(leads):.1f}s")
        print(f"  (reactive |dP/dt| baseline lead = 0.0s for every event)")


def isolated_trained(df, horizon=la.HORIZON_S, gap=ISO_GAP_S):
    """Confirmatory: train RF DIRECTLY on the isolated-onset label (not the
    all-spike label) and score it on the same. If lift is still ~1-2x, cold
    onsets are genuinely unpredictable at 1 Hz -- not merely unlearned."""
    from sklearn.ensemble import RandomForestClassifier
    iso = isolated_onsets(df["power_watts"], gap)
    iso_lbl = (spike_in_horizon(iso, horizon) > 0).astype(int)
    X, _ = la.build_xy(df, lane_a=False, horizon=horizon)
    y = iso_lbl.reindex(X.index).fillna(0).astype(int)

    yt, ys = [], []
    for t0, t1 in la._folds(X.index, la.TRAIN_H, TEST_H):
        tr = X.index < t0 - pd.Timedelta(seconds=horizon)
        te = (X.index >= t0) & (X.index < t1)
        if y[tr].sum() < 5 or y[te].sum() < 1:
            continue
        m = RandomForestClassifier(n_estimators=200, min_samples_leaf=20,
                                   class_weight="balanced", n_jobs=-1, random_state=0)
        m.fit(X[tr].to_numpy(), y[tr].to_numpy())
        yt.append(y[te].to_numpy())
        ys.append(m.predict_proba(X[te].to_numpy())[:, 1])
    yt, ys = np.concatenate(yt).astype(float), np.concatenate(ys)
    rate = yt.mean()
    print(f"RF trained ON isolated onsets: rate={rate:.4f}  "
          f"PR-AUC={ev._pr_auc(yt, ys):.4f} (lift {ev._pr_auc(yt, ys)/rate:.2f})")


def run():
    df = la.load()
    print(f"Loaded {len(df)} rows  {df.index.min()} -> {df.index.max()}\n")
    cache = {}
    print("=== 1. PR-AUC vs horizon (RF base vs trivial |dP/dt|, same rows) ===")
    horizon_curve(df, cache)
    sweep5 = cache.get(la.HORIZON_S) or _sweep(df, la.HORIZON_S)
    print("\n=== 2. Isolated-onset lift ===")
    isolated_lift(df, sweep5)
    print("\n=== 3. Per-event lead vs reactive baseline ===")
    per_event_lead(df, sweep5)


def selfcheck():
    df = la.load().head(40_000)
    iso = isolated_onsets(df["power_watts"], gap=ISO_GAP_S)
    ev_flags = spike_events(df["power_watts"], DTHR)
    assert iso.sum() <= ev_flags.sum(), "isolated onsets must be subset of spikes"
    assert iso.sum() > 0, "expected some isolated onsets"
    # an isolated onset must have no spike in the prior gap
    t = iso.index[iso][0]
    win = ev_flags.loc[t - pd.Timedelta(seconds=ISO_GAP_S):t - pd.Timedelta(seconds=1)]
    assert not win.any(), "isolated onset has a spike in its prior window"
    # trivial score is causal (depends only on current+prior power)
    triv = _trivial_score(df)
    assert triv.isna().iloc[0] and np.isfinite(triv.iloc[1]), "diff score shape"
    print(f"selfcheck OK: {ev_flags.sum()} spikes, {iso.sum()} isolated onsets")


def main():
    if "--selfcheck" in sys.argv:
        selfcheck()
        return
    run()


if __name__ == "__main__":
    main()
