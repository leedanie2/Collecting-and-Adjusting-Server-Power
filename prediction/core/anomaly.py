#!/usr/bin/env python3
"""Simplest-first anomaly detection (E): matrix-profile discords + an EWMA
control chart. Per your instruction, no deep model unless these underperform.
"""

import argparse

import numpy as np
import stumpy

from core import telemetry as common


def matrix_profile_discords(series, window_s, top=5):
    mp = stumpy.stump(series.to_numpy(), m=window_s)
    discord_idx = np.argsort(mp[:, 0])[::-1][:top]
    return [(series.index[i], mp[i, 0]) for i in discord_idx]


def ewma_z(series, span=30):
    """Causal EWMA z-score -- only uses data up to and including each point."""
    mean = series.ewm(span=span).mean()
    std = series.ewm(span=span).std()
    return (series - mean) / std


def ewma_flags(series, span=30, k=3):
    z = ewma_z(series, span=span)
    return series.index[z.abs() > k]


def main():
    p = argparse.ArgumentParser(description="Matrix-profile discords + EWMA control chart.")
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--column", default="power_watts")
    p.add_argument("--window", type=int, default=7, help="matrix profile subsequence length, seconds")
    p.add_argument("--top", type=int, default=5)
    args = p.parse_args()

    df = common.load_telemetry(args.start, args.end)
    series = df[args.column].dropna()

    print(f"Matrix profile discords (window={args.window}s):")
    for ts, score in matrix_profile_discords(series, args.window, args.top):
        print(f"  {ts}  discord_score={score:.2f}")

    print("\nEWMA control chart flags (|z| > 3):")
    flags = ewma_flags(series)
    print(f"  {len(flags)} flagged points" + (f", first: {flags[0]}" if len(flags) else ""))


if __name__ == "__main__":
    main()
