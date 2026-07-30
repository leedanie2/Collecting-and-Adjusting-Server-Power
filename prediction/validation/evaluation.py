#!/usr/bin/env python3
"""Walk-forward eval scoreboard for the 1 Hz spike predictor.

single file, four public functions, no new deps. Spike label and
spike-events reused from precursor_screen — not reimplemented here.
"""

import sys

import numpy as np
import pandas as pd

from core import telemetry as common
from detectors.ols import legacy_detector as spike_daemon
from core.onsets import find_onsets
from core.spike_labels import (
    DEFAULT_DERIV_THRESHOLD_W,
    DEFAULT_HORIZON_S,
    signed_spike_events,
    spike_events,
    spike_in_horizon,
)


def triple_barrier_label(power, horizon_s=DEFAULT_HORIZON_S, direction="both"):
    """Return 0/1 Series: 1 if a spike occurs in the next horizon_s samples.

    Wraps the frozen spike definition. direction="both" preserves historical
    behavior; "rise" and "drop" expose the signed event labels used by the
    auxiliary detector heads.
    """
    events = signed_spike_events(power, DEFAULT_DERIV_THRESHOLD_W, direction)
    return (spike_in_horizon(events, horizon_s) > 0).astype(int)


def true_transition_ts(power, direction="rise", min_step=DEFAULT_DERIV_THRESHOLD_W,
                       refractory=1):
    """Timestamps of TRUE signed transitions: raw single-sample power jumps.

    This is NOT the frozen label's confirmation time: the label's EWMA-z arm
    keeps firing for seconds after the step, so measuring lead against "next
    label event" credits reactive flags with apparent lead. Each onset here is
    stamped at the first post-jump sample; the physical step happened up to one
    sample interval earlier, so a lead measured against these timestamps is an
    UPPER bound on honest lead.
    """
    p = power.dropna()
    vals = p.to_numpy(dtype=float)
    if direction == "drop":
        vals = -vals
    elif direction != "rise":
        raise ValueError(f"unknown transition direction: {direction}")
    idx = np.asarray(find_onsets(vals, busy=-np.inf,
                                 dip_max=np.inf, min_step=min_step,
                                 refractory=refractory), dtype=int)
    return p.index.to_numpy()[idx + 1]


def true_onset_ts(power, min_step=DEFAULT_DERIV_THRESHOLD_W, refractory=1):
    """Timestamps of TRUE spike onsets: raw upward power jumps."""
    return true_transition_ts(power, "rise", min_step=min_step,
                              refractory=refractory)


def true_drop_ts(power, min_step=DEFAULT_DERIV_THRESHOLD_W, refractory=1):
    """Timestamps of TRUE drop onsets: raw downward power jumps."""
    return true_transition_ts(power, "drop", min_step=min_step,
                              refractory=refractory)


def walk_forward_folds(df, train_min=10, step_s=60):
    """Yield strictly-causal (train_df, test_df) pairs.

    train window = last train_min minutes before each fold boundary.
    test  window = next step_s seconds.
    Skips folds where train has < 30 rows (matches spike_daemon's floor).
    """
    train_td = pd.Timedelta(minutes=train_min)
    step_td = pd.Timedelta(seconds=step_s)
    t = df.index[0] + train_td
    end = df.index[-1]
    while t <= end:
        train = df.loc[(df.index >= t - train_td) & (df.index < t)]
        test = df.loc[(df.index >= t) & (df.index < t + step_td)]
        if len(train) >= 30 and not test.empty:
            yield train, test
        t += step_td


def _pr_auc(y_true, y_score):
    """Average precision (area under PR curve) via step-function integration.
    Matches sklearn's average_precision_score formula without the dependency.
    """
    order = np.argsort(-y_score)
    yt = y_true[order]
    n_pos = int(yt.sum())
    if n_pos == 0:
        return float("nan")
    tp = np.cumsum(yt)
    fp = np.cumsum(1 - yt)
    prec = tp / (tp + fp)
    rec = tp / n_pos
    # prepend (recall=0, precision=1) — standard AP convention
    prec = np.concatenate([[1.0], prec])
    rec = np.concatenate([[0.0], rec])
    return float(np.sum(np.diff(rec) * prec[1:]))


def score(y_true, y_pred_flag, lead_times=None, lead_times_true=None):
    """PR-AUC + precision/recall at threshold=0 + median lead-time.

    y_pred_flag: continuous score (pred - threshold from daemon); flag = score > 0.
    lead_times:  list of seconds-to-spike for each true-positive prediction
                 (pre-computed by baseline_score from fold timestamps).
    lead_times_true: same, but measured to the TRUE step onset (true_onset_ts)
                 instead of the label's confirmation time — the honest metric.
    """
    y_true = np.asarray(y_true, dtype=float)
    y_score = np.asarray(y_pred_flag, dtype=float)
    valid = np.isfinite(y_score) & np.isfinite(y_true)
    y_true, y_score = y_true[valid], y_score[valid]

    pa = _pr_auc(y_true, y_score)

    flag = y_score > 0
    tp = int((flag & (y_true > 0)).sum())
    fp = int((flag & (y_true == 0)).sum())
    fn = int((~flag & (y_true > 0)).sum())
    precision = tp / (tp + fp) if (tp + fp) > 0 else float("nan")
    recall    = tp / (tp + fn) if (tp + fn) > 0 else float("nan")

    lt_median = float(np.median(lead_times)) if lead_times else float("nan")

    return {
        "pr_auc": pa,
        "precision": precision,
        "recall": recall,
        "lead_time_median_s": lt_median,
        "lead_true_median_s": float(np.median(lead_times_true)) if lead_times_true else float("nan"),
        "n_tp_onset": len(lead_times_true) if lead_times_true else 0,
        "n_spikes": int((y_true > 0).sum()),
        "n_flags": int(flag.sum()),
        "n_tp": tp,
    }


def alert_episodes(alert_ts, merge_gap_s=2.0):
    """Collapse alert timestamps into episodes.

    Returns a list of (start_ts, end_ts, n_points). The gap is deliberately in
    seconds rather than samples so this works for both 1 Hz offline scoring and
    slower live scorer cadences when configured by the caller.
    """
    idx = pd.DatetimeIndex(alert_ts).dropna().sort_values()
    if len(idx) == 0:
        return []
    episodes = []
    start = prev = idx[0]
    n = 1
    for t in idx[1:]:
        if (t - prev).total_seconds() <= merge_gap_s:
            prev = t
            n += 1
            continue
        episodes.append((start, prev, n))
        start = prev = t
        n = 1
    episodes.append((start, prev, n))
    return episodes


def event_alert_score(event_ts, alert_ts, start=None, end=None,
                      lead_s=DEFAULT_HORIZON_S, lag_s=DEFAULT_HORIZON_S,
                      merge_gap_s=2.0):
    """Event-level detector score.

    Event recall answers: "for each real spike onset, did the detector flag at
    least once in [onset - lead_s, onset + lag_s]?"

    Detection latency is first matched alert minus onset, in seconds. Negative
    values mean true lead; zero/positive values mean confirmation after the
    spike started. This is the number that distinguishes "prediction" from
    "fast confirmation".

    Alert precision is episode-level: what fraction of alert episodes overlapped
    at least one event window. It is supporting context, not the primary metric.
    """
    events = pd.DatetimeIndex(event_ts).dropna().sort_values()
    alerts = pd.DatetimeIndex(alert_ts).dropna().sort_values()
    if start is not None:
        start = pd.Timestamp(start)
        if len(events):
            events = events[events >= start]
        if len(alerts):
            alerts = alerts[alerts >= start]
    if end is not None:
        end = pd.Timestamp(end)
        if len(events):
            events = events[events <= end]
        if len(alerts):
            alerts = alerts[alerts <= end]

    latencies = []
    matched_events = []
    for ev_ts in events:
        lo = ev_ts - pd.Timedelta(seconds=lead_s)
        hi = ev_ts + pd.Timedelta(seconds=lag_s)
        hit = alerts[(alerts >= lo) & (alerts <= hi)]
        if len(hit) == 0:
            continue
        matched_events.append(ev_ts)
        latencies.append((hit[0] - ev_ts).total_seconds())

    episodes = alert_episodes(alerts, merge_gap_s=merge_gap_s)
    matched_episode_n = 0
    for a0, a1, _n in episodes:
        matched = False
        for ev_ts in events:
            lo = ev_ts - pd.Timedelta(seconds=lead_s)
            hi = ev_ts + pd.Timedelta(seconds=lag_s)
            if a1 >= lo and a0 <= hi:
                matched = True
                break
        matched_episode_n += int(matched)

    duration_h = float("nan")
    if start is not None and end is not None:
        duration_h = max((end - start).total_seconds() / 3600.0, 1e-9)
    elif len(alerts) > 1:
        duration_h = max((alerts[-1] - alerts[0]).total_seconds() / 3600.0, 1e-9)
    elif len(events) > 1:
        duration_h = max((events[-1] - events[0]).total_seconds() / 3600.0, 1e-9)

    false_episodes = len(episodes) - matched_episode_n
    lat = np.asarray(latencies, dtype=float)
    return {
        "event_recall": (len(matched_events) / len(events)) if len(events) else float("nan"),
        "event_latency_median_s": float(np.median(lat)) if len(lat) else float("nan"),
        "event_latency_p25_s": float(np.quantile(lat, 0.25)) if len(lat) else float("nan"),
        "event_latency_p75_s": float(np.quantile(lat, 0.75)) if len(lat) else float("nan"),
        "event_pre_onset_frac": float(np.mean(lat < 0.0)) if len(lat) else float("nan"),
        "alert_event_precision": (matched_episode_n / len(episodes)) if episodes else float("nan"),
        "false_alert_episodes_per_h": (
            false_episodes / duration_h if np.isfinite(duration_h) else float("nan")
        ),
        "n_events": int(len(events)),
        "n_events_detected": int(len(matched_events)),
        "n_alert_episodes": int(len(episodes)),
        "n_false_alert_episodes": int(false_episodes),
        "event_eval_hours": float(duration_h) if np.isfinite(duration_h) else float("nan"),
    }


def baseline_score(df):
    """Run spike_daemon's OLS fit_predict through the walk-forward scoreboard.

    Uses CYCLE_S=5s step to match the daemon's prediction cadence and give
    meaningful (sub-horizon) lead-time resolution.
    Returns the score() dict — the baseline all future features must beat.
    """
    label  = triple_barrier_label(df["power_watts"])
    # actual spike timestamps (for lead-time computation)
    all_events = spike_events(df["power_watts"], DEFAULT_DERIV_THRESHOLD_W)
    spike_ts = all_events.index[all_events]
    onset_ts = pd.DatetimeIndex(true_onset_ts(df["power_watts"]))

    y_true_list, y_score_list, pred_ts, lead_times, lead_times_true = [], [], [], [], []

    for train_df, _ in walk_forward_folds(
        df,
        train_min=spike_daemon.LOOKBACK_MIN,
        step_s=spike_daemon.CYCLE_S,
    ):
        result = spike_daemon.fit_predict(train_df)
        if result is None:
            continue
        pred, _rmse = result
        power = train_df["power_watts"].dropna()
        threshold = float(power.mean() + spike_daemon.RISK_K * power.std())

        t = train_df.index[-1]
        lbl = int(label.loc[t]) if t in label.index else 0
        score_val = pred - threshold  # continuous; > 0 means flagged

        y_true_list.append(lbl)
        y_score_list.append(score_val)
        pred_ts.append(t)

        if lbl == 1 and score_val > 0:
            future = spike_ts[spike_ts > t]
            if len(future) > 0:
                lt = (future[0] - t).total_seconds()
                if lt <= spike_daemon.HORIZON_S:
                    lead_times.append(lt)
            fut_on = onset_ts[onset_ts > t]
            if len(fut_on) > 0:
                lt = (fut_on[0] - t).total_seconds()
                if lt <= spike_daemon.HORIZON_S:
                    lead_times_true.append(lt)

    out = score(y_true_list, y_score_list, lead_times if lead_times else None,
                lead_times_true if lead_times_true else None)
    flags = [t for t, s in zip(pred_ts, y_score_list) if s > 0]
    out.update(event_alert_score(
        onset_ts,
        flags,
        start=pred_ts[0] if pred_ts else None,
        end=pred_ts[-1] if pred_ts else None,
        lead_s=spike_daemon.HORIZON_S,
        lag_s=spike_daemon.HORIZON_S,
        merge_gap_s=spike_daemon.CYCLE_S * 1.5,
    ))
    return out


def _selfcheck_structural():
    df = common.load_telemetry().head(120_000)
    power = df["power_watts"]

    lbl = triple_barrier_label(power)
    assert set(lbl.dropna().unique()).issubset({0, 1}), "label must be 0/1"
    assert (lbl == 1).any() and (lbl == 0).any(), "both classes must be present"

    folds = list(walk_forward_folds(df, train_min=10, step_s=60))
    assert len(folds) > 0, "no folds generated"
    train0, test0 = folds[0]
    assert train0.index.max() < test0.index.min(), "train/test overlap — leakage!"
    assert len(train0) >= 30, f"train too short: {len(train0)}"
    # verify strictly rolling: fold N's test starts where fold N-1's test started + step_s
    train1, test1 = folds[1]
    gap = (test1.index[0] - test0.index[0]).total_seconds()
    assert abs(gap - 60) < 2, f"unexpected fold step: {gap}s"

    print(f"structural selfcheck OK: {len(folds)} folds, label rate={lbl.mean():.3f}")


def _selfcheck_score():
    # synthetic: 10 positives, 90 negatives, perfect predictor
    y_true  = np.array([1]*10 + [0]*90, dtype=float)
    y_score = np.array([1.0]*10 + [-1.0]*90, dtype=float)
    s = score(y_true, y_score, lead_times=[2.0, 3.0, 1.5])
    assert abs(s["pr_auc"] - 1.0) < 1e-6, f"perfect predictor should have PR-AUC=1, got {s['pr_auc']}"
    assert s["precision"] == 1.0, f"expected precision=1, got {s['precision']}"
    assert s["recall"] == 1.0, f"expected recall=1, got {s['recall']}"
    assert abs(s["lead_time_median_s"] - 2.0) < 1e-6, f"unexpected median lead-time: {s['lead_time_median_s']}"

    # all-negative predictor
    s2 = score(y_true, np.zeros_like(y_score))
    assert s2["n_tp"] == 0

    # true-onset detection: flat floor -> single severe step -> exactly one onset,
    # stamped at the first post-jump sample; no lead_times_true -> NaN median
    idx = pd.date_range("2026-01-01", periods=80, freq="1s")
    stepped = pd.Series(np.r_[np.full(40, 100.0), np.full(40, 220.0)], index=idx)
    onsets = true_onset_ts(stepped)
    assert len(onsets) == 1 and pd.Timestamp(onsets[0]) == idx[40], onsets
    drops = true_drop_ts(pd.Series(np.r_[np.full(40, 220.0), np.full(40, 100.0)], index=idx))
    assert len(drops) == 1 and pd.Timestamp(drops[0]) == idx[40], drops
    rise_lbl = triple_barrier_label(stepped, horizon_s=5, direction="rise")
    drop_lbl = triple_barrier_label(stepped, horizon_s=5, direction="drop")
    assert rise_lbl.iloc[35:40].sum() > 0 and drop_lbl.sum() == 0
    assert np.isnan(s2["lead_true_median_s"]) and s2["n_tp_onset"] == 0

    s3 = score(y_true, y_score, lead_times=[2.0], lead_times_true=[0.5, 1.0, 1.5])
    assert abs(s3["lead_true_median_s"] - 1.0) < 1e-9 and s3["n_tp_onset"] == 3

    events = pd.DatetimeIndex([
        "2026-01-01T00:00:10Z",
        "2026-01-01T00:00:30Z",
        "2026-01-01T00:00:50Z",
    ])
    alerts = pd.DatetimeIndex([
        "2026-01-01T00:00:08Z",   # event 1: 2 s lead
        "2026-01-01T00:00:31Z",   # event 2: 1 s late
        "2026-01-01T00:01:20Z",   # false alert
    ])
    es = event_alert_score(events, alerts, start=events[0] - pd.Timedelta(seconds=5),
                           end=alerts[-1], lead_s=5, lag_s=5)
    assert abs(es["event_recall"] - (2 / 3)) < 1e-9, es
    assert abs(es["event_latency_median_s"] - (-0.5)) < 1e-9, es
    assert es["n_alert_episodes"] == 3 and es["n_false_alert_episodes"] == 1, es

    # live-leg scorer: onset at t=40 s, detector fired risk=1 over t=38..41 -> caught early
    idx2 = pd.date_range("2026-01-01T00:00:00Z", periods=80, freq="1s")
    pw = pd.Series(np.r_[np.full(40, 100.0), np.full(40, 230.0)], index=idx2)
    rk = pd.Series(np.r_[np.zeros(38), np.ones(4), np.zeros(38)], index=idx2)
    sl = score_series(pw, rk, lead_s=5, lag_s=5)
    assert sl["n_events"] == 1 and sl["n_events_detected"] == 1, sl
    assert sl["event_latency_median_s"] < 0.0, sl

    print("score selfcheck OK (incl. true-onset lead + live-leg scorer)")


def _read_epoch_csv(path):
    """A fetch_mycroft_trace.py --epoch CSV (col0 = epoch seconds, col1 = value)
    -> UTC-indexed Series. A power trace and a risk series pulled --epoch over the
    same window share one clock, so alerts align to onsets."""
    d = pd.read_csv(path)
    ts = pd.to_datetime(d.iloc[:, 0].to_numpy(), unit="s", utc=True)
    return pd.Series(d.iloc[:, 1].to_numpy(dtype=float), index=ts)


def score_series(power, risk, lead_s=DEFAULT_HORIZON_S, lag_s=DEFAULT_HORIZON_S,
                 risk_thresh=0.5):
    """Live-leg detector score: real power onsets vs the flags the detector
    ACTUALLY fired (risk >= risk_thresh) over the same run. Not the cached gate."""
    onsets = true_onset_ts(power)
    alerts = pd.DatetimeIndex(risk.index[risk.to_numpy() >= risk_thresh])
    return event_alert_score(onsets, alerts, start=power.index[0],
                             end=power.index[-1], lead_s=lead_s, lag_s=lag_s)


def score_live_leg(power_csv, risk_csv, **kw):
    return score_series(_read_epoch_csv(power_csv), _read_epoch_csv(risk_csv), **kw)


def main():
    if "--selfcheck" in sys.argv:
        _selfcheck_structural()
        _selfcheck_score()
        return

    if "--score-live" in sys.argv:
        i = sys.argv.index("--score-live")
        s = score_live_leg(sys.argv[i + 1], sys.argv[i + 2])
        print(f"event_recall        {s['event_recall']:.4f} "
              f"({s['n_events_detected']}/{s['n_events']} onsets)")
        print(f"latency median      {s['event_latency_median_s']:.1f} s (negative = lead)")
        print(f"alert episodes      {s['n_alert_episodes']} "
              f"(false {s['false_alert_episodes_per_h']:.2f}/h over {s['event_eval_hours']:.2f} h)")
        return

    df = common.load_telemetry()
    print(f"Loaded {len(df)} rows. Running baseline walk-forward scoreboard...")
    print(f"(step={spike_daemon.CYCLE_S}s, train={spike_daemon.LOOKBACK_MIN}min, horizon={spike_daemon.HORIZON_S}s)\n")

    stats = baseline_score(df)

    print("=== Baseline scoreboard (spike_daemon.py OLS) ===")
    print(f"  PR-AUC              {stats['pr_auc']:.4f}")
    print(f"  Precision @ thresh  {stats['precision']:.4f}")
    print(f"  Recall    @ thresh  {stats['recall']:.4f}")
    print(f"  Median lead-time    {stats['lead_time_median_s']:.1f} s  (old: to label confirm — inflated by EWMA lag)")
    print(f"  Median lead (TRUE)  {stats['lead_true_median_s']:.1f} s  (to raw step onset; upper bound, "
          f"{stats['n_tp_onset']}/{stats['n_tp']} TPs preceded a real onset)")
    print(f"  Event recall        {stats['event_recall']:.4f} "
          f"({stats['n_events_detected']}/{stats['n_events']} onsets)")
    print(f"  Detection latency   {stats['event_latency_median_s']:.1f} s median "
          "(first alert minus onset; negative = lead)")
    print(f"  Alert precision     {stats['alert_event_precision']:.4f} episode-level, "
          f"false alerts {stats['false_alert_episodes_per_h']:.2f}/h")
    print(f"  True positives      {stats['n_tp']} / {stats['n_spikes']} spikes caught")
    print(f"  Total flags raised  {stats['n_flags']}")


if __name__ == "__main__":
    main()
