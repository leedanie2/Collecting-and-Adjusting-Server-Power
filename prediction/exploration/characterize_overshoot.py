#!/usr/bin/env python3
"""Characterize the PL2->PL1 turbo overshoot at each load onset.

The high-freq track's deliverable, after forecasting was ruled out
(see onset_leading_structure.py): the onset is reactive, so the honest
product is *describing* the overshoot, not predicting it. Each load onset
shows Intel RAPL's PL2 (short-duration turbo) -> PL1 (sustained) clamp:
power surges to a turbo level, holds for the PL2 time window (Tau), then
steps down to the sustained limit.

Per onset this extracts: PL2 (turbo) level, PL1 (sustained) level, the
overshoot (abs + %), and the PL2 window duration in seconds.

Run:  .venv/bin/python characterize_overshoot.py [capture.csv]   (default rapl_power.csv)
Self: .venv/bin/python characterize_overshoot.py --selfcheck

On rapl_power.csv (62 s ai_load.sh cycle, 10 Hz, light load): PL2 window
~1.6 s, overshoot +6.2% (217 W -> 204 W) -- consistent across onsets and
matching the docs' 434->410 W (+6%) at higher load. The 1.6 s is the
hardware-configured PL2 Tau.
"""
import sys
import numpy as np
import pandas as pd

from core.onsets import DEFAULT_CAPTURE, load, find_onsets

POST_S = 3.5  # how far past the jump to look (must exceed the PL2 window)


def characterize_onset(z, i, busy_std, dt=0.1):
    """Return dict for the onset whose jump is at i->i+1, or None if too short.
    PL1 = settled median 2-3.5 s out; turbo band = samples above PL1 + 2*noise;
    PL2 window = time to the last above-band sample before the clamp."""
    n_post = int(POST_S / dt)
    seg = z[i + 1:i + 1 + n_post]
    if len(seg) < int(3.0 / dt):
        return None
    settled_from = int(2.0 / dt)
    pl1 = float(np.median(seg[settled_from:]))
    band_hi = pl1 + 2 * busy_std
    turbo_band = seg[seg > band_hi]
    pl2 = float(np.median(turbo_band)) if len(turbo_band) else pl1
    # PL2 window: last above-band sample within the turbo phase (first 2.5 s)
    above = np.where(seg[:int(2.5 / dt)] > band_hi)[0]
    window_s = float((above.max() + 1) * dt) if len(above) else 0.0
    return {"pl2": pl2, "pl1": pl1, "overshoot_w": pl2 - pl1,
            "overshoot_pct": (pl2 - pl1) / pl1 * 100, "window_s": window_s}


def report(path):
    t, z = load(path)
    dt = float(np.median(np.diff(t)))
    busy_std = float(z[z > np.percentile(z, 75)].std())  # plateau noise
    onsets = find_onsets(z)
    print(f"{path}: {len(z)} samples, dt~{dt*1000:.0f} ms, "
          f"plateau noise std ~{busy_std:.1f} W, {len(onsets)} onsets\n")
    print(f"{'onset':>7} {'PL2(turbo)':>11} {'PL1(sustain)':>13} "
          f"{'overshoot':>16} {'PL2 window':>11}")
    rows = []
    for i in onsets:
        c = characterize_onset(z, i, busy_std, dt)
        if c is None:
            continue
        rows.append(c)
        print(f"{t[i]:6.1f}s {c['pl2']:10.1f}W {c['pl1']:12.1f}W "
              f"{c['overshoot_w']:+8.1f}W ({c['overshoot_pct']:+4.1f}%) "
              f"{c['window_s']:9.1f}s")
    if rows:
        mw = np.mean([r["window_s"] for r in rows])
        mo = np.mean([r["overshoot_pct"] for r in rows])
        print(f"\nmean PL2 window = {mw:.2f}s (Intel Tau)  "
              f"mean overshoot = {mo:+.1f}%")
        print("Reactive turbo clamp: power surges to PL2, holds ~Tau, steps to PL1.")
    return rows


def _selfcheck():
    # synthetic onset: dip -> turbo plateau (1.5s) -> step down to sustained
    dt = 0.1
    dip = np.full(20, 120.0)
    turbo = np.full(15, 217.0) + np.random.default_rng(0).normal(0, 1, 15)  # 1.5s
    sustain = np.full(25, 204.0)
    z = np.r_[dip, turbo, sustain]
    i = 19  # last dip sample; jump at 19->20
    c = characterize_onset(z, i, busy_std=2.0, dt=dt)
    assert c is not None
    assert abs(c["pl1"] - 204) < 2, f"PL1 off: {c['pl1']}"
    assert abs(c["pl2"] - 217) < 2, f"PL2 off: {c['pl2']}"
    assert 1.3 <= c["window_s"] <= 1.7, f"PL2 window off: {c['window_s']}"
    assert c["overshoot_w"] > 10, f"overshoot off: {c['overshoot_w']}"
    print(f"selfcheck OK: PL2={c['pl2']:.0f}W PL1={c['pl1']:.0f}W "
          f"window={c['window_s']:.1f}s overshoot={c['overshoot_pct']:+.1f}%")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        args = [a for a in sys.argv[1:] if not a.startswith("-")]
        report(args[0] if args else DEFAULT_CAPTURE)
