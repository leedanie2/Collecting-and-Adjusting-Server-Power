#!/usr/bin/env python3
"""Fixed-model evaluator for the RF spike detector.

This is inference-only: it loads an existing pickle, builds the same strictly
causal feature frame used by scorer.py, and reports event-level detector
metrics against raw power-step onsets.
"""

import argparse
import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

from core import features as la
from core import telemetry as common
from detectors.random_forest import live_detector as rf
from validation import evaluation as ev

DEFAULT_MODEL = common.DATA_DIR / "models" / "spike_model_current"


def load_model(path=DEFAULT_MODEL):
    """Load a versioned RF pickle or the spike_model_current symlink."""
    p = Path(path)
    target = p.resolve()
    with open(target, "rb") as f:
        model_dict = pickle.load(f)
    if not isinstance(model_dict, dict) or "model" not in model_dict or "cols" not in model_dict:
        raise ValueError(f"{p} is not an RF detector pickle")
    model_dict = dict(model_dict)
    model_dict["cols"] = list(model_dict["cols"])
    model_dict["threshold"] = rf.threshold_floor(float(model_dict.get("threshold", 0.5)))
    for direction in ("rise", "drop"):
        key = f"{direction}_threshold"
        if key in model_dict:
            model_dict[key] = rf.threshold_floor(float(model_dict[key]))
    return model_dict, target.stem.replace("spike_model_", "")


def _score_direction_head(model_dict, X, direction, event_ts, start, end,
                          lead_s, lag_s, merge_gap_s):
    prefix = f"{direction}_head"
    model = model_dict.get(f"{direction}_model")
    if model is None:
        return {
            f"{prefix}_present": 0,
            f"{prefix}_n_events": int(len(pd.DatetimeIndex(event_ts))),
        }
    cols = list(model_dict.get(f"{direction}_cols", model_dict["cols"]))
    missing = [c for c in cols if c not in X.columns]
    if missing:
        shown = ", ".join(missing[:8])
        tail = "" if len(missing) <= 8 else f", ... ({len(missing)} total)"
        raise ValueError(f"{direction} head requires missing feature columns: {shown}{tail}")
    threshold = rf.threshold_floor(float(model_dict.get(f"{direction}_threshold", 0.5)))
    proba = model.predict_proba(X[cols].to_numpy())[:, 1]
    flag = proba > threshold
    stats = ev.event_alert_score(
        event_ts,
        X.index[flag],
        start=start,
        end=end,
        lead_s=lead_s,
        lag_s=lag_s,
        merge_gap_s=merge_gap_s,
    )
    return {
        f"{prefix}_present": 1,
        f"{prefix}_threshold": float(threshold),
        f"{prefix}_alert_duty": float(np.mean(flag)) if len(flag) else float("nan"),
        f"{prefix}_event_recall": stats["event_recall"],
        f"{prefix}_event_latency_median_s": stats["event_latency_median_s"],
        f"{prefix}_alert_event_precision": stats["alert_event_precision"],
        f"{prefix}_false_alert_episodes_per_h": stats["false_alert_episodes_per_h"],
        f"{prefix}_n_events": stats["n_events"],
        f"{prefix}_n_events_detected": stats["n_events_detected"],
        f"{prefix}_n_alert_episodes": stats["n_alert_episodes"],
        f"{prefix}_n_false_alert_episodes": stats["n_false_alert_episodes"],
    }


def _query_dataframe(start, stop, upstream=True):
    """Pull telemetry for evaluation without fitting a model."""
    from detectors.random_forest import trainer

    df_base = trainer._query_chunked(
        common.query_window,
        start=start,
        stop=stop,
        measurements=("cpu_power", "cpu_usage"),
        timeout_ms=trainer.BULK_QUERY_TIMEOUT_MS,
    )
    if not upstream or df_base.empty:
        return df_base
    df_up = trainer._query_chunked(
        common.query_upstream,
        start=start,
        stop=stop,
        timeout_ms=trainer.BULK_QUERY_TIMEOUT_MS,
    )
    if df_up.empty:
        return df_base
    df_up = df_up.drop(columns=df_up.columns.intersection(df_base.columns))
    return df_base.join(df_up, how="left")


def load_dataframe(args):
    if args.query:
        return _query_dataframe(args.start, args.stop, upstream=not args.no_upstream)
    df = common.load_telemetry(start=args.start, end=None if args.stop == "now()" else args.stop)
    return df


def evaluate_model(model_dict, df, upstream=True, business_hours=False,
                   lead_s=la.HORIZON_S, lag_s=la.HORIZON_S, merge_gap_s=2.0):
    """Return row-level legacy scores plus event-level alert scores."""
    if business_hours:
        df = common.weekday_business_hours(df)
    if df.empty:
        raise ValueError("empty evaluation dataframe")

    X, y = la.build_xy(df, lane_a=False, kf=True, upstream=upstream)
    cols = list(model_dict["cols"])
    missing = [c for c in cols if c not in X.columns]
    if missing:
        shown = ", ".join(missing[:8])
        tail = "" if len(missing) <= 8 else f", ... ({len(missing)} total)"
        raise ValueError(f"model requires missing feature columns: {shown}{tail}")
    if X.empty:
        raise ValueError("no complete feature rows to evaluate")

    threshold = rf.threshold_floor(float(model_dict.get("threshold", 0.5)))
    proba = model_dict["model"].predict_proba(X[cols].to_numpy())[:, 1]
    cont = proba - threshold
    flag = cont > 0.0

    onset_ts = pd.DatetimeIndex(ev.true_onset_ts(df["power_watts"]))
    drop_ts = pd.DatetimeIndex(ev.true_drop_ts(df["power_watts"]))
    true_pos_ts = X.index.to_numpy()[flag & (y.to_numpy() == 1)]
    lead_times = la._lead_times(onset_ts, true_pos_ts)

    out = ev.score(y.to_numpy(), cont, lead_times if lead_times else None)
    out.update(ev.event_alert_score(
        onset_ts,
        X.index[flag],
        start=X.index[0],
        end=X.index[-1],
        lead_s=lead_s,
        lag_s=lag_s,
        merge_gap_s=merge_gap_s,
    ))
    out.update({
        "alert_duty": float(np.mean(flag)) if len(flag) else float("nan"),
        "n_alert_points": int(flag.sum()),
        "n_eval_points": int(len(flag)),
        "model_threshold": float(threshold),
        "window_start": str(X.index[0]),
        "window_end": str(X.index[-1]),
    })
    combined_drop = ev.event_alert_score(
        drop_ts,
        X.index[flag],
        start=X.index[0],
        end=X.index[-1],
        lead_s=lead_s,
        lag_s=lag_s,
        merge_gap_s=merge_gap_s,
    )
    out.update({
        "combined_drop_event_recall": combined_drop["event_recall"],
        "combined_drop_events_detected": combined_drop["n_events_detected"],
        "combined_drop_events": combined_drop["n_events"],
    })
    out.update(_score_direction_head(
        model_dict, X, "rise", onset_ts, X.index[0], X.index[-1],
        lead_s, lag_s, merge_gap_s,
    ))
    out.update(_score_direction_head(
        model_dict, X, "drop", drop_ts, X.index[0], X.index[-1],
        lead_s, lag_s, merge_gap_s,
    ))
    return out


def print_summary(stats, version):
    latency = stats["event_latency_median_s"]
    latency_txt = "nan" if not np.isfinite(latency) else f"{latency:.1f}s"
    print(f"RF detector event evaluator ({version})")
    print(f"  Window              {stats['window_start']} -> {stats['window_end']}")
    print(f"  Rows scored          {stats['n_eval_points']}")
    print(f"  Threshold            {stats['model_threshold']:.4f}")
    print(f"  Event recall         {stats['event_recall']:.4f} "
          f"({stats['n_events_detected']}/{stats['n_events']} raw onsets)")
    print(f"  First-flag latency   {latency_txt} median "
          f"[p25={stats['event_latency_p25_s']:.1f}s, p75={stats['event_latency_p75_s']:.1f}s]")
    print(f"  Pre-onset fraction   {stats['event_pre_onset_frac']:.4f}")
    print(f"  Alert duty           {stats['alert_duty']:.4f} "
          f"({stats['n_alert_points']}/{stats['n_eval_points']} rows)")
    print(f"  Alert precision      {stats['alert_event_precision']:.4f} episode-level")
    print(f"  False alert rate     {stats['false_alert_episodes_per_h']:.2f} episodes/hour "
          f"({stats['n_false_alert_episodes']}/{stats['n_alert_episodes']} episodes)")
    print(f"  Combined drop recall {stats['combined_drop_event_recall']:.4f} "
          f"({stats['combined_drop_events_detected']}/{stats['combined_drop_events']} drops)")
    if stats.get("drop_head_present"):
        print(f"  Drop head recall     {stats['drop_head_event_recall']:.4f} "
              f"({stats['drop_head_n_events_detected']}/{stats['drop_head_n_events']} drops)")
    else:
        print("  Drop head recall     n/a (pickle has no signed drop head)")
    print(f"  Legacy PR-AUC        {stats['pr_auc']:.4f}")


def selfcheck():
    idx = pd.date_range("2026-01-01T00:00:00Z", periods=120, freq="1s")
    power = pd.Series(np.r_[np.full(40, 100.0), np.full(80, 230.0)], index=idx)
    df = pd.DataFrame({"power_watts": power, "usage_percent": 50.0}, index=idx)

    class StepModel:
        def predict_proba(self, X):
            p = (X[:, 0] > 150.0).astype(float)
            return np.column_stack([1.0 - p, p])

    model = {"model": StepModel(), "cols": ["power_lag0"], "threshold": 0.5}
    stats = evaluate_model(model, df, upstream=False, lead_s=5, lag_s=5)
    assert stats["n_events"] == 1 and stats["n_events_detected"] == 1, stats
    assert abs(stats["event_latency_median_s"]) < 1e-9, stats
    assert stats["alert_duty"] > 0.0, stats
    print("selfcheck OK: event recall + first-flag latency evaluator")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", default=str(DEFAULT_MODEL), help="RF pickle/symlink to evaluate")
    p.add_argument("--start", default=None, help="cache/query start time")
    p.add_argument("--stop", default="now()", help="cache/query stop time")
    p.add_argument("--query", action="store_true", help="pull the window from InfluxDB")
    p.add_argument("--business-hours", action="store_true",
                   help="score only the detector training window")
    p.add_argument("--no-upstream", action="store_true",
                   help="build base features only; use for base-feature pickles")
    p.add_argument("--lead-s", type=float, default=la.HORIZON_S,
                   help="seconds before onset that counts as a hit")
    p.add_argument("--lag-s", type=float, default=la.HORIZON_S,
                   help="seconds after onset that counts as a hit")
    p.add_argument("--merge-gap-s", type=float, default=2.0,
                   help="max gap between alert rows inside one alert episode")
    p.add_argument("--json", action="store_true", help="print raw JSON metrics")
    p.add_argument("--selfcheck", action="store_true", help="run evaluator selfcheck")
    args = p.parse_args()

    if args.selfcheck:
        selfcheck()
        return
    if args.query and args.start is None:
        p.error("--query requires --start")

    model, version = load_model(args.model)
    df = load_dataframe(args)
    stats = evaluate_model(
        model,
        df,
        upstream=not args.no_upstream,
        business_hours=args.business_hours,
        lead_s=args.lead_s,
        lag_s=args.lag_s,
        merge_gap_s=args.merge_gap_s,
    )
    stats["model_version"] = version
    if args.json:
        print(json.dumps(stats, indent=2, allow_nan=True))
    else:
        print_summary(stats, version)


if __name__ == "__main__":
    main()
