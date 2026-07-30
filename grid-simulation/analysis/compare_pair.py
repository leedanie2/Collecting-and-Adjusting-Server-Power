#!/usr/bin/env python3
"""Compare a baseline workload against the same workload with a smoother applied.

Two independent numbers, no dollars / annualization / fleet scaling:

  COST  = ratio of Riemann sums of the two raw single-node CSVs
          (integral P dt). Reported as % additional energy-area of running
          the smoother. Different trace durations are included on purpose:
          holding power up longer *is* part of the smoother's cost.

  RISK  = the grid metrics the MATLAB pipeline already wrote to metrics.json,
          shown as baseline / smoother / ratio for the fully-synchronised
          (worst_case) runs.

Usage:
    python3 analysis/compare_pair.py                       # stepped_baseline vs stepped_smoother
    python3 analysis/compare_pair.py aiload_baseline aiload_gov
    python3 analysis/compare_pair.py <base> <smoother> --suffix _worst

Run from the grid/ root. Reads traces from data/traces/, writes
data/comparisons/<prefix>_comparison.csv (prefix = shared prefix of the two
run names, e.g. hpl_rampc).
"""
import os, sys, csv, json, argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def find_trace(name):
    """Traces live in data/traces/. Accept a bare filename; an explicit relative
    path still wins."""
    base = ROOT / "data"
    direct = base / name
    if direct.is_file():
        return direct
    hits = (sorted(base.glob(f"traces/{name}"))
            + sorted(base.glob(f"traces/*/{name}")))
    if not hits:
        raise SystemExit(f"trace not found under data/traces/: {name}")
    return hits[0]

# 9 risk stats: (label, metrics.json section, key). ratio = smoother / baseline;
# for all of these lower = safer, so ratio < 1 means the smoother helped.
# Voltage sag depth (1 - min V) is the graded companion to the sag COUNT: it
# discriminates the smoother even when neither run breaches the 0.95 threshold.
RISK = [
    ("RREI (exceedances/yr)",   "risk",      "rrei_exceedances_per_year"),
    ("NRS (MW/yr)",             "risk",      "nrs_MW_per_year"),
    ("LOLE proxy (days/yr)",    "risk",      "lole_proxy_days_per_year"),
    ("CV",                      "risk",      "cv"),
    ("Peak-to-mean",            "risk",      "peak_to_mean"),
    ("Freq nadir (Hz)",         "frequency", "freq_nadir_hz"),
    ("ROCOF (Hz/s)",            "frequency", "rocof_max_hz_per_s"),
    ("Voltage sag (events/yr)", "voltage",   "voltage_sag_events_per_yr"),
    ("Voltage sag depth (pu)",  "voltage",   "voltage_sag_depth_pu"),
]


def energy_j(csv_path):
    """Trapezoidal integral of power over time (Joules) from a time_s,power_W CSV."""
    t, w = [], []
    with open(csv_path) as fh:
        r = csv.reader(fh)
        next(r)  # header (positional, text ignored)
        for row in r:
            t.append(float(row[0])); w.append(float(row[1]))
    return sum((w[i] + w[i + 1]) / 2 * (t[i + 1] - t[i]) for i in range(len(t) - 1)), t[-1] - t[0]


def gflops_for(run):
    """Sustained Gflops for a run, from data/sweep_meta.csv if present.

    Not captured for every collection -- where the workload's stdout (which is
    where HPL prints its score) went to the terminal rather than a file, there
    is no Gflops row. Returns None and the caller degrades.
    """
    p = ROOT / "data" / "sweep_meta.csv"
    if not p.exists():
        return None
    base = run[:-len("_worst")] if run.endswith("_worst") else run
    with open(p) as fh:
        for r in csv.DictReader(fh):
            if r.get("run_name") in (run, base):
                try:
                    return float(r["gflops"])
                except (TypeError, ValueError, KeyError):
                    return None
    return None


def load_metrics(run):
    p = ROOT / "results" / run / "metrics.json"
    if not p.exists():
        sys.exit(f"metrics.json not found for run '{run}' ({p}). "
                 f"Run the sim + scripts/score_all.sh first.")
    return json.loads(p.read_text())


def ratio(num, den):
    return num / den if den not in (0, 0.0) else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("baseline", nargs="?", default="stepped_baseline")
    ap.add_argument("smoother", nargs="?", default="stepped_smoother")
    ap.add_argument("--suffix", default="_worst",
                    help="results-dir suffix for the risk runs (default _worst = fully synchronised)")
    ap.add_argument("--out", default=None,
                    help="output CSV prefix (default: shared prefix of the two run names). "
                         "Set it per pair when several smoothers share one baseline, "
                         "else every pair writes <baseline-prefix>_comparison.csv and clobbers.")
    args = ap.parse_args()

    # ── COST: Riemann-sum ratio of the raw CSVs ────────────────────────────────
    eb, db = energy_j(find_trace(f"{args.baseline}.csv"))
    es, ds = energy_j(find_trace(f"{args.smoother}.csv"))
    cost_ratio = es / eb
    print("COST  (energy area, integral P dt over raw single-node trace)")
    print(f"  {'baseline':<24}{eb/1000:10.2f} kJ   ({db:.1f} s)")
    print(f"  {'smoother':<24}{es/1000:10.2f} kJ   ({ds:.1f} s)")
    print(f"  {'ratio smoother/baseline':<24}{cost_ratio:10.3f}   = {(cost_ratio-1)*100:+.1f}% energy-area")

    # ── PERFORMANCE: what the smoother costs in time, not just watts ──────────
    # runtime_s is the trace duration. It is a real time-to-completion number
    # ONLY for hpl, which is fixed-WORK: HPL solves N=80000 and takes as long as
    # it takes, so a longer trace means the same work ran slower.
    #
    # It is NOT one for aisim2 or step. Both are fixed-TIME: ai_sim_2 runs a
    # ~120 s REST/PREFILL schedule and ./load plays a fixed waveform, so runtime
    # is pinned by construction (~0% here) and any real cost shows up as less
    # work done inside the window -- which nothing currently measures.
    #
    # And for any rampc cell runtime_s is the TRIMMED PLATEAU width, not a
    # workload duration, so it is not comparable to its baseline at all.
    # Read this column for hpl_powersmoother and hpl_slewgov; treat the rest
    # as descriptive.
    runtime_pct = (ds / db - 1.0) * 100.0
    print("\nPERFORMANCE  (runtime = trace duration; a true time-to-completion "
          "only for fixed-work hpl -- see compare_pair.py)")
    print(f"  {'baseline runtime':<24}{db:10.1f} s")
    print(f"  {'smoother runtime':<24}{ds:10.1f} s")
    print(f"  {'change':<24}{runtime_pct:+10.1f} %")
    gb, gs = gflops_for(args.baseline), gflops_for(args.smoother)
    perf_rows = [("runtime_s", round(db, 1), round(ds, 1), round(runtime_pct, 1))]
    if gb and gs:
        gpct = (gs / gb - 1.0) * 100.0
        # Gflops/W uses mean power over the run = energy / duration.
        gwb, gws = gb / (eb / db), gs / (es / ds)
        print(f"  {'baseline Gflops':<24}{gb:10.1f}")
        print(f"  {'smoother Gflops':<24}{gs:10.1f}   ({gpct:+.1f}%)")
        print(f"  {'Gflops/W':<24}{gwb:10.3f} -> {gws:.3f}   ({(gws/gwb-1)*100:+.1f}%)")
        perf_rows += [("gflops", gb, gs, round(gpct, 1)),
                      ("gflops_per_W", round(gwb, 4), round(gws, 4),
                       round((gws / gwb - 1) * 100, 1))]
    else:
        print("  Gflops                   n/a  (not captured during collection; "
              "see data/sweep_meta.csv)")

    # ── RISK: 9 grid metrics, baseline / smoother / ratio ──────────────────────
    mb = load_metrics(args.baseline + args.suffix)
    ms = load_metrics(args.smoother + args.suffix)
    print(f"\nRISK  (fully-synchronised worst case; % change smoother vs baseline; "
          f"negative = smoother lower)")
    print(f"  {'metric':<26}{'baseline':>14}{'smoother':>14}{'% change':>12}")
    rows = []
    for label, sect, key in RISK:
        b = mb.get(sect, {}).get(key)
        s = ms.get(sect, {}).get(key)
        pct = (ratio(s, b) - 1.0) * 100.0 if (b is not None and s is not None) else float("nan")
        rows.append((label, b, s, pct))
        bs = "n/a" if b is None else f"{b:,.4g}"
        ss = "n/a" if s is None else f"{s:,.4g}"
        ps = "n/a" if pct != pct else f"{pct:+.1f}%"
        print(f"  {label:<26}{bs:>14}{ss:>14}{ps:>12}")

    # ── CSV ─────────────────────────────────────────────────────────────────────
    prefix = args.out or os.path.commonprefix([args.baseline, args.smoother]).rstrip("_") or "pair"
    # contaminated set in the same directory listing.
    outdir = ROOT / "data" / "comparisons"
    outdir.mkdir(parents=True, exist_ok=True)
    out = outdir / f"{prefix}_comparison.csv"
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["metric", "baseline", "smoother", "pct_change_smoother_vs_baseline"])
        w.writerow(["cost_energy_kJ", round(eb / 1000, 2), round(es / 1000, 2),
                    round((cost_ratio - 1) * 100, 1)])
        for name, b_, s_, p_ in perf_rows:
            w.writerow([name, b_, s_, p_])
        for label, b, s, pct in rows:
            w.writerow([label, b, s, round(pct, 1) if pct == pct else ""])
    print(f"\nwrote {out}")


def _selfcheck():
    # trapezoid of a flat 100 W line over 10 s = 1000 J; ratio of 200W/100W traces = 2.0
    import tempfile, os
    def mk(vals):
        fd, p = tempfile.mkstemp(suffix=".csv"); os.close(fd)
        with open(p, "w") as f:
            f.write("time_s,power_W\n")
            for i, v in enumerate(vals):
                f.write(f"{i},{v}\n")
        return p
    p1 = mk([100, 100, 100])  # 2 s * 100 W = 200 J
    p2 = mk([200, 200, 200])  # 2 s * 200 W = 400 J
    e1, d1 = energy_j(p1); e2, d2 = energy_j(p2)
    assert abs(e1 - 200) < 1e-9 and abs(e2 - 400) < 1e-9, (e1, e2)
    assert abs(e2 / e1 - 2.0) < 1e-9
    os.remove(p1); os.remove(p2)
    print("SELFCHECK OK")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        main()
