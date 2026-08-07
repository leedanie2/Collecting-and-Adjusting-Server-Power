"""Shared helpers for the power telemetry analysis scripts.

Schema note: cpu_freq/cpu_usage are tagged per logical core, cpu_temp per
physical core (see influx2.py). Their `core` tag values are NOT a shared join
key, so everything here works on cross-core means, never per-core joins.
"""

import warnings
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from influxdb_client import InfluxDBClient
from influxdb_client.client.warnings import MissingPivotFunction

warnings.simplefilter("ignore", MissingPivotFunction)

ANALYSIS_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ANALYSIS_DIR / "data"
CACHE_PATH = DATA_DIR / "telemetry.csv"

SECRETS_DIR = Path.home() / ".secrets"
TOKEN_PATH = SECRETS_DIR / "influx_read_token.txt"
ORG_PATH = SECRETS_DIR / "influx_org.txt"
WRITE_TOKEN_PATH = SECRETS_DIR / "influx_token.txt"

INFLUX_URL = "http://mycroft:8086"
TRAINING_TZ = "America/New_York"
TRAINING_START_HOUR = 8
TRAINING_END_HOUR = 16

RENAME = {
    "cpu_power": "power_watts",
    "cpu_freq": "freq_mhz",
    "cpu_temp": "temp_celsius",
    "cpu_usage": "usage_percent",
    "pdu_power": "pdu_watts",
}

FLUX = """
from(bucket: "{bucket}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "cpu_power" or r._measurement == "cpu_freq" or r._measurement == "cpu_temp" or r._measurement == "cpu_usage" or r._measurement == "pdu_power")
  |> group(columns: ["_time", "_measurement"])
  |> mean()
  |> sort(columns: ["_time"])
"""


def load_org():
    return ORG_PATH.read_text().strip()


def load_token():
    return TOKEN_PATH.read_text().strip()


def load_write_token():
    return WRITE_TOKEN_PATH.read_text().strip()


def weekday_business_hours_mask(index, tz=TRAINING_TZ,
                                start_hour=TRAINING_START_HOUR,
                                end_hour=TRAINING_END_HOUR):
    """Rows allowed for detector training: Mon-Fri, [start_hour, end_hour).

    Timestamps in the telemetry cache are UTC-aware; this helper also accepts
    naive indexes and treats them as UTC. The returned mask is positional so it
    can be applied to DataFrames or aligned feature matrices.
    """
    idx = pd.DatetimeIndex(index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    local = idx.tz_convert(ZoneInfo(tz))
    return (local.weekday < 5) & (local.hour >= start_hour) & (local.hour < end_hour)


def weekday_business_hours(df, tz=TRAINING_TZ,
                           start_hour=TRAINING_START_HOUR,
                           end_hour=TRAINING_END_HOUR):
    """Return only weekday business-hour rows for training/evaluation."""
    if df.empty:
        return df
    return df.loc[weekday_business_hours_mask(df.index, tz, start_hour, end_hour)]


def query_recent(start="-15m", stop="now()", timeout_ms=30_000):
    """Query InfluxDB directly (no local cache) for a recent window. Cross-core
    mean is collapsed server-side same as pull.py's cache, then timestamps are
    rounded to the nearest second so cpu_power and the per-core measurements
    (slightly different microsecond timestamps within the same 1Hz tick) land
    in the same row.
    """
    org = load_org()
    token = load_token()
    flux = FLUX.format(bucket=org, start=start, stop=stop)

    client = InfluxDBClient(url=INFLUX_URL, token=token, org=org, timeout=timeout_ms)
    try:
        df = client.query_api().query_data_frame(flux)
    finally:
        client.close()

    if isinstance(df, list):
        df = pd.concat(df, ignore_index=True)
    if df.empty:
        return pd.DataFrame(columns=list(RENAME.values()))

    df["_time"] = pd.to_datetime(df["_time"]).dt.round("1s")
    wide = df.pivot_table(index="_time", columns="_measurement", values="_value", aggfunc="mean")
    return wide.rename(columns=RENAME).sort_index()


FLUX_WINDOW = """
from(bucket: "{bucket}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => {meas_filter})
  |> group(columns: ["_measurement"])
  |> aggregateWindow(every: 1s, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_measurement", "_value"])
"""


def query_window(start, stop="now()", measurements=("cpu_power", "cpu_usage"),
                 timeout_ms=120_000):
    """Efficient bulk query for a wide window (minutes to days). Unlike
    query_recent's per-timestamp group(), aggregateWindow keeps one table per
    measurement, so a 12h pull doesn't explode into millions of tiny frames.
    Cross-core mean per second; returns the same RENAME-d wide frame.
    """
    org, token = load_org(), load_token()
    meas_filter = " or ".join(f'r._measurement == "{m}"' for m in measurements)
    flux = FLUX_WINDOW.format(bucket=org, start=start, stop=stop, meas_filter=meas_filter)
    client = InfluxDBClient(url=INFLUX_URL, token=token, org=org, timeout=timeout_ms)
    try:
        df = client.query_api().query_data_frame(flux)
    finally:
        client.close()
    if isinstance(df, list):
        df = pd.concat(df, ignore_index=True)
    if df.empty:
        return pd.DataFrame(columns=[RENAME.get(m, m) for m in measurements])
    df["_time"] = pd.to_datetime(df["_time"]).dt.round("1s")
    wide = df.pivot_table(index="_time", columns="_measurement", values="_value", aggfunc="mean")
    return wide.rename(columns=RENAME).sort_index()


# Upstream OS signals (canonical feature name -> InfluxDB measurement.field).
# These are causally upstream of power in different ways (run-queue = work
# arriving before the CPU executes it, mem pressure, scheduler churn, turbo).
# nr_running streams at 10 Hz but is meaned to the 1 Hz feature grid here; the
# rest are natively 1 Hz. Confirmed live on mycroft 2026-07-01. See session_2026-07-01c.
UPSTREAM_FIELDS = {
    "nr_running":     ("run_queue", "nr_running"),
    "procs_running":  ("system", "procs_running"),
    "load1":          ("system", "load1"),
    "load5":          ("system", "load5"),
    "load15":         ("system", "load15"),
    "ctxt_per_s":     ("system", "ctxt_per_s"),
    "mem_used":       ("mem", "used"),
    "mem_available":  ("mem", "available"),
    "mem_cached":     ("mem", "cached"),
    "mem_swap_used":  ("mem", "swap_used"),
    "disk_read_bps":  ("disk_io", "read_bps"),
    "disk_write_bps": ("disk_io", "write_bps"),
    "net_rx_bps":     ("net_io", "rx_bps"),
    "net_tx_bps":     ("net_io", "tx_bps"),
    "freq_mhz":       ("cpu_freq", "mhz"),
    "temp_celsius":   ("cpu_temp", "celsius"),
}

FLUX_UPSTREAM = """
from(bucket: "{bucket}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => {meas_filter})
  |> group(columns: ["_measurement", "_field"])
  |> aggregateWindow(every: 1s, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_measurement", "_field", "_value"])
"""


def query_upstream(start, stop="now()", timeout_ms=120_000):
    """Pull the UPSTREAM_FIELDS signals as a wide 1 Hz frame with canonical column
    names. Multi-field measurements (system/mem/disk_io/net_io) carry several
    fields, so this pivots on _measurement+_field -- unlike query_recent/window
    which assume one field per measurement. Cross-tag mean per second (device/
    iface/core tags collapsed; scale-only, RF is scale-invariant)."""
    org, token = load_org(), load_token()
    meas = sorted({m for m, _ in UPSTREAM_FIELDS.values()})
    meas_filter = " or ".join(f'r._measurement == "{m}"' for m in meas)
    flux = FLUX_UPSTREAM.format(bucket=org, start=start, stop=stop, meas_filter=meas_filter)
    client = InfluxDBClient(url=INFLUX_URL, token=token, org=org, timeout=timeout_ms)
    try:
        df = client.query_api().query_data_frame(flux)
    finally:
        client.close()
    if isinstance(df, list):
        df = pd.concat(df, ignore_index=True)
    if df.empty:
        return pd.DataFrame(columns=list(UPSTREAM_FIELDS))
    df["_time"] = pd.to_datetime(df["_time"]).dt.round("1s")
    df["_key"] = df["_measurement"] + "." + df["_field"]
    wide = df.pivot_table(index="_time", columns="_key", values="_value", aggfunc="mean")
    out = pd.DataFrame(index=wide.index)
    for name, (m, f) in UPSTREAM_FIELDS.items():
        key = f"{m}.{f}"
        if key in wide.columns:
            out[name] = wide[key]
    return out.sort_index()


def load_telemetry(start=None, end=None):
    """Load cached telemetry (see pull.py), optionally sliced to [start, end)."""
    if not CACHE_PATH.exists():
        raise FileNotFoundError(
            f"No cached data at {CACHE_PATH}. Run `python pull.py` first."
        )
    df = pd.read_csv(CACHE_PATH, index_col="_time", parse_dates=["_time"])
    df = df.sort_index()
    if start is not None:
        df = df[df.index >= pd.Timestamp(start, tz="UTC")]
    if end is not None:
        df = df[df.index < pd.Timestamp(end, tz="UTC")]
    return df


def segment_runs(df, usage_threshold=15.0, min_run_s=10):
    """Find contiguous (start, end) windows where usage_percent stays above
    usage_threshold for at least min_run_s seconds. Threshold-based, justified
    because the known busy/dip cycle (~21s) is far above the 1s sample rate.
    """
    busy = df["usage_percent"] > usage_threshold
    runs = []
    run_start = None
    prev_t = None
    for t, is_busy in busy.items():
        if is_busy and run_start is None:
            run_start = t
        elif not is_busy and run_start is not None:
            if (prev_t - run_start).total_seconds() >= min_run_s:
                runs.append((run_start, prev_t))
            run_start = None
        prev_t = t
    if run_start is not None and (prev_t - run_start).total_seconds() >= min_run_s:
        runs.append((run_start, prev_t))
    return runs


def idle_baseline_watts(df, runs):
    """Median power_watts outside any segmented run (the idle draw to net out)."""
    mask = pd.Series(True, index=df.index)
    for start, end in runs:
        mask &= ~((df.index >= start) & (df.index <= end))
    idle = df.loc[mask, "power_watts"]
    if idle.empty:
        raise ValueError("No idle samples found outside segmented runs.")
    return idle.median()


def write_risk_flag(path, risk):
    """Atomically publish 0/1 spike risk for same-box consumers
    (ramp.c --risk-file, rapl_capper --watch-file). mtime doubles as the
    freshness signal, so call every successful prediction cycle even when
    the value is unchanged."""
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("1\n" if risk else "0\n")
    tmp.replace(path)
