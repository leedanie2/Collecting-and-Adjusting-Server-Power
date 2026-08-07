#!/usr/bin/env python3
"""Daemon stress-test: score spike_daemon_rf against ai_load.sh's KNOWN onsets.

ai_load.sh drives a repeatable HPL burst cycle (busy plateau -> brief checkpoint
dip -> resume). Because the cycle timing is *scripted*, the busy-phase onset
times are clean ground truth -- far better than the derivative-threshold label,
which only fires once power has already moved. This harness aligns the RF
daemon's spike flags against those derived onsets and reports:

  - detection rate per burst (did any flag land near each onset?)
  - lead / lag of the earliest flag per detected burst (+ = early warning,
    - = nowcast/late -- the honest CP4 signal for these cold step-resumes)
  - false-alarm rate during the quiet dip/idle seconds (regime flagging during
    the busy plateau is the detector working, NOT a false alarm -- reported
    separately as busy_flag_frac).

Ground truth is DERIVED from ai_load.sh's cycle params (BUSY_S, DIP_S, STAGGER_S,
-np N) -- no hardcoded period. See cycle_period_s().

Offline by design. --selfcheck proves the alignment/scoring math with a synthetic
flag stream and asserts (e.g.) a flag 1 s before onset scores as a 1 s lead. The
live run is GATED: it needs a real ai_load.sh execution on mycroft first, then
  stress_test_daemon.py --start <iso> --end <iso>   (or --autodetect)

Mirrors hpl_validate.py's model wiring (train pre-run, predict over run); the new
part here is the scripted-onset ground truth and per-burst alignment.
"""
import argparse
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from core import telemetry as common
from core import features as la
from detectors.random_forest import live_detector as rf

SCRIPT = Path(__file__).resolve().parents[3] / "Linpack Artifacts" / "ai_load.sh"
LEAD_MAX_S = rf.HORIZON_S   # credit a flag up to HORIZON_S before onset as early warning
MATCH_LAG_S = 3             # nowcast grace: a flag this soon after onset still "caught" it


# --- ground truth from the load script -------------------------------------

def parse_cycle_params(path=SCRIPT):
    """Read BUSY_S / DIP_S / STAGGER_S and the MPI rank count from ai_load.sh."""
    txt = Path(path).read_text()

    def num(name):
        m = re.search(rf"^{name}=([0-9.]+)", txt, re.M)
        if not m:
            raise ValueError(f"{name} not found in {path}")
        return float(m.group(1))

    m = re.search(r"-np\s+(\d+)", txt)
    return {
        "busy_s": num("BUSY_S"),
        "dip_s": num("DIP_S"),
        "stagger_s": num("STAGGER_S"),
        "nranks": int(m.group(1)) if m else 1,
    }


def cycle_period_s(p):
    """One full cycle = busy plateau + stop-stagger ramp-down + dip + cont-stagger
    ramp-up. The two stagger loops each touch every rank, so they add
    2*stagger_s*nranks (~0.77 s at 128 ranks) -- small per cycle but it accumulates
    over a long run, so derive it rather than rounding to busy+dip."""
    return p["busy_s"] + p["dip_s"] + 2 * p["stagger_s"] * p["nranks"]


def burst_onsets(t0, end, p):
    """Scripted busy-phase onsets in [t0, end): the first is xhpl launch (t0),
    each later one is a kill -CONT resume, spaced one cycle apart."""
    period = cycle_period_s(p)
    t0, end = pd.Timestamp(t0), pd.Timestamp(end)
    out, k = [], 0
    while True:
        t = t0 + pd.Timedelta(seconds=k * period)
        if t >= end:
            break
        out.append(t)
        k += 1
    return pd.DatetimeIndex(out)


def region_labels(index, t0, end, p):
    """Classify each timestamp into the scripted phase:
      idle  - before the run (t < t0)
      busy  - the high-power plateau [onset, onset+busy_s)
      dip   - the checkpoint dip (ranks stopped) up to the next onset
      edge  - sub-second transition / at-or-after run end (ignored)
    """
    period = cycle_period_s(p)
    stop_ramp = p["stagger_s"] * p["nranks"]
    idx = pd.DatetimeIndex(index)
    t0, end = pd.Timestamp(t0), pd.Timestamp(end)

    secs = (idx - t0).total_seconds()
    phase = np.mod(secs, period)
    in_run = (idx >= t0) & (idx < end)

    lab = np.full(len(idx), "edge", dtype=object)
    lab[idx < t0] = "idle"
    lab[in_run & (phase < p["busy_s"])] = "busy"
    lab[in_run & (phase >= p["busy_s"] + stop_ramp)] = "dip"
    return pd.Series(lab, index=idx)


# --- alignment / scoring (pure; this is what --selfcheck exercises) ---------

def align(onsets, flag_times, lead_max_s=LEAD_MAX_S, match_lag_s=MATCH_LAG_S):
    """Per-burst detection + lead/lag from a set of flag timestamps.

    A burst is detected if any flag lands in [onset - lead_max_s, onset + match_lag_s].
    The earliest such flag gives the lead the operator would have had: lead_s > 0
    means early warning, lead_s < 0 means the flag fired after onset (nowcast/late).
    """
    onsets = pd.DatetimeIndex(onsets).sort_values()
    flags = pd.DatetimeIndex(flag_times).sort_values()
    lead, lag = pd.Timedelta(seconds=lead_max_s), pd.Timedelta(seconds=match_lag_s)

    leads, detected = [], 0
    for to in onsets:
        win = flags[(flags >= to - lead) & (flags <= to + lag)]
        if len(win):
            detected += 1
            leads.append((to - win[0]).total_seconds())  # earliest flag = best lead

    n = len(onsets)
    n_early = sum(1 for l in leads if l > 0)
    return {
        "n_bursts": n,
        "n_detected": detected,
        "detection_rate": detected / n if n else float("nan"),
        "median_lead_s": float(np.median(leads)) if leads else float("nan"),
        "frac_early": (n_early / len(leads)) if leads else float("nan"),
        "leads": leads,
    }


def false_alarm(region, flag_mask):
    """Flag rate over the genuinely quiet seconds (dip + idle). Flags on the busy
    plateau are regime detection, not false alarms, so they are excluded here."""
    region = pd.Series(region)
    flag_mask = pd.Series(flag_mask).reindex(region.index).fillna(False)
    quiet = region.isin(["dip", "idle"])
    n_quiet = int(quiet.sum())
    fa = int((quiet & flag_mask).sum())
    return {
        "quiet_seconds": n_quiet,
        "false_alarms": fa,
        "false_alarm_rate": fa / n_quiet if n_quiet else float("nan"),
    }


# --- live (gated) run -------------------------------------------------------

def autodetect_window(pad_h=1):
    """Most recent sustained busy span = the HPL run. Read-only (no load)."""
    recent = common.query_window(start=f"-{pad_h + 2}h")
    runs = common.segment_runs(recent, usage_threshold=15.0, min_run_s=30)
    return max(runs, key=lambda r: (r[1] - r[0])) if runs else None


def stress_test(start, end, script=SCRIPT, lead_max_s=LEAD_MAX_S, match_lag_s=MATCH_LAG_S):
    """Score the RF daemon against scripted onsets over a real ai_load.sh run."""
    p = parse_cycle_params(script)
    start, end = pd.Timestamp(start), pd.Timestamp(end)

    train_start = (start - pd.Timedelta(hours=rf.TRAIN_H)).isoformat()
    df = common.query_window(start=train_start, stop=end.isoformat())
    df = df.dropna(subset=["power_watts", "usage_percent"])

    fit = rf.train(df[df.index < start])
    if fit is None:
        print("not enough pre-run history to train")
        return None
    model, cols, thr = fit

    X = la.feature_frame(df, lane_a=False, kf=True, upstream=rf.UPSTREAM_ENABLED).dropna()
    proba = pd.Series(model.predict_proba(X[cols].to_numpy())[:, 1], index=X.index)
    flag_mask = proba > thr
    region = region_labels(proba.index, start, end, p)

    in_run = (proba.index >= start) & (proba.index < end)
    flag_times = proba.index[flag_mask & in_run]
    onsets = burst_onsets(start, end, p)

    det = align(onsets, flag_times, lead_max_s, match_lag_s)
    fa = false_alarm(region, flag_mask)
    busy = region == "busy"
    busy_flag_frac = float((flag_mask & busy).sum() / busy.sum()) if busy.any() else float("nan")

    print(f"HPL run window : {start} -> {end}")
    print(f"cycle period   : {cycle_period_s(p):.3f} s "
          f"(busy {p['busy_s']:.0f}s + dip {p['dip_s']:.0f}s + stagger {2*p['stagger_s']*p['nranks']:.3f}s)")
    print(f"threshold      : {thr:.3f}\n")

    print("=== Per-burst onset detection (scripted ground truth) ===")
    print(f"  bursts (onsets)       : {det['n_bursts']}")
    print(f"  detected (>=1 flag)   : {det['n_detected']}  ({det['detection_rate']:.1%})")
    print(f"  median lead           : {det['median_lead_s']:+.1f} s  (+ early, - nowcast/late)")
    print(f"  fraction with lead>0  : {det['frac_early']:.1%}  (genuine early warning)")

    print("\n=== False alarms during quiet (dip + idle) seconds ===")
    print(f"  quiet seconds         : {fa['quiet_seconds']}")
    print(f"  false alarms          : {fa['false_alarms']}  ({fa['false_alarm_rate']:.1%})")

    print("\n=== Regime sanity (flagging the busy plateau is expected, not a FA) ===")
    print(f"  busy-plateau flagged  : {busy_flag_frac:.1%}")
    print("\n(Read: high busy_flag + low quiet false-alarm = regime detector works; "
          "median lead <= 0 = cold step-resumes nowcasted, not foreseen, per CP4.)")
    return {"detection": det, "false_alarm": fa, "busy_flag_frac": busy_flag_frac}


# --- selfcheck (offline, synthetic) ----------------------------------------

def selfcheck():
    """Drive the alignment/scoring math with synthetic flags + known onsets."""
    # 1. params come from the real script, period derived (not hardcoded)
    p = parse_cycle_params()
    assert (p["busy_s"], p["dip_s"], p["nranks"]) == (18.0, 3.0, 128), p
    period = cycle_period_s(p)
    assert abs(period - (18 + 3 + 2 * 0.003 * 128)) < 1e-9, period

    # 2. onsets are one period apart; first onset == run start
    t0 = pd.Timestamp("2026-06-30T00:00:00Z")
    onsets = burst_onsets(t0, t0 + pd.Timedelta(seconds=period * 3 + 1), p)
    assert len(onsets) == 4, len(onsets)
    assert onsets[0] == t0
    gaps = (onsets[1:] - onsets[:-1]) / pd.Timedelta(seconds=1)
    assert np.allclose(gaps, period), gaps

    # 3. a flag exactly 1 s before each onset -> full detection, +1 s lead
    early = onsets - pd.Timedelta(seconds=1)
    d = align(onsets, early)
    assert d["n_detected"] == len(onsets) and d["detection_rate"] == 1.0, d
    assert abs(d["median_lead_s"] - 1.0) < 1e-9, d
    assert d["frac_early"] == 1.0, d

    # 4. a flag 2 s AFTER onset (within match grace) -> detected, lead = -2 s
    d2 = align(onsets[:1], pd.DatetimeIndex([onsets[0] + pd.Timedelta(seconds=2)]))
    assert d2["n_detected"] == 1 and abs(d2["median_lead_s"] + 2.0) < 1e-9, d2
    assert d2["frac_early"] == 0.0, d2

    # 5. a flag far from any onset (mid-plateau) -> not detected
    d3 = align(onsets[:1], pd.DatetimeIndex([onsets[0] + pd.Timedelta(seconds=10)]))
    assert d3["n_detected"] == 0, d3

    # 6. region labels: pre-run is idle; busy + dip both appear
    end = t0 + pd.Timedelta(seconds=period * 2)
    idx = pd.date_range(t0 - pd.Timedelta(seconds=10), end, freq="1s")
    region = region_labels(idx, t0, end, p)
    assert (region == "idle").sum() == 10, (region == "idle").sum()
    assert (region == "busy").any() and (region == "dip").any(), region.value_counts().to_dict()

    # 7. false-alarm rate is computed over dip+idle only
    fa_all = false_alarm(region, region.isin(["dip", "idle"]))
    assert fa_all["false_alarm_rate"] == 1.0, fa_all
    fa_none = false_alarm(region, pd.Series(False, index=idx))
    assert fa_none["false_alarm_rate"] == 0.0, fa_none
    # flagging only the busy plateau must NOT count as a false alarm
    fa_busy = false_alarm(region, region == "busy")
    assert fa_busy["false_alarms"] == 0, fa_busy

    print(f"selfcheck OK: period={period:.3f}s, "
          f"1s-early flag -> {d['median_lead_s']:+.1f}s lead @ {d['detection_rate']:.0%} detection, "
          f"quiet-only FA rate {fa_all['false_alarm_rate']:.0%}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selfcheck", action="store_true", help="offline synthetic check")
    ap.add_argument("--start", help="ISO start of the real HPL run")
    ap.add_argument("--end", help="ISO end of the real HPL run")
    ap.add_argument("--autodetect", action="store_true",
                    help="find the most recent sustained run (read-only InfluxDB query)")
    args = ap.parse_args()

    if args.selfcheck:
        selfcheck()
        return
    if args.start and args.end:
        stress_test(args.start, args.end)
        return
    if args.autodetect:
        win = autodetect_window()
        if win is None:
            print("no sustained run found; pass --start/--end explicitly")
            sys.exit(1)
        print(f"auto-detected run: {win[0]} -> {win[1]}\n")
        stress_test(win[0].isoformat(), win[1].isoformat())
        return
    ap.error("need --selfcheck, --start/--end, or --autodetect "
             "(the live paths require a real ai_load.sh run on mycroft first)")


if __name__ == "__main__":
    main()
