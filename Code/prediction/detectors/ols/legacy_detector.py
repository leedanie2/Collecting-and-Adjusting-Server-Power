#!/usr/bin/env python3
"""Predict near-future cpu_power spikes from recent telemetry; runs forever.

one plain OLS refit per cycle on a short lookback window (cheap --
~600x9 lstsq, milliseconds), no ML framework. power_watts + usage_percent
lags only -- analysis/regression.py's multivariate fit already showed
freq/temp add only marginal R2 (standardized coefs 0.20/0.11 vs usage's 0.99),
not worth two more columns in a model that refits every cycle. No persistent
trained state between cycles either: refit-from-scratch each time is simpler
than incremental update and, at this data size, just as fast.

Honest limitation (see docs/prediction_scaffold.md): lagged self-features
only buy ~single-digit-second lead time. This is the model that's actually
backed by live data today, not a claim of long-horizon foresight.
"""

import socket
import sys
import time

import numpy as np
import pandas as pd
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

from core import telemetry as common

LOOKBACK_MIN = 10   # training window
CYCLE_S = 5         # predict (and refit) every N seconds
HORIZON_S = 5        # how far ahead to forecast -- kept short, lead time is weak
LAGS_S = [0, 1, 2, 3]  # 0 = most recent reading, 1 = 1s before that, etc.
RISK_K = 2.5         # flag predicted spike if it's RISK_K std-devs above the window mean
INFLUX_BUCKET = "Power"  # same bucket cpu_power lives in (Grafana/influx2.py)


def make_prediction_point(server, pred, rmse, threshold):
    return (
        Point("power_prediction")
        .tag("server", server)
        .field("predicted_watts", pred)
        .field("rmse_watts", rmse)
        .field("threshold_watts", threshold)
        .field("spike_risk", pred > threshold)
    )


def make_dataset(df, lags, horizon):
    df = df[["power_watts", "usage_percent"]].dropna()
    cols = {}
    for lag in lags:
        cols[f"power_lag{lag}"] = df["power_watts"].shift(lag)
        cols[f"usage_lag{lag}"] = df["usage_percent"].shift(lag)
    X = pd.DataFrame(cols)
    y = df["power_watts"].shift(-horizon)
    return pd.concat([X, y.rename("target")], axis=1).dropna()


def latest_features(df, lags):
    tail = df[["power_watts", "usage_percent"]].dropna().tail(max(lags) + 1)
    if len(tail) < max(lags) + 1:
        return None
    feat = [1.0]
    for lag in lags:
        row = tail.iloc[-(lag + 1)]
        feat += [row["power_watts"], row["usage_percent"]]
    return feat


def fit_predict(df, lags=LAGS_S, horizon=HORIZON_S):
    data = make_dataset(df, lags, horizon)
    if len(data) < 30:
        return None  # fixed floor, not worth a config knob for this

    X = np.column_stack([np.ones(len(data)), data.drop(columns="target").to_numpy()])
    y = data["target"].to_numpy()
    coefs, *_ = np.linalg.lstsq(X, y, rcond=None)

    feat = latest_features(df, lags)
    if feat is None:
        return None
    pred = float(np.dot(coefs, feat))
    rmse = float(np.sqrt(((y - X @ coefs) ** 2).mean()))
    return pred, rmse


def run_once(write_api=None, org=None, server=None):
    df = common.query_recent(start=f"-{LOOKBACK_MIN}m")
    if df.empty:
        print("no data yet")
        return

    result = fit_predict(df)
    if result is None:
        print("not enough data yet to fit")
        return
    pred, rmse = result

    power = df["power_watts"].dropna()
    # threshold = mean + K*std of the same lookback window. Drifts
    # if the whole window is already mid-spike -- upgrade to power_profile.py's
    # idle-baseline subtraction if that turns out to matter in practice.
    threshold = float(power.mean() + RISK_K * power.std())
    flag = " <-- SPIKE RISK" if pred > threshold else ""
    print(f"t+{HORIZON_S}s predicted={pred:.1f}W (rmse={rmse:.1f}W) threshold={threshold:.1f}W{flag}")

    if write_api is not None:
        point = make_prediction_point(server, pred, rmse, threshold)
        write_api.write(bucket=INFLUX_BUCKET, org=org, record=point)


def selfcheck():
    df = common.load_telemetry()  # cached CSV -- no live connection needed
    result = fit_predict(df)
    assert result is not None, "fit_predict returned None on cached data"
    pred, rmse = result
    assert np.isfinite(pred) and np.isfinite(rmse), "non-finite prediction"

    risky_point = make_prediction_point("selfcheck-host", pred=pred, rmse=rmse, threshold=pred - 1)
    risky_line = risky_point.to_line_protocol()
    assert "power_prediction,server=selfcheck-host" in risky_line, f"unexpected schema: {risky_line}"
    assert "spike_risk=true" in risky_line, f"expected spike_risk=true when pred>threshold: {risky_line}"

    calm_point = make_prediction_point("selfcheck-host", pred=pred, rmse=rmse, threshold=pred + 1)
    assert "spike_risk=false" in calm_point.to_line_protocol(), "expected spike_risk=false when pred<threshold"

    print(f"selfcheck OK: predicted={pred:.1f}W rmse={rmse:.1f}W")


def main():
    if "--selfcheck" in sys.argv:
        selfcheck()
        return

    once = "--once" in sys.argv
    dry_run = "--dry-run" in sys.argv
    server = socket.gethostname()

    client = None
    write_api = None
    org = None
    if not dry_run:
        org = common.load_org()
        token = common.load_write_token()
        client = InfluxDBClient(url=common.INFLUX_URL, token=token, org=org)
        write_api = client.write_api(write_options=SYNCHRONOUS)

    try:
        while True:
            run_once(write_api=write_api, org=org, server=server)
            if once:
                return
            time.sleep(CYCLE_S)
    finally:
        if write_api is not None:
            write_api.close()
        if client is not None:
            client.close()


if __name__ == "__main__":
    main()
