#!/usr/bin/env python3
"""Plot one baseline-vs-smoother pair of raw single-node power-vs-time traces.
Usage: python3 analysis/plot_traces.py aisim2_baseline.csv aisim2_powersmoother.csv
       python3 analysis/plot_traces.py hpl_baseline.csv hpl_rampc.csv --offset 86.7
Reads from data/traces/, writes data/plots/<smoother-stem>.png (e.g.
hpl_rampc.csv -> hpl_rampc.png), so the plot filename always matches the
smoother trace's own workload_smoother name.

--offset SECONDS: for ramp.c pairs, the smoother launches the real workload
only after its own ballast pre-ramp finishes, so its own t=0 is NOT the
workload's start. --offset shifts the smoother trace left by SECONDS so the
real workload start lines up with the baseline's t=0 (workload start there
is immediate). Omit for smoothers with no separate pre-launch delay
(power-smoother, usage+gov). Also draws vertical markers at the workload
start (t=0) and end. End defaults to a symmetric ramp-down of the same
SECONDS as --offset; pass --ramp-down for an asymmetric one (e.g. hpl_rampc
measured 86.7s ramp-up / 49.8s ramp-down from ramp.c's own debug log)."""
import argparse, csv, pathlib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load(name):
    p = ROOT / "data" / "traces" / name
    t, w = [], []
    with open(p) as fh:
        r = csv.reader(fh)
        next(r)  # header
        for row in r:
            t.append(float(row[0])); w.append(float(row[1]))
    return t, w


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("baseline")
    ap.add_argument("smoother")
    ap.add_argument("--offset", type=float, default=0.0,
                     help="shift smoother trace left by this many seconds "
                          "(ramp.c pre-launch ballast duration) so both "
                          "traces' real workload start aligns at t=0")
    ap.add_argument("--ramp-down", type=float, default=None,
                     help="ramp.c's post-workload ramp-down duration, if "
                          "different from --offset's ramp-up duration "
                          "(defaults to --offset, i.e. symmetric)")
    args = ap.parse_args()
    ramp_down = args.ramp_down if args.ramp_down is not None else args.offset

    fig, ax = plt.subplots(figsize=(11, 5))
    smoother_t = None
    for f, shift in [(args.baseline, 0.0), (args.smoother, args.offset)]:
        t, w = load(f)
        if f == args.smoother:
            smoother_t = t
        t = [x - shift for x in t]
        label = f"{f}  (mean {sum(w)/len(w):.1f} W, peak {max(w):.1f} W)"
        if shift:
            label += f"  [shifted -{shift:g}s]"
        ax.plot(t, w, lw=0.8, label=label)

    if args.offset:
        workload_end = smoother_t[-1] - args.offset - ramp_down
        ax.axvline(0, color="k", ls="--", lw=1, alpha=0.6)
        ax.axvline(workload_end, color="k", ls="--", lw=1, alpha=0.6)
        ymin, ymax = ax.get_ylim()
        ytext = ymin + 0.03 * (ymax - ymin)
        ax.text(0, ytext, " workload start", fontsize=8, rotation=90, va="bottom")
        ax.text(workload_end, ytext, " workload end", fontsize=8, rotation=90, va="bottom")

    smoother_stem = pathlib.Path(args.smoother).stem
    ax.set_xlabel("time (s)" + (" — 0 = real workload start" if args.offset else ""))
    ax.set_ylabel("single-node CPU power (W)")
    ax.set_title(f"{smoother_stem} — raw RAPL power trace (baseline vs smoother)")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=9)
    plt.tight_layout()
    (ROOT / "data" / "plots").mkdir(parents=True, exist_ok=True)
    out = ROOT / "data" / "plots" / f"{smoother_stem}.png"
    plt.savefig(out, dpi=150)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
