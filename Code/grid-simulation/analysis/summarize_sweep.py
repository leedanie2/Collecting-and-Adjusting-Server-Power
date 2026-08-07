#!/usr/bin/env python3
"""Regenerate the fleet-size sweep tables in data/summary/sweep/.

The sweep was originally run by hand, so nothing in the repo could rebuild its
CSVs -- adding a 5th arm left them silently stale at three arms while every
other table had four. This script is that missing step.

Reads   results/<workload>_<arm>_r<run>[_N<n>]_worst/metrics.json
Writes  data/summary/sweep/scoreboard_N<n>.csv   grid risk, % vs baseline
        data/summary/sweep/cost.csv              runtime/energy, N-independent
        data/summary/sweep/grid_vs_count.csv     absolute load vs N

No N suffix means N=10,000 (the paper's fleet size), which is how
run_simulation.m names an un-overridden run.

Percentages are per-workload vs that workload's own baseline, averaged over the
three workloads within a run, then mean +/- SD across the four runs -- identical
to summarize_n4.py, just over more metrics.

Cost (runtime, energy) comes from the raw traces and does not depend on N, so it
is written once rather than copied into each scoreboard.

    python3 analysis/summarize_sweep.py
    python3 analysis/summarize_sweep.py --selfcheck
"""
import argparse
import csv
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data/summary/sweep"
RUNS = [1, 2, 3, 4]
WORKLOADS = ["hpl", "aisim2", "step"]
MITIG = ["rampc", "powersmoother", "usagegov"]
NS = [1000, 5000, 10000, 20000]

# (csv column, metrics.json path, magnitude?).
#
# magnitude=True compares |new| vs |base|. freq_nadir_deviation_hz is stored as
# min(delta_f), i.e. a NEGATIVE Hz offset below nominal, so a signed percentage
# would report a deeper nadir as a decrease and put this column in the opposite
# convention to rocof beside it. Every other metric here is non-negative, so the
# two forms agree. Convention for both: POSITIVE = worse.
METRICS = [
    ("cv",           ("risk", "cv"), False),
    ("peak_to_mean", ("risk", "peak_to_mean"), False),
    ("rrei",         ("risk", "rrei_exceedances_per_year"), False),
    ("nrs",          ("risk", "nrs_MW_per_year"), False),
    ("rocof",        ("frequency", "rocof_max_hz_per_s"), False),
    ("nadir_dev",    ("frequency", "freq_nadir_deviation_hz"), True),
]


def run_dir(workload, arm, run, n):
    suffix = f"_r{run}" + (f"_N{n}" if n != 10000 else "") + "_worst"
    return ROOT / "results" / f"{workload}_{arm}{suffix}"


def load(workload, arm, run, n):
    p = run_dir(workload, arm, run, n) / "metrics.json"
    if not p.exists():
        return None
    return json.load(open(p))


def dig(m, path):
    for k in path:
        m = m[k]
    return float(m)


def pct(new, base):
    """% change, guarding the divide. A zero baseline means the metric is
    degenerate at this scale, not that the change is infinite."""
    if base == 0:
        return None
    return 100.0 * (new - base) / abs(base)


def scoreboard(n):
    """-> {arm: {col: mean, col_sd: sd}} for one fleet size."""
    out = {}
    for arm in MITIG:
        per_run = {c: [] for c, _, _ in METRICS}
        for r in RUNS:
            per_wl = {c: [] for c, _, _ in METRICS}
            for w in WORKLOADS:
                base, mit = load(w, "baseline", r, n), load(w, arm, r, n)
                if base is None or mit is None:
                    continue
                for col, path, mag in METRICS:
                    a, b = dig(mit, path), dig(base, path)
                    if mag:
                        a, b = abs(a), abs(b)
                    v = pct(a, b)
                    if v is not None:
                        per_wl[col].append(v)
            for col in per_wl:
                if per_wl[col]:
                    per_run[col].append(statistics.mean(per_wl[col]))
        if not any(per_run.values()):
            continue                      # arm not simulated at this N
        row = {}
        for col, _, _ in METRICS:
            vals = per_run[col]
            row[col] = round(statistics.mean(vals), 1) if vals else ""
            row[col + "_sd"] = (round(statistics.stdev(vals), 1)
                                if len(vals) > 1 else 0.0)
        out[arm] = row
    return out


def trace_cost(workload, arm, run):
    """Runtime (s) and energy (J) from the FULL untrimmed trace -- ramp.c's
    ballast flanks are real cost, so they are not trimmed away here."""
    p = ROOT / f"data/runs/run{run}/{workload}_{arm}.csv"
    if not p.exists():
        return None
    t, w = [], []
    for row in csv.DictReader(open(p)):
        try:
            t.append(float(row["time_s"]))
            w.append(float(row["power_W"]))
        except (KeyError, ValueError):
            continue
    if len(t) < 2:
        return None
    e = sum((t[i] - t[i - 1]) * (w[i] + w[i - 1]) / 2 for i in range(1, len(t)))
    return t[-1] - t[0], e


def cost_table():
    out = {}
    for arm in MITIG:
        rt, en = [], []
        for r in RUNS:
            rr, ee = [], []
            for w in WORKLOADS:
                b, m = trace_cost(w, "baseline", r), trace_cost(w, arm, r)
                if b is None or m is None:
                    continue
                rr.append(pct(m[0], b[0]))
                ee.append(pct(m[1], b[1]))
            if rr:
                rt.append(statistics.mean(rr))
                en.append(statistics.mean(ee))
        if not rt:
            continue
        out[arm] = {
            "runtime": round(statistics.mean(rt), 1),
            "runtime_sd": round(statistics.stdev(rt), 1) if len(rt) > 1 else 0.0,
            "energy": round(statistics.mean(en), 1),
            "energy_sd": round(statistics.stdev(en), 1) if len(en) > 1 else 0.0,
        }
    return out


def grid_vs_count():
    """Absolute baseline grid response at each N -- the load-scaling check."""
    rows = []
    for n in NS:
        acc = {k: [] for k in ("mean_MW", "peak_MW", "min_v_pu", "nadir_hz",
                               "rocof_hz_s", "rrei_per_yr", "nrs_MW_yr", "cv")}
        for r in RUNS:
            for w in WORKLOADS:
                m = load(w, "baseline", r, n)
                if m is None:
                    continue
                acc["mean_MW"].append(dig(m, ("power", "mean_MW")))
                acc["peak_MW"].append(dig(m, ("power", "peak_MW")))
                acc["min_v_pu"].append(dig(m, ("voltage", "min_voltage_pu")))
                acc["nadir_hz"].append(dig(m, ("frequency", "freq_nadir_hz")))
                acc["rocof_hz_s"].append(dig(m, ("frequency", "rocof_max_hz_per_s")))
                acc["rrei_per_yr"].append(dig(m, ("risk", "rrei_exceedances_per_year")))
                acc["nrs_MW_yr"].append(dig(m, ("risk", "nrs_MW_per_year")))
                acc["cv"].append(dig(m, ("risk", "cv")))
        if not acc["mean_MW"]:
            continue
        rows.append({"N_servers": n,
                     **{k: round(statistics.mean(v), 4) for k, v in acc.items()}})
    return rows


def write_csv(path, fieldnames, rows):
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def selfcheck():
    fail = 0
    if pct(110.0, 100.0) != 10.0:
        fail += 1
        print("FAIL pct", file=sys.stderr)
    # a DEEPER nadir must read as worse (positive), matching rocof beside it.
    # -0.85 Hz -> -1.55 Hz is an 82% bigger deviation, not an 82% decrease.
    if round(pct(abs(-1.55), abs(-0.85)), 0) != 82.0:
        fail += 1
        print(f"FAIL nadir magnitude -> {pct(abs(-1.55), abs(-0.85))}", file=sys.stderr)
    if [m for m in METRICS if m[0] == "nadir_dev"][0][2] is not True:
        fail += 1
        print("FAIL nadir_dev must be a magnitude metric", file=sys.stderr)
    if pct(1.0, 0.0) is not None:
        fail += 1
        print("FAIL pct zero base must be None", file=sys.stderr)
    # N=10000 has no suffix, every other N does
    if run_dir("hpl", "rampc", 1, 10000).name != "hpl_rampc_r1_worst":
        fail += 1
        print("FAIL 10k naming", file=sys.stderr)
    if run_dir("hpl", "rampc", 1, 5000).name != "hpl_rampc_r1_N5000_worst":
        fail += 1
        print("FAIL N naming", file=sys.stderr)
    print("selfcheck: OK" if not fail else f"selfcheck: {fail} FAILED")
    return fail


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()
    if args.selfcheck:
        sys.exit(selfcheck())

    OUT.mkdir(parents=True, exist_ok=True)
    cols = ["smoother"] + [c for m, _, _ in METRICS for c in (m, m + "_sd")]
    for n in NS:
        sb = scoreboard(n)
        write_csv(OUT / f"scoreboard_N{n}.csv", cols,
                  [{"smoother": a, **r} for a, r in sb.items()])
        missing = [a for a in MITIG if a not in sb]
        note = f"  (missing: {', '.join(missing)})" if missing else ""
        print(f"scoreboard_N{n}.csv  {len(sb)} arms{note}")

    ct = cost_table()
    write_csv(OUT / "cost.csv",
              ["smoother", "runtime", "runtime_sd", "energy", "energy_sd"],
              [{"smoother": a, **r} for a, r in ct.items()])
    print(f"cost.csv  {len(ct)} arms")

    gvc = grid_vs_count()
    write_csv(OUT / "grid_vs_count.csv",
              ["N_servers", "mean_MW", "peak_MW", "min_v_pu", "nadir_hz",
               "rocof_hz_s", "rrei_per_yr", "nrs_MW_yr", "cv"], gvc)
    print(f"grid_vs_count.csv  {len(gvc)} fleet sizes")
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
