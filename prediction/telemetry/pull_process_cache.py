#!/usr/bin/env python3
"""One-time pull of the overlap window where BOTH cpu_power and procs_running
exist (procs_running only started ~2026-06-25). Saves a Lane-A cache that the
stale telemetry.csv lacks.

separate cache file, does not clobber telemetry.csv (the baseline's
data). Run once; eval reads the CSV from disk thereafter.

system has many fields (load1/5/15, procs_running, ctxt, uptime) -- we filter to
procs_running ONLY, else group()+mean() would average them into nonsense.
"""
import sys

import pandas as pd
from influxdb_client import InfluxDBClient

from core import telemetry as common

OUT = common.DATA_DIR / "telemetry_procs.csv"

# procs_running's first timestamp (verified live 2026-06-29); start a minute
# before so the 1s-round join keeps the first real rows.
PROCS_START = "2026-06-25T17:46:00Z"

FLUX = """
from(bucket: "{bucket}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) =>
       r._measurement == "cpu_power"
       or r._measurement == "cpu_freq"
       or r._measurement == "cpu_temp"
       or r._measurement == "cpu_usage"
       or r._measurement == "pdu_power"
       or (r._measurement == "system" and r._field == "procs_running"))
  |> group(columns: ["_measurement"])
  |> aggregateWindow(every: 1s, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_measurement", "_value"])
"""


def _query(client, org, start, stop):
    flux = FLUX.format(bucket=org, start=start, stop=stop)
    df = client.query_api().query_data_frame(flux)
    if isinstance(df, list):
        df = pd.concat(df, ignore_index=True)
    return df


def pull(start=PROCS_START):
    org = common.load_org()
    token = common.load_token()
    # chunk by day: a single 90h aggregateWindow exceeds the server read timeout.
    now = pd.Timestamp.now(tz="UTC")
    edges = list(pd.date_range(start=pd.Timestamp(start), end=now, freq="1D"))
    if not edges or edges[-1] < now:
        edges.append(now)
    client = InfluxDBClient(url=common.INFLUX_URL, token=token, org=org, timeout=300_000)
    parts = []
    try:
        for a, b in zip(edges[:-1], edges[1:]):
            s, e = a.isoformat().replace("+00:00", "Z"), b.isoformat().replace("+00:00", "Z")
            d = _query(client, org, s, e)
            if not d.empty:
                parts.append(d)
            print(f"  {s} -> {e}: {0 if d.empty else len(d)} points")
    finally:
        client.close()

    df = pd.concat(parts, ignore_index=True)
    df["_time"] = pd.to_datetime(df["_time"]).dt.round("1s")
    wide = df.pivot_table(index="_time", columns="_measurement", values="_value", aggfunc="mean")
    rename = dict(common.RENAME)
    rename["system"] = "procs_running"  # the only system field we pulled
    wide = wide.rename(columns=rename).sort_index()
    return wide


def main():
    df = pull()
    df.to_csv(OUT)
    n_procs = df["procs_running"].notna().sum()
    n_both = (df["procs_running"].notna() & df["power_watts"].notna()).sum()
    print(f"saved {len(df)} rows -> {OUT}")
    print(f"  cols: {list(df.columns)}")
    print(f"  span: {df.index.min()} -> {df.index.max()}")
    print(f"  procs_running present: {n_procs}; both power+procs: {n_both}")
    assert n_both > 10_000, f"too little overlap to model: {n_both}"


if __name__ == "__main__":
    main()
