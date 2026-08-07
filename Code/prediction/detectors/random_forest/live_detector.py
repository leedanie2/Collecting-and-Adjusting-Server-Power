#!/usr/bin/env python3
"""Live RandomForest spike-risk daemon — a CLUSTERED/CONTINUATION regime detector.

Honest scope (see Model/checkpoint4_lead_diagnosis_2026-06-29.md): at 1 Hz this
predicts *continuation* of spike activity (you're in / entering a turbulent
regime), NOT genuine early warning of isolated cold-start spikes — those are
~unpredictable at this sample rate (RF lift 2.5x vs 11x for clustered spikes).
Use it as "turbulence will persist", not "a bolt from the blue is coming".

Parallels spike_daemon.py (OLS), but:
- classifies P(spike in next HORIZON_S s) instead of regressing power magnitude;
- RF needs more data than a 10-min OLS window, so it trains on a multi-hour
  window at startup and refits every REFIT_MIN minutes, rather than every cycle.

Features and label come from lane_a (one shared definition with the eval).
"""
import argparse
import random
import socket
import sys
import time
from collections import deque

import numpy as np
import pandas as pd
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
from sklearn.ensemble import RandomForestClassifier

from core import telemetry as common
from core import features as la

HORIZON_S = la.HORIZON_S      # forecast window (s)
CYCLE_S = 1                   # predict every N seconds (1 Hz = input data rate)
TRAIN_H = 12                  # hours of history to train on
REFIT_MIN = 60               # refit the forest every N minutes
RF_N_JOBS = 8                # cap refit CPU fan-out; -1 contaminates power traces
ALARM_RATE = 0.10            # flag ~this fraction of the time; sets the threshold
                             # from out-of-bag proba (balanced RF inflates proba,
                             # so a fixed 0.5 would flag almost always)
MIN_THRESHOLD = 0.35         # production floor for spike probability. Live idle
                             # diagnosis 2026-07-20: current champion scored strict
                             # idle p50=0.271/p99=0.340 against a stored threshold
                             # of 0.294, yielding ~20% idle duty. Keep raw proba
                             # visible, but actuator-facing risk needs a quiet-box
                             # mute above that idle score scale.
FLAG_BUDGET = 0.10           # live duty cap: flag at most ~this fraction of recent
                             # cycles. The static OOB threshold goes stale between
                             # refits (score-scale shift after adoption; sustained-
                             # load saturation -> 100% duty); ranking against recent
                             # live probas caps duty by construction at any scale.
RANK_WINDOW = 3600           # probas the rank threshold ranks against
                             # (~1 h at CYCLE_S=1 = one refit period)
IDLE_USAGE_MAX = 5.0         # idle calibration mask: low current CPU usage
IDLE_POWER_SPAN_MAX_W = 15.0 # and stable recent power lags (strictly causal)
IDLE_THRESHOLD_Q = 0.999     # require almost all idle OOB rows below threshold
IDLE_THRESHOLD_MARGIN = 0.01
IDLE_MIN_ROWS = 60
EVENT_THRESHOLD_MIN_EVENTS = 3
EVENT_THRESHOLD_TARGET_RECALL = 0.99
EVENT_THRESHOLD_GRID_N = 101
RETRY_ATTEMPTS = 3           # InfluxDB query/write retries before giving up a call
RETRY_BASE_S = 3.0           # backoff base; total span ~= 3+9 s plus jitter
WARN_EVERY_S = 60            # rate-limit repeated degraded-mode warnings
KF_WARMUP_S = 60             # KF needs a run-up from its diffuse prior before
                             # kf_level/kf_slope mean anything -- lag features
                             # only need max(LAGS), the KF needs much more
LOOKBACK_FEAT_S = max(la.LAGS + [KF_WARMUP_S]) + 5   # seconds of recent data needed for one feature row
INFLUX_BUCKET = "Power"
UPSTREAM_ENABLED = True      # include nr_running_jerk, memory deltas, etc.


def _retry(fn, *args, _what="influx", **kwargs):
    """Call fn with exponential backoff + jitter. Re-raises the last error after
    RETRY_ATTEMPTS so callers decide how to degrade (skip cycle / keep old model /
    drop write). stdlib backoff, no tenacity dep for three retries."""
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if attempt == RETRY_ATTEMPTS - 1:
                raise
            delay = RETRY_BASE_S * (3 ** attempt) + random.uniform(0, RETRY_BASE_S)
            print(f"warning: {_what} attempt {attempt + 1}/{RETRY_ATTEMPTS} failed "
                  f"({e}); retrying in {delay:.1f}s")
            time.sleep(delay)


def new_forest():
    return RandomForestClassifier(
        n_estimators=200, min_samples_leaf=20, class_weight="balanced",
        oob_score=True, n_jobs=RF_N_JOBS, random_state=0)


def threshold_floor(raw_threshold):
    """Apply the production spike-risk floor to any calibrated/stored threshold."""
    if not np.isfinite(raw_threshold):
        return MIN_THRESHOLD
    return max(float(raw_threshold), MIN_THRESHOLD)


def idle_feature_mask(X):
    """Strictly-causal idle rows for threshold calibration.

    Uses only feature columns already available to the model at time t:
    current usage plus the short trailing power-lag span. Missing columns mean
    "no idle mask" so older/base feature sets degrade to the global floor.
    """
    if "usage_lag0" not in X.columns:
        return np.zeros(len(X), dtype=bool)
    power_cols = [c for c in ("power_lag0", "power_lag1", "power_lag2", "power_lag3")
                  if c in X.columns]
    usage_ok = X["usage_lag0"].to_numpy(dtype=float) < IDLE_USAGE_MAX
    if not power_cols:
        return usage_ok
    p = X[power_cols].to_numpy(dtype=float)
    span = np.nanmax(p, axis=1) - np.nanmin(p, axis=1)
    return usage_ok & np.isfinite(span) & (span < IDLE_POWER_SPAN_MAX_W)


def calibrate_threshold_from_oob(oob, X=None):
    """Calibrate the actuator-facing threshold from OOB scores.

    The ranking model can still assign non-trivial probability to flat idle
    rows when laggy upstream signals (load averages, memory state) remain high
    after work has ended. The ordinary q90 alarm budget is therefore not enough
    for an actuator trigger. Keep the q90 budget for detection, then add an
    idle-negative floor so quiet rows cannot dominate the risk flag.
    """
    oob = np.asarray(oob, dtype=float)
    finite = np.isfinite(oob)
    if not finite.any():
        return MIN_THRESHOLD
    threshold = threshold_floor(float(np.nanquantile(oob[finite], 1.0 - ALARM_RATE)))
    if X is not None and len(X) == len(oob):
        idle = idle_feature_mask(X) & finite
        if int(idle.sum()) >= IDLE_MIN_ROWS:
            idle_cut = float(np.nanquantile(oob[idle], IDLE_THRESHOLD_Q))
            threshold = max(threshold, idle_cut + IDLE_THRESHOLD_MARGIN)
    return threshold_floor(threshold)


def tune_threshold_for_event_cost(scores, index, event_ts, base_threshold,
                                  lead_s=HORIZON_S, lag_s=HORIZON_S,
                                  merge_gap_s=2.0):
    """Raise a calibrated threshold when event recall survives.

    `scores` are OOB probabilities for the training rows, so this is still
    calibration rather than refitting. The OOB/idle threshold is treated as a
    reference point, not a hard lower bound: if it misses too many raw onsets,
    the event-cost search may choose a lower threshold, but never below the
    production floor.
    """
    idx = pd.DatetimeIndex(index)
    scores = np.asarray(scores, dtype=float)
    finite = np.isfinite(scores)
    base_threshold = threshold_floor(base_threshold)
    meta = {
        "policy": "oob_event_cost_v1",
        "base_threshold": float(base_threshold),
        "threshold": float(base_threshold),
        "search_floor": float(MIN_THRESHOLD),
        "n_events": 0,
        "base_event_recall": float("nan"),
        "event_recall": float("nan"),
        "floor_event_recall": float("nan"),
        "base_alert_duty": float("nan"),
        "alert_duty": float("nan"),
        "floor_alert_duty": float("nan"),
    }
    if len(idx) != len(scores) or not finite.any():
        meta["reason"] = "bad_scores"
        return base_threshold, meta

    start, end = idx[finite][0], idx[finite][-1]
    events = pd.DatetimeIndex(event_ts).dropna().sort_values()
    events = events[(events >= start) & (events <= end)]
    meta["n_events"] = int(len(events))
    if len(events) < EVENT_THRESHOLD_MIN_EVENTS:
        meta["reason"] = "too_few_events"
        return base_threshold, meta

    from validation import evaluation as ev

    def stats_at(thr):
        flag = finite & (scores > thr)
        stats = ev.event_alert_score(
            events,
            idx[flag],
            start=start,
            end=end,
            lead_s=lead_s,
            lag_s=lag_s,
            merge_gap_s=merge_gap_s,
        )
        stats["alert_duty"] = float(np.mean(flag)) if len(flag) else float("nan")
        return stats

    search_floor = threshold_floor(MIN_THRESHOLD)
    base_stats = stats_at(base_threshold)
    floor_stats = stats_at(search_floor)
    base_recall = float(base_stats.get("event_recall", np.nan))
    base_duty = float(base_stats.get("alert_duty", np.nan))
    floor_recall = float(floor_stats.get("event_recall", np.nan))
    floor_duty = float(floor_stats.get("alert_duty", np.nan))
    meta.update({
        "base_event_recall": base_recall,
        "floor_event_recall": floor_recall,
        "event_recall": base_recall,
        "base_alert_duty": base_duty,
        "floor_alert_duty": floor_duty,
        "alert_duty": base_duty,
    })
    if not np.isfinite(floor_recall) or floor_recall <= 0.0:
        meta["reason"] = "floor_misses_events"
        return base_threshold, meta

    target_recall = min(EVENT_THRESHOLD_TARGET_RECALL, floor_recall)
    vals = scores[finite]
    grid = np.unique(np.r_[base_threshold,
                           search_floor,
                           np.quantile(vals, np.linspace(0.0, 1.0,
                                                         EVENT_THRESHOLD_GRID_N))])
    grid = grid[np.isfinite(grid)]
    grid = np.sort(grid[grid >= search_floor])

    best_thr = search_floor
    best_stats = floor_stats
    for thr in grid:
        stats = stats_at(float(thr))
        rec = float(stats.get("event_recall", np.nan))
        if not np.isfinite(rec) or rec + 1e-12 < target_recall:
            continue
        duty = float(stats.get("alert_duty", np.inf))
        best_duty = float(best_stats.get("alert_duty", np.inf))
        false_h = float(stats.get("false_alert_episodes_per_h", np.inf))
        best_false_h = float(best_stats.get("false_alert_episodes_per_h", np.inf))
        if (duty < best_duty - 1e-12
                or (abs(duty - best_duty) <= 1e-12 and false_h < best_false_h)
                or (abs(duty - best_duty) <= 1e-12
                    and abs(false_h - best_false_h) <= 1e-12
                    and thr > best_thr)):
            best_thr = float(thr)
            best_stats = stats

    meta.update({
        "threshold": float(threshold_floor(best_thr)),
        "target_event_recall": float(target_recall),
        "event_recall": float(best_stats.get("event_recall", np.nan)),
        "alert_duty": float(best_stats.get("alert_duty", np.nan)),
        "false_alert_episodes_per_h": float(
            best_stats.get("false_alert_episodes_per_h", np.nan)
        ),
        "n_events_detected": int(best_stats.get("n_events_detected", 0)),
        "n_alert_episodes": int(best_stats.get("n_alert_episodes", 0)),
        "n_false_alert_episodes": int(best_stats.get("n_false_alert_episodes", 0)),
        "reason": (
            "raised" if best_thr > base_threshold else
            "lowered" if best_thr < base_threshold else
            "base_kept"
        ),
    })
    return threshold_floor(best_thr), meta


def train(df, horizon=HORIZON_S, upstream=UPSTREAM_ENABLED):
    """Fit the forest on a history window. Returns (model, feature_cols, threshold)
    or None if there aren't enough spike examples to learn from. The threshold is
    calibrated from out-of-bag proba (unbiased on training data) to flag ~ALARM_RATE
    of the time -- balanced-RF proba is uncalibrated, so a fixed 0.5 is meaningless."""
    X, y = la.build_xy(df, lane_a=False, horizon=horizon, kf=True, upstream=upstream)
    if y.sum() < 20 or len(X) < 500:
        return None
    model = new_forest()
    model.fit(X.to_numpy(), y.to_numpy())
    oob = model.oob_decision_function_[:, 1]
    threshold = calibrate_threshold_from_oob(oob, X)
    return model, list(X.columns), threshold


def seed_scores(model):
    """Rank-buffer seed at adoption: a RANK_WINDOW-point quantile sketch of the
    new model's own OOB probas, so the first live quantile matches the static
    OOB calibration -- scale-consistent with the scores it is about to emit.
    (A strided subsample was ~0.07 off at q90 on the RF's lumpy OOB
    distribution; the sketch preserves the full ECDF up to interpolation.)"""
    oob = model.oob_decision_function_[:, 1]
    oob = oob[np.isfinite(oob)]
    if len(oob) == 0:
        return deque(maxlen=RANK_WINDOW)   # degenerate seed -> static fallback
    if len(oob) > RANK_WINDOW:
        oob = np.quantile(oob, np.linspace(0.0, 1.0, RANK_WINDOW))
    return deque(oob, maxlen=RANK_WINDOW)


def live_threshold(scores, static_thr):
    """Operative alarm threshold: the (1-FLAG_BUDGET) quantile of recent live
    probas, never below MIN_THRESHOLD (the quiet-box mute). Empty buffer
    (pre-first-fit / degenerate OOB seed) -> the static OOB threshold."""
    if not scores:
        return threshold_floor(static_thr)
    return threshold_floor(float(np.quantile(scores, 1.0 - FLAG_BUDGET)))


def decide(scores, proba, static_thr):
    """One cycle's alarm decision. Strict > keeps a constant-score stream
    (stuck or poisoned scorer) permanently mute; append AFTER the decision so
    a sample can't dilute its own rank."""
    thr_live = live_threshold(scores, static_thr)
    risk = bool(proba > thr_live)
    scores.append(proba)
    return thr_live, risk


GATE_EVAL_MIN = 60  # held-out tail (minutes) the refit promotion gate judges on


def gated_train(hist, current=None, upstream=UPSTREAM_ENABLED):
    """Refit with a promotion gate: the challenger trains on the window minus
    the last GATE_EVAL_MIN minutes and replaces `current` only if it beats it
    on that held-out tail (event recall + first-flag latency gate shared with
    spike_daemon_rf_continual; neither model has trained on the tail).
    Adopts unconditionally when there is no incumbent; keeps the incumbent
    when the tail has no spikes (a quiet hour can't judge either model).
    Returns ((model, cols, threshold), verdict) or None if training isn't
    possible."""
    if hist.empty:
        return None
    cut = hist.index.max() - pd.Timedelta(minutes=GATE_EVAL_MIN)
    fit = train(hist[hist.index < cut], upstream=upstream)
    if fit is None:
        return None
    if current is None:
        return fit, "adopted: no incumbent"
    try:
        from detectors.random_forest import continual_detector as cc  # deferred: cc imports this module
        from validation import evaluation as ev
        X, y = la.build_xy(hist, lane_a=False, horizon=HORIZON_S, kf=True,
                           upstream=upstream)
        mask = X.index >= cut
        Xe, ye = X[mask], y[mask]
        spike_ts = pd.DatetimeIndex(ev.true_onset_ts(hist["power_watts"]))
        ch = {"model": fit[0], "cols": fit[1], "threshold": fit[2]}
        inc = {"model": current[0], "cols": current[1], "threshold": current[2]}
        if cc.beats(cc.score_on_fold(ch, Xe, ye, spike_ts),
                    cc.score_on_fold(inc, Xe, ye, spike_ts)):
            return fit, "promoted: beats incumbent on held-out tail"
        return current, "kept incumbent: challenger didn't beat it"
    except Exception as e:
        # the gate must never take the daemon down (e.g. incumbent's columns
        # missing after an upstream outage) -> old unconditional behavior
        print(f"warning: promotion gate failed ({e}); adopting challenger")
        return fit, "adopted: gate error"


def predict_latest(model, cols, df, upstream=UPSTREAM_ENABLED):
    """P(spike in next HORIZON_S s) from the most recent complete feature row."""
    X = la.feature_frame(df, lane_a=False, kf=True, upstream=upstream)
    X = X[cols].dropna()   # only the model's own columns must be complete
    if X.empty:
        return None
    row = X.iloc[[-1]].to_numpy()
    return float(model.predict_proba(row)[0, 1])


def make_prediction_point(server, proba, threshold):
    return (
        Point("power_spike_prediction")
        .tag("server", server)
        .tag("model", "rf")
        .field("spike_proba", proba)
        .field("threshold", threshold)
        .field("horizon_s", HORIZON_S)
        .field("spike_risk", proba > threshold)
    )


def run(write_api=None, org=None, server=None, once=False, risk_path=None):
    model = cols = threshold = None
    scores = deque(maxlen=RANK_WINDOW)  # live proba history for the rank threshold
    last_fit = None
    last_warn = 0.0

    def warn(msg):
        # rate-limited so an outage logs one line/minute, not one/second
        nonlocal last_warn
        if time.monotonic() - last_warn >= WARN_EVERY_S:
            print(msg)
            last_warn = time.monotonic()

    while True:
        now = pd.Timestamp.now(tz="UTC")
        need_refit = model is None or (now - last_fit) >= pd.Timedelta(minutes=REFIT_MIN)
        if need_refit:
            try:
                hist = _retry(common.query_window, start=f"-{TRAIN_H}h",
                              _what="query_window(refit)")  # efficient bulk pull
            except Exception as e:
                # keep serving the existing model through an outage; never crash-loop
                warn(f"refit query failed after retries ({e}); "
                     f"{'keeping current model' if model else 'no model yet, will retry'}")
                if once and model is None:
                    return
                time.sleep(CYCLE_S)
                continue
            if UPSTREAM_ENABLED and not hist.empty:
                try:
                    up = common.query_upstream(start=f"-{TRAIN_H}h", timeout_ms=60000)
                    if not up.empty:
                        up = up.drop(columns=up.columns.intersection(hist.columns))
                        hist = hist.join(up, how="left")
                except Exception as e:
                    print(f"warning: upstream query failed: {e}, continuing with base features only")
            current = (model, cols, threshold) if model is not None else None
            res = gated_train(hist, current=current, upstream=UPSTREAM_ENABLED)
            if res is None:
                print("not enough spike history to train yet")
                if once:
                    return
                time.sleep(CYCLE_S)
                continue
            (new_model, cols, threshold), verdict = res
            if new_model is not model:
                # adoption/promotion: new score scale -> reseed from its OOB
                # probas; a kept incumbent keeps its buffer tracking live drift
                scores = seed_scores(new_model)
            model = new_model
            last_fit = now
            print(f"[{now}] refit RF on {len(hist)} rows "
                  f"(threshold={threshold:.3f}, {verdict})")

        try:
            recent = _retry(common.query_recent, start=f"-{LOOKBACK_FEAT_S + 5}s",
                            _what="query_recent(predict)")
        except Exception as e:
            warn(f"predict query failed after retries ({e}); skipping cycle, "
                 f"keeping model")
            if once:
                return
            time.sleep(CYCLE_S)
            continue
        if UPSTREAM_ENABLED and not recent.empty:
            try:
                up = common.query_upstream(start=f"-{LOOKBACK_FEAT_S + 5}s", timeout_ms=30000)
                if not up.empty:
                    up = up.drop(columns=up.columns.intersection(recent.columns))
                    recent = recent.join(up, how="left")
            except Exception as e:
                print(f"warning: upstream query failed on predict: {e}")
        proba = predict_latest(model, cols, recent) if not recent.empty else None
        if proba is None:
            print("no recent data to predict on")
        else:
            thr_live, risk = decide(scores, proba, threshold)
            flag = " <-- SPIKE RISK" if risk else ""
            print(f"P(spike in {HORIZON_S}s) = {proba:.3f} (thr {thr_live:.3f}){flag}")
            if risk_path:
                # skipped when proba is None: no prediction => mtime ages =>
                # ramp.c/rapl_capper fail open rather than trust a stale flag
                try:
                    common.write_risk_flag(risk_path, risk)
                except OSError as e:
                    warn(f"risk-flag write failed ({e}); prediction unaffected")
            if write_api is not None:
                try:
                    _retry(write_api.write, bucket=INFLUX_BUCKET, org=org,
                           record=make_prediction_point(server, proba, thr_live),
                           _what="influx write")
                except Exception as e:
                    warn(f"prediction write failed after retries ({e}); dropping point")
        if once:
            return
        time.sleep(CYCLE_S)


def selfcheck():
    df = la.load()  # procs cache (has the needed power/usage); no live connection
    # Train without upstream (cached data doesn't have upstream signals)
    fit = train(df.iloc[: TRAIN_H * 3600], upstream=False)
    assert fit is not None, "train returned None on cached data"
    model, cols, threshold = fit
    expected = ({f"power_lag{l}" for l in la.LAGS} | {f"usage_lag{l}" for l in la.LAGS}
                | {"kf_level", "kf_slope", "temp_celsius_slope"})
    assert expected.issubset(set(cols)), f"missing features: {expected - set(cols)}"
    assert 0.0 <= threshold <= 1.0, f"bad threshold: {threshold}"
    proba = predict_latest(model, cols, df.tail(LOOKBACK_FEAT_S + 5), upstream=False)
    assert proba is not None and 0.0 <= proba <= 1.0, f"bad proba: {proba}"

    risky = make_prediction_point("selfcheck-host", proba=0.9, threshold=0.5).to_line_protocol()
    assert risky.startswith("power_spike_prediction,") and "server=selfcheck-host" in risky and "model=rf" in risky, risky
    assert "spike_risk=true" in risky, risky
    calm = make_prediction_point("selfcheck-host", proba=0.1, threshold=0.5).to_line_protocol()
    assert "spike_risk=false" in calm, calm

    # threshold floor: a degenerate all-zero OOB distribution must not yield thr=0
    assert threshold >= MIN_THRESHOLD, f"threshold below floor: {threshold}"
    # idle FP regression: the July-20 failure mode was idle OOB scores clustered
    # just above the old stored threshold (~0.294). The new actuator threshold
    # must mute that distribution sharply without changing raw proba output.
    idle_X = pd.DataFrame({
        "usage_lag0": np.full(1005, 2.0),
        "power_lag0": np.full(1005, 201.0),
        "power_lag1": np.full(1005, 200.8),
        "power_lag2": np.full(1005, 200.6),
        "power_lag3": np.full(1005, 200.7),
    })
    idle_oob = np.r_[np.full(1000, 0.30), np.full(5, 0.34)]
    old_thr = 0.293
    new_thr = calibrate_threshold_from_oob(idle_oob, idle_X)
    old_idle_duty = float(np.mean(idle_oob > old_thr))
    new_idle_duty = float(np.mean(idle_oob > new_thr))
    assert old_idle_duty > 0.99 and new_idle_duty == 0.0, \
        f"idle FP duty did not drop: old={old_idle_duty:.3f} new={new_idle_duty:.3f} thr={new_thr:.3f}"

    idx = pd.date_range("2026-01-01T00:00:00Z", periods=120, freq="1s")
    event_scores = np.full(len(idx), 0.20)
    event_scores[10:35] = 0.45       # false-alert plateau above the base floor
    event_scores[58:61] = 0.85       # true onset window survives a higher cut
    event_scores[90:95] = 0.42       # another weaker false plateau
    event_ts = pd.DatetimeIndex([idx[60], idx[80], idx[100]])
    tuned, tune_meta = tune_threshold_for_event_cost(
        event_scores, idx, event_ts, 0.35, lead_s=5, lag_s=5
    )
    assert tuned >= 0.45 - 1e-12, (tuned, tune_meta)
    assert tune_meta["event_recall"] >= tune_meta["target_event_recall"], tune_meta
    assert tune_meta["alert_duty"] < tune_meta["base_alert_duty"], tune_meta

    high_base_scores = np.full(len(idx), 0.20)
    high_base_scores[58:61] = 0.60
    lowered, lower_meta = tune_threshold_for_event_cost(
        high_base_scores, idx, event_ts, 0.80, lead_s=5, lag_s=5
    )
    assert lowered < 0.80 and lower_meta["reason"] == "lowered", lower_meta
    assert lower_meta["event_recall"] >= lower_meta["target_event_recall"], lower_meta

    # promotion gate: no incumbent -> adopt; identical incumbent (same data,
    # fixed random_state -> identical scores) must NOT be strictly beaten
    res = gated_train(df.iloc[: TRAIN_H * 3600], current=None, upstream=False)
    assert res is not None, "gated_train returned None on cached data"
    (gm, gc, gt), verdict = res
    assert verdict.startswith("adopted"), verdict
    res2 = gated_train(df.iloc[: TRAIN_H * 3600], current=(gm, gc, gt), upstream=False)
    (gm2, _, _), verdict2 = res2
    assert verdict2.startswith("kept") and gm2 is gm, \
        f"identical incumbent should be kept, got: {verdict2}"

    # _retry: fails N-1 times then succeeds (retries), and re-raises when it never
    # succeeds (so run()'s degrade paths trigger instead of silently swallowing)
    calls = {"n": 0}
    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("transient")
        return "ok"
    global RETRY_BASE_S
    saved, RETRY_BASE_S = RETRY_BASE_S, 0.0  # no real sleeping in the selfcheck
    try:
        assert _retry(flaky, _what="test") == "ok" and calls["n"] == 2
        raised = False
        try:
            _retry(lambda: (_ for _ in ()).throw(RuntimeError("always")), _what="test")
        except RuntimeError:
            raised = True
        assert raised, "_retry must re-raise after exhausting attempts"
    finally:
        RETRY_BASE_S = saved

    # risk flag helper: content, trailing newline, atomicity (no .tmp residue)
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "spike_risk.flag"
        common.write_risk_flag(p, True)
        assert p.read_text() == "1\n", "risk flag should be '1\\n'"
        common.write_risk_flag(p, False)
        assert p.read_text() == "0\n", "risk flag should be '0\\n'"
        assert not p.with_suffix(".tmp").exists(), "tmp residue left behind"

    # --- rank-based alarm budget (spec 2026-07-08) ---
    from collections import deque as _dq
    # empty buffer -> static fallback, with the production floor still enforced
    assert live_threshold(_dq(maxlen=RANK_WINDOW), 0.42) == 0.42
    assert live_threshold(_dq(maxlen=RANK_WINDOW), 0.10) == MIN_THRESHOLD
    # constant-score stream (stuck/poisoned scorer): strict > must never flag
    s = _dq([0.5] * 300, maxlen=RANK_WINDOW)
    thr_c, risk_c = decide(s, 0.5, 0.42)
    assert thr_c == 0.5 and not risk_c, (thr_c, risk_c)
    # sub-floor noise: thr_live >= MIN_THRESHOLD keeps a quiet box silent
    rng = np.random.RandomState(0)
    s = _dq(rng.uniform(0.0, 0.05, 500), maxlen=RANK_WINDOW)
    n_quiet = sum(decide(s, p, 0.10)[1] for p in rng.uniform(0.0, 0.05, 1000))
    assert n_quiet == 0, f"sub-floor noise flagged {n_quiet}x"
    # OOB seed: finite, bounded, first live quantile ~= the static calibration
    s = seed_scores(model)
    assert 0 < len(s) <= RANK_WINDOW and np.isfinite(list(s)).all()
    oob_full = model.oob_decision_function_[:, 1]
    want = max(float(np.nanquantile(oob_full, 1.0 - ALARM_RATE)), MIN_THRESHOLD)
    assert abs(live_threshold(s, 0.0) - want) < 0.05, (live_threshold(s, 0.0), want)
    # score-scale shift after adoption: duty capped ~= FLAG_BUDGET once the
    # buffer turns over (the 15:10 poisoned-challenger scenario)
    risks = [decide(s, p, 0.10)[1] for p in rng.uniform(0.4, 0.6, 3 * RANK_WINDOW)]
    duty = float(np.mean(risks[-RANK_WINDOW:]))
    assert duty <= FLAG_BUDGET + 0.02, f"steady-state duty {duty:.3f} > budget"

    print(f"selfcheck OK: trained RF, latest P(spike)={proba:.3f}, "
          f"threshold={threshold:.3f} (floor {MIN_THRESHOLD}), rank budget "
          f"{FLAG_BUDGET:.0%}/{RANK_WINDOW} cycles, retry+degrade wired, "
          f"upstream={UPSTREAM_ENABLED}")


def main():
    global UPSTREAM_ENABLED
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--once", action="store_true", help="one predict cycle then exit")
    ap.add_argument("--dry-run", action="store_true", help="don't write to InfluxDB")
    ap.add_argument("--no-upstream", action="store_true", help="disable upstream signals (nr_running_jerk, memory, etc.)")
    ap.add_argument("--risk-file", default=str(common.DATA_DIR / "spike_risk.flag"),
                    help="flag file published each cycle for same-box consumers "
                         "(ramp.c --risk-file / rapl_capper --watch-file)")
    args = ap.parse_args()

    if args.no_upstream:
        UPSTREAM_ENABLED = False

    if args.selfcheck:
        selfcheck()
        return

    server = socket.gethostname()
    client = write_api = org = None
    if not args.dry_run:
        org = common.load_org()
        client = InfluxDBClient(url=common.INFLUX_URL, token=common.load_write_token(), org=org)
        write_api = client.write_api(write_options=SYNCHRONOUS)
    try:
        run(write_api=write_api, org=org, server=server, once=args.once,
            risk_path=args.risk_file)
    finally:
        if write_api is not None:
            write_api.close()
        if client is not None:
            client.close()


if __name__ == "__main__":
    main()
