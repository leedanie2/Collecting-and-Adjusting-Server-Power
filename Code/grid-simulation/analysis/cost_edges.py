#!/usr/bin/env python3
"""Split ramp.c's cost into the ramp legs and the workload window.

summarize_n4.py scores runtime/energy on the FULL untrimmed trace, because
ramp.c's ramp-up/down ballast is real wall-time and real energy the datacenter
spends. That is the right total, but it hides where the cost comes from: the
published +121% runtime is mostly the ~80 s up / 40-90 s down ballast legs, not
the mitigation slowing the workload itself down.

This writes both numbers so a table or figure can show the decomposition:

    with_edges     full trace vs baseline full trace   (== scoreboard.csv)
    without_edges  trim_auto.py plateau vs the SAME baseline

Both are ratios against the same baseline duration/energy, so they are additive
in percentage points and a stacked bar is exact:

    with_edges_pct - without_edges_pct = the ramp legs' share

Only rampc has ballast legs, so only rampc gets a split; the other two
mitigations' with/without values are identical by construction and their
without_edges columns are left empty.

Aggregation matches summarize_n4.score(): mean across the 3 workloads within a
run, then mean +/- SD across the 4 runs. That is why the with_edges output
reproduces scoreboard.csv rather than merely resembling it -- --selfcheck
asserts exactly that.

    python3 analysis/cost_edges.py            # -> data/summary/cost_edges.csv
    python3 analysis/cost_edges.py --selfcheck
"""
import csv, statistics, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from trim_auto import detect_window, trim  # noqa: E402

RUNS = [1, 2, 3, 4]
WORKLOADS = ["hpl", "aisim2", "step"]
MITIG = ["rampc", "powersmoother", "usagegov"]
SPLIT = {"rampc"}                      # only this one has ballast legs
OUT = ROOT / "data/summary/cost_edges.csv"


def load(path):
    rows = []
    for r in list(csv.reader(open(path)))[1:]:
        if len(r) >= 2 and r[0] and r[1]:
            rows.append((float(r[0]), float(r[1])))
    return rows


def energy_and_dur(rows):
    """(integral P dt in J, duration s). Same trapezoid as summarize_n4."""
    t = [a for a, _ in rows]
    p = [b for _, b in rows]
    e = sum((t[i] - t[i - 1]) * (p[i] + p[i - 1]) / 2 for i in range(1, len(t)))
    return e, t[-1] - t[0]


def pct(s, b):
    return (s / b - 1.0) * 100.0 if b else None


def mean_sd(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return None, None
    return statistics.mean(xs), (statistics.stdev(xs) if len(xs) > 1 else 0.0)


def per_cell(runs=None):
    """{(run, workload, mitigation): {col: pct}} for both windows."""
    runs = runs or (ROOT / "data/runs")
    out = {}
    for r in RUNS:
        for w in WORKLOADS:
            be, bd = energy_and_dur(load(runs / f"run{r}" / f"{w}_baseline.csv"))
            for m in MITIG:
                rows = load(runs / f"run{r}" / f"{w}_{m}.csv")
                se, sd = energy_and_dur(rows)
                cell = {"runtime_with_pct": pct(sd, bd),
                        "energy_with_pct": pct(se, be),
                        "runtime_without_pct": None,
                        "energy_without_pct": None}
                if m in SPLIT:
                    a, z, _, _ = detect_window(rows)
                    te, td = energy_and_dur(trim(rows, a, z))
                    cell["runtime_without_pct"] = pct(td, bd)
                    cell["energy_without_pct"] = pct(te, be)
                out[(r, w, m)] = cell
    return out


COLS = ["runtime_with_pct", "runtime_without_pct",
        "energy_with_pct", "energy_without_pct"]


def score(cells):
    """Per mitigation: mean over workloads within a run, then mean +/- SD."""
    rows = []
    for m in MITIG:
        row = {"smoother": m}
        for c in COLS:
            per_run = [mean_sd([cells[(r, w, m)][c] for w in WORKLOADS])[0]
                       for r in RUNS]
            mn, sd = mean_sd(per_run)
            row[c] = None if mn is None else round(mn, 1)
            row[c + "_sd"] = None if sd is None else round(sd, 1)
        rows.append(row)
    return rows


def write(rows):
    OUT.parent.mkdir(parents=True, exist_ok=True)
    hdr = ["smoother"] + [c + s for c in COLS for s in ("", "_sd")]
    with open(OUT, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=hdr)
        wr.writeheader()
        for r in rows:
            wr.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in hdr})
    return OUT


def selfcheck():
    """with_edges must reproduce the committed scoreboard, not just resemble it.

    That is the whole reliability claim: if this file's trapezoid, pairing or
    aggregation drifted from summarize_n4's, the totals would diverge here.
    """
    fail = 0
    rows = {r["smoother"]: r for r in score(per_cell())}
    sb = {r["smoother"]: r
          for r in csv.DictReader(open(ROOT / "data/summary/scoreboard.csv"))}
    for m in MITIG:
        for mine, theirs in (("runtime_with_pct", "runtime_pct"),
                             ("energy_with_pct", "energy_pct")):
            a, b = rows[m][mine], float(sb[m][theirs])
            if abs(a - b) > 0.05:
                fail += 1
                print(f"FAIL {m} {mine}: {a} != scoreboard {b}", file=sys.stderr)
    # the split must be a reduction, or the trim did nothing
    for m in SPLIT:
        if not rows[m]["runtime_without_pct"] < rows[m]["runtime_with_pct"]:
            fail += 1; print(f"FAIL {m} trimmed runtime not lower", file=sys.stderr)
        if not rows[m]["energy_without_pct"] < rows[m]["energy_with_pct"]:
            fail += 1; print(f"FAIL {m} trimmed energy not lower", file=sys.stderr)
    # non-ballast arms must have no split at all
    for m in set(MITIG) - SPLIT:
        if rows[m]["runtime_without_pct"] is not None:
            fail += 1; print(f"FAIL {m} should have no split", file=sys.stderr)
    print("selfcheck: OK" if not fail else f"selfcheck: {fail} FAILED")
    return fail


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selfcheck":
        sys.exit(selfcheck())
    rows = score(per_cell())
    print(write(rows))
    for r in rows:
        def f(k):
            v, s = r[k], r[k + "_sd"]
            return "      --     " if v is None else f"{v:+7.1f} +/- {s:<4.1f}"
        print(f"  {r['smoother']:15} runtime {f('runtime_with_pct')} "
              f"{f('runtime_without_pct')}   energy {f('energy_with_pct')} "
              f"{f('energy_without_pct')}")
