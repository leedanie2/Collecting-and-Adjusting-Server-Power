#!/usr/bin/env python3
"""Lane A (procs_running precursors) + model-class A/B for the 1 Hz predictor.

On ONE procs-inclusive overlap window (telemetry_procs.csv), with identical
walk-forward folds / labels / features, this separates the two questions the
ml_methods_menu addendum says must not be conflated:

  feature effect:  base features        vs  + procs_running & slope   (Lane A)
  model effect:    LogisticRegression   vs  RandomForest              (menu A1)

The deployed OLS daemon is reported alongside as the real-world floor.

Judging rule (from the spec): PR-AUC is the threshold-free headline. For the
operating-point precision/recall/lead-time, every model gets the SAME alarm
budget -- it may flag exactly as many rows as there are spikes (top-K) -- so the
numbers reflect ranking quality, not who flags more often. Class weighting
(menu D1) is on from the first run.

one file, sklearn only (already needed for trees), reuses eval's
label + spike_events. No deep learning -- the menu defers it until trees plateau.
"""
import sys

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from core import telemetry as common
from validation import evaluation as ev
from models.kalman import kalman_filter as kn
from core.spike_labels import DEFAULT_DERIV_THRESHOLD_W, DEFAULT_HORIZON_S, spike_events

CACHE = common.DATA_DIR / "telemetry_procs.csv"
HORIZON_S = DEFAULT_HORIZON_S
LAGS = [0, 1, 2, 3]
SLOPE_S = 3            # trailing window for procs_running slope (3-5 s per spec)
UPSTREAM_LOOKBACK_S = 30
HAWKES_TAU_S = 35.0
USAGE_EDGE_THRESHOLDS = (10.0, 20.0, 40.0)
TRAIN_H = 12           # walk-forward train window (hours)
TEST_H = 3             # test window (hours) -- also the slide step
EMBARGO_S = HORIZON_S  # purge gap so train labels can't peek into test

# KF regime-awareness features (kf=True in feature_frame/build_xy below).
# (q, r) are SEEDS for the online-adaptive KF (kalmannet.run_classical adapt=True):
# the filter re-estimates its noise model from the innovation sequence as more
# diverse load data accumulates, so kf_level/kf_slope stay clean across regimes
# without a per-window re-tune -- item-1 of the 2026-07-01b session. Adaptation
# uses only past innovations, so it stays strictly causal (selfcheck asserts it).
# Seeds are kalmannet.py's tuned 1 Hz result: q=0.1, r=5.0.
KF_Q = 0.1
KF_R = 5.0

# Upstream OS signals wired in as candidate features (session_2026-07-01c). For
# each signal present in the frame we add its raw level (causal, current reading)
# and a trailing SLOPE_S-second delta -- "work is rising" precursor structure,
# same shape as procs_slope. Only nr_running showed a real +1 s lead in the
# cross-corr pre-check; the rest are coincident-or-flat but wired anyway so the
# RF can find nonlinear interactions correlation can't see. Source names/rates:
# common.UPSTREAM_FIELDS. Absent columns are silently skipped so cached-data runs
# (no upstream cols) degrade to base features.
# pdu_watts rides the base query (RENAME), not query_upstream -- adding it to
# UPSTREAM_FIELDS would duplicate the column and break the live hist.join(up).
UPSTREAM_SRC = list(common.UPSTREAM_FIELDS) + ["pdu_watts"]
UPSTREAM_FEATURE_COLS = [c for s in UPSTREAM_SRC for c in (s, f"{s}_d{SLOPE_S}")]
UPSTREAM_FEATURE_COLS += [
    "nr_running_jerk",
    "nr_running_cv",
    "nr_running_peak_to_mean",
    "nr_running_hawkes",
]
PHYSICS_FEATURE_COLS = [c for n in ("freq_sq_x_usage", "freq_cubed", "freq_x_usage")
                        for c in (n, f"{n}_slope30")]
UPSTREAM_FEATURE_COLS += PHYSICS_FEATURE_COLS


def _hawkes_intensity(sig, tau_s=HAWKES_TAU_S):
    """Fixed-kernel self-exciting run-queue arrival intensity.

    Positive one-second increments are marks; intensity decays with the fitted
    30-40 s kernel from the Hawkes checkpoint. Strictly causal and cheap enough
    for both training and live scoring.
    """
    arrivals = sig.diff().clip(lower=0).fillna(0.0).to_numpy(dtype=float)
    decay = float(np.exp(-1.0 / tau_s))
    out = np.empty(len(arrivals), dtype=float)
    acc = 0.0
    for i, mark in enumerate(arrivals):
        acc = acc * decay + mark
        out[i] = acc
    return pd.Series(out, index=sig.index)


def load():
    df = pd.read_csv(CACHE, index_col="_time", parse_dates=["_time"]).sort_index()
    # keep only rows where the modelled signals exist
    need = ["power_watts", "usage_percent", "procs_running"]
    return df.dropna(subset=need)


def feature_frame(df, lane_a=False, kf=False, upstream=False):
    """Causal feature matrix (no label, no NaN drop). Shared by the eval and the
    live daemon so both compute features identically. Lane A defaults off (CP3:
    procs_running is dead weight); kept as a flag for A/B reproducibility.

    kf=True adds kf_level/kf_slope: an online-adaptive KF (kalmannet.ClassicalKF,
    adapt=True, seeded at KF_Q/KF_R) run causally over power_watts. Its noise
    model improves as load diversity accumulates. Regime-awareness only -- tells the
    RF "still falling from a peak" vs "settled", NOT a Tau/settle-level predictor
    (characterize_overshoot.py already covers that statically). At 1 Hz these
    can't resolve the sub-second PL2->PL1 transition timing (same Nyquist floor
    as everything else here); see Model/status_2026-06-30.md.
    """
    power, usage = df["power_watts"], df["usage_percent"]
    cols = {}
    for lag in LAGS:
        cols[f"power_lag{lag}"] = power.shift(lag)
        cols[f"usage_lag{lag}"] = usage.shift(lag)
    usage_d1 = usage - usage.shift(1)
    usage_d3 = usage - usage.shift(SLOPE_S)
    cols["usage_d1"] = usage_d1
    cols[f"usage_d{SLOPE_S}"] = usage_d3
    cols[f"usage_slope{SLOPE_S}"] = usage_d3 / SLOPE_S
    cols["usage_abs_d1"] = usage_d1.abs()
    for thr in USAGE_EDGE_THRESHOLDS:
        tag = int(thr)
        prev = usage.shift(1)
        cols[f"usage_cross_up_{tag}"] = (
            (usage >= thr) & (prev < thr)
        ).astype(float)
        cols[f"usage_cross_down_{tag}"] = (
            (usage <= thr) & (prev > thr)
        ).astype(float)
    # Thermal slope: 30-second trailing delta (precursor_screen_extended confirmed +4.7% PR-AUC)
    if "temp_celsius" in df.columns:
        temp = df["temp_celsius"].ffill()
        cols["temp_celsius_slope"] = temp.diff(periods=30) / 30.0
    if lane_a:
        procs = df["procs_running"]
        cols["procs_running"] = procs                         # raw level, lag 0
        cols["procs_slope"] = (procs - procs.shift(SLOPE_S)) / SLOPE_S  # trailing slope
    if kf:
        # KF is a recursion -- a NaN reading would corrupt every state after it,
        # not just that row. Run over the clean subsequence only, reindex back
        # (leaving NaN where power itself was missing, same as the lag columns).
        clean = power.dropna()
        out = kn.run_classical(clean.to_numpy(dtype=float), KF_Q, KF_R, adapt=True)
        cols["kf_level"] = pd.Series(out["level"], index=clean.index).reindex(df.index)
        cols["kf_slope"] = pd.Series(out["slope"], index=clean.index).reindex(df.index)
    if upstream:
        # raw level (lag 0) + trailing SLOPE_S delta for each upstream signal
        # present in df. Both are strictly causal (t and t-SLOPE_S only).
        for s in UPSTREAM_SRC:
            if s in df.columns:
                # ffill so a transient outage in ONE upstream stream (e.g. the
                # 10 Hz nr_running daemon bouncing) can't NaN-drop the whole row
                # and starve the base model too. Causal (past values only); leading
                # NaN before the first reading stays NaN and is dropped normally.
                sig = df[s].ffill()
                cols[s] = sig
                cols[f"{s}_d{SLOPE_S}"] = sig - sig.shift(SLOPE_S)
                if s == "freq_mhz":
                    # DVFS physics: P ~ C*V^2*f*act with V~f -> f^2*act / f^3.
                    # 30 s slope (not _d3): the ablation gain was measured there.
                    ghz = sig / 1000.0
                    u = usage.ffill()
                    for n, phys in {"freq_sq_x_usage": ghz**2 * u,
                                    "freq_cubed": ghz**3,
                                    "freq_x_usage": ghz * u}.items():
                        cols[n] = phys
                        cols[f"{n}_slope30"] = (
                            phys - phys.shift(UPSTREAM_LOOKBACK_S)
                        ) / UPSTREAM_LOOKBACK_S
                if s == "nr_running":
                    d1 = sig.diff()
                    roll = sig.rolling(window=UPSTREAM_LOOKBACK_S,
                                       min_periods=UPSTREAM_LOOKBACK_S)
                    cols["nr_running_jerk"] = d1.rolling(
                        UPSTREAM_LOOKBACK_S,
                        min_periods=UPSTREAM_LOOKBACK_S,
                    ).std()
                    cols["nr_running_cv"] = roll.std() / (roll.mean() + 1e-6)
                    cols["nr_running_peak_to_mean"] = roll.max() / (roll.mean() + 1e-6)
                    cols["nr_running_hawkes"] = _hawkes_intensity(sig)
    return pd.DataFrame(cols, index=df.index)


def build_xy(df, lane_a, horizon=HORIZON_S, kf=False, upstream=False,
             direction="both"):
    """Causal feature matrix X and 0/1 label y (spike in next `horizon` s)."""
    X = feature_frame(df, lane_a, kf=kf, upstream=upstream)
    y = ev.triple_barrier_label(df["power_watts"], horizon, direction=direction)
    keep = X.notna().all(axis=1) & y.notna()
    return X[keep], y[keep].astype(int)


def _model(kind):
    if kind == "logreg":
        # scale + balanced logistic = the linear classifier floor
        return ("scale", LogisticRegression(max_iter=1000, class_weight="balanced"))
    if kind == "rf":
        return (None, RandomForestClassifier(
            n_estimators=200, max_depth=None, min_samples_leaf=20,
            class_weight="balanced", n_jobs=-1, random_state=0))
    raise ValueError(kind)


def _folds(index, train_h, test_h):
    train_td, test_td = pd.Timedelta(hours=train_h), pd.Timedelta(hours=test_h)
    t = index[0] + train_td
    while t + test_td <= index[-1] + pd.Timedelta(seconds=1):
        yield t, t + test_td
        t += test_td


def wf_classify(df, lane_a, kind, train_h=TRAIN_H, test_h=TEST_H, horizon=HORIZON_S):
    """Walk-forward classification; returns (y_true, y_score, timestamps) pooled
    over all test rows. Strictly causal with an embargo (=horizon) purge before
    each test window so a train row's forward-looking label can't see test data."""
    X, y = build_xy(df, lane_a, horizon)
    embargo = horizon
    scaler_kind, _ = _model(kind)
    yt, ys, ts = [], [], []
    for t0, t1 in _folds(X.index, train_h, test_h):
        tr = (X.index < t0 - pd.Timedelta(seconds=embargo))
        te = (X.index >= t0) & (X.index < t1)
        Xtr, ytr = X[tr], y[tr]
        Xte, yte = X[te], y[te]
        if ytr.sum() < 5 or yte.sum() < 1:
            continue  # need both classes to fit/score
        scaler, model = _model(kind)
        Xtr_v, Xte_v = Xtr.to_numpy(), Xte.to_numpy()
        if scaler == "scale":
            sc = StandardScaler().fit(Xtr_v)
            Xtr_v, Xte_v = sc.transform(Xtr_v), sc.transform(Xte_v)
        model.fit(Xtr_v, ytr.to_numpy())
        proba = model.predict_proba(Xte_v)[:, 1]
        yt.append(yte.to_numpy()); ys.append(proba); ts.append(Xte.index.to_numpy())
    if not yt:
        return None
    return np.concatenate(yt), np.concatenate(ys), np.concatenate(ts)


def _lead_times(spike_ts, flagged_true_ts):
    """Seconds from each true-positive flag to the next actual spike (<=horizon).
    Pandas Timestamp subtraction -> resolution- and tz-agnostic."""
    out = []
    s = pd.DatetimeIndex(spike_ts).sort_values()
    for t in pd.DatetimeIndex(flagged_true_ts).sort_values():
        i = s.searchsorted(t, side="right")
        if i < len(s):
            lt = (s[i] - t).total_seconds()
            if 0 < lt <= HORIZON_S:
                out.append(float(lt))
    return out


def evaluate(df, lane_a, kind):
    res = wf_classify(df, lane_a, kind)
    if res is None:
        return None
    y_true, y_score, ts = res
    n_spikes = int(y_true.sum())

    # same alarm budget for everyone: flag the top-n_spikes scores
    order = np.argsort(-y_score)
    flag = np.zeros_like(y_true, dtype=bool)
    flag[order[:n_spikes]] = True

    tp = int((flag & (y_true == 1)).sum())
    precision = tp / flag.sum() if flag.sum() else float("nan")
    recall = tp / n_spikes if n_spikes else float("nan")

    spike_all = spike_events(df["power_watts"], DEFAULT_DERIV_THRESHOLD_W)
    spike_ts = spike_all.index[spike_all].to_numpy()
    flagged_true_ts = ts[flag & (y_true == 1)]
    lts = _lead_times(spike_ts, flagged_true_ts)
    # honest lead: same flags, but measured to the raw step onset instead of the
    # label's (EWMA-lagged) confirmation time. See eval.true_onset_ts.
    lts_true = _lead_times(ev.true_onset_ts(df["power_watts"]), flagged_true_ts)

    return {
        "pr_auc": ev._pr_auc(y_true.astype(float), y_score),
        "precision": precision,
        "recall": recall,
        "lead_time_median_s": float(np.median(lts)) if lts else float("nan"),
        "lead_true_median_s": float(np.median(lts_true)) if lts_true else float("nan"),
        "n_tp_onset": len(lts_true),
        "n_spikes": n_spikes,
        "n_flags": int(flag.sum()),
        "n_tp": tp,
        "n_rows": len(y_true),
    }


def _row(name, s):
    if s is None:
        return f"{name:<22} (no valid folds)"
    return (f"{name:<22} {s['pr_auc']:.4f}   {s['precision']:.4f}   "
            f"{s['recall']:.4f}   {s['lead_time_median_s']:>4.1f}   "
            f"{s['lead_true_median_s']:>6.1f}   "
            f"{s['n_tp_onset']:>4}/{s['n_tp']:<4}   {s['n_tp']:>5}/{s['n_spikes']:<6}")


def run():
    df = load()
    rate = ev.triple_barrier_label(df["power_watts"], HORIZON_S).mean()
    print(f"Loaded {len(df)} rows  {df.index.min()} -> {df.index.max()}")
    print(f"Label rate (random PR-AUC floor) = {rate:.4f}\n")

    # deployed OLS daemon on the SAME window, rebaselined (real-world floor)
    print("Rebaselining deployed OLS daemon on overlap window ...")
    ols = ev.baseline_score(df)

    print(f"\n{'model / features':<22} {'PR-AUC':>6}   {'prec':>6}   {'recall':>6}   {'lead':>4}   "
          f"{'leadTR':>6}   {'onTP/TP':>7}   {'TP/spikes':>11}")
    print("-" * 96)
    print(_row("OLS daemon (deployed)", ols))
    print(_row("LogReg base", evaluate(df, False, "logreg")))
    print(_row("LogReg +LaneA", evaluate(df, True, "logreg")))
    print(_row("RF base", evaluate(df, False, "rf")))
    print(_row("RF +LaneA", evaluate(df, True, "rf")))
    print("-" * 96)
    print("PR-AUC is the headline (threshold-free). prec/recall/lead at equal "
          "alarm budget\n(top-K=n_spikes) for the classifiers; OLS at its own "
          "deployed mean+K*std threshold.")
    print("lead   = OLD metric: flag -> next frozen-label event (EWMA confirm lags the "
          "step, inflating lead).\nleadTR = HONEST metric: flag -> next raw step onset "
          "(upper bound; onTP/TP = TP flags that\npreceded any real onset at all — the "
          "rest were nowcasting an already-running spike).")


def selfcheck():
    df = load().head(60_000)  # a slice is enough to exercise the paths
    X0, y0 = build_xy(df, False)
    X1, y1 = build_xy(df, True)
    Xr, yr = build_xy(df, False, direction="rise")
    Xd, yd = build_xy(df, False, direction="drop")
    assert "procs_running" not in X0.columns and "procs_slope" not in X0.columns
    assert "procs_running" in X1.columns and "procs_slope" in X1.columns
    assert X1.shape[1] == X0.shape[1] + 2, "Lane A should add exactly 2 columns"
    assert set(y1.unique()).issubset({0, 1}) and y1.sum() > 0, "need positive labels"
    assert list(Xr.columns) == list(X0.columns) and list(Xd.columns) == list(X0.columns)
    assert set(yr.unique()).issubset({0, 1}) and set(yd.unique()).issubset({0, 1})
    # causality: a feature row at time t must not depend on any future row.
    # procs_slope at t uses t and t-SLOPE_S only -> check it equals manual calc.
    t = X1.index[100]
    man = (df["procs_running"].loc[t] - df["procs_running"].shift(SLOPE_S).loc[t]) / SLOPE_S
    assert abs(X1["procs_slope"].loc[t] - man) < 1e-9, "slope not causal/trailing"
    # one tiny fold to prove the model path runs end-to-end
    res = wf_classify(df, True, "logreg", train_h=4, test_h=2)
    assert res is not None, "no folds produced on 60k-row slice"
    yt, ys, _ = res
    assert len(yt) == len(ys) and np.isfinite(ys).all(), "bad scores"

    # kf=True: regime-awareness columns present, finite, and causal (a row at
    # time t only depends on power up to and including t, never the future).
    Xk, yk = build_xy(df, False, kf=True)
    assert {"kf_level", "kf_slope"} <= set(Xk.columns)
    assert Xk.shape[1] == X0.shape[1] + 2, "kf=True should add exactly 2 columns"
    assert np.isfinite(Xk[["kf_level", "kf_slope"]].to_numpy()).all(), "non-finite KF features"
    cutoff = Xk.index[len(Xk) // 2]
    full_val = Xk.loc[cutoff, "kf_level"]
    truncated = feature_frame(df[df.index <= cutoff], kf=True)
    assert abs(truncated.loc[cutoff, "kf_level"] - full_val) < 1e-9, \
        "kf_level depends on future rows -- not causal"

    # upstream=True: cache has no upstream cols, so synthesize two of them and
    # assert each adds level + trailing-slope, that the slope is causal, and that
    # missing upstream signals are silently skipped (degrade to base).
    dfu = df.copy()
    dfu["nr_running"] = np.arange(len(dfu), dtype=float)      # ramp -> slope == 1
    dfu["load1"] = df["usage_percent"].to_numpy() / 100.0
    Xu = feature_frame(dfu, upstream=True)
    for s in ("nr_running", "load1"):
        assert s in Xu.columns and f"{s}_d{SLOPE_S}" in Xu.columns, f"missing {s} upstream cols"
    assert "mem_used" not in Xu.columns, "absent upstream signal should be skipped"
    t = Xu.index[100]
    assert abs(Xu.loc[t, f"nr_running_d{SLOPE_S}"] - float(SLOPE_S)) < 1e-9, \
        "nr_running slope not the trailing SLOPE_S delta / not causal"

    print(f"selfcheck OK: base={X0.shape[1]} cols, laneA={X1.shape[1]} cols, "
          f"kf={Xk.shape[1]} cols, upstream(+2 sig)={Xu.shape[1]} cols, "
          f"{len(yt)} test rows scored")


def main():
    if "--selfcheck" in sys.argv:
        selfcheck()
        return
    run()


if __name__ == "__main__":
    main()
