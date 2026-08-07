#!/usr/bin/env python3
"""Periodogram of a telemetry column to quantify cyclic behavior (C).

At 1Hz the Nyquist floor is 0.5Hz (period >= 2s) -- this can resolve the
known ~21s busy/dip cycle from ai_load.sh, but cannot resolve anything
faster than 2s (e.g. the sub-second shape of a PL2 turbo spike).
"""

import argparse

import numpy as np

from core import telemetry as common


def main():
    p = argparse.ArgumentParser(description="Periodogram of a telemetry column.")
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--column", default="power_watts")
    p.add_argument("--top", type=int, default=5, help="how many dominant periods to report")
    args = p.parse_args()

    df = common.load_telemetry(args.start, args.end)
    x = df[args.column].dropna().to_numpy()
    x = x - x.mean()

    fs = 1.0  # Hz, fixed by the 1s sample interval
    n = len(x)
    freqs = np.fft.rfftfreq(n, d=1 / fs)
    power = np.abs(np.fft.rfft(x)) ** 2

    # Skip the DC bin (freqs[0] == 0) and anything below 2s period (Nyquist floor).
    nonzero = freqs > 0
    freqs, power = freqs[nonzero], power[nonzero]
    valid = (1 / freqs) >= 2
    freqs, power = freqs[valid], power[valid]

    order = np.argsort(power)[::-1][: args.top]
    print(f"Column: {args.column}  n={n}s  Nyquist floor: 2s period")
    print("Dominant periods (resolvable range only):")
    for i in order:
        print(f"  period={1 / freqs[i]:.1f}s  power={power[i]:.3e}")


if __name__ == "__main__":
    main()
