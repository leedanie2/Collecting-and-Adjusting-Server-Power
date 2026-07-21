#!/usr/bin/env python3
"""Remote model trainer: pull data from InfluxDB, fit challenger, gate vs champion.

Runs on a different server (not mycroft). Pulls data via InfluxDB HTTP, trains
a new model (with hard-example reweighting), scores it against the current
champion via walk-forward validation, and atomically promotes it via symlink
swap if it's an improvement.

Model versioning:
  - Model files: models/spike_model_v001.pkl, v002.pkl, etc. (pickle + timestamp)
  - Current symlink: models/spike_model_current -> spike_model_vXXX.pkl
  - Scorer on mycroft: reads from the symlink, safe fallback if broken

This reuses the champion-challenger gate from spike_daemon_rf_continual.py
and the eval framework from eval.py.
"""

import argparse
import json
import pickle
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from core import telemetry as common
from validation import evaluation as ev
from core import features as la
from detectors.random_forest import live_detector as rf
from detectors.random_forest.continual_detector import (
    hard_example_weights,
    score_on_fold,
    train_rf,
    beats,
    TRAIN_H,
    EVAL_H,
    EMBARGO_S,
    MIN_TRAIN_POS,
    ALARM_RATE,
    BOOST,
    UPSTREAM,
)

MODEL_DIR = common.DATA_DIR / "models"
SYMLINK_PATH = MODEL_DIR / "spike_model_current"
METRICS_LOG = common.DATA_DIR / "trainer_metrics.jsonl"
BULK_QUERY_TIMEOUT_MS = 600_000
QUERY_CHUNK_H = 6
MIN_GATE_EVENTS = 1
AUX_DIRECTION_MIN_POS = 10
AUX_DIRECTION_MAX_ROWS = 40_000
AUX_DIRECTION_NEG_PER_POS = 10
TRAINING_WINDOW_LABEL = (
    f"weekdays {common.TRAINING_START_HOUR:02d}:00-"
    f"{common.TRAINING_END_HOUR:02d}:00 {common.TRAINING_TZ}"
)


def ensure_model_dir():
    MODEL_DIR.mkdir(parents=True, exist_ok=True)


def _parse_flux_time(value, now=None):
    now = now or pd.Timestamp.now(tz="UTC")
    if value == "now()":
        return now
    if isinstance(value, str) and value.startswith("-"):
        unit = value[-1]
        amount = int(value[1:-1])
        if unit == "s":
            return now - pd.Timedelta(seconds=amount)
        if unit == "m":
            return now - pd.Timedelta(minutes=amount)
        if unit == "h":
            return now - pd.Timedelta(hours=amount)
        if unit == "d":
            return now - pd.Timedelta(days=amount)
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def _flux_ts(ts):
    return pd.Timestamp(ts).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def _query_chunked(query_fn, start, stop="now()", chunk_h=QUERY_CHUNK_H,
                   timeout_ms=BULK_QUERY_TIMEOUT_MS, **kwargs):
    """Pull long training ranges in small absolute windows.

    InfluxDB handles 6 h aggregate windows reliably from the laptop, while
    multi-day windows can hang before returning the first byte. Chunking keeps
    the off-box trainer usable without moving training back onto mycroft.
    """
    now = pd.Timestamp.now(tz="UTC")
    t0 = _parse_flux_time(start, now=now)
    t1 = _parse_flux_time(stop, now=now)
    frames = []
    cur = t0
    step = pd.Timedelta(hours=chunk_h)
    while cur < t1:
        nxt = min(cur + step, t1)
        df = query_fn(start=_flux_ts(cur), stop=_flux_ts(nxt),
                      timeout_ms=timeout_ms, **kwargs)
        if not df.empty:
            frames.append(df)
        cur = nxt
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames).sort_index().loc[lambda x: ~x.index.duplicated(keep="last")]


def load_champion():
    """Load the current champion model from symlink. Returns (model_dict, version_str)
    or (None, None) if symlink missing/broken."""
    if not SYMLINK_PATH.is_symlink():
        return None, None
    try:
        target = SYMLINK_PATH.resolve()
        if not target.exists():
            return None, None
        with open(target, "rb") as f:
            data = pickle.load(f)
        if isinstance(data, dict) and "threshold" in data:
            data["threshold"] = rf.threshold_floor(float(data["threshold"]))
            for direction in ("rise", "drop"):
                key = f"{direction}_threshold"
                if key in data:
                    data[key] = rf.threshold_floor(float(data[key]))
        version = target.stem.replace("spike_model_", "")
        return data, version
    except Exception as e:
        print(f"Warning: failed to load champion: {e}", file=sys.stderr)
        return None, None


def select_gate_windows(folds):
    """Train on folds[-2]'s TRAIN window; judge on the two strictly held-out
    TEST windows after it (folds[-2].test + folds[-1].test = ~2*EVAL_H causal
    tail, mean-aggregated). The pre-2026-07-08 code evaluated on folds[-1][0]
    -- the last fold's TRAIN window, 4 of whose 6 h lay inside the challenger's
    own training data -- so the gate largely measured memorization and was
    biased toward promoting challengers."""
    train_df = folds[-2][0]
    return train_df, [folds[-2][1], folds[-1][1]]


def gate_window_candidates(folds):
    """Yield recent causal gate candidates as (fold_index, train_df, eval_frames).

    Candidate i trains on folds[i].train and gates on folds[i].test plus
    folds[i+1].test. Iterate backward so a quiet latest slice does not block a
    useful daily retrain when an earlier recent slice is viable.
    """
    for i in range(len(folds) - 2, -1, -1):
        yield i, folds[i][0], [folds[i][1], folds[i + 1][1]]


def _event_count_in_frames(spike_ts, frames):
    nonempty = [f for f in frames if not f.empty]
    if not nonempty:
        return 0
    start = min(f.index.min() for f in nonempty)
    end = max(f.index.max() for f in nonempty)
    return int(((spike_ts >= start) & (spike_ts <= end)).sum())


def select_viable_gate_data(folds, df, upstream=UPSTREAM,
                            min_train_pos=MIN_TRAIN_POS,
                            min_train_rows=500,
                            min_gate_events=MIN_GATE_EVENTS):
    """Build feature matrices for the most recent gate candidate worth judging.

    The previous trainer always used the final candidate. That is brittle after
    quiet periods: a daily run can have plenty of usable data in the 7-day pull
    but zero positives in the latest 6-hour train slice. This walks backward to
    the most recent strictly-causal train/eval pair with enough training labels
    and at least one raw onset in the gate window.
    """
    spike_ts = pd.DatetimeIndex(ev.true_onset_ts(df["power_watts"]))
    skipped = []
    for fold_index, train_df, eval_frames in gate_window_candidates(folds):
        X_train, y_train = la.build_xy(train_df, lane_a=False, kf=True,
                                       upstream=upstream)
        train_pos = int((y_train == 1).sum())
        if len(X_train) < min_train_rows:
            skipped.append((fold_index, f"train_rows={len(X_train)}"))
            continue
        if train_pos < min_train_pos:
            skipped.append((fold_index, f"train_pos={train_pos}"))
            continue

        fold_xy = [la.build_xy(ef, lane_a=False, kf=True, upstream=upstream)
                   for ef in eval_frames]
        eval_rows = int(sum(len(Xe) for Xe, _ in fold_xy))
        gate_events = _event_count_in_frames(spike_ts, eval_frames)
        if eval_rows == 0:
            skipped.append((fold_index, "eval_rows=0"))
            continue
        if gate_events < min_gate_events:
            skipped.append((fold_index, f"gate_events={gate_events}"))
            continue

        return {
            "fold_index": fold_index,
            "train_df": train_df,
            "eval_frames": eval_frames,
            "X_train": X_train,
            "y_train": y_train,
            "fold_xy": fold_xy,
            "spike_ts": spike_ts,
            "train_pos": train_pos,
            "eval_rows": eval_rows,
            "eval_pos": int(sum((ye == 1).sum() for _, ye in fold_xy)),
            "gate_events": gate_events,
            "skipped_recent_candidates": len(skipped),
            "skipped_reasons": skipped[:5],
        }
    return None


def score_on_folds(champ, fold_xy, spike_ts):
    """Aggregate score_on_fold across held-out folds.

    PR-AUC may be undefined on quiet folds, but those folds still count toward
    false alerts and duty. Returns a dict beats() accepts plus event metrics and
    n_folds, or None if no fold had rows.
    """
    scores = [s for s in (score_on_fold(champ, Xe, ye, spike_ts)
                          for Xe, ye in fold_xy) if s is not None]
    if not scores:
        return None
    keys = (
        "pr_auc", "precision", "recall", "lead_time_median_s",
        "event_latency_median_s", "event_latency_p25_s", "event_latency_p75_s",
        "event_pre_onset_frac",
    )

    def finite_mean(k):
        vals = np.asarray([s.get(k, np.nan) for s in scores], dtype=float)
        vals = vals[np.isfinite(vals)]
        return float(vals.mean()) if len(vals) else float("nan")

    agg = {k: finite_mean(k) for k in keys}
    n_events = int(sum(s.get("n_events", 0) for s in scores))
    n_detected = int(sum(s.get("n_events_detected", 0) for s in scores))
    n_alerts = int(sum(s.get("n_alert_episodes", 0) for s in scores))
    n_false = int(sum(s.get("n_false_alert_episodes", 0) for s in scores))
    n_alert_points = int(sum(s.get("n_alert_points", 0) for s in scores))
    n_eval_points = int(sum(s.get("n_eval_points", 0) for s in scores))
    hours = float(np.nansum([s.get("event_eval_hours", np.nan) for s in scores]))
    agg.update({
        "event_recall": n_detected / n_events if n_events else float("nan"),
        "alert_event_precision": (n_alerts - n_false) / n_alerts if n_alerts else float("nan"),
        "false_alert_episodes_per_h": n_false / hours if hours > 0 else float("nan"),
        "alert_duty": n_alert_points / n_eval_points if n_eval_points else float("nan"),
        "n_events": n_events,
        "n_events_detected": n_detected,
        "n_alert_episodes": n_alerts,
        "n_false_alert_episodes": n_false,
        "n_alert_points": n_alert_points,
        "n_eval_points": n_eval_points,
        "event_eval_hours": hours,
    })
    agg["n_folds"] = len(scores)
    return agg


def _direction_label_for_X(train_df, X, direction):
    y = ev.triple_barrier_label(train_df["power_watts"], rf.HORIZON_S,
                                direction=direction)
    y = y.reindex(X.index).dropna().astype(int)
    return X.loc[y.index], y


def _balanced_direction_rows(X, y, max_rows=AUX_DIRECTION_MAX_ROWS,
                             neg_per_pos=AUX_DIRECTION_NEG_PER_POS):
    pos = y[y == 1].index
    neg = y[y == 0].index
    if len(pos) == 0 or len(neg) == 0:
        return X.loc[y.index], y
    max_neg = max_rows - len(pos)
    max_neg = min(len(neg), max_neg, max(len(pos), neg_per_pos * len(pos)))
    if max_neg <= 0:
        keep = pos[-max_rows:]
    else:
        keep = pos.union(neg[-max_neg:]).sort_values()
    return X.loc[keep], y.loc[keep]


def attach_direction_heads(model_dict, train_df, X_train,
                           min_pos=AUX_DIRECTION_MIN_POS):
    """Add signed rise/drop auxiliary RF heads to a champion dict.

    These heads share the same strictly-causal features as the combined spike
    model, but learn signed labels. They are advisory until a consumer chooses
    to use `rise_risk`/`drop_risk`; the legacy `model`/`threshold` contract is
    unchanged for champion gating and the existing smoother trigger.
    """
    out = dict(model_dict)
    out.setdefault("label_schema", "signed_v1")
    for direction in ("rise", "drop"):
        X_dir, y_dir = _direction_label_for_X(train_df, X_train, direction)
        X_dir, y_dir = _balanced_direction_rows(X_dir, y_dir)
        n_pos = int((y_dir == 1).sum())
        n_neg = int((y_dir == 0).sum())
        if n_pos < min_pos or n_neg < min_pos:
            out[f"{direction}_head_status"] = (
                f"skipped: pos={n_pos} neg={n_neg} min_pos={min_pos}"
            )
            continue
        head = train_rf(X_dir, y_dir)
        out[f"{direction}_model"] = head["model"]
        out[f"{direction}_cols"] = head["cols"]
        out[f"{direction}_threshold"] = head["threshold"]
        out[f"{direction}_train_positives"] = n_pos
        out[f"{direction}_train_rows"] = int(len(y_dir))
        out[f"{direction}_head_status"] = "trained"
    return out


def save_model(model_dict, version):
    """Save model as pickle. Returns the Path."""
    model_path = MODEL_DIR / f"spike_model_{version}.pkl"
    with open(model_path, "wb") as f:
        pickle.dump(model_dict, f, protocol=pickle.HIGHEST_PROTOCOL)
    return model_path


def atomic_swap(new_model_path, old_path=SYMLINK_PATH):
    """Atomically swap the symlink to point to new model.
    Uses temp symlink + rename for atomicity (POSIX guaranteed)."""
    temp_link = old_path.parent / f"{old_path.name}.tmp"
    try:
        if temp_link.exists():
            temp_link.unlink()
        temp_link.symlink_to(new_model_path.name)
        # atomic rename: this is the critical section
        temp_link.replace(old_path)
    except Exception as e:
        print(f"Error: atomic swap failed: {e}", file=sys.stderr)
        if temp_link.exists():
            temp_link.unlink()
        raise


def train_cycle(start_str="-72h", stop_str="now()", dry_run=False):
    """Single training cycle: pull data, fit challenger, gate, promote if better.

    Returns dict with cycle metadata (champion_version, event metrics, promoted, etc.).
    """
    ensure_model_dir()
    champion_model, champion_version = load_champion()

    print(f"Pulling data from {start_str} to {stop_str}...")
    df_base = _query_chunked(
        common.query_window,
        start=start_str,
        stop=stop_str,
        # pdu_power deliberately NOT pulled: 2026-07-14 ablation showed pdu
        # features redundant-to-harmful (collinear with power lags); the
        # feature code degrades gracefully when the column is absent.
        measurements=("cpu_power", "cpu_usage"),
        timeout_ms=BULK_QUERY_TIMEOUT_MS,
    )
    if df_base.empty:
        print(f"Error: empty data from {start_str} to {stop_str}")
        return None

    if UPSTREAM:
        print("Joining upstream signals...")
        df_upstream = _query_chunked(
            common.query_upstream,
            start=start_str,
            stop=stop_str,
            timeout_ms=BULK_QUERY_TIMEOUT_MS,
        )
        if df_upstream.empty:
            df = df_base
        else:
            df_upstream = df_upstream.drop(
                columns=df_upstream.columns.intersection(df_base.columns)
            )
            df = df_base.join(df_upstream, how="left")
    else:
        df = df_base

    raw_samples = len(df)
    df = common.weekday_business_hours(df)
    print(f"Loaded {raw_samples} samples; training/eval window "
          f"{TRAINING_WINDOW_LABEL}: {len(df)} samples")
    if df.empty:
        print(f"Error: no samples inside {TRAINING_WINDOW_LABEL}")
        return None

    # Prepare folds; select the most recent viable train/gate pair.
    print("Preparing walk-forward folds...")

    # Walk-forward: train on last fold, evaluate on next
    folds = list(ev.walk_forward_folds(
        df,
        train_min=TRAIN_H * 60,
        step_s=EVAL_H * 3600,
    ))

    if len(folds) < 2:
        print("Warning: too few folds, skipping cycle")
        return None

    gate = select_viable_gate_data(folds, df, upstream=UPSTREAM)
    if gate is None:
        print("Warning: no viable gate window found; skipping cycle")
        return None
    if gate["skipped_recent_candidates"]:
        print(f"Selected older viable gate fold {gate['fold_index']} after skipping "
              f"{gate['skipped_recent_candidates']} recent quiet/invalid candidates")

    X_train = gate["X_train"]
    y_train = gate["y_train"]
    fold_xy = gate["fold_xy"]
    spike_ts = gate["spike_ts"]
    gate_eval_start = min(ef.index.min() for ef in gate["eval_frames"] if not ef.empty)
    aux_train_df = df[df.index < gate_eval_start]
    X_aux, _ = la.build_xy(aux_train_df, lane_a=False, kf=True, upstream=UPSTREAM)

    # Hard-example reweighting if champion exists
    if champion_model is not None:
        w_challenge, n_hard = hard_example_weights(champion_model, X_train, y_train)
    else:
        w_challenge, n_hard = None, 0

    print(f"Training challenger (hard_examples={n_hard})...")
    challenger = train_rf(X_train, y_train, sample_weight=w_challenge,
                          event_ts=spike_ts)
    challenger = attach_direction_heads(challenger, aux_train_df, X_aux)
    for direction in ("rise", "drop"):
        print(f"{direction.capitalize()} head: "
              f"{challenger.get(f'{direction}_head_status', 'missing')}")

    print("Scoring challenger on held-out folds...")
    challenger_score = score_on_folds(challenger, fold_xy, spike_ts)

    promoted = False
    promoted_version = None

    if challenger_score is None:
        print("Challenger had no positives in eval folds, skipping promotion")
    else:
        champion_score = None
        if champion_model is not None:
            print("Scoring champion on held-out folds...")
            champion_score = score_on_folds(champion_model, fold_xy, spike_ts)

        if beats(challenger_score, champion_score):
            print("Challenger beats champion! Promoting...")
            version = datetime.now().strftime("v%Y%m%d_%H%M%S")
            model_path = save_model(challenger, version)

            if not dry_run:
                try:
                    atomic_swap(model_path)
                    promoted = True
                    promoted_version = version
                    print(f"Promoted to {version}")
                except Exception as e:
                    print(f"Error during promotion: {e}", file=sys.stderr)
            else:
                print(f"DRY RUN: would promote to {version}")
                promoted_version = version
        else:
            print("Challenger does not beat champion, keeping current model")

    # Log metrics
    cycle_result = {
        "timestamp": datetime.now().isoformat(),
        "champion_version": champion_version,
        "data_samples": len(df),
        "train_samples": len(X_train),
        "aux_train_samples": len(X_aux),
        "eval_samples": int(sum(len(Xe) for Xe, _ in fold_xy)),
        "train_positives": int((y_train == 1).sum()),
        "eval_positives": int(sum((ye == 1).sum() for _, ye in fold_xy)),
        "gate_fold_index": int(gate["fold_index"]),
        "gate_events": int(gate["gate_events"]),
        "skipped_recent_gate_candidates": int(gate["skipped_recent_candidates"]),
        "gate_folds": len(fold_xy),
        "hard_examples": n_hard,
        "rise_head_status": challenger.get("rise_head_status"),
        "drop_head_status": challenger.get("drop_head_status"),
        "rise_train_positives": challenger.get("rise_train_positives"),
        "drop_train_positives": challenger.get("drop_train_positives"),
        "rise_train_rows": challenger.get("rise_train_rows"),
        "drop_train_rows": challenger.get("drop_train_rows"),
        "threshold": float(challenger.get("threshold", np.nan)),
        "threshold_calibration": challenger.get("threshold_calibration"),
        "promoted": promoted,
        "promoted_version": promoted_version,
    }

    if challenger_score:
        cycle_result.update({
            "challenger_pr_auc": float(challenger_score.get("pr_auc", np.nan)),
            "challenger_precision": float(challenger_score.get("precision", np.nan)),
            "challenger_recall": float(challenger_score.get("recall", np.nan)),
            "challenger_lead_time_s": float(challenger_score.get("lead_time_median_s", np.nan)),
            "challenger_event_recall": float(challenger_score.get("event_recall", np.nan)),
            "challenger_event_latency_s": float(challenger_score.get("event_latency_median_s", np.nan)),
            "challenger_alert_precision": float(challenger_score.get("alert_event_precision", np.nan)),
            "challenger_false_alerts_per_h": float(challenger_score.get("false_alert_episodes_per_h", np.nan)),
            "challenger_alert_duty": float(challenger_score.get("alert_duty", np.nan)),
            "challenger_events_detected": int(challenger_score.get("n_events_detected", 0)),
            "challenger_events": int(challenger_score.get("n_events", 0)),
            "challenger_alert_episodes": int(challenger_score.get("n_alert_episodes", 0)),
            "challenger_false_alert_episodes": int(challenger_score.get("n_false_alert_episodes", 0)),
        })

    if champion_score:
        cycle_result.update({
            "champion_pr_auc": float(champion_score.get("pr_auc", np.nan)),
            "champion_precision": float(champion_score.get("precision", np.nan)),
            "champion_recall": float(champion_score.get("recall", np.nan)),
            "champion_lead_time_s": float(champion_score.get("lead_time_median_s", np.nan)),
            "champion_event_recall": float(champion_score.get("event_recall", np.nan)),
            "champion_event_latency_s": float(champion_score.get("event_latency_median_s", np.nan)),
            "champion_alert_precision": float(champion_score.get("alert_event_precision", np.nan)),
            "champion_false_alerts_per_h": float(champion_score.get("false_alert_episodes_per_h", np.nan)),
            "champion_alert_duty": float(champion_score.get("alert_duty", np.nan)),
            "champion_events_detected": int(champion_score.get("n_events_detected", 0)),
            "champion_events": int(champion_score.get("n_events", 0)),
            "champion_alert_episodes": int(champion_score.get("n_alert_episodes", 0)),
            "champion_false_alert_episodes": int(champion_score.get("n_false_alert_episodes", 0)),
        })

    return cycle_result


def log_cycle(result):
    """Append cycle result to metrics log."""
    if result is None:
        return
    METRICS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(METRICS_LOG, "a") as f:
        f.write(json.dumps(result) + "\n")


def main():
    p = argparse.ArgumentParser(description="Remote model trainer (champion-challenger).")
    p.add_argument("--start", default="-72h", help="InfluxDB time range start")
    p.add_argument("--stop", default="now()", help="InfluxDB time range stop")
    p.add_argument("--dry-run", action="store_true", help="Don't actually promote")
    p.add_argument("--once", action="store_true", help="Run one cycle and exit")
    p.add_argument("--no-upstream", action="store_true",
                   help="Train base features only (only for a scorer running with "
                        "UPSTREAM=False; default scorer expects upstream columns)")
    p.add_argument("--selfcheck", action="store_true", help="Run selfcheck on cached data and exit")
    args = p.parse_args()

    if args.no_upstream:
        # scorer builds features with upstream=False; the deployed model's columns
        # must match or every inference throws and silently falls back. 
        # global toggle, train_cycle reads this module-level UPSTREAM.
        global UPSTREAM
        UPSTREAM = False

    if "--selfcheck" in sys.argv:
        selfcheck()
        return

    if args.once:
        result = train_cycle(args.start, args.stop, dry_run=args.dry_run)
        if result:
            log_cycle(result)
            print(json.dumps(result, indent=2))
    else:
        # Continuous loop: retrain every 6 hours (matches TRAIN_H default)
        CYCLE_INTERVAL_S = 6 * 3600
        while True:
            try:
                result = train_cycle("-72h", "now()", dry_run=args.dry_run)
                if result:
                    log_cycle(result)
                    print(json.dumps(result, indent=2))
            except Exception as e:
                print(f"Error in train cycle: {e}", file=sys.stderr)
                time.sleep(60)
                continue

            print(f"Sleeping for {CYCLE_INTERVAL_S}s before next cycle...")
            time.sleep(CYCLE_INTERVAL_S)


def selfcheck():
    """Offline validation: simulate a train cycle on cached data."""
    try:
        df = common.load_telemetry()
        if df.empty:
            print("selfcheck SKIP: no cached data")
            return

        raw_samples = len(df)
        df = common.weekday_business_hours(df)
        print(f"Loaded {raw_samples} cached samples; training/eval window "
              f"{TRAINING_WINDOW_LABEL}: {len(df)} samples")
        if df.empty:
            print("selfcheck SKIP: no cached business-hour data")
            return

        X, y = la.build_xy(df, lane_a=False, kf=True, upstream=UPSTREAM)
        print(f"Features: {X.shape}, label: {y.shape}")

        # Dummy walk-forward: last two folds
        folds = list(ev.walk_forward_folds(df, train_min=TRAIN_H * 60, step_s=EVAL_H * 3600))
        assert len(folds) >= 2, "need at least 2 folds for selfcheck"

        print(f"Generated {len(folds)} folds")

        gate = select_viable_gate_data(folds, df, upstream=UPSTREAM)
        assert gate is not None, "expected at least one viable gate candidate"
        train_df, eval_frames = gate["train_df"], gate["eval_frames"]
        assert gate["train_pos"] >= MIN_TRAIN_POS
        assert gate["gate_events"] >= MIN_GATE_EVENTS
        # the gate must judge on data the challenger never trained on
        for ef in eval_frames:
            assert ef.index.min() >= train_df.index.max(), \
                "gate eval window overlaps the training window (leaky gate)"

        X_train, y_train = gate["X_train"], gate["y_train"]
        fold_xy = gate["fold_xy"]
        gate_eval_start = min(ef.index.min() for ef in eval_frames if not ef.empty)
        aux_train_df = df[df.index < gate_eval_start]
        X_aux, _ = la.build_xy(aux_train_df, lane_a=False, kf=True, upstream=UPSTREAM)

        print(f"Train: {X_train.shape} ({(y_train == 1).sum()} pos), "
              f"Aux: {X_aux.shape}, "
              f"Eval folds: {[len(Xe) for Xe, _ in fold_xy]}, "
              f"gate_events={gate['gate_events']}, fold={gate['fold_index']}, "
              f"skipped_recent={gate['skipped_recent_candidates']}")

        challenger = train_rf(X_train, y_train)
        challenger = attach_direction_heads(challenger, aux_train_df, X_aux)
        print(f"Trained challenger: {challenger['model'].n_estimators} trees")
        print(f"Direction heads: rise={challenger.get('rise_head_status')}, "
              f"drop={challenger.get('drop_head_status')}")
        if challenger.get("drop_head_status") == "trained":
            assert "drop_model" in challenger and "drop_threshold" in challenger
        if challenger.get("rise_head_status") == "trained":
            assert "rise_model" in challenger and "rise_threshold" in challenger

        score_dict = score_on_folds(challenger, fold_xy, gate["spike_ts"])
        if score_dict:
            print(f"Challenger score: PR-AUC={score_dict['pr_auc']:.3f}, "
                  f"event_recall={score_dict['event_recall']:.3f}, "
                  f"latency={score_dict['event_latency_median_s']:.1f}s, "
                  f"lead={score_dict['lead_time_median_s']:.1f}s "
                  f"over {score_dict['n_folds']} held-out folds")
        else:
            print("Challenger score: no positives in eval folds")

        print("selfcheck OK")

    except Exception as e:
        print(f"selfcheck FAILED: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
