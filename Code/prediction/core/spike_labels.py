#!/usr/bin/env python3
"""Precursor significance screen (I): for each candidate heuristic feature,
test whether its trailing-window value differs significantly between
"spike coming in the next HORIZON_S seconds" and "normal" timestamps.

a two-group Welch's t-test is mathematically identical to one-way
ANOVA with 2 groups (F = t**2) -- no need for a separate ANOVA call. scipy is
already an installed transitive dep (via stumpy), so this adds no new
dependency, just an explicit one in requirements.txt.

This is a screening report only -- no model, no daemon write. It tells you
which heuristics are worth feeding into spike_daemon.py next, nothing more.
"""

import argparse
import sys

import numpy as np
from scipy import stats

from core import anomaly
from core import telemetry as common

# 1Hz data, so "samples" and "seconds" are interchangeable -- same assumption
# regression.py/spike_daemon.py already make for their lag windows.
DEFAULT_HORIZON_S = 5     # matches spike_daemon.py's HORIZON_S
DEFAULT_LOOKBACK_S = 30   # trailing window for rolling features
DEFAULT_DERIV_THRESHOLD_W = 97.0  # EVT/POT-derived severe tier


def signed_spike_events(power, deriv_threshold, direction="both", ewma_span=30, ewma_k=3):
    """Per-timestamp signed power-event flags.

    direction="rise" catches sharp upward steps / high EWMA-z excursions.
    direction="drop" catches sharp downward steps / low EWMA-z excursions.
    direction="both" preserves the historical absolute-spike behavior.
    """
    d = power.diff()
    z = anomaly.ewma_z(power, span=ewma_span)
    if direction == "rise":
        return (d > deriv_threshold) | (z > ewma_k)
    if direction == "drop":
        return (d < -deriv_threshold) | (z < -ewma_k)
    if direction == "both":
        return (d.abs() > deriv_threshold) | (z.abs() > ewma_k)
    raise ValueError(f"unknown spike direction: {direction}")


def spike_events(power, deriv_threshold, ewma_span=30, ewma_k=3):
    """Per-timestamp spike flag: a >deriv_threshold W/s jump OR an EWMA
    |z|>ewma_k point (anomaly.py's existing flag, reused not reimplemented).
    """
    return signed_spike_events(power, deriv_threshold, "both", ewma_span, ewma_k)


def rise_events(power, deriv_threshold=DEFAULT_DERIV_THRESHOLD_W, ewma_span=30, ewma_k=3):
    return signed_spike_events(power, deriv_threshold, "rise", ewma_span, ewma_k)


def drop_events(power, deriv_threshold=DEFAULT_DERIV_THRESHOLD_W, ewma_span=30, ewma_k=3):
    return signed_spike_events(power, deriv_threshold, "drop", ewma_span, ewma_k)


def spike_in_horizon(events, horizon_s):
    """Label per timestamp: does a spike occur in the next horizon_s samples
    (strictly after the current one)? Forward-looking rolling max via
    reverse -> rolling -> reverse.
    """
    future = events.shift(-1)
    return future[::-1].rolling(horizon_s, min_periods=1).max()[::-1]


def candidate_features(df, lookback_s):
    power = df["power_watts"]
    usage = df["usage_percent"]
    roll = power.rolling(window=lookback_s, min_periods=lookback_s)
    feats = {
        "rolling_cv": roll.std() / roll.mean(),
        "rolling_peak_to_mean": roll.max() / roll.mean(),
        "ewma_z": anomaly.ewma_z(power),
        "usage_slope_per_s": usage.diff(periods=lookback_s) / lookback_s,
    }
    # level + slope for every other cached channel present
    for col in ("freq_mhz", "temp_celsius", "pdu_watts", "procs_running", "usage_percent"):
        if col in df:
            sig = df[col].ffill()
            feats[col] = sig
            feats[f"{col}_slope"] = sig.diff(periods=lookback_s) / lookback_s
    # physics transforms: P ~ C*V^2*f*activity, V~f under DVFS -> f^2/f^3 terms
    if "freq_mhz" in df:
        ghz = df["freq_mhz"].ffill() / 1000.0
        u = usage.ffill()
        for name, sig in {
            "freq_sq_x_usage": ghz**2 * u,     # dynamic-power model, V^2*f*act
            "freq_cubed": ghz**3,               # full DVFS cube
            "freq_x_usage": ghz * u,            # throughput proxy
            "power_resid": power - power.mean() / max((ghz**2 * u).mean(), 1e-9) * ghz**2 * u,
        }.items():                              # resid: RAPL unexplained by f^2*u
            feats[name] = sig
            feats[f"{name}_slope"] = sig.diff(periods=lookback_s) / lookback_s
    return feats


def screen(df, horizon_s, lookback_s, deriv_threshold):
    events = spike_events(df["power_watts"], deriv_threshold)
    label = spike_in_horizon(events, horizon_s)
    features = candidate_features(df, lookback_s)

    results = []
    for name, feat in features.items():
        valid = feat.notna() & label.notna()
        pre_spike = feat[valid & (label > 0)]
        normal = feat[valid & (label == 0)]
        if len(pre_spike) < 2 or len(normal) < 2:
            results.append((name, len(pre_spike), len(normal), None, None, None, None))
            continue
        t, p = stats.ttest_ind(pre_spike, normal, equal_var=False)
        pooled_std = np.sqrt((pre_spike.std() ** 2 + normal.std() ** 2) / 2)
        cohens_d = (pre_spike.mean() - normal.mean()) / pooled_std if pooled_std else float("nan")
        results.append((name, len(pre_spike), len(normal), pre_spike.mean(), normal.mean(), p, cohens_d))
    return results


def print_report(results):
    header = f"{'feature':<24}{'n_pre':>8}{'n_normal':>10}{'mean_pre':>12}{'mean_normal':>14}{'p_value':>10}{'cohens_d':>10}  sig"
    print(header)
    for name, n_pre, n_normal, mean_pre, mean_normal, p, d in results:
        if p is None:
            print(f"{name:<24}{n_pre:>8}{n_normal:>10}{'--':>12}{'--':>14}{'--':>10}{'--':>10}  n/a (too few spike windows)")
            continue
        sig = "yes" if p < 0.05 else "no"
        print(f"{name:<24}{n_pre:>8}{n_normal:>10}{mean_pre:>12.4f}{mean_normal:>14.4f}{p:>10.4g}{d:>10.3f}  {sig}")


def main():
    if "--selfcheck" in sys.argv:
        selfcheck()
        return

    p = argparse.ArgumentParser(description="Precursor heuristic significance screen.")
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--horizon", type=int, default=DEFAULT_HORIZON_S,
                    help="seconds ahead a spike counts as 'pre-spike' (default matches spike_daemon.py)")
    p.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK_S,
                    help="trailing window in seconds for rolling features")
    p.add_argument("--derivative-threshold", type=float, default=DEFAULT_DERIV_THRESHOLD_W,
                    help="W/s jump that counts as a spike event")
    p.add_argument("--business-hours", action="store_true",
                    help="screen only weekday 08:00-16:00 %s rows" % common.TRAINING_TZ)
    p.add_argument("--procs-cache", action="store_true",
                    help="use telemetry_procs.csv (adds procs_running)")
    args = p.parse_args()

    if args.procs_cache:
        import pandas as pd
        df = pd.read_csv(common.DATA_DIR / "telemetry_procs.csv",
                         index_col="_time", parse_dates=["_time"]).sort_index()
    else:
        df = common.load_telemetry(args.start, args.end)
    if args.business_hours:
        df = common.weekday_business_hours(df)
    results = screen(df, args.horizon, args.lookback, args.derivative_threshold)
    print_report(results)


def selfcheck():
    df = common.load_telemetry()  # cached CSV -- no live connection needed
    events = spike_events(df["power_watts"], DEFAULT_DERIV_THRESHOLD_W)
    assert events.any(), "expected at least one spike event on cached data"

    label = spike_in_horizon(events, DEFAULT_HORIZON_S)
    assert (label > 0).any() and (label == 0).any(), "expected both labeled classes present"

    results = screen(df, DEFAULT_HORIZON_S, DEFAULT_LOOKBACK_S, DEFAULT_DERIV_THRESHOLD_W)
    assert len(results) >= 4, f"expected >=4 candidate features, got {len(results)}"
    for name, n_pre, n_normal, mean_pre, mean_normal, pval, d in results:
        if pval is None:
            continue
        assert 0.0 <= pval <= 1.0, f"{name}: p-value out of range: {pval}"
        assert np.isfinite(d), f"{name}: non-finite effect size"

    print(f"selfcheck OK: {len(results)} features screened")


if __name__ == "__main__":
    main()
