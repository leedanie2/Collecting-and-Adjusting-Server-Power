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


def find_trace(name):
    """Traces live in data/traces/.
    Accept a bare filename and search one level down so callers don't need to
    know which set a trace belongs to; an explicit relative path still wins."""
    base = ROOT / "data"
    direct = base / name
    if direct.is_file():
        return direct
    hits = (sorted(base.glob(f"traces/{name}"))
            + sorted(base.glob(f"traces/*/{name}")))
    if not hits:
        raise SystemExit(f"trace not found under data/traces/: {name}")
    return hits[0]


def load(name):
    p = find_trace(name)
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
        label = (f"{f}  ({t[-1] - t[0]:.0f}s, mean {sum(w)/len(w):.1f} W, "
                 f"peak {max(w):.1f} W)")
        if shift:
            label += f"  [shifted -{shift:g}s]"
        ax.plot(t, w, lw=0.8, label=label)

    # Time-to-completion markers: each trace spans its own workload run, so the
    # right-hand edges show the runtime difference directly. Drawn per trace in
    # its own colour, and skipped when --offset already draws its own markers.
    if not args.offset:
        # Snapshot the DATA lines first -- axvline appends to ax.get_lines(),
        # so reading it afterwards returns 4 entries and the pair check below
        # silently never fires.
        data_lines = list(ax.get_lines())
        ends = [ln.get_xdata()[-1] for ln in data_lines]
        for ln in data_lines:
            ax.axvline(ln.get_xdata()[-1], color=ln.get_color(),
                       ls=":", lw=1.2, alpha=0.8)
        ax.axvline(0, color="k", ls="--", lw=1, alpha=0.4)
        if len(ends) == 2 and ends[0] > 0:
            # Axes coords, not data coords: the data-space bottom-right is
            # occupied by the trace itself and the text was unreadable there.
            ax.text(0.99, 0.04,
                    f"time to completion {ends[1] - ends[0]:+.0f}s "
                    f"({(ends[1] / ends[0] - 1) * 100:+.1f}%)",
                    transform=ax.transAxes, ha="right", va="bottom", fontsize=9,
                    bbox=dict(boxstyle="round,pad=0.35", fc="white",
                              ec="0.7", alpha=0.9))

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
    # Mirror the traces split (see data/README.md) so clean-set overlays never
    # sit in the same listing as the original contaminated ones.
    outdir = ROOT / "data" / "plots"
    outdir.mkdir(parents=True, exist_ok=True)
    out = outdir / f"{smoother_stem}.png"
    plt.savefig(out, dpi=150)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
