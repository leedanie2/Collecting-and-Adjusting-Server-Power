#!/usr/bin/env python3
"""Validate spike_daemon_rf against a controlled HPL load run (ai_load.sh).

Run AFTER ai_load.sh has executed on mycroft. Pulls the telemetry, mirrors the
daemon exactly (train RF on the 12 h BEFORE the run, predict P(spike) across the
run), and scores against two honest yardsticks:

  1. REGIME DETECTION (the detector's real claim): mean P(spike) during the busy
     HPL run vs. an idle baseline before it. Should be clearly higher.
  2. PER-EVENT PREDICTION (the CP4 ceiling): ai_load.sh's kill -STOP/-CONT makes
     each resume a hard step with no power precursor -- a cold onset. Recall/lead
     on these is expected to be poor; that CONFIRMS CP4, it isn't a daemon bug.

Usage:
  hpl_validate.py                 # auto-detect the most recent sustained run
  hpl_validate.py --start <iso> --end <iso>
"""
import argparse
import sys

import numpy as np
import pandas as pd

from core import telemetry as common
from core import features as la
from detectors.random_forest import live_detector as rf
from core.spike_labels import DEFAULT_DERIV_THRESHOLD_W as DTHR

H = rf.HORIZON_S


def autodetect_window(pad_h=1):
    """Most recent sustained busy span = the HPL run. Looks back pad_h+1 hours."""
    recent = common.query_window(start=f"-{pad_h + 2}h")
    runs = common.segment_runs(recent, usage_threshold=15.0, min_run_s=30)
    if not runs:
        return None
    return max(runs, key=lambda r: (r[1] - r[0]))  # longest run


def validate(start, end):
    train_start = (pd.Timestamp(start) - pd.Timedelta(hours=rf.TRAIN_H)).isoformat()
    df = common.query_window(start=train_start, stop=pd.Timestamp(end).isoformat())
    df = df.dropna(subset=["power_watts", "usage_percent"])

    run = (df.index >= pd.Timestamp(start)) & (df.index < pd.Timestamp(end))
    pre = df.index < pd.Timestamp(start)
    fit = rf.train(df[pre])
    if fit is None:
        print("not enough pre-run history to train")
        return
    model, cols, thr = fit

    X = la.feature_frame(df, lane_a=False, kf=True).dropna()
    proba = pd.Series(model.predict_proba(X[cols].to_numpy())[:, 1], index=X.index)

    run_p = proba[(proba.index >= pd.Timestamp(start)) & (proba.index < pd.Timestamp(end))]
    idle_p = proba[proba.index < pd.Timestamp(start)].tail(1800)  # last 30 min before run

    print(f"HPL window: {start} -> {end}  ({run.sum()} s)")
    print(f"trained on {pre.sum()} pre-run rows\n")

    print("=== 1. Regime detection (the detector's real claim) ===")
    print(f"  calibrated threshold         : {thr:.3f}")
    print(f"  mean P(spike) during HPL run : {run_p.mean():.3f}")
    print(f"  mean P(spike) idle (pre-run) : {idle_p.mean():.3f}")
    print(f"  flagged fraction during run  : {(run_p > thr).mean():.3f}")
    print(f"  flagged fraction idle        : {(idle_p > thr).mean():.3f}")

    # 2. controlled events = resume spikes (large +dP/dt) inside the run
    dP = df["power_watts"].diff()
    events = df.index[(dP > DTHR) & run]
    print(f"\n=== 2. Per-event prediction (expected weak -- cold onsets) ===")
    print(f"  resume/step spikes detected  : {len(events)}")
    warned, leads = 0, []
    flagged_times = run_p.index[run_p > thr]
    for te in events:
        win = flagged_times[(flagged_times >= te - pd.Timedelta(seconds=H)) &
                            (flagged_times < te)]
        if len(win):
            warned += 1
            leads.append((te - win[0]).total_seconds())
    if events.size:
        print(f"  warned before event          : {warned} ({warned/len(events):.1%})")
        if leads:
            print(f"  lead median                  : {np.median(leads):.1f} s")
    print("\n(Interpretation: high regime contrast = continuation detector works; "
          "low per-event recall = cold-onset ceiling from CP4, as expected.)")


def selfcheck():
    """Offline: train on the first 12h of cache, confirm clean regime contrast
    (busy seconds flag far more than idle). No live connection or HPL run."""
    cache = la.load()
    split = cache.index[0] + pd.Timedelta(hours=rf.TRAIN_H)
    model, cols, thr = rf.train(cache[cache.index < split])
    te = cache[cache.index >= split]
    X = la.feature_frame(te, kf=True).dropna()
    proba = pd.Series(model.predict_proba(X[cols].to_numpy())[:, 1], index=X.index)
    u = te["usage_percent"].reindex(proba.index)
    idle_flag = (proba[u < 5] > thr).mean()
    busy_flag = (proba[u > 50] > thr).mean()
    assert busy_flag > 0.5 > idle_flag, f"no regime contrast: idle={idle_flag}, busy={busy_flag}"
    print(f"selfcheck OK: idle flag {idle_flag:.3f} << busy flag {busy_flag:.3f} (thr {thr:.3f})")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--start")
    ap.add_argument("--end")
    args = ap.parse_args()

    if args.selfcheck:
        selfcheck()
        return

    if args.start and args.end:
        validate(args.start, args.end)
        return
    win = autodetect_window()
    if win is None:
        print("no sustained run found; pass --start/--end explicitly")
        sys.exit(1)
    print(f"auto-detected run: {win[0]} -> {win[1]}\n")
    validate(win[0].isoformat(), win[1].isoformat())


if __name__ == "__main__":
    main()
