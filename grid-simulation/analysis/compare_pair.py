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
run names, e.g. aisim2), so workloads don't clobber each other.
"""
import os, sys, csv, json, argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

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
    eb, db = energy_j(ROOT / "data" / "traces" / f"{args.baseline}.csv")
    es, ds = energy_j(ROOT / "data" / "traces" / f"{args.smoother}.csv")
    cost_ratio = es / eb
    print("COST  (energy area, integral P dt over raw single-node trace)")
    print(f"  {'baseline':<24}{eb/1000:10.2f} kJ   ({db:.1f} s)")
    print(f"  {'smoother':<24}{es/1000:10.2f} kJ   ({ds:.1f} s)")
    print(f"  {'ratio smoother/baseline':<24}{cost_ratio:10.3f}   = {(cost_ratio-1)*100:+.1f}% energy-area")

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
    outdir = ROOT / "data" / "comparisons"
    outdir.mkdir(parents=True, exist_ok=True)
    out = outdir / f"{prefix}_comparison.csv"
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["metric", "baseline", "smoother", "pct_change_smoother_vs_baseline"])
        w.writerow(["cost_energy_kJ", round(eb / 1000, 2), round(es / 1000, 2),
                    round((cost_ratio - 1) * 100, 1)])
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
