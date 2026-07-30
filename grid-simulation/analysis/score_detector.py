#!/usr/bin/env python3
"""Score the usagegov detector/actuator against the workload's own ground truth.

Fills the four `detector` rows that have been `pending` in full_ranking.csv
since the start of the project: recall, latency, lead time, precision.

Ground truth is the aisim2 schedule, not a power-derived label. ai_sim_2.py
prints its own phase transitions -- `PREFILL START (req N) t=...` -- and those
are causally upstream of the power step, so they are the honest reference for a
lead-time claim. hpl and step carry no
phase log, so only aisim2 is scored.

Alerts are the governor's `events.jsonl`, i.e. what the actuator actually DID:
    preburn dir=+1  -> ballast ramped ahead of a predicted rise
    preburn dir=-1  -> ballast ramped ahead of a predicted fall
    engage          -> RAPL ceiling took hold

NOT the detector's own `detector.jsonl`: that file carries no timestamp field
(usage_edge writes usage/risk/edge/direction only), so its samples cannot be
placed on a wall clock. Scoring actuation is the stricter reading anyway -- a
detector edge that never moved the actuator did nothing for the grid.

Timing. events.jsonl stamps UTC to the millisecond. The aisim2 log stamps local
wall clock to the SECOND, plus workload-relative t to the millisecond. So every
onset within a run shares one anchor uncertainty of +/-0.5 s; relative spacing is
exact. Lead times below ~1 s should not be read as precise.

    python3 analysis/score_detector.py
    python3 analysis/score_detector.py --selfcheck
"""
import argparse
import csv
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = [1, 2, 3, 4]
ARM = "usagegov"
WORKLOAD = "aisim2"
OUT = ROOT / "data/summary/detector_scores.csv"

# match window around each ground-truth transition
LEAD_S = 5.0
LAG_S = 5.0
MERGE_GAP_S = 2.0


LOG_ANCHOR_RE = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\]\s+(\w+)\s+(START|STOP)"
                           r".*?t=\s*([\d.]+)\s*s")


def parse_log(path):
    """-> (anchor_utc for workload t=0, [(kind, direction, t_rel), ...]).

    kind is REST/PREFILL, direction +1 when the phase starting raises power
    (PREFILL START) and -1 when it drops it (PREFILL STOP).
    """
    anchor = None
    events = []
    for line in path.read_text(errors="replace").splitlines():
        m = LOG_ANCHOR_RE.match(line.strip())
        if not m:
            continue
        hh, mm, ss, kind, edge, t_rel = m.groups()
        t_rel = float(t_rel)
        if anchor is None:
            # first stamped line pins wall clock to workload time.
            # local -> UTC via the machine's own offset at that date.
            anchor = (int(hh), int(mm), int(ss), t_rel)
        if kind == "PREFILL":
            events.append((kind, +1 if edge == "START" else -1, t_rel))
    return anchor, events


def anchor_to_utc(anchor, ref_utc, to_utc_h):
    """Workload t=0 as a UTC datetime, from the log's local HH:MM:SS.

    to_utc_h is added to the local stamp (EDT -> UTC is +4). ref_utc is any
    event from the same run; it supplies the date and resolves the case where
    local and UTC fall on different calendar days.
    """
    hh, mm, ss, t_rel = anchor
    midnight = ref_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    stamp = midnight + timedelta(hours=hh + to_utc_h, minutes=mm, seconds=ss)
    # a run is minutes long, so the anchor is within hours of any of its events
    while stamp - ref_utc > timedelta(hours=12):
        stamp -= timedelta(days=1)
    while ref_utc - stamp > timedelta(hours=12):
        stamp += timedelta(days=1)
    return stamp - timedelta(seconds=t_rel)


def read_events(path):
    """-> list of (utc_datetime, event_name, direction_or_None)."""
    out = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "ts" not in d:
            continue
        out.append((datetime.fromisoformat(d["ts"]), d.get("event"), d.get("dir")))
    return sorted(out, key=lambda r: r[0])


def episodes(times, merge_gap_s=MERGE_GAP_S):
    """Collapse alerts closer than merge_gap_s into one episode."""
    if not times:
        return []
    eps = [[times[0], times[0]]]
    for t in times[1:]:
        if (t - eps[-1][1]).total_seconds() <= merge_gap_s:
            eps[-1][1] = t
        else:
            eps.append([t, t])
    return [tuple(e) for e in eps]


def score(truth, alerts, lead_s=LEAD_S, lag_s=LAG_S):
    """truth/alerts are lists of datetimes. Mirrors evaluation.event_alert_score,
    which needs a pandas DatetimeIndex we do not have here."""
    lats = []
    matched = 0
    for ev in truth:
        lo, hi = ev - timedelta(seconds=lead_s), ev + timedelta(seconds=lag_s)
        hit = [a for a in alerts if lo <= a <= hi]
        if not hit:
            continue
        matched += 1
        lats.append((hit[0] - ev).total_seconds())
    eps = episodes(alerts)
    matched_eps = 0
    for a0, a1 in eps:
        for ev in truth:
            if a1 >= ev - timedelta(seconds=lead_s) and a0 <= ev + timedelta(seconds=lag_s):
                matched_eps += 1
                break
    lats.sort()
    med = lats[len(lats) // 2] if lats else float("nan")
    return {
        "n_truth": len(truth),
        "n_detected": matched,
        "recall": matched / len(truth) if truth else float("nan"),
        "latency_median_s": med,
        "lead_time_median_s": -med if lats else float("nan"),
        "pre_onset_frac": sum(1 for x in lats if x < 0) / len(lats) if lats else float("nan"),
        "n_alert_episodes": len(eps),
        "precision": matched_eps / len(eps) if eps else float("nan"),
    }


def tz_offset_hours(sample_utc, local_hhmmss):
    """Hours to ADD to the log's local stamp to get UTC (EDT -> +4).

    Derived from an events.jsonl stamp in the same run rather than hardcoded,
    so a DST change or a move off EDT does not silently skew every lead time.
    """
    hh, mm, ss = local_hhmmss
    local_s = hh * 3600 + mm * 60 + ss
    utc_s = sample_utc.hour * 3600 + sample_utc.minute * 60 + sample_utc.second
    return round((utc_s - local_s) / 3600.0) % 24


def score_run(run):
    d = ROOT / f"data/detector/run{run}"
    log = d / f"{WORKLOAD}_{ARM}.log"
    evf = d / f"{WORKLOAD}_{ARM}.events.jsonl"
    if not log.exists() or not evf.exists():
        return None
    anchor, phases = parse_log(log)
    events = read_events(evf)
    if anchor is None or not events:
        return None
    off = tz_offset_hours(events[0][0], anchor[:3])
    t0 = anchor_to_utc(anchor, events[0][0], off)
    truth_up = [t0 + timedelta(seconds=t) for _k, d_, t in phases if d_ > 0]
    truth_dn = [t0 + timedelta(seconds=t) for _k, d_, t in phases if d_ < 0]
    pre_up = [t for t, e, dr in events if e == "preburn" and dr == 1]
    pre_dn = [t for t, e, dr in events if e == "preburn" and dr == -1]
    rows = []
    for label, truth, alerts in [("rise", truth_up, pre_up),
                                 ("drop", truth_dn, pre_dn)]:
        r = {"run": run, "workload": WORKLOAD, "arm": ARM, "transition": label}
        r.update(score(truth, alerts))
        rows.append(r)
    return rows


def selfcheck():
    fail = 0
    base = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)
    truth = [base + timedelta(seconds=s) for s in (10, 40, 70)]
    # alert 2 s BEFORE each onset -> perfect recall, lead time +2 s
    alerts = [t - timedelta(seconds=2) for t in truth]
    s = score(truth, alerts)
    if s["recall"] != 1.0 or abs(s["lead_time_median_s"] - 2.0) > 1e-9:
        fail += 1
        print(f"FAIL lead -> {s}", file=sys.stderr)
    if s["pre_onset_frac"] != 1.0:
        fail += 1
        print(f"FAIL pre_onset -> {s}", file=sys.stderr)
    # an alert 30 s away matches nothing and is a false episode
    s2 = score(truth, [base + timedelta(seconds=200)])
    if s2["recall"] != 0.0 or s2["precision"] != 0.0:
        fail += 1
        print(f"FAIL far alert -> {s2}", file=sys.stderr)
    # alerts inside merge_gap collapse to one episode
    burst = [base + timedelta(seconds=10 + 0.5 * i) for i in range(5)]
    if len(episodes(burst)) != 1:
        fail += 1
        print("FAIL episode merge", file=sys.stderr)
    # tz: 08:08 local reads 12:08 UTC -> add 4 h to local
    if tz_offset_hours(base.replace(hour=12, minute=8), (8, 8, 14)) != 4:
        fail += 1
        print("FAIL tz offset", file=sys.stderr)
    # and the anchor must land on the UTC stamp, not 8 h away from it
    ref = datetime(2026, 7, 28, 12, 8, 14, tzinfo=timezone.utc)
    got = anchor_to_utc((8, 8, 14, 0.0), ref, 4)
    if got != ref:
        fail += 1
        print(f"FAIL anchor -> {got}", file=sys.stderr)
    print("selfcheck: OK" if not fail else f"selfcheck: {fail} FAILED")
    return fail


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()
    if args.selfcheck:
        sys.exit(selfcheck())

    rows = []
    for r in RUNS:
        got = score_run(r)
        if got is None:
            print(f"run{r}: no {WORKLOAD}_{ARM} artifacts, skipped", file=sys.stderr)
            continue
        rows.extend(got)
    if not rows:
        sys.exit("no runs scored")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    hdr = f"{'run':>4} {'transition':>10} {'recall':>8} {'lead_s':>8} {'pre%':>6} {'prec':>6} {'eps':>5}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r['run']:>4} {r['transition']:>10} "
              f"{r['n_detected']}/{r['n_truth']:<6} "
              f"{r['lead_time_median_s']:>8.2f} {100*r['pre_onset_frac']:>5.0f}% "
              f"{r['precision']:>6.2f} {r['n_alert_episodes']:>5}")
    print(f"\nwrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
