#!/usr/bin/env python3
"""Extended precursor screen: new upstream heuristics (memory, thermal, IO, process).

Screens candidates causally (trailing windows only) against TRUE spike onsets
(not label confirmation time) using Cohen's d effect size. Same methodology
as precursor_screen.py but with expanded candidate set.

Candidates:
- nr_running: slope, jerk, rolling_cv, rolling_peak_to_mean
- ctxt_per_s: slope
- memory: rolling delta cached/available, swap_used slope
- thermal: temp_celsius slope (if available)
- per-process: procs_running slope (proxy for new process arrival)
- IO: disk_io/net_io slopes
"""

import argparse
import sys

import numpy as np
import pandas as pd
from scipy import stats

from core import telemetry as common
from validation.evaluation import true_onset_ts
from core.spike_labels import DEFAULT_DERIV_THRESHOLD_W, DEFAULT_HORIZON_S


DEFAULT_LOOKBACK_S = 30


def candidate_features(df, lookback_s):
    """Compute trailing-window candidate features. Returns dict of Series."""
    features = {}

    # nr_running: slope, jerk, rolling_cv, rolling_peak_to_mean
    if "nr_running" in df.columns:
        nr = df["nr_running"].ffill()
        roll = nr.rolling(window=lookback_s, min_periods=lookback_s)
        features["nr_running_slope"] = nr.diff(periods=lookback_s) / lookback_s
        features["nr_running_jerk"] = nr.diff(periods=1).rolling(lookback_s).std()
        features["nr_running_cv"] = roll.std() / (roll.mean() + 1e-6)
        features["nr_running_peak_to_mean"] = roll.max() / (roll.mean() + 1e-6)

    # ctxt_per_s: slope
    if "ctxt_per_s" in df.columns:
        ctxt = df["ctxt_per_s"].ffill()
        features["ctxt_per_s_slope"] = ctxt.diff(periods=lookback_s) / lookback_s

    # memory: rolling delta in cached/available (cache eviction pressure)
    if "mem_cached" in df.columns:
        cached = df["mem_cached"].ffill()
        features["mem_cached_delta"] = cached.diff(periods=lookback_s)

    if "mem_available" in df.columns:
        avail = df["mem_available"].ffill()
        features["mem_available_delta"] = avail.diff(periods=lookback_s)

    if "mem_used" in df.columns:  # hf_upstream.csv carries used/available, no cached
        used = df["mem_used"].ffill()
        features["mem_used_delta"] = used.diff(periods=lookback_s)

    # swap activity
    if "mem_swap_used" in df.columns:
        swap = df["mem_swap_used"].ffill()
        features["mem_swap_slope"] = swap.diff(periods=lookback_s) / lookback_s

    # thermal: temp_celsius slope (distance to throttle)
    if "temp_celsius" in df.columns:
        temp = df["temp_celsius"].ffill()
        features["temp_celsius_slope"] = temp.diff(periods=lookback_s) / lookback_s

    # per-process: procs_running as proxy for new process arrival
    if "procs_running" in df.columns:
        procs = df["procs_running"].ffill()
        features["procs_running_slope"] = procs.diff(periods=lookback_s) / lookback_s

    # IO slopes
    if "disk_read_bps" in df.columns:
        disk_rd = df["disk_read_bps"].ffill()
        features["disk_read_slope"] = disk_rd.diff(periods=lookback_s) / lookback_s

    if "disk_write_bps" in df.columns:
        disk_wr = df["disk_write_bps"].ffill()
        features["disk_write_slope"] = disk_wr.diff(periods=lookback_s) / lookback_s

    if "net_rx_bps" in df.columns:
        net_rx = df["net_rx_bps"].ffill()
        features["net_rx_slope"] = net_rx.diff(periods=lookback_s) / lookback_s

    if "net_tx_bps" in df.columns:
        net_tx = df["net_tx_bps"].ffill()
        features["net_tx_slope"] = net_tx.diff(periods=lookback_s) / lookback_s

    return features


def screen_vs_true_onset(df, lookback_s, deriv_threshold=DEFAULT_DERIV_THRESHOLD_W):
    """Screen each feature against TRUE spike onsets (not label confirmation time).

    Returns list of (name, n_pre, n_normal, mean_pre, mean_normal, p_value, cohens_d).
    """
    # Get TRUE onset timestamps
    onsets_ts = pd.DatetimeIndex(true_onset_ts(df["power_watts"], min_step=deriv_threshold))

    # For each timestamp, is a true onset within the next lookback_s samples (HORIZON_S)?
    # This is backward: we want to know if this feature value predicted an onset,
    # so we should label each timestamp with "will an onset happen in the next horizon?"
    horizon_s = DEFAULT_HORIZON_S
    label = pd.Series(0, index=df.index, dtype=int)
    for onset_t in onsets_ts:
        mask = (df.index > onset_t - pd.Timedelta(seconds=horizon_s)) & (df.index <= onset_t)
        label[mask] = 1

    features = candidate_features(df, lookback_s)
    results = []

    for name, feat in features.items():
        valid = feat.notna() & label.notna()
        pre_onset = feat[valid & (label > 0)]
        normal = feat[valid & (label == 0)]

        if len(pre_onset) < 2 or len(normal) < 2:
            results.append((name, len(pre_onset), len(normal), None, None, None, None))
            continue

        t, p = stats.ttest_ind(pre_onset, normal, equal_var=False)
        pooled_std = np.sqrt((pre_onset.std() ** 2 + normal.std() ** 2) / 2)
        cohens_d = (pre_onset.mean() - normal.mean()) / pooled_std if pooled_std else float("nan")
        results.append((name, len(pre_onset), len(normal), pre_onset.mean(), normal.mean(), p, cohens_d))

    return results


def print_report(results):
    header = f"{'feature':<28}{'n_pre':>8}{'n_normal':>10}{'mean_pre':>14}{'mean_normal':>14}{'p_value':>10}{'cohens_d':>10}  sig"
    print(header)
    for name, n_pre, n_normal, mean_pre, mean_normal, p, d in results:
        if p is None:
            print(f"{name:<28}{n_pre:>8}{n_normal:>10}{'--':>14}{'--':>14}{'--':>10}{'--':>10}  n/a (too few)")
            continue
        sig = "***" if p < 0.001 else "**" if p < 0.01 else "*" if p < 0.05 else "ns"
        print(f"{name:<28}{n_pre:>8}{n_normal:>10}{mean_pre:>14.6g}{mean_normal:>14.6g}{p:>10.4g}{d:>10.3f}  {sig}")


def main():
    if "--selfcheck" in sys.argv:
        selfcheck()
        return

    p = argparse.ArgumentParser(description="Extended precursor heuristic screen (vs TRUE onsets).")
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK_S,
                   help="trailing window in seconds for rolling features")
    p.add_argument("--derivative-threshold", type=float, default=DEFAULT_DERIV_THRESHOLD_W,
                   help="W/s jump that counts as a spike onset")
    p.add_argument("--minutes", type=int, default=None,
                   help="bound upstream query and data slice to last N minutes (default: full cache span)")
    args = p.parse_args()

    print("Loading upstream telemetry...")
    df_base = common.load_telemetry(args.start, args.end)
    upstream_stop = "now()"
    if args.minutes is not None:
        if len(df_base) == 0:
            print(f"Warning: no cached rows in window; upstream query still uses -{args.minutes}m")
            upstream_start = f"-{args.minutes}m"
        else:
            stop_ts = df_base.index.max()
            start_ts = stop_ts - pd.Timedelta(minutes=args.minutes)
            df_base = df_base[df_base.index >= start_ts]
            upstream_start = start_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
            upstream_stop = stop_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        upstream_start = f"-{len(df_base)}s" if len(df_base) > 0 else "-15m"

    print(f"Querying upstream signals (nr_running, ctxt, mem, thermal, IO) from {upstream_start} to {upstream_stop}...")
    try:
        df_upstream = common.query_upstream(
            start=upstream_start,
            stop=upstream_stop
        )
        overlap = df_upstream.columns.intersection(df_base.columns)
        if len(overlap):
            df_upstream = df_upstream.drop(columns=overlap)
        df = pd.merge(df_base, df_upstream, left_index=True, right_index=True, how="left")
    except Exception as e:
        print(f"Warning: could not fetch upstream signals (live connection?): {e}")
        print("Falling back to base telemetry only.")
        df = df_base

    print(f"Screening {len(df)} samples...")
    results = screen_vs_true_onset(df, args.lookback, args.derivative_threshold)
    print_report(results)


def selfcheck():
    df = common.load_telemetry()
    if "power_watts" not in df.columns:
        print("selfcheck SKIP: no cached data")
        return

    onsets = true_onset_ts(df["power_watts"], DEFAULT_DERIV_THRESHOLD_W)
    assert len(onsets) > 0, "expected at least one true onset on cached data"

    results = screen_vs_true_onset(df, DEFAULT_LOOKBACK_S, DEFAULT_DERIV_THRESHOLD_W)
    assert len(results) > 0, "expected some candidates"

    for name, n_pre, n_normal, mean_pre, mean_normal, pval, d in results:
        if pval is None:
            continue
        assert 0.0 <= pval <= 1.0, f"{name}: p-value out of range: {pval}"
        assert np.isfinite(d) or np.isnan(d), f"{name}: invalid Cohen's d"

    print(f"selfcheck OK: {len(results)} candidates screened vs {len(onsets)} true onsets")


if __name__ == "__main__":
    main()
