#!/usr/bin/env python3
"""Rank ALL grid metrics, not just the four the headline scoreboard trusts.

summarize_n3.py deliberately ranks on the four metrics that discriminate. This
ranks every metric the pipeline emits, per mitigation, mean +/- SD across the 4
runs, with the winner marked and a trust tag so the reader knows which rankings
mean something. Nothing is hidden -- but the tag says which to believe.

    python3 analysis/rank_all.py           # -> data/clean/summary/full_ranking.{csv,md}
    python3 analysis/rank_all.py --selfcheck

Cost (runtime, energy) is the full untrimmed trace; everything else is the
worst-case sim (results/<cell>_r<N>_worst/metrics.json). Convention: lower %
change vs baseline = better (safer/cheaper/flatter) for every row, so the
smallest mean wins.
"""
import argparse, csv, json, statistics, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = [1, 2, 3, 4]
WORKLOADS = ["hpl", "aisim2", "step"]
MITIG = ["rampc", "powersmoother", "usagegov"]

# (label, source, trust). source: ('cost', kind) or (section, key) in metrics.json.
# trust: direct | perf | coldstart | degenerate | cliff  (see summarize_n3.py).
METRICS = [
    ("CV",                 ("risk", "cv"),                          "direct"),
    ("peak-to-mean",       ("risk", "peak_to_mean"),               "direct"),
    ("runtime",            ("cost", "dur"),                        "perf"),
    ("energy",             ("cost", "energy"),                     "perf"),
    ("ROCOF",              ("frequency", "rocof_max_hz_per_s"),    "coldstart"),
    ("NRS",                ("risk", "nrs_MW_per_year"),            "coldstart"),
    ("freq nadir dev",     ("frequency", "freq_nadir_deviation_hz"), "coldstart"),
    ("volt sag depth",     ("voltage", "voltage_sag_depth_pu"),    "coldstart"),
    ("RREI",               ("risk", "rrei_exceedances_per_year"),  "degenerate"),
    ("LOLE proxy",         ("risk", "lole_proxy_days_per_year"),   "degenerate"),
    ("under-freq events",  ("frequency", "under_freq_events_per_year"), "degenerate"),
    ("volt sag events",    ("voltage", "voltage_sag_events_per_yr"), "cliff"),
]
TRUST_ORDER = ["direct", "perf", "coldstart", "degenerate", "cliff", "detector"]
TRUST_NOTE = {
    "direct": "computed straight from the signal — believe the ranking",
    "perf": "real cost (full trace)",
    "coldstart": "dominated by the model's t=0 cold start; needs WARMUP_S=45 to mean anything",
    "degenerate": "no signal — one t=0 exceedance per run makes it ~constant",
    "cliff": "count across a threshold both sides sit ~equal distance from",
    "detector": "detection quality of usage_edge / RF — NOT YET SCORED "
                "(needs a mycroft eval run with /proc/stat + onset labels; see RESULTS.md)",
}
# Named so the gap is explicit, not a silent omission. Filled by a detector
# eval run (usage_edge.py / RF evaluator.py), which the power traces can't supply.
DETECTOR_METRICS = ["event recall", "event latency (s)", "lead time (s)",
                    "alert precision"]


def energy_and_dur(csv_path):
    t, p = [], []
    for row in list(csv.reader(open(csv_path)))[1:]:
        if len(row) >= 2 and row[0] and row[1]:
            t.append(float(row[0])); p.append(float(row[1]))
    e = sum((t[i] - t[i-1]) * (p[i] + p[i-1]) / 2 for i in range(1, len(t)))
    return e, t[-1] - t[0]


def raw_value(cell, run, source):
    kind, key = source
    if kind == "cost":
        e, d = energy_and_dur(ROOT / "data/runs" / f"run{run}" / f"{cell}.csv")
        return d if key == "dur" else e
    m = json.load(open(ROOT / "results" / f"{cell}_r{run}_worst" / "metrics.json"))
    return m[kind][key]


def pct(s, b):
    return (s / b - 1.0) * 100.0 if b else None


def mean_sd(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return None, None
    return statistics.mean(xs), (statistics.stdev(xs) if len(xs) > 1 else 0.0)


def rank():
    """[(label, trust, {mitig: (mean, sd)}, winner)] over all metrics."""
    out = []
    for label, source, trust in METRICS:
        per_mitig = {}
        for s in MITIG:
            per_run = []
            for r in RUNS:
                # mean of the workload-level pct changes within a run
                wl = []
                for w in WORKLOADS:
                    b = raw_value(f"{w}_baseline", r, source)
                    v = raw_value(f"{w}_{s}", r, source)
                    wl.append(pct(v, b))
                per_run.append(mean_sd(wl)[0])
            per_mitig[s] = mean_sd(per_run)
        # winner = smallest mean (lower is better everywhere); skip if all None
        cand = [(v[0], s) for s, v in per_mitig.items() if v[0] is not None]
        winner = min(cand)[1] if cand else None
        out.append((label, trust, per_mitig, winner))
    # detector rows: named but unscored, so the scoreboard shows the gap
    for label in DETECTOR_METRICS:
        out.append((label, "detector", {m: (None, None) for m in MITIG}, None))
    out.sort(key=lambda r: TRUST_ORDER.index(r[1]))
    return out


def fmt(mv):
    m, sd = mv
    return "—" if m is None else (f"{m:+.1f}±{sd:.1f}" if sd else f"{m:+.1f}")


def write_csv(rows, out):
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["metric", "trust", "winner"]
                   + [f"{m}_pct" for m in MITIG] + [f"{m}_sd" for m in MITIG])
        for label, trust, pm, winner in rows:
            win = "pending" if trust == "detector" else (winner or "")
            w.writerow([label, trust, win]
                       + ["pending" if trust == "detector" else
                          ("" if pm[m][0] is None else round(pm[m][0], 1)) for m in MITIG]
                       + ["" if pm[m][1] is None else round(pm[m][1], 1) for m in MITIG])


def write_md(rows, out):
    L = ["# Full metric ranking — every grid metric, all four runs", "",
         "% change vs baseline, mean ± SD across 4 runs (lower = better "
         "everywhere). **Winner** is the best mitigation for that metric. The "
         "*trust* column says whether the ranking means anything — only "
         "`direct`/`perf` rows are safe to rank on as-is.", "",
         "| metric | trust | " + " | ".join(MITIG) + " | winner |",
         "|---|---|" + "---|" * (len(MITIG) + 1)]
    last = None
    for label, trust, pm, winner in rows:
        if trust != last:
            L.append(f"| _{trust}_ | | " + " | ".join([""] * len(MITIG)) + " | |")
            last = trust
        cells = []
        for m in MITIG:
            if trust == "detector":
                cells.append("_pending_")
            else:
                s = fmt(pm[m])
                cells.append(f"**{s}**" if m == winner and pm[m][0] is not None else s)
        win = "_pending_" if trust == "detector" else (winner or "—")
        L.append(f"| {label} | {trust} | " + " | ".join(cells) + f" | {win} |")
    L += ["", "### Trust tags", ""]
    for t in TRUST_ORDER:
        L.append(f"- **{t}** — {TRUST_NOTE[t]}")
    L += ["", "The headline `scoreboard.csv` ranks on the four `direct`/`perf` "
          "rows only. The `coldstart` rows become meaningful with "
          "`WARMUP_S=45 scripts/score_all.sh`; `degenerate`/`cliff` rows carry "
          "no mitigation signal at worst-case aggregation.", ""]
    open(out, "w").write("\n".join(L) + "\n")


def selfcheck():
    fail = 0
    if pct(150, 100) != 50.0:
        fail += 1; print("FAIL pct", file=sys.stderr)
    if pct(1, 0) is not None:
        fail += 1; print("FAIL pct div0", file=sys.stderr)
    if mean_sd([]) != (None, None):
        fail += 1; print("FAIL mean_sd empty", file=sys.stderr)
    m, sd = mean_sd([10, 20, 30])
    if m != 20:
        fail += 1; print("FAIL mean_sd", file=sys.stderr)
    # every metric must carry a known trust tag
    for _, _, t in METRICS:
        if t not in TRUST_ORDER:
            fail += 1; print(f"FAIL bad trust {t}", file=sys.stderr)
    print("selfcheck: OK" if not fail else f"selfcheck: {fail} FAILED")
    return fail


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selfcheck", action="store_true")
    if ap.parse_args().selfcheck:
        sys.exit(selfcheck())
    rows = rank()
    outdir = ROOT / "data" / "summary"
    write_csv(rows, outdir / "full_ranking.csv")
    write_md(rows, outdir / "full_ranking.md")
    for label, trust, pm, winner in rows:
        print(f"  [{trust:10}] {label:18} winner={winner}")
    print(f"-> {outdir}/full_ranking.{{csv,md}}")
