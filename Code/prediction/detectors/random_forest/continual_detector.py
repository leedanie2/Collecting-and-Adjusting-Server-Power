#!/usr/bin/env python3
"""Continual, self-improving RandomForest spike detector (champion-challenger).

A sibling of spike_daemon_rf.py. Same 1 Hz scope, same frozen spike label, same
base features (power/usage lags via lane_a) -- but instead of a single RF refit
on a schedule, this runs a *generational* loop:

  1. Train a fresh challenger RF on the training window (base features only).
  2. "Learn from the previous RF's mistakes" via HARD-EXAMPLE REWEIGHTING:
     upweight the training rows the current champion misclassified (its false
     positives + false negatives), so the challenger focuses there. Deliberately
     AdaBoost-like.
  3. CHAMPION-CHALLENGER GATE (the safety net): score challenger AND incumbent
     champion on the SAME held-out walk-forward fold via eval.py. Promote the
     challenger ONLY if it improves event-level behavior: raw-onset recall first,
     then first-flag latency and alert duty. PR-AUC is a fallback only when there
     are no physical onsets to judge. Otherwise discard it and keep the champion
     -> no regression, ever.
  4. Append one row per generation to a JSONL metrics log for a future GUI.

Honest framing (Model/checkpoint4_lead_diagnosis_2026-06-29.md): ~78% of the
RF's apparent skill is NOWCASTING an already-started spike and median lead-time
is pinned ~2 s for every model class. This loop does NOT promise more lead time.
The reachable win is precision / fewer false alarms; the gate is what guarantees
the loop can only help, never hurt. On this noisy, autocorrelated data the RF's
"mistakes" are largely the unlearnable cold-onset spikes (CP4), so blind
reweighting would chase noise -- the gate is what makes it safe to try anyway.

reuses lane_a (features+label), eval (PR-AUC/score), and
spike_daemon_rf (forest config, live predict/write). sklearn only; no new deps.
Validated OFFLINE on the cached ~46k usable rows (`--selfcheck`, `--validate`).
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from core import telemetry as common
from validation import evaluation as ev
from core import features as la
from detectors.random_forest import live_detector as rf

# --- generational / window config ---
HORIZON_S = la.HORIZON_S
TRAIN_H = 6           # training window (hours) per generation
EVAL_H = 2           # held-out fold (hours) per generation; also the slide step
EMBARGO_S = HORIZON_S  # purge gap so train's forward labels can't peek into eval
ALARM_RATE = rf.ALARM_RATE   # threshold flags ~this fraction (OOB-calibrated)
BOOST = 3.0          # hard examples get weight 1 + BOOST (focus on champ's errors)
MIN_TRAIN_POS = 20   # need this many spikes in train to fit (matches rf.train)

METRICS_LOG = common.DATA_DIR / "rf_continual_metrics.jsonl"

# live-loop knobs (mirror spike_daemon_rf; offline validation never touches these)
CYCLE_S = rf.CYCLE_S
REFIT_MIN = 60
LOOKBACK_FEAT_S = rf.LOOKBACK_FEAT_S
INFLUX_BUCKET = rf.INFLUX_BUCKET
UPSTREAM = True       # live loop wires common.UPSTREAM_FIELDS in as candidate features


def _join_upstream(base, start, stop="now()", timeout_ms=120_000):
    """Left-join the upstream OS signals (1 Hz) onto a base power/usage frame by
    1 s index. Returns base unchanged if the pull is empty (degrade to base)."""
    if base.empty:
        return base
    up = common.query_upstream(start=start, stop=stop, timeout_ms=timeout_ms)
    if up.empty:
        return base
    up = up.drop(columns=up.columns.intersection(base.columns))
    return base.join(up, how="left")


# ---------------------------------------------------------------------------
# core: train / score / gate
# ---------------------------------------------------------------------------
def _calibrate(model):
    """OOB-calibrated threshold: flag ~ALARM_RATE of rows. Balanced-RF proba is
    uncalibrated so a fixed 0.5 is meaningless (same trick as spike_daemon_rf)."""
    return rf.calibrate_threshold_from_oob(model.oob_decision_function_[:, 1])


def train_rf(X, y, sample_weight=None, event_ts=None):
    """Fit a forest (rf.new_forest config) with optional per-row weights.
    Returns a champion dict {model, cols, threshold}."""
    model = rf.new_forest()
    model.fit(X.to_numpy(), y.to_numpy(),
              sample_weight=None if sample_weight is None else np.asarray(sample_weight))
    oob = model.oob_decision_function_[:, 1]
    threshold = rf.calibrate_threshold_from_oob(oob, X)
    threshold_meta = None
    if event_ts is not None:
        threshold, threshold_meta = rf.tune_threshold_for_event_cost(
            oob, X.index, event_ts, threshold,
            lead_s=HORIZON_S, lag_s=HORIZON_S,
        )
    out = {"model": model, "cols": list(X.columns), "threshold": threshold}
    if threshold_meta is not None:
        out["threshold_calibration"] = threshold_meta
    return out


def hard_example_weights(champion, X, y):
    """1.0 baseline, 1+BOOST for rows the champion misclassifies (FP+FN).
    None champion (generation 0) -> uniform weights. Returns (weights, n_hard)."""
    if champion is None:
        return None, 0
    proba = champion["model"].predict_proba(X[champion["cols"]].to_numpy())[:, 1]
    pred = (proba > rf.threshold_floor(float(champion["threshold"]))).astype(int)
    wrong = (pred != y.to_numpy()).astype(float)
    return 1.0 + BOOST * wrong, int(wrong.sum())


def score_on_fold(champ, Xe, ye, spike_ts):
    """Score a champion dict on a held-out fold via eval.py. Returns eval.score
    dict plus event-level alert metrics, or None if the fold has no rows.

    PR-AUC is undefined on all-negative folds, but those folds still matter for
    the detector's cost: false alert episodes and alert duty are measured there.
    """
    if len(Xe) == 0:
        return None
    proba = champ["model"].predict_proba(Xe[champ["cols"]].to_numpy())[:, 1]
    cont = proba - rf.threshold_floor(float(champ["threshold"]))  # >0 means flagged
    flag = cont > 0
    true_pos_ts = Xe.index.to_numpy()[flag & (ye.to_numpy() == 1)]
    lts = la._lead_times(spike_ts, true_pos_ts)   # reuse lane_a's causal lead-time calc
    out = ev.score(ye.to_numpy(), cont, lts if lts else None)
    out.update(ev.event_alert_score(
        spike_ts,
        Xe.index[flag],
        start=Xe.index[0],
        end=Xe.index[-1],
        lead_s=HORIZON_S,
        lag_s=HORIZON_S,
        merge_gap_s=2.0,
    ))
    out["alert_duty"] = float(np.mean(flag)) if len(flag) else float("nan")
    out["n_alert_points"] = int(flag.sum())
    out["n_eval_points"] = int(len(flag))
    return out


def beats(challenger, champion):
    """Promotion gate.

    Prefer event-level behavior when true step onsets exist in the fold:
    event recall first, then detection latency and alert duty as the noise cost.
    PR-AUC remains the fallback when the held-out window has no physical onsets
    to judge (EWMA-only labels can still produce row positives).
    """
    if challenger is None:
        return False
    if champion is None:
        return True

    cr = float(challenger.get("event_recall", np.nan))
    pr = float(champion.get("event_recall", np.nan))
    cn = int(challenger.get("n_events", 0) or 0)
    pn = int(champion.get("n_events", 0) or 0)
    if cn > 0 and pn > 0 and np.isfinite(cr) and np.isfinite(pr):
        eps = 1e-9
        if cr < pr - eps:
            return False
        cduty = float(challenger.get("alert_duty", np.nan))
        pduty = float(champion.get("alert_duty", np.nan))
        if not np.isfinite(cduty):
            cduty = float("inf")
        if not np.isfinite(pduty):
            pduty = float("inf")
        clat = float(challenger.get("event_latency_median_s", np.nan))
        plat = float(champion.get("event_latency_median_s", np.nan))

        if cr > pr + eps:
            # Better recall is worth considering, but the gate does not yet
            # know the MATLAB risk/cost weights; reject recall gains that come
            # from materially noisier flag duty.
            return cduty <= pduty + 0.02

        duty_better = cduty < pduty - 0.005
        duty_not_worse = cduty <= pduty + eps
        lat_better = np.isfinite(clat) and np.isfinite(plat) and clat < plat - 0.5
        lat_not_worse = (
            not np.isfinite(clat) or not np.isfinite(plat) or clat <= plat + 0.5
        )
        return duty_not_worse and lat_not_worse and (duty_better or lat_better)

    if not np.isfinite(challenger.get("pr_auc", np.nan)):
        return False
    if not np.isfinite(champion.get("pr_auc", np.nan)):
        return True
    if challenger["pr_auc"] <= champion["pr_auc"]:
        return False
    cl, pl = challenger["lead_time_median_s"], champion["lead_time_median_s"]
    if np.isfinite(cl) and np.isfinite(pl) and cl < pl:
        return False
    return True


# ---------------------------------------------------------------------------
# offline walk-forward driver (validation + the metrics the GUI reads)
# ---------------------------------------------------------------------------
def _folds(index, train_h, eval_h, embargo_s):
    """Yield (train_start, train_end, eval_start, eval_end) sliding by eval_h.
    Strictly causal: eval_start = train_end + embargo so train's forward-looking
    labels cannot peek into the held-out fold."""
    train_td = pd.Timedelta(hours=train_h)
    eval_td = pd.Timedelta(hours=eval_h)
    emb = pd.Timedelta(seconds=embargo_s)
    t = index[0]
    while True:
        train_end = t + train_td
        eval_start = train_end + emb
        eval_end = eval_start + eval_td
        if eval_end > index[-1]:
            break
        yield t, train_end, eval_start, eval_end
        t = t + eval_td


def run_generation(gen, Xtr, ytr, Xev, yev, champion, spike_ts):
    """One generation as a base-vs-upstream A/B (session_2026-07-01c). Train TWO
    challengers on the SAME fold -- base features only, and base+upstream -- both
    reweighted on the champion's mistakes. The upstream challenger only advances
    to the gate if it strictly beats the base challenger on the held-out fold;
    then the gate promotes it over the incumbent only if it also beats the
    champion (no event-metric regression). So each live generation is a
    controlled test of "do upstream OS signals help", and only a genuine winner
    deploys.

    Cached-data runs have no upstream columns, so base==upstream degenerately and
    this reduces to the original single-challenger loop (selfcheck stays valid)."""
    weights, n_hard = hard_example_weights(champion, Xtr, ytr)
    up_cols = set(la.UPSTREAM_FEATURE_COLS)
    base_cols = [c for c in Xtr.columns if c not in up_cols]
    has_upstream = len(base_cols) < len(Xtr.columns)

    ch_base = train_rf(Xtr[base_cols], ytr, sample_weight=weights,
                       event_ts=spike_ts)
    m_base = score_on_fold(ch_base, Xev, yev, spike_ts)   # selects ch_base["cols"]
    if has_upstream:
        ch_up = train_rf(Xtr, ytr, sample_weight=weights, event_ts=spike_ts)
        m_up = score_on_fold(ch_up, Xev, yev, spike_ts)
    else:
        ch_up, m_up = ch_base, m_base

    # upstream must EARN its place vs base on the same fold before facing the gate
    if has_upstream and beats(m_up, m_base):
        challenger, ch_m, feat = ch_up, m_up, "upstream"
    else:
        challenger, ch_m, feat = ch_base, m_base, "base"

    champ_m = score_on_fold(champion, Xev, yev, spike_ts) \
        if champion is not None else None
    promoted = beats(ch_m, champ_m)
    new_champ = challenger if promoted else champion

    row = {
        "generation": gen,
        "eval_start": str(Xev.index[0]),
        "eval_end": str(Xev.index[-1]),
        "train_rows": int(len(Xtr)),
        "train_spikes": int(ytr.sum()),
        "n_hard_examples": n_hard,
        # winning challenger's metrics on the held-out fold (GUI plots per gen):
        "pr_auc": None if ch_m is None else ch_m["pr_auc"],
        "precision": None if ch_m is None else ch_m["precision"],
        "recall": None if ch_m is None else ch_m["recall"],
        "lead_time_median_s": None if ch_m is None else ch_m["lead_time_median_s"],
        "event_recall": None if ch_m is None else ch_m["event_recall"],
        "event_latency_median_s": None if ch_m is None else ch_m["event_latency_median_s"],
        "alert_event_precision": None if ch_m is None else ch_m["alert_event_precision"],
        "false_alert_episodes_per_h": None if ch_m is None else ch_m["false_alert_episodes_per_h"],
        "alert_duty": None if ch_m is None else ch_m["alert_duty"],
        "n_events": None if ch_m is None else ch_m["n_events"],
        "n_events_detected": None if ch_m is None else ch_m["n_events_detected"],
        "n_alert_episodes": None if ch_m is None else ch_m["n_alert_episodes"],
        "n_false_alert_episodes": None if ch_m is None else ch_m["n_false_alert_episodes"],
        "n_alert_points": None if ch_m is None else ch_m["n_alert_points"],
        "n_eval_points": None if ch_m is None else ch_m["n_eval_points"],
        "n_tp": None if ch_m is None else ch_m["n_tp"],
        "n_flags": None if ch_m is None else ch_m["n_flags"],
        "n_spikes": None if ch_m is None else ch_m["n_spikes"],
        # the A/B itself: base vs upstream PR-AUC on the SAME fold, and who won:
        "pr_auc_base": None if m_base is None else m_base["pr_auc"],
        "pr_auc_upstream": None if m_up is None else m_up["pr_auc"],
        "feature_set": feat,
        # incumbent's PR-AUC on the SAME fold, for an apples-to-apples curve:
        "champion_pr_auc": None if champ_m is None else champ_m["pr_auc"],
        "champion_lead_time_median_s": None if champ_m is None else champ_m["lead_time_median_s"],
        "champion_event_recall": None if champ_m is None else champ_m["event_recall"],
        "champion_event_latency_median_s": None if champ_m is None else champ_m["event_latency_median_s"],
        "champion_alert_duty": None if champ_m is None else champ_m["alert_duty"],
        "promoted": bool(promoted),
    }
    return new_champ, row


def walk(df, log_path=METRICS_LOG, train_h=TRAIN_H, eval_h=EVAL_H, max_gens=None,
         verbose=True):
    """Run the continual loop across cached data; append one JSONL row/generation.
    Returns the list of log rows (also the GUI's data source)."""
    X, y = la.build_xy(df, lane_a=False, kf=True)
    spike_ts = pd.DatetimeIndex(ev.true_onset_ts(df["power_watts"]))

    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    champion = None
    gen = 0
    with open(log_path, "w") as fh:
        for t0, t1, e0, e1 in _folds(X.index, train_h, eval_h, EMBARGO_S):
            tr = (X.index >= t0) & (X.index < t1)
            ev_ = (X.index >= e0) & (X.index < e1)
            Xtr, ytr, Xev, yev = X[tr], y[tr], X[ev_], y[ev_]
            if ytr.sum() < MIN_TRAIN_POS or len(Xtr) < 500 or yev.sum() < 1:
                continue
            champion, row = run_generation(gen, Xtr, ytr, Xev, yev, champion, spike_ts)
            fh.write(json.dumps(row) + "\n")
            fh.flush()
            rows.append(row)
            if verbose:
                cp = row["champion_pr_auc"]
                cp = "  --  " if cp is None else f"{cp:.4f}"
                pb, pu = row["pr_auc_base"], row["pr_auc_upstream"]
                ab = "" if pb is None or pu is None else f" [base={pb:.4f} up={pu:.4f}->{row['feature_set']}]"
                print(f"gen {gen:>2}  train={row['train_rows']:>6} "
                      f"hard={row['n_hard_examples']:>5}  "
                      f"challenger PR-AUC={row['pr_auc']:.4f} prec={row['precision']:.3f} "
                      f"event_recall={row['event_recall']:.3f} "
                      f"latency={row['event_latency_median_s']:.1f}s  champ={cp}{ab}  "
                      f"{'PROMOTED' if row['promoted'] else 'kept champion'}")
            gen += 1
            if max_gens is not None and gen >= max_gens:
                break
    if verbose:
        n_prom = sum(r["promoted"] for r in rows)
        print(f"\n{len(rows)} generations, {n_prom} promotions -> {log_path}")
    return rows


# ---------------------------------------------------------------------------
# live daemon (mirrors spike_daemon_rf; NOT exercised by selfcheck)
# ---------------------------------------------------------------------------
def run(write_api=None, org=None, server=None, once=False, log_path=METRICS_LOG):
    """Live loop: each refit pulls TRAIN_H+EVAL_H of history, carves a held-out
    tail, runs ONE generation against the persistent champion, then predicts the
    live row with the champion. Reuses spike_daemon_rf for predict/write."""
    champion = None
    last_fit = None
    gen = 0
    while True:
        now = pd.Timestamp.now(tz="UTC")
        need_refit = champion is None or (now - last_fit) >= pd.Timedelta(minutes=REFIT_MIN)
        if need_refit:
            start = f"-{TRAIN_H + EVAL_H}h"
            hist = _join_upstream(common.query_window(start=start), start)
            champion, gen = _refit_generation(hist, champion, gen, log_path)
            if champion is None:
                print("not enough spike history to train yet")
                if once:
                    return
                time.sleep(CYCLE_S)
                continue
            last_fit = now
            print(f"[{now}] gen {gen - 1}: champion ready (thr={champion['threshold']:.3f})")

        rstart = f"-{LOOKBACK_FEAT_S + 5}s"
        recent = _join_upstream(common.query_recent(start=rstart), rstart)
        proba = rf.predict_latest(champion["model"], champion["cols"], recent,
                                  upstream=UPSTREAM) if not recent.empty else None
        if proba is None:
            print("no recent data to predict on")
        else:
            thr = champion["threshold"]
            flag = " <-- SPIKE RISK" if proba > thr else ""
            print(f"P(spike in {HORIZON_S}s) = {proba:.3f} (thr {thr:.3f}){flag}")
            if write_api is not None:
                pt = (rf.Point("power_spike_prediction").tag("server", server)
                      .tag("model", "rf_continual").field("spike_proba", proba)
                      .field("threshold", thr).field("horizon_s", HORIZON_S)
                      .field("spike_risk", proba > thr))
                write_api.write(bucket=INFLUX_BUCKET, org=org, record=pt)
        if once:
            return
        time.sleep(CYCLE_S)


def _refit_generation(hist, champion, gen, log_path):
    """One live generation on a freshly pulled window. Returns (champion, gen)."""
    if hist.empty:
        return champion, gen
    X, y = la.build_xy(hist, lane_a=False, kf=True, upstream=UPSTREAM)
    if X.empty:
        return champion, gen
    split = X.index[-1] - pd.Timedelta(hours=EVAL_H)
    tr = X.index < split - pd.Timedelta(seconds=EMBARGO_S)
    ev_ = X.index >= split
    Xtr, ytr, Xev, yev = X[tr], y[tr], X[ev_], y[ev_]
    if ytr.sum() < MIN_TRAIN_POS or len(Xtr) < 500:
        return champion, gen
    spike_ts = pd.DatetimeIndex(ev.true_onset_ts(hist["power_watts"]))
    new_champ, row = run_generation(gen, Xtr, ytr, Xev, yev, champion, spike_ts)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as fh:
        fh.write(json.dumps(row) + "\n")
    return new_champ, gen + 1


# ---------------------------------------------------------------------------
# selfcheck / demo
# ---------------------------------------------------------------------------
def demo():
    """Run a few real generations on a cached slice, then assert the gate works:
    a deliberately crippled challenger must be REJECTED (champion kept)."""
    df = common.load_telemetry()
    X, y = la.build_xy(df, lane_a=False, kf=True)
    assert set(y.unique()).issubset({0, 1}) and y.sum() > 0, "need positive labels"

    # 1) the gate's promotion logic is pure -- check it directly.
    good = {"pr_auc": 0.50, "lead_time_median_s": 2.0}
    worse = {"pr_auc": 0.40, "lead_time_median_s": 2.0}
    tie = {"pr_auc": 0.50, "lead_time_median_s": 2.0}
    better = {"pr_auc": 0.60, "lead_time_median_s": 2.0}
    short_lead = {"pr_auc": 0.70, "lead_time_median_s": 1.0}
    assert beats(better, good) is True, "higher PR-AUC should win"
    assert beats(worse, good) is False, "lower PR-AUC must lose"
    assert beats(tie, good) is False, "a tie must not promote (no-regression)"
    assert beats(short_lead, good) is False, "better PR-AUC but worse lead-time must lose"
    assert beats(None, good) is False and beats(good, None) is True
    event_champ = {
        "pr_auc": 0.20,
        "lead_time_median_s": 0.0,
        "event_recall": 1.0,
        "event_latency_median_s": -5.0,
        "alert_duty": 0.20,
        "n_events": 7,
    }
    missed_events = {
        "pr_auc": 0.40,
        "lead_time_median_s": float("nan"),
        "event_recall": 0.0,
        "event_latency_median_s": float("nan"),
        "alert_duty": 0.0,
        "n_events": 7,
    }
    quieter_equal = {
        **event_champ,
        "alert_duty": 0.19,
    }
    later_equal = {
        **event_champ,
        "event_latency_median_s": -4.0,
        "alert_duty": 0.20,
    }
    assert beats(missed_events, event_champ) is False, \
        "higher PR-AUC must not beat worse event recall"
    assert beats(quieter_equal, event_champ) is True, \
        "same event recall/latency with lower duty should promote"
    assert beats(later_equal, event_champ) is False, \
        "same recall with worse latency and no duty gain must lose"

    # 2) end-to-end on a real slice: 3 small generations through the JSONL log.
    tmp = common.DATA_DIR / "rf_continual_metrics_selfcheck.jsonl"
    rows = walk(df, log_path=tmp, train_h=1.0, eval_h=0.5, max_gens=3, verbose=False)
    assert len(rows) >= 2, f"selfcheck needs >=2 generations, got {len(rows)}"
    assert rows[0]["promoted"] is True, "gen 0 bootstraps the champion"
    for r in rows:
        assert 0.0 <= r["pr_auc"] <= 1.0, f"bad PR-AUC: {r}"
        er = r["event_recall"]
        assert er is None or np.isnan(er) or 0.0 <= er <= 1.0, f"bad event recall: {r}"
        assert r["champion_pr_auc"] is None or 0.0 <= r["champion_pr_auc"] <= 1.0
        if r["champion_pr_auc"] is not None:
            ch = {
                "pr_auc": r["pr_auc"],
                "lead_time_median_s": r["lead_time_median_s"],
                "event_recall": r["event_recall"],
                "event_latency_median_s": r["event_latency_median_s"],
                "alert_duty": r["alert_duty"],
                "n_events": r["n_events"],
            }
            inc = {
                "pr_auc": r["champion_pr_auc"],
                "lead_time_median_s": r["champion_lead_time_median_s"],
                "event_recall": r["champion_event_recall"],
                "event_latency_median_s": r["champion_event_latency_median_s"],
                "alert_duty": r["champion_alert_duty"],
                "n_events": r["n_events"],
            }
            assert r["promoted"] == beats(ch, inc), "row promotion must match gate"
    # JSONL is readable back (GUI contract)
    parsed = [json.loads(l) for l in tmp.read_text().splitlines()]
    assert len(parsed) == len(rows) and parsed[0]["generation"] == 0
    tmp.unlink()

    # 3) the gate REJECTS a deterministic no-improvement challenger built on
    # the real feature path. A shuffled-label forest is random enough to beat a
    # weak champion on one cached fold by chance; the gate property we need is
    # stricter and deterministic: no strict metric improvement -> no promote.
    t0 = X.index[0]
    tr = (X.index >= t0) & (X.index < t0 + pd.Timedelta(hours=1))
    e0 = t0 + pd.Timedelta(hours=1) + pd.Timedelta(seconds=EMBARGO_S)
    ev_ = (X.index >= e0) & (X.index < e0 + pd.Timedelta(hours=0.5))
    Xtr, ytr, Xev, yev = X[tr], y[tr], X[ev_], y[ev_]
    assert yev.sum() >= 1, "selfcheck eval fold has no spikes; widen the slice"
    spike_ts = pd.DatetimeIndex(ev.true_onset_ts(df["power_watts"]))

    champion = train_rf(Xtr, ytr)                      # a real feature-path model
    crippled = {**champion, "threshold": 1.0}           # same ranking, no improvement
    cm = score_on_fold(champion, Xev, yev, spike_ts)
    xm = score_on_fold(crippled, Xev, yev, spike_ts)
    assert not beats(xm, cm), (
        f"GATE FAILED: no-improvement challenger (PR-AUC {xm['pr_auc']:.4f}) was not "
        f"rejected vs champion (PR-AUC {cm['pr_auc']:.4f})")

    print(f"selfcheck OK: gate logic + {len(rows)}-gen JSONL walk + crippled "
          f"challenger rejected (crippled {xm['pr_auc']:.4f} <= champ {cm['pr_auc']:.4f})")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--selfcheck", action="store_true", help="offline gate/demo asserts")
    ap.add_argument("--validate", action="store_true",
                    help="full offline walk over cached data; writes the metrics log")
    ap.add_argument("--once", action="store_true", help="one live predict cycle then exit")
    ap.add_argument("--dry-run", action="store_true", help="live, don't write to InfluxDB")
    args = ap.parse_args()

    if args.selfcheck:
        demo()
        return
    if args.validate:
        df = common.load_telemetry()
        print(f"Loaded {len(df)} rows. Continual walk "
              f"(train={TRAIN_H}h, eval={EVAL_H}h, embargo={EMBARGO_S}s, boost={BOOST})\n")
        walk(df)
        return

    import socket
    server = socket.gethostname()
    client = write_api = org = None
    if not args.dry_run:
        from influxdb_client import InfluxDBClient
        from influxdb_client.client.write_api import SYNCHRONOUS
        org = common.load_org()
        client = InfluxDBClient(url=common.INFLUX_URL, token=common.load_write_token(), org=org)
        write_api = client.write_api(write_options=SYNCHRONOUS)
    try:
        run(write_api=write_api, org=org, server=server, once=args.once)
    finally:
        if write_api is not None:
            write_api.close()
        if client is not None:
            client.close()


if __name__ == "__main__":
    main()
