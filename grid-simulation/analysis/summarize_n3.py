#!/usr/bin/env python3
"""The headline scoreboard: which mitigation wins, with n=4 error bars.

The 3x4 matrix was collected four times on a quiesced server (aisim2 schedule
seed pinned across all runs, so the repeats isolate system variance, not
workload luck). This aggregates them into one ranked table with a run-to-run
standard deviation on every number -- so a real effect is distinguishable from a
lucky draw. The single-run per-cell durations swung 10-19% between runs; the
grid-relevant CV did not, which is the whole reason the repeats were collected.

Inputs, all checked in:
    data/runs/run<N>/<cell>.csv          raw trace: runtime (last t) + energy
    results/<cell>_r<N>_worst/metrics.json   sim: CV, peak-to-mean (post-UPS)
where cell = {hpl,aisim2,step}_{baseline,powersmoother,rampc,usagegov}, N=1..4.
ramp.c traces are trimmed to their real-workload plateau inline (trim_auto.py),
matching what was simulated.

Ranks on four metrics only. CV and peak-to-mean are direct grid quantities;
runtime and energy are real costs. The other nine the pipeline emits do not
discriminate here (see "Why only four" in the README): RREI/LOLE are degenerate
(one t=0 exceedance per worst-case run, so RREI == year/duration), and
NRS/ROCOF/nadir/sag-depth are dominated by the model's t=0 fleet cold start.
CV/peak are read from the simulated PCC power, NOT the raw trace, because the
15 s UPS low-pass is exactly what separates the mitigations.

    python3 analysis/summarize_n3.py            # -> data/summary/{scoreboard,matrix,summary}
    python3 analysis/summarize_n3.py --selfcheck
"""
import argparse, csv, json, statistics, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from trim_auto import load as trim_load, detect_window, trim  # noqa: E402

RUNS = [1, 2, 3, 4]
WORKLOADS = ["hpl", "aisim2", "step"]
SMOOTHERS = ["powersmoother", "rampc", "usagegov"]
METRICS = [("CV", "cv_pct"), ("Peak-to-mean", "peak_pct"),
           ("runtime", "runtime_pct"), ("energy", "energy_pct")]


def trace_rows(cell, run):
    """(time, power) rows for a cell; ramp.c trimmed to its plateau like the sim."""
    rows = trim_load(ROOT / "data" / "runs" / f"run{run}" / f"{cell}.csv")
    if "rampc" in cell:
        a, b, _, _ = detect_window(rows)
        rows = [(t, p) for t, p in trim(rows, a, b)]
    return rows


def energy_and_dur(rows):
    """(integral P dt in J, duration s)."""
    e = sum((rows[i][0] - rows[i - 1][0]) * (rows[i][1] + rows[i - 1][1]) / 2
            for i in range(1, len(rows)))
    return e, rows[-1][0] - rows[0][0]


def cv_peak(cell, run):
    r = json.load(open(ROOT / "results" / f"{cell}_r{run}_worst" / "metrics.json"))["risk"]
    return r["cv"], r["peak_to_mean"]


def pct(s, b):
    return (s / b - 1.0) * 100.0 if b else None


def per_run_pairs():
    """{(run, workload, smoother): {metric_col: pct_change_vs_baseline}}."""
    out = {}
    for r in RUNS:
        for w in WORKLOADS:
            be, bd = energy_and_dur(trace_rows(f"{w}_baseline", r))
            bcv, bpk = cv_peak(f"{w}_baseline", r)
            for s in SMOOTHERS:
                se, sd = energy_and_dur(trace_rows(f"{w}_{s}", r))
                scv, spk = cv_peak(f"{w}_{s}", r)
                out[(r, w, s)] = {"cv_pct": pct(scv, bcv),
                                  "peak_pct": pct(spk, bpk),
                                  "runtime_pct": pct(sd, bd),
                                  "energy_pct": pct(se, be)}
    return out


def mean_sd(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return None, None
    return statistics.mean(xs), (statistics.stdev(xs) if len(xs) > 1 else 0.0)


def score(pairs):
    """Per smoother: mean each metric across the 3 workloads within a run, then
    mean +/- SD across the 4 runs. The SD is the run-to-run error bar."""
    rows = []
    for s in SMOOTHERS:
        row = {"smoother": s}
        for _, col in METRICS:
            per_run = [mean_sd([pairs[(r, w, s)][col] for w in WORKLOADS])[0]
                       for r in RUNS]
            m, sd = mean_sd(per_run)
            row[col] = None if m is None else round(m, 1)
            row[col + "_sd"] = None if sd is None else round(sd, 1)
        rows.append(row)
    rows.sort(key=lambda r: (r["cv_pct"] if r["cv_pct"] is not None else 1e9))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def verdict(cv, cv_sd):
    if cv is None:
        return "no data"
    band = f" (+/-{cv_sd:.0f})" if cv_sd else ""
    if cv <= -20:
        return f"strong smoothing: {-cv:.0f}%{band} less variability"
    if cv <= -5:
        return f"modest smoothing: {-cv:.0f}%{band} less variability"
    if cv < 5:
        return f"no measurable smoothing ({cv:+.0f}%{band})"
    return f"WORSENS variability by {cv:.0f}%{band}"


def fmt(row, col):
    m, sd = row.get(col), row.get(col + "_sd")
    if m is None:
        return "—"
    return f"{m:+.1f} ± {sd:.1f}%" if sd else f"{m:+.1f}%"


def write_csv(rows, out):
    cols = ["rank", "smoother"]
    for _, c in METRICS:
        cols += [c, c + "_sd"]
    cols.append("verdict")
    with open(out, "w", newline="") as fh:
        wr = csv.writer(fh); wr.writerow(cols)
        for r in rows:
            r["verdict"] = verdict(r["cv_pct"], r.get("cv_pct_sd"))
            wr.writerow(["" if r.get(c) is None else r.get(c) for c in cols])


def write_md(rows, out):
    L = ["# Which mitigation wins? (n=4, mean ± SD across runs)", "",
         "| rank | mitigation | CV | peak | runtime | energy | verdict |",
         "|---|---|---|---|---|---|---|"]
    for r in rows:
        L.append(f"| {r['rank']} | **{r['smoother']}** | {fmt(r,'cv_pct')} | "
                 f"{fmt(r,'peak_pct')} | {fmt(r,'runtime_pct')} | "
                 f"{fmt(r,'energy_pct')} | {verdict(r['cv_pct'], r.get('cv_pct_sd'))} |")
    L += ["", "Each cell: mean ± standard deviation across four repeat "
          "collections of the full 3×4 matrix (quiesced server, aisim2 schedule "
          "seed pinned). Negative = mitigation lower (better) for CV/peak/"
          "runtime; energy is a cost either way. CV and peak are on the "
          "simulated PCC power (post 15 s UPS filter); runtime and energy are "
          "direct from the RAPL trace.", "",
          "Ranked by CV reduction — the flatness a smoother exists to deliver. "
          "The ± is run-to-run spread; where it rivals the mean (runtime, "
          "energy), a single collection cannot be trusted, which is why three "
          "were taken. Per-cell numbers: `matrix.csv` (run × workload × "
          "mitigation).", ""]
    open(out, "w").write("\n".join(L) + "\n")


def write_matrix(pairs, out):
    with open(out, "w", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(["run", "workload", "mitigation"] + [c for _, c in METRICS])
        for (r, w, s), v in sorted(pairs.items()):
            wr.writerow([r, w, s] + [round(v[c], 2) if v[c] is not None else ""
                                     for _, c in METRICS])


def selfcheck():
    fail = 0
    e, d = energy_and_dur([(0, 100), (1, 100), (2, 100)])
    if not (199 <= e <= 201 and d == 2):
        fail += 1; print(f"FAIL energy/dur -> {e},{d}", file=sys.stderr)
    fake = {}
    for r in RUNS:
        for w in WORKLOADS:
            fake[(r, w, "rampc")] = {"cv_pct": -60.0, "peak_pct": -5.0,
                                     "runtime_pct": 5.0, "energy_pct": 35.0}
            fake[(r, w, "powersmoother")] = {"cv_pct": 1.0, "peak_pct": 0.0,
                                             "runtime_pct": 5.0, "energy_pct": 10.0}
            fake[(r, w, "usagegov")] = {"cv_pct": 12.0, "peak_pct": 1.0,
                                        "runtime_pct": 4.0, "energy_pct": 13.0}
    got = [r["smoother"] for r in score(fake)]
    if got != ["rampc", "powersmoother", "usagegov"]:
        fail += 1; print(f"FAIL rank -> {got}", file=sys.stderr)
    if score(fake)[0]["cv_pct_sd"] != 0.0:
        fail += 1; print("FAIL identical runs should give SD 0", file=sys.stderr)
    fake2 = {k: dict(v) for k, v in fake.items()}
    for w in WORKLOADS:
        fake2[(1, w, "rampc")]["energy_pct"] = 20.0
        fake2[(3, w, "rampc")]["energy_pct"] = 50.0
    if not score(fake2)[0]["energy_pct_sd"] > 0:
        fail += 1; print("FAIL varying runs should give SD>0", file=sys.stderr)
    print("selfcheck: OK" if not fail else f"selfcheck: {fail} FAILED")
    return fail


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args()
    if a.selfcheck:
        sys.exit(selfcheck())
    pairs = per_run_pairs()
    rows = score(pairs)
    outdir = ROOT / "data" / "summary"
    outdir.mkdir(parents=True, exist_ok=True)
    write_csv(rows, outdir / "scoreboard.csv")
    write_matrix(pairs, outdir / "matrix.csv")
    write_md(rows, outdir / "summary.md")
    for r in rows:
        print(f"  {r['rank']}. {r['smoother']:15s} CV {fmt(r,'cv_pct')}  "
              f"energy {fmt(r,'energy_pct')}")
    print(f"-> {outdir}/{{scoreboard.csv,matrix.csv,summary.md}}")
