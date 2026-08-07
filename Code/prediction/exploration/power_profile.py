#!/usr/bin/env python3
"""Per-run power profile (A), idle-baseline net power (D), and cross-signal
correlation (B). See the README for what each of these can and can't
tell you given the live schema.
"""

import argparse

import pandas as pd

from core import telemetry as common


def ramp_rate(series, window_s=5):
    """Max |slope| (watts/s) over any window_s-second span, for ramp-up/down."""
    diffs = series.diff().abs()
    return diffs.rolling(f"{window_s}s").sum().max() / window_s


def describe_run(df, start, end):
    run = df.loc[start:end]
    power = run["power_watts"]
    stats = power.describe(percentiles=[0.5, 0.9, 0.99])
    print(f"\nRun {start} -> {end} ({len(run)}s)")
    print(stats.to_string())
    print(f"ramp_up_max_w_per_s:   {ramp_rate(power):.1f}")
    print(f"net_power_mean_watts:  {run['net_power_watts'].mean():.1f}")
    # Same formulas as MATLAB Grid Simulation/analysis/cost_analysis.py's
    # compute_risk_metrics -- cv and peak_to_mean are unitless ratios, so
    # they're directly comparable to the grid-side values, no rescaling.
    print(f"cv (std/mean):         {power.std() / power.mean():.4f}")
    print(f"peak_to_mean:          {power.max() / power.mean():.4f}")


def main():
    p = argparse.ArgumentParser(description="Per-run power profile + correlation.")
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--usage-threshold", type=float, default=15.0,
                    help="usage_percent above this counts as 'busy' for run segmentation")
    args = p.parse_args()

    df = common.load_telemetry(args.start, args.end)
    runs = common.segment_runs(df, usage_threshold=args.usage_threshold)

    if not runs:
        print("No runs detected above usage threshold; treating whole window as idle.")
        baseline = df["power_watts"].median()
    else:
        baseline = common.idle_baseline_watts(df, runs)
    print(f"Idle baseline: {baseline:.1f} W")
    df["net_power_watts"] = df["power_watts"] - baseline

    if not runs:
        return
    for start, end in runs:
        describe_run(df, start, end)

    print("\nCorrelation (power_watts, freq_mhz, temp_celsius, usage_percent), full window:")
    print(df[["power_watts", "freq_mhz", "temp_celsius", "usage_percent"]].corr().to_string())


if __name__ == "__main__":
    main()
