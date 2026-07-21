#!/usr/bin/env python3
"""Aggregate CPU-usage edge detector.

This is a deliberately small baseline/control detector: watch aggregate
`/proc/stat`, emit a short 0/1 pulse on a rising usage edge, and let consumers
such as `ramp.c --risk-file` handle the bounded trapezoid. It does not train and
does not read power telemetry.
"""

import argparse
import datetime
import json
import sys
import time
from pathlib import Path

from core import telemetry as common

DEFAULT_THRESHOLD_PCT = 20.0
DEFAULT_RISE_PCT_PER_S = 25.0
DEFAULT_CYCLE_S = 0.10
DEFAULT_PULSE_S = 1.0
DEFAULT_COOLDOWN_S = 5.0
DEFAULT_RISK_FILE = common.DATA_DIR / "usage_spike_risk.flag"
INFLUX_BUCKET = "Power"
DEFAULT_MEASUREMENT = "power_usage_edge_prediction"


def read_total_cpu_stat(path="/proc/stat"):
    """Return aggregate CPU counters from /proc/stat."""
    with open(path, "r") as f:
        line = f.readline()
    parts = line.split()
    if not parts or parts[0] != "cpu":
        raise RuntimeError(f"aggregate cpu line not found in {path}")
    vals = [int(x) for x in parts[1:9]]
    while len(vals) < 8:
        vals.append(0)
    return tuple(vals)


def cpu_usage(prev, curr):
    """Busy fraction in percent between two aggregate /proc/stat samples."""
    prev_idle = prev[3] + prev[4]
    curr_idle = curr[3] + curr[4]
    prev_total = sum(prev)
    curr_total = sum(curr)
    dt = curr_total - prev_total
    if dt <= 0:
        return 0.0
    return 100.0 * (1.0 - (curr_idle - prev_idle) / dt)


def usage_edge_fired(prev_usage, usage, dt_s, threshold, min_rise_per_s):
    """Same edge semantics as ramp.c's built-in --usage-edge path."""
    if dt_s <= 0.0:
        return False
    if usage < threshold:
        return False
    if prev_usage < threshold:
        return True
    return ((usage - prev_usage) / dt_s) >= min_rise_per_s


def usage_edge_flags(index, usage, threshold=DEFAULT_THRESHOLD_PCT,
                     min_rise_per_s=DEFAULT_RISE_PCT_PER_S):
    idx = list(index)
    vals = list(usage)
    flags = []
    for i in range(1, len(idx)):
        t0 = idx[i - 1]
        t1 = idx[i]
        dt_s = (t1 - t0).total_seconds()
        prev_usage = float(vals[i - 1])
        cur_usage = float(vals[i])
        flags.append(bool(usage_edge_fired(
            prev_usage, cur_usage, dt_s, threshold, min_rise_per_s
        )))
    if not idx:
        return []
    return [False] + flags


def evaluate_dataframe(df, threshold=DEFAULT_THRESHOLD_PCT,
                       min_rise_per_s=DEFAULT_RISE_PCT_PER_S,
                       lead_s=5.0, lag_s=5.0, merge_gap_s=2.0):
    """Score usage-edge alerts against raw upward power onsets."""
    need = {"usage_percent", "power_watts"}
    missing = need - set(df.columns)
    if missing:
        raise ValueError(f"missing required columns: {sorted(missing)}")
    df = df.dropna(subset=["usage_percent", "power_watts"]).sort_index()
    if len(df) < 2:
        raise ValueError("need at least two rows to evaluate usage edges")

    from validation import evaluation as ev

    flags = usage_edge_flags(df.index, df["usage_percent"], threshold,
                             min_rise_per_s)
    alert_ts = df.index[flags]
    event_ts = ev.true_onset_ts(df["power_watts"])
    stats = ev.event_alert_score(
        event_ts,
        alert_ts,
        start=df.index[0],
        end=df.index[-1],
        lead_s=lead_s,
        lag_s=lag_s,
        merge_gap_s=merge_gap_s,
    )
    stats.update({
        "threshold": float(threshold),
        "min_rise_per_s": float(min_rise_per_s),
        "alert_duty": float(sum(flags) / len(flags)) if flags else float("nan"),
        "n_alert_points": int(sum(flags)),
        "n_eval_points": int(len(flags)),
        "window_start": str(df.index[0]),
        "window_end": str(df.index[-1]),
    })
    return stats


def write_prediction(write_api, measurement, usage, prev_usage, risk, edge,
                     threshold, min_rise_per_s):
    from influxdb_client.client.write_api import Point

    point = (
        Point(measurement)
        .tag("server", "mycroft")
        .tag("model", "usage_edge")
        .field("usage_percent", float(usage))
        .field("usage_risk", int(risk))
        .field("usage_edge", int(edge))
        .field("threshold", float(threshold))
        .field("min_rise_per_s", float(min_rise_per_s))
        .time(datetime.datetime.utcnow(), write_precision="ns")
    )
    if prev_usage is not None:
        point = point.field("prev_usage_percent", float(prev_usage))
    write_api.write(bucket=INFLUX_BUCKET, record=point)


class UsageEdgeState:
    def __init__(self, threshold=DEFAULT_THRESHOLD_PCT,
                 min_rise_per_s=DEFAULT_RISE_PCT_PER_S,
                 pulse_s=DEFAULT_PULSE_S,
                 cooldown_s=DEFAULT_COOLDOWN_S):
        self.threshold = float(threshold)
        self.min_rise_per_s = float(min_rise_per_s)
        self.pulse_s = float(pulse_s)
        self.cooldown_s = float(cooldown_s)
        self.pulse_until = 0.0
        self.cooldown_until = 0.0

    def update(self, prev_usage, usage, dt_s, now=None):
        now = time.monotonic() if now is None else float(now)
        edge = False
        if now >= self.cooldown_until and usage_edge_fired(
            prev_usage, usage, dt_s, self.threshold, self.min_rise_per_s
        ):
            edge = True
            self.pulse_until = now + self.pulse_s
            self.cooldown_until = now + self.cooldown_s
        risk = now < self.pulse_until
        return int(risk), edge


def run(risk_file, threshold, min_rise_per_s, cycle_s, pulse_s, cooldown_s,
        samples=0, dry_run=False, json_lines=False, influx=False,
        measurement=DEFAULT_MEASUREMENT):
    state = UsageEdgeState(threshold, min_rise_per_s, pulse_s, cooldown_s)
    prev_stat = read_total_cpu_stat()
    prev_t = time.monotonic()
    prev_usage = None
    n = 0
    client = write_api = None
    if influx and not dry_run:
        from influxdb_client import InfluxDBClient
        from influxdb_client.client.write_api import SYNCHRONOUS

        client = InfluxDBClient(url=common.INFLUX_URL,
                                token=common.load_write_token(),
                                org=common.load_org())
        write_api = client.write_api(write_options=SYNCHRONOUS)

    try:
        while True:
            time.sleep(cycle_s)
            curr_t = time.monotonic()
            curr_stat = read_total_cpu_stat()
            usage = cpu_usage(prev_stat, curr_stat)
            dt_s = max(curr_t - prev_t, 1e-9)

            if prev_usage is None:
                risk, edge = 0, False
            else:
                risk, edge = state.update(prev_usage, usage, dt_s, now=curr_t)

            if not dry_run:
                common.write_risk_flag(risk_file, bool(risk))
            if write_api is not None:
                write_prediction(write_api, measurement, usage, prev_usage,
                                 risk, edge, threshold, min_rise_per_s)
            if json_lines:
                print(json.dumps({
                    "usage_percent": usage,
                    "prev_usage_percent": prev_usage,
                    "risk": risk,
                    "edge": int(edge),
                    "threshold": float(threshold),
                    "min_rise_per_s": float(min_rise_per_s),
                }), flush=True)

            prev_stat = curr_stat
            prev_t = curr_t
            prev_usage = usage
            n += 1
            if samples and n >= samples:
                break
    finally:
        if client is not None:
            client.close()


def selfcheck():
    assert not usage_edge_fired(2.0, 4.0, 0.1, 20.0, 25.0)
    assert usage_edge_fired(2.0, 22.0, 0.1, 20.0, 25.0)
    assert usage_edge_fired(30.0, 40.0, 0.1, 20.0, 25.0)
    assert not usage_edge_fired(30.0, 31.0, 0.1, 20.0, 25.0)

    state = UsageEdgeState(threshold=20.0, min_rise_per_s=25.0,
                           pulse_s=1.0, cooldown_s=3.0)
    risk, edge = state.update(2.0, 30.0, 0.1, now=10.0)
    assert risk == 1 and edge, (risk, edge)
    risk, edge = state.update(30.0, 90.0, 0.1, now=10.5)
    assert risk == 1 and not edge, (risk, edge)
    risk, edge = state.update(10.0, 50.0, 0.1, now=11.2)
    assert risk == 0 and not edge, (risk, edge)
    risk, edge = state.update(10.0, 50.0, 0.1, now=13.1)
    assert risk == 1 and edge, (risk, edge)

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "usage.flag"
        common.write_risk_flag(p, True)
        assert p.read_text() == "1\n"
        common.write_risk_flag(p, False)
        assert p.read_text() == "0\n"

    import pandas as pd
    import numpy as np
    idx = pd.date_range("2026-01-01T00:00:00Z", periods=80, freq="1s")
    df = pd.DataFrame({
        "usage_percent": np.r_[np.full(38, 2.0), 30.0, np.full(41, 35.0)],
        "power_watts": np.r_[np.full(40, 100.0), np.full(40, 230.0)],
    }, index=idx)
    stats = evaluate_dataframe(df, threshold=20.0, min_rise_per_s=10.0,
                               lead_s=5.0, lag_s=5.0)
    assert stats["n_events"] == 1 and stats["n_events_detected"] == 1, stats
    assert stats["event_latency_median_s"] < 0.0, stats

    print("selfcheck OK: usage edge pulse + risk flag writer + evaluator")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--risk-file", default=str(DEFAULT_RISK_FILE),
                   help="0/1 flag path consumed by ramp.c --risk-file")
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD_PCT,
                   help="aggregate CPU usage percent that starts an edge")
    p.add_argument("--rise", type=float, default=DEFAULT_RISE_PCT_PER_S,
                   help="minimum aggregate usage rise rate in percent/s")
    p.add_argument("--cycle-s", type=float, default=DEFAULT_CYCLE_S,
                   help="/proc/stat polling interval")
    p.add_argument("--pulse-s", type=float, default=DEFAULT_PULSE_S,
                   help="seconds to hold the flag high after an edge")
    p.add_argument("--cooldown-s", type=float, default=DEFAULT_COOLDOWN_S,
                   help="minimum seconds between edge pulses")
    p.add_argument("--samples", type=int, default=0,
                   help="exit after N samples; default runs forever")
    p.add_argument("--dry-run", action="store_true",
                   help="do not write the risk flag")
    p.add_argument("--json", action="store_true",
                   help="print one JSON status line per sample")
    p.add_argument("--influx", action="store_true",
                   help=f"write Grafana-visible points to {DEFAULT_MEASUREMENT}")
    p.add_argument("--measurement", default=DEFAULT_MEASUREMENT,
                   help="InfluxDB measurement for --influx")
    p.add_argument("--evaluate-cache", action="store_true",
                   help="score cached telemetry instead of watching /proc/stat")
    p.add_argument("--start", default=None,
                   help="cache start time for --evaluate-cache")
    p.add_argument("--stop", default=None,
                   help="cache stop time for --evaluate-cache")
    p.add_argument("--selfcheck", action="store_true")
    args = p.parse_args()

    if args.selfcheck:
        selfcheck()
        return
    if args.threshold < 0.0 or args.threshold > 100.0:
        p.error("--threshold must be in [0, 100]")
    if args.rise < 0.0 or args.cycle_s <= 0.0 or args.pulse_s <= 0.0:
        p.error("--rise must be >=0 and --cycle-s/--pulse-s must be >0")
    if args.cooldown_s < args.pulse_s:
        p.error("--cooldown-s must be >= --pulse-s")

    if args.evaluate_cache:
        df = common.load_telemetry(args.start, args.stop)
        stats = evaluate_dataframe(df, threshold=args.threshold,
                                   min_rise_per_s=args.rise)
        if args.json:
            print(json.dumps(stats, indent=2))
        else:
            print("Usage-edge detector evaluator")
            print(f"  Window              {stats['window_start']} -> {stats['window_end']}")
            print(f"  Event recall         {stats['event_recall']:.4f} "
                  f"({stats['n_events_detected']}/{stats['n_events']} raw onsets)")
            print(f"  First-flag latency   {stats['event_latency_median_s']:.1f}s median")
            print(f"  Alert duty           {stats['alert_duty']:.4f} "
                  f"({stats['n_alert_points']}/{stats['n_eval_points']} rows)")
            print(f"  False alert rate     {stats['false_alert_episodes_per_h']:.2f} episodes/hour")
        return

    run(Path(args.risk_file), args.threshold, args.rise, args.cycle_s,
        args.pulse_s, args.cooldown_s, samples=args.samples,
        dry_run=args.dry_run, json_lines=args.json, influx=args.influx,
        measurement=args.measurement)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(0)
