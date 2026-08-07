#!/usr/bin/env python3
"""Lightweight model scorer: runs on mycroft, reads versioned model, does inference only.

Replaces the always-on trainer component of spike_daemon_rf.py. This scorer:
1. Reads the current model from a versioned pickle file (via symlink)
2. Does lightweight inference-only prediction on live data
3. Falls back safely if model is missing/corrupt/stale
4. Writes predictions to InfluxDB

The remote trainer (model_trainer.py) handles all training and model versioning.
The scorer just reads and predicts, so it can be thin and reliable on mycroft.
"""

import argparse
import json
import pickle
import sys
import time
from collections import deque
from pathlib import Path

import numpy as np
import pandas as pd
from influxdb_client import InfluxDBClient
from influxdb_client.client.write_api import SYNCHRONOUS

from core import telemetry as common
from core import features as la
from core.spike_labels import DEFAULT_DERIV_THRESHOLD_W
from detectors.random_forest import live_detector as rf  # reuse predict_latest so features match the live daemon exactly

# same as spike_daemon_rf
CYCLE_S = 5
LOOKBACK_FEAT_S = 180  # 3 min for feature computation
INFLUX_BUCKET = "Power"  # same bucket cpu_power + the live daemons write to
SERVER = "mycroft"
UPSTREAM = True  # score the KF + upstream model (matches spike_daemon_rf)

MODEL_DIR = common.DATA_DIR / "models"
SYMLINK_PATH = MODEL_DIR / "spike_model_current"
FALLBACK_THRESHOLD = 0.5  # if no model, use fixed threshold for dumb baseline
ADAPTIVE_THRESHOLD_WINDOW_S = 3600.0
ADAPTIVE_THRESHOLD_REFRESH_S = 300.0
ADAPTIVE_THRESHOLD_MIN_SAMPLES = 60
ADAPTIVE_ALARM_RATE = rf.FLAG_BUDGET
ADAPTIVE_MAX_RISE_PER_REFRESH = 0.02
ADAPTIVE_MAX_FALL_PER_REFRESH = 0.005
USAGE_IDLE_MAX = 8.0
USAGE_IDLE_SPAN_MAX = 3.0
USAGE_IDLE_THRESHOLD = 0.80


class ModelReader:
    """Reads the current model from symlink with safe fallback."""

    def __init__(self, symlink_path=SYMLINK_PATH):
        self.symlink_path = symlink_path
        self._model = None
        self._model_time = None
        self._model_version = None

    def load(self):
        """Load current model from symlink. Returns (model_dict, version_str) or (None, None)."""
        if not self.symlink_path.is_symlink():
            return None, None
        try:
            target = self.symlink_path.resolve()
            if not target.exists():
                return None, None

            # reload if file has been updated
            mtime = target.stat().st_mtime
            if self._model_time != mtime:
                with open(target, "rb") as f:
                    self._model = pickle.load(f)
                self._model_time = mtime
                self._model_version = target.stem.replace("spike_model_", "")

            return self._model, self._model_version
        except Exception as e:
            print(f"Warning: failed to load model: {e}", file=sys.stderr)
            return None, None


class AdaptiveThresholdState:
    """Slow live threshold adapter.

    This is inference-only bookkeeping: no labels, no fitting, no tree updates.
    It tracks recent RF probabilities and nudges a smoothed quantile threshold.
    The adapter never goes below the model's stored production floor and its
    per-refresh step is capped so a quiet/workload regime change cannot produce
    an abrupt threshold jump.
    """

    def __init__(self, window_s=ADAPTIVE_THRESHOLD_WINDOW_S,
                 refresh_s=ADAPTIVE_THRESHOLD_REFRESH_S,
                 min_samples=ADAPTIVE_THRESHOLD_MIN_SAMPLES,
                 alarm_rate=ADAPTIVE_ALARM_RATE):
        self.window_s = float(window_s)
        self.refresh_s = float(refresh_s)
        self.min_samples = int(min_samples)
        self.alarm_rate = float(alarm_rate)
        self.samples = deque()
        self.dynamic_threshold = None
        self.target_threshold = None
        self.last_refresh = None
        self.model_version = None

    def observe(self, proba, model_version, base_threshold, now=None):
        now = time.monotonic() if now is None else float(now)
        base_threshold = rf.threshold_floor(base_threshold)
        if model_version != self.model_version:
            self.samples.clear()
            self.dynamic_threshold = base_threshold
            self.target_threshold = base_threshold
            self.last_refresh = None
            self.model_version = model_version
        self.samples.append((now, float(proba)))
        cutoff = now - self.window_s
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()
        if len(self.samples) < self.min_samples:
            return self.dynamic_threshold
        if self.last_refresh is not None and now - self.last_refresh < self.refresh_s:
            return self.dynamic_threshold
        vals = np.asarray([p for _t, p in self.samples if np.isfinite(p)], dtype=float)
        if len(vals) >= self.min_samples:
            q = float(np.nanquantile(vals, 1.0 - self.alarm_rate))
            self.target_threshold = max(base_threshold, rf.threshold_floor(q))
            cur = base_threshold if self.dynamic_threshold is None else self.dynamic_threshold
            delta = self.target_threshold - cur
            delta = min(delta, ADAPTIVE_MAX_RISE_PER_REFRESH)
            delta = max(delta, -ADAPTIVE_MAX_FALL_PER_REFRESH)
            self.dynamic_threshold = max(base_threshold, rf.threshold_floor(cur + delta))
            self.last_refresh = now
        return self.dynamic_threshold


def _usage_idle_gate(row):
    vals = []
    for c in ("usage_lag0", "usage_lag1", "usage_lag2", "usage_lag3"):
        if c in row.index and np.isfinite(row[c]):
            vals.append(float(row[c]))
    if not vals:
        return False
    return max(vals) <= USAGE_IDLE_MAX and (max(vals) - min(vals)) <= USAGE_IDLE_SPAN_MAX


def _model_cols(model_dict):
    cols = set(model_dict.get("cols", []))
    for prefix in ("rise", "drop"):
        cols.update(model_dict.get(f"{prefix}_cols", model_dict.get("cols", [])))
    return list(cols)


def _head_proba(model_dict, row, prefix):
    model = model_dict.get(f"{prefix}_model")
    if model is None:
        return None
    cols = list(model_dict.get(f"{prefix}_cols", model_dict.get("cols", [])))
    return float(model.predict_proba(row[cols].to_numpy()[None, :])[0, 1])


def _predict_latest_with_row(model_dict, df):
    X = la.feature_frame(df, lane_a=False, kf=True, upstream=UPSTREAM)
    cols = _model_cols(model_dict)
    X = X[cols].dropna()
    if X.empty:
        return None, None, None, None
    row = X.iloc[-1]
    main_cols = list(model_dict["cols"])
    proba = float(model_dict["model"].predict_proba(row[main_cols].to_numpy()[None, :])[0, 1])
    return proba, _head_proba(model_dict, row, "rise"), _head_proba(model_dict, row, "drop"), row


def _signed_power_state(row):
    p0 = row.get("power_lag0", np.nan)
    p1 = row.get("power_lag1", np.nan)
    delta = float(p0 - p1) if np.isfinite(p0) and np.isfinite(p1) else float("nan")
    rise_now = np.isfinite(delta) and delta > DEFAULT_DERIV_THRESHOLD_W
    drop_now = np.isfinite(delta) and delta < -DEFAULT_DERIV_THRESHOLD_W
    direction = 1 if rise_now else (-1 if drop_now else 0)
    return delta, int(rise_now), int(drop_now), direction


def fit_predict_with_fallback(df, model_reader, threshold_state=None):
    """Classify: P(power spike within the horizon) from the RF model on the last row.

    Returns {spike_proba, spike_risk, model_threshold, model_version}. This is a
    classifier, not a regressor -- spike_proba is the RF predict_proba, spike_risk
    is that thresholded to 0/1. No model -> proba 0 (honest "don't know").
    """
    lookback_df = df.tail(LOOKBACK_FEAT_S + 1).copy()
    if len(lookback_df) < 30:
        return None

    model_dict, model_version = model_reader.load()

    def out(proba, model_thr, ver, decision_thr=None, base_thr=None,
            adaptive_thr=None, usage_gate=False, usage_lag0=None,
            rise_proba=None, rise_thr=None, rise_risk=0,
            drop_proba=None, drop_thr=None, drop_risk=0,
            power_delta_1s=None, rise_observed=0, drop_observed=0,
            spike_direction=0):
        decision_thr = model_thr if decision_thr is None else decision_thr
        return {
            "spike_proba": float(proba),
            "spike_risk": 1 if proba > decision_thr else 0,
            "model_threshold": float(model_thr),
            "decision_threshold": float(decision_thr),
            "base_model_threshold": None if base_thr is None else float(base_thr),
            "adaptive_threshold": None if adaptive_thr is None else float(adaptive_thr),
            "usage_gate_active": int(bool(usage_gate)),
            "usage_lag0": None if usage_lag0 is None else float(usage_lag0),
            "rise_proba": None if rise_proba is None else float(rise_proba),
            "rise_threshold": None if rise_thr is None else float(rise_thr),
            "rise_risk": int(rise_risk),
            "drop_proba": None if drop_proba is None else float(drop_proba),
            "drop_threshold": None if drop_thr is None else float(drop_thr),
            "drop_risk": int(drop_risk),
            "power_delta_1s": None if power_delta_1s is None else float(power_delta_1s),
            "rise_observed": int(rise_observed),
            "drop_observed": int(drop_observed),
            "spike_direction": int(spike_direction),
            "model_version": ver,
        }

    if model_dict is None:
        return out(0.0, FALLBACK_THRESHOLD, "none")

    try:
        # Synced models may carry an older permissive threshold. Enforce the
        # current production floor at scoring time so stale pickles cannot keep
        # actuator-facing risk permissive after a code deploy.
        base_thr = rf.threshold_floor(float(model_dict.get("threshold", FALLBACK_THRESHOLD)))
        proba, rise_proba, drop_proba, row = _predict_latest_with_row(model_dict, lookback_df)
        if proba is None:
            return out(0.0, base_thr, model_version, base_thr=base_thr)

        adaptive_thr = None
        if threshold_state is not None:
            adaptive_thr = threshold_state.observe(proba, model_version, base_thr)
        model_thr = max(base_thr, adaptive_thr) if adaptive_thr is not None else base_thr

        usage_gate = _usage_idle_gate(row)
        usage_lag0 = row.get("usage_lag0", None)
        decision_thr = model_thr
        if usage_gate:
            decision_thr = max(decision_thr, USAGE_IDLE_THRESHOLD)

        power_delta, rise_observed, drop_observed, direction = _signed_power_state(row)
        rise_thr = rf.threshold_floor(float(model_dict.get("rise_threshold", base_thr)))
        drop_thr = rf.threshold_floor(float(model_dict.get("drop_threshold", base_thr)))
        rise_risk = int(rise_proba > rise_thr) if rise_proba is not None else 0
        drop_risk = int(drop_proba > drop_thr) if drop_proba is not None else 0
        return out(proba, model_thr, model_version, decision_thr=decision_thr,
                   base_thr=base_thr,
                   adaptive_thr=adaptive_thr, usage_gate=usage_gate,
                   usage_lag0=usage_lag0, rise_proba=rise_proba,
                   rise_thr=rise_thr if rise_proba is not None else None,
                   rise_risk=rise_risk, drop_proba=drop_proba,
                   drop_thr=drop_thr if drop_proba is not None else None,
                   drop_risk=drop_risk, power_delta_1s=power_delta,
                   rise_observed=rise_observed, drop_observed=drop_observed,
                   spike_direction=direction)

    except Exception as e:
        print(f"Warning: model inference failed: {e}, using fallback", file=sys.stderr)
        return out(0.0, FALLBACK_THRESHOLD, model_version)


def write_prediction(client, prediction_dict, server=SERVER, measurement="power_prediction"):
    """Write prediction to InfluxDB."""
    if prediction_dict is None:
        return

    from influxdb_client.client.write_api import Point
    import datetime

    risk = bool(prediction_dict["spike_risk"])
    risk_field = risk if measurement == "power_spike_prediction" else int(risk)
    point = (
        Point(measurement)
        .tag("server", server)
        .tag("model", "rf")
        .field("spike_proba", prediction_dict["spike_proba"])
        .field("spike_risk", risk_field)
        .field("model_threshold", prediction_dict["model_threshold"])
        .field("threshold", prediction_dict["model_threshold"])
        .field("decision_threshold",
               prediction_dict.get("decision_threshold", prediction_dict["model_threshold"]))
        .field("usage_gate_active", int(prediction_dict.get("usage_gate_active", 0)))
        .field("rise_risk", int(prediction_dict.get("rise_risk", 0)))
        .field("drop_risk", int(prediction_dict.get("drop_risk", 0)))
        .field("rise_observed", int(prediction_dict.get("rise_observed", 0)))
        .field("drop_observed", int(prediction_dict.get("drop_observed", 0)))
        .field("spike_direction", int(prediction_dict.get("spike_direction", 0)))
        .field("horizon_s", rf.HORIZON_S)
        .field("model_version", prediction_dict["model_version"])
        .time(datetime.datetime.utcnow(), write_precision="ns")
    )
    base_thr = prediction_dict.get("base_model_threshold")
    if base_thr is None:
        base_thr = prediction_dict["model_threshold"]
    point = point.field("base_model_threshold", base_thr)
    if prediction_dict.get("adaptive_threshold") is not None:
        point = point.field("adaptive_threshold", prediction_dict["adaptive_threshold"])
    if prediction_dict.get("usage_lag0") is not None:
        point = point.field("usage_lag0", prediction_dict["usage_lag0"])
    for field in ("rise_proba", "rise_threshold", "drop_proba", "drop_threshold",
                  "power_delta_1s"):
        if prediction_dict.get(field) is not None:
            point = point.field(field, prediction_dict[field])

    try:
        write_api = client.write_api(write_options=SYNCHRONOUS)
        write_api.write(bucket=INFLUX_BUCKET, record=point)
    except Exception as e:
        print(f"Warning: failed to write prediction: {e}", file=sys.stderr)


def prediction_loop(dry_run=False, measurement="power_prediction", risk_path=None,
                    rise_risk_path=None, drop_risk_path=None):
    """Main loop: fetch live data every CYCLE_S, predict, write."""
    model_reader = ModelReader()
    threshold_state = AdaptiveThresholdState()
    org = common.load_org()
    token = common.load_write_token()

    while True:
        cycle_start = time.monotonic()
        try:
            # Fetch recent window (+ upstream signals, best-effort join)
            df = common.query_recent(start=f"-{LOOKBACK_FEAT_S + 5}s")
            if df.empty:
                print("Warning: empty data from InfluxDB", file=sys.stderr)
                time.sleep(CYCLE_S)
                continue
            if UPSTREAM:
                try:
                    up = common.query_upstream(start=f"-{LOOKBACK_FEAT_S + 5}s", timeout_ms=30000)
                    if not up.empty:
                        up = up.drop(columns=up.columns.intersection(df.columns))
                        df = df.join(up, how="left")
                except Exception as e:
                    print(f"warning: upstream query failed: {e}, base features only", file=sys.stderr)

            # Predict
            result = fit_predict_with_fallback(df, model_reader, threshold_state)
            if result is None:
                time.sleep(CYCLE_S)
                continue

            if risk_path:
                try:
                    common.write_risk_flag(risk_path, bool(result["spike_risk"]))
                except OSError as e:
                    print(f"warning: risk-flag write failed ({e})", file=sys.stderr)
            if rise_risk_path:
                try:
                    common.write_risk_flag(rise_risk_path, bool(result["rise_risk"]))
                except OSError as e:
                    print(f"warning: rise-risk flag write failed ({e})", file=sys.stderr)
            if drop_risk_path:
                try:
                    common.write_risk_flag(drop_risk_path, bool(result["drop_risk"]))
                except OSError as e:
                    print(f"warning: drop-risk flag write failed ({e})", file=sys.stderr)

            # Write (or dry-run)
            if not dry_run:
                client = InfluxDBClient(url=common.INFLUX_URL, token=token, org=org)
                try:
                    write_prediction(client, result, measurement=measurement)
                finally:
                    client.close()
            else:
                print(f"DRY RUN: {json.dumps(result, indent=2)}")

            time.sleep(max(0.0, CYCLE_S - (time.monotonic() - cycle_start)))

        except KeyboardInterrupt:
            print("Shutting down...")
            break
        except Exception as e:
            print(f"Error in prediction loop: {e}", file=sys.stderr)
            time.sleep(CYCLE_S)


def main():
    p = argparse.ArgumentParser(description="Lightweight model scorer (inference only).")
    p.add_argument("--dry-run", action="store_true", help="Don't write to InfluxDB")
    p.add_argument("--measurement", default="power_prediction",
                   help="InfluxDB measurement to write (use a distinct name to avoid "
                        "colliding with the live daemon / OLS daemon)")
    p.add_argument("--once", action="store_true", help="Run one cycle and exit")
    p.add_argument("--risk-file", default=None,
                   help="optional flag file to publish spike_risk to (leave unset "
                        "while the live daemon owns the default flag path)")
    p.add_argument("--rise-risk-file", default=None,
                   help="optional flag file to publish signed upward-risk events")
    p.add_argument("--drop-risk-file", default=None,
                   help="optional flag file to publish signed downward-risk events")
    p.add_argument("--selfcheck", action="store_true", help="Run selfcheck on cached data and exit")
    args = p.parse_args()

    if "--selfcheck" in sys.argv:
        selfcheck()
        return

    if args.once:
        # Single prediction cycle (join upstream so features match the model)
        df = common.query_recent(start=f"-{LOOKBACK_FEAT_S + 5}s")
        if UPSTREAM and not df.empty:
            try:
                up = common.query_upstream(start=f"-{LOOKBACK_FEAT_S + 5}s", timeout_ms=30000)
                if not up.empty:
                    up = up.drop(columns=up.columns.intersection(df.columns))
                    df = df.join(up, how="left")
            except Exception as e:
                print(f"warning: upstream query failed: {e}", file=sys.stderr)
        result = fit_predict_with_fallback(df, ModelReader(), AdaptiveThresholdState())
        if result:
            print(json.dumps(result, indent=2))
    else:
        prediction_loop(dry_run=args.dry_run, measurement=args.measurement,
                        risk_path=args.risk_file,
                        rise_risk_path=args.rise_risk_file,
                        drop_risk_path=args.drop_risk_file)


def selfcheck():
    """Validate on cached data."""
    try:
        df = common.load_telemetry()
        if df.empty:
            print("selfcheck SKIP: no cached data")
            return

        print(f"Loaded {len(df)} samples")

        # Force the no-model fallback path (point reader at a nonexistent symlink)
        lookback_df = df.tail(LOOKBACK_FEAT_S + 1)
        result = fit_predict_with_fallback(lookback_df, ModelReader(Path("/nonexistent")))

        assert result is not None, "expected a result"
        assert "spike_proba" in result, "missing spike_proba"
        assert "spike_risk" in result, "missing spike_risk"
        assert result["model_version"] == "none", "should be no model in fallback"
        assert result["spike_proba"] == 0.0 and result["spike_risk"] == 0, "fallback must be 0"

        state = AdaptiveThresholdState(window_s=10.0, refresh_s=0.0,
                                       min_samples=3, alarm_rate=0.10)
        assert abs(state.observe(0.20, "vtest", 0.35, now=0.0) - 0.35) < 1e-9
        assert abs(state.observe(0.50, "vtest", 0.35, now=1.0) - 0.35) < 1e-9
        dyn = state.observe(0.90, "vtest", 0.35, now=2.0)
        assert dyn is not None and 0.35 < dyn <= 0.37, f"bad adaptive threshold {dyn}"
        assert abs(state.observe(0.10, "vnext", 0.35, now=3.0) - 0.35) < 1e-9, \
            "model change must reset adaptive threshold"

        class FixedProbaModel:
            def predict_proba(self, X):
                p = np.full(len(X), 0.70)
                return np.column_stack([1.0 - p, p])

        fake = {
            "model": FixedProbaModel(),
            "cols": ["usage_lag0", "usage_lag1", "usage_lag2", "usage_lag3"],
            "threshold": 0.35,
        }

        class FakeReader:
            def __init__(self, model):
                self.model = model
            def load(self):
                return self.model, "fake"

        idx = pd.date_range("2026-01-01T00:00:00Z", periods=80, freq="1s")
        idle_df = pd.DataFrame({
            "power_watts": np.full(len(idx), 200.0),
            "usage_percent": np.full(len(idx), 2.0),
        }, index=idx)
        gated = fit_predict_with_fallback(idle_df, FakeReader(fake))
        assert gated["spike_proba"] == 0.70 and gated["spike_risk"] == 0, gated
        assert gated["usage_gate_active"] == 1, gated
        assert gated["model_threshold"] == 0.35 and gated["decision_threshold"] == USAGE_IDLE_THRESHOLD, gated

        rising_df = idle_df.copy()
        rising_df["usage_percent"] = np.linspace(2.0, 20.0, len(idx))
        open_gate = fit_predict_with_fallback(rising_df, FakeReader(fake))
        assert open_gate["usage_gate_active"] == 0 and open_gate["spike_risk"] == 1, open_gate

        class LowAdaptive:
            def observe(self, proba, model_version, base_threshold):
                return 0.10

        no_lower = fit_predict_with_fallback(rising_df, FakeReader(fake), LowAdaptive())
        assert no_lower["model_threshold"] == 0.35, no_lower

        signed_fake = {
            "model": FixedProbaModel(),
            "cols": ["power_lag0", "power_lag1", "usage_lag0", "usage_lag1",
                     "usage_lag2", "usage_lag3"],
            "threshold": 0.35,
        }
        drop_df = pd.DataFrame({
            "power_watts": np.r_[np.full(79, 250.0), 100.0],
            "usage_percent": np.full(len(idx), 25.0),
        }, index=idx)
        signed = fit_predict_with_fallback(drop_df, FakeReader(signed_fake))
        assert signed["drop_observed"] == 1 and signed["drop_risk"] == 0, signed
        assert signed["rise_observed"] == 0 and signed["spike_direction"] == -1, signed

        class DropHeadModel:
            def predict_proba(self, X):
                p = np.full(len(X), 0.90)
                return np.column_stack([1.0 - p, p])

        signed_fake["drop_model"] = DropHeadModel()
        signed_fake["drop_cols"] = signed_fake["cols"]
        signed_fake["drop_threshold"] = 0.35
        signed_head = fit_predict_with_fallback(drop_df, FakeReader(signed_fake))
        assert signed_head["drop_risk"] == 1 and signed_head["drop_proba"] == 0.90, signed_head

        print(f"selfcheck OK: proba={result['spike_proba']:.3f}, "
              f"risk={result['spike_risk']}, model={result['model_version']} "
              "+ smooth adaptive threshold + signed rise/drop")

    except Exception as e:
        print(f"selfcheck FAILED: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
