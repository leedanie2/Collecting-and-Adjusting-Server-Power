#!/usr/bin/env python3
"""Figures for experiment.odt -- the protocol, not the results.

make_figures.py covers what we measured; this covers how. Three placeholders in
the draft ask for these by name:

    (RUN MATRIX!!!)                  -> run_matrix.png
    (EXPERIMENT SCHEMATIC ...)       -> experiment_schematic.png
    "time-series power graphs"       -> wl_hpl.png, wl_aisim2.png, wl_step.png

Baseline traces come from run1 because it is the reference set every other run
is compared against in reproducibility.png; any run would show the same shape.

    python3 "Figures and Schematics/make_experiment_figs.py"
"""
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

FIG = Path(__file__).resolve().parent
ROOT = FIG.parent
sys.path.insert(0, str(FIG))
from make_figures import C, SHORT, WORKLOADS, plt as _plt  # noqa: E402,F401  (shares rcParams)

CONDITIONS = ["baseline", "powersmoother", "rampc", "usagegov"]
CELL_LABEL = {"baseline": "baseline", "powersmoother": "power\nsmoother",
              "rampc": "ramp.c", "usagegov": "usagegov"}
WL_TITLE = {"hpl": "HPL (ai_load.sh)", "aisim2": "AI Sim 2", "step": "Step"}
RUNS_DIR = ROOT / "data/runs/run1"

# Palette for the schematic. Kept off the mitigation hues in C so a reader never
# reads a grey process box as "baseline".
INK = "#2b2b2b"
LIMIT = "#D55E00"   # hardware limits / sampling artefacts, never a mitigation
BOX = {"host": "#e8e8e8", "data": "#dbeafe", "sim": "#cfe8f3", "out": "#d7ecd9"}


def load(path):
    t, p = [], []
    for line in open(path).read().splitlines()[1:]:
        a, _, b = line.partition(",")
        if a and b:
            t.append(float(a)); p.append(float(b))
    return t, p


# ---------------------------------------------------------------- workloads
def workload_traces():
    """One panel per workload -- these sit in their own subsections."""
    for w in WORKLOADS:
        t, p = load(RUNS_DIR / f"{w}_baseline.csv")
        fig, ax = plt.subplots(figsize=(6.6, 2.5))
        ax.plot(t, p, lw=0.9, color=C["baseline"])
        ax.fill_between(t, min(p), p, color=C["baseline"], alpha=0.10, lw=0)
        ax.set_xlim(0, t[-1])
        ax.set_xlabel("time (s)"); ax.set_ylabel("package power (W)")
        ax.set_title(f"{WL_TITLE[w]} — baseline power trace", loc="left",
                     fontsize=11, fontweight="bold")
        ax.margins(y=0.12)
        fig.tight_layout(); fig.savefig(FIG / f"wl_{w}.png"); plt.close(fig)
        print(f"  wl_{w}.png  ({t[-1]:.0f} s, {min(p):.0f}-{max(p):.0f} W)")


# --------------------------------------------------------------- run matrix
def run_matrix():
    """The 3x4 grid plus the per-cell timing, which is the part reviewers ask
    about: settle windows are why adjacent cells do not contaminate."""
    fig = plt.figure(figsize=(7.4, 4.3))
    gs = fig.add_gridspec(2, 1, height_ratios=[3.0, 1.0], hspace=0.55)
    ax = fig.add_subplot(gs[0]); ax.set_axis_off()

    nc, nr = len(CONDITIONS), len(WORKLOADS)
    for j, c in enumerate(CONDITIONS):
        ax.text(j + 0.5, nr + 0.28, CELL_LABEL[c], ha="center", va="bottom",
                fontsize=10, fontweight="bold", color=C[c], linespacing=1.25)
    for i, w in enumerate(WORKLOADS):
        y = nr - 1 - i
        ax.text(-0.12, y + 0.5, WL_TITLE[w], ha="right", va="center",
                fontsize=10, fontweight="bold", color=INK)
        for j, c in enumerate(CONDITIONS):
            ax.add_patch(FancyBboxPatch(
                (j + 0.06, y + 0.10), 0.88, 0.80,
                boxstyle="round,pad=0.012,rounding_size=0.06",
                facecolor=C[c], alpha=0.16, edgecolor=C[c], lw=1.2))
            ax.text(j + 0.5, y + 0.50, "×4", ha="center", va="center",
                    fontsize=13, color=C[c], fontweight="bold")
    ax.set_xlim(-1.55, nc + 0.05); ax.set_ylim(-0.05, nr + 0.75)
    ax.text(nc / 2, -0.42,
            "3 workloads × 4 conditions × 4 repeats = 48 traces",
            ha="center", va="top", fontsize=10.5, color=INK)
    ax.set_title("Run matrix", loc="left", fontsize=12, fontweight="bold",
                 x=-0.205)

    # ---- per-cell timeline
    tl = fig.add_subplot(gs[1]); tl.set_axis_off()
    steps = [("condition\nup", 5, "#bdbdbd"), ("sampler", 1, "#9e9e9e"),
             ("workload run", 11, C["baseline"]), ("teardown", 3, "#9e9e9e"),
             ("idle settle", 20, "#bdbdbd")]
    total = sum(s[1] for s in steps)
    x = 0.0
    for name, dur, col in steps:
        tl.add_patch(FancyBboxPatch(
            (x, 0.32), dur - total * 0.006, 0.42,
            boxstyle="round,pad=0.002,rounding_size=0.25",
            facecolor=col, alpha=0.30, edgecolor=col, lw=1.1))
        tl.text(x + dur / 2, 0.53, name, ha="center", va="center", fontsize=8.4,
                color=INK, linespacing=1.15)
        lab = "workload-dependent" if name == "workload run" else f"{dur} s"
        tl.text(x + dur / 2, 0.18, lab, ha="center", va="center", fontsize=7.8,
                color="#6b6b6b", style="italic")
        x += dur
    tl.set_xlim(-total * 0.005, total * 1.005); tl.set_ylim(0, 1.0)
    tl.set_title("Each cell", loc="left", fontsize=10.5, fontweight="bold",
                 x=-0.148)
    fig.savefig(FIG / "run_matrix.png"); plt.close(fig)
    print("  run_matrix.png")


# ---------------------------------------------------------------- schematic
def _box(ax, x, y, w, h, text, kind, fs=9.2, bold_first=True):
    ax.add_patch(FancyBboxPatch(
        (x, y), w, h, boxstyle="round,pad=0.008,rounding_size=0.10",
        facecolor=BOX[kind], edgecolor=INK, lw=1.1))
    head, _, rest = text.partition("\n")
    ax.text(x + w / 2, y + h / 2, head, ha="center",
            va="bottom" if rest else "center", fontsize=fs,
            fontweight="bold" if bold_first else "normal", color=INK)
    if rest:
        ax.text(x + w / 2, y + h / 2 - 0.055, rest, ha="center", va="top",
                fontsize=fs - 1.0, color="#4a4a4a", linespacing=1.35)


def _arrow(ax, p0, p1, rad=0.0):
    ax.add_patch(FancyArrowPatch(
        p0, p1, arrowstyle="-|>", mutation_scale=13, lw=1.3, color=INK,
        connectionstyle=f"arc3,rad={rad}", shrinkA=2, shrinkB=2))


def schematic():
    """Mycroft -> CSVs -> the two analysis branches. pipeline.png is only the
    grid half of this; the draft asks for the whole path including what we
    collected, so the collection side is spelled out here."""
    fig, ax = plt.subplots(figsize=(9.4, 4.2))
    ax.set_axis_off(); ax.set_xlim(0, 10); ax.set_ylim(0, 4.3)

    _box(ax, 0.05, 1.75, 1.72, 0.95,
         "Mycroft\nquiesced\n2×Xeon 6338", "host")
    _box(ax, 2.07, 1.75, 1.72, 0.95,
         "48 runs\n12 cells × 4", "host")
    _box(ax, 4.09, 1.75, 1.72, 0.95,
         "RAPL traces\ntime_s, power_W\n10 Hz CSV", "data")

    _box(ax, 6.35, 2.95, 3.55, 0.95,
         "Server level\nCV · peak-to-mean · runtime · energy · Gflops", "out",
         fs=9.0)
    _box(ax, 6.35, 0.25, 3.55, 0.95,
         "Grid level\nCV · ROCOF · NRS · RREI · nadir · sag", "out", fs=9.0)
    _box(ax, 4.09, 0.25, 1.72, 0.95,
         "MATLAB\n×10,000 · PUE\nUPS · Simulink", "sim")

    _arrow(ax, (1.77, 2.22), (2.07, 2.22))
    _arrow(ax, (3.79, 2.22), (4.09, 2.22))
    _arrow(ax, (5.81, 2.45), (6.35, 3.42), rad=-0.16)   # up to server metrics
    _arrow(ax, (4.95, 1.75), (4.95, 1.20))              # down into MATLAB
    _arrow(ax, (5.81, 0.72), (6.35, 0.72))
    ax.text(5.94, 3.10, "measured", fontsize=8.2, color="#6b6b6b",
            ha="right", va="center", style="italic")
    ax.text(5.10, 1.47, "extrapolated", fontsize=8.2, color="#6b6b6b",
            ha="left", va="center", style="italic")
    ax.set_title("From one quiesced server to fleet-scale grid risk",
                 loc="left", fontsize=12, fontweight="bold")
    fig.tight_layout(); fig.savefig(FIG / "experiment_schematic.png")
    plt.close(fig)
    print("  experiment_schematic.png")


def rampc_profile():
    """One annotated ramp.c run against its baseline.

    overlay_rampc.png is the three-panel analysis figure with the scored trim
    window drawn on; the solutions section wants the plain illustration -- ramp
    up, run flat, ramp down, and the wall-time that costs. aisim2 shows it best
    because its baseline is the noisiest of the three."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from trim_auto import detect_window                      # noqa: E402

    tb, pb = load(RUNS_DIR / "aisim2_baseline.csv")
    tr, pr = load(RUNS_DIR / "aisim2_rampc.csv")
    a, z, _, _ = detect_window([(t, p) for t, p in zip(tr, pr)])

    # Slide the baseline onto ramp.c's workload window. Left at its own t=0 it
    # sits under the ramp-up leg and reads as though the two ran concurrently.
    tb = [t + a for t in tb]

    fig, ax = plt.subplots(figsize=(7.2, 3.0))
    ax.plot(tb, pb, lw=0.8, color=C["baseline"], label="baseline (aligned)", zorder=2)
    ax.plot(tr, pr, lw=1.5, color=C["rampc"], label="ramp.c", zorder=3)
    lo = min(min(pb), min(pr))
    ax.fill_between(tr, lo, pr, color=C["rampc"], alpha=0.08, lw=0, zorder=1)

    y = max(max(pb), max(pr))
    for x0, x1, lab in ((tr[0], a, "ramp up"), (a, z, "workload"), (z, tr[-1], "ramp down")):
        ax.axvspan(x0, x1, color="none")
        ax.text((x0 + x1) / 2, y * 1.045, lab, ha="center", va="bottom",
                fontsize=9, color=INK, style="italic")
    for x in (a, z):
        ax.axvline(x, color=C["rampc"], lw=0.9, ls=(0, (4, 3)), alpha=0.55, zorder=1)

    ax.set_xlim(0, tr[-1]); ax.set_ylim(lo * 0.97, y * 1.13)
    ax.set_xlabel("time (s)"); ax.set_ylabel("package power (W)")
    ax.legend(loc="lower right", frameon=True, framealpha=0.9, fontsize=9)
    ax.set_title("ramp.c on AI Sim 2: flat power, paid for in wall time",
                 loc="left", fontsize=11, fontweight="bold")
    fig.tight_layout(); fig.savefig(FIG / "rampc_profile.png"); plt.close(fig)
    print(f"  rampc_profile.png  (legs {a:.0f}s / {tr[-1]-z:.0f}s, "
          f"baseline {tb[-1]:.0f}s vs ramp.c {tr[-1]:.0f}s)")


def mitigation_profile(m, title):
    """Baseline vs one mitigation on aisim2, same frame as rampc_profile.

    The three solutions get visually identical figures so a reader can compare
    them across sections; only ramp.c gets phase annotation, because only it
    has ballast legs to annotate."""
    tb, pb = load(RUNS_DIR / "aisim2_baseline.csv")
    tm, pm = load(RUNS_DIR / f"aisim2_{m}.csv")

    fig, ax = plt.subplots(figsize=(7.2, 3.0))
    ax.plot(tb, pb, lw=0.8, color=C["baseline"], label="baseline", zorder=2)
    ax.plot(tm, pm, lw=1.2, color=C[m], label=SHORT[m], zorder=3)
    lo = min(min(pb), min(pm)); hi = max(max(pb), max(pm))
    ax.fill_between(tm, lo, pm, color=C[m], alpha=0.08, lw=0, zorder=1)
    ax.set_xlim(0, max(tb[-1], tm[-1])); ax.set_ylim(lo * 0.97, hi * 1.06)
    ax.set_xlabel("time (s)"); ax.set_ylabel("package power (W)")
    ax.legend(loc="lower right", frameon=True, framealpha=0.9, fontsize=9)
    ax.set_title(title, loc="left", fontsize=11, fontweight="bold")
    fig.tight_layout(); fig.savefig(FIG / f"{m}_profile.png"); plt.close(fig)
    print(f"  {m}_profile.png  (baseline {tb[-1]:.0f}s vs {m} {tm[-1]:.0f}s)")


# One HPL cycle, wide enough to show several clamp events.
CLAMP_WINDOW = (55.0, 95.0)

def _window(t, p, lo, hi):
    return zip(*[(a, b) for a, b in zip(t, p) if lo <= a <= hi])


def rapl_clamp():
    """PL2 burst then the PL1 step-down, in the measured trace.

    Mycroft's per-package PL1 is 205 W and PL2 is 246 W, so a dual-socket
    sustained ceiling lands at 410 W -- which is exactly the shelf every HPL
    cycle settles onto after its overshoot."""
    t, p = load(RUNS_DIR / "hpl_baseline.csv")
    tw, pw = _window(t, p, *CLAMP_WINDOW)

    fig, ax = plt.subplots(figsize=(7.0, 2.9))
    ax.plot(tw, pw, lw=1.0, color=C["baseline"], zorder=3)
    # Deliberately not a mitigation colour -- this is a hardware limit, and a
    # blue line here reads as ramp.c to anyone who saw the other figures.
    ax.axhline(410, color=LIMIT, lw=1.2, ls=(0, (5, 3)), zorder=2)
    ax.text(CLAMP_WINDOW[0] + 0.6, 424, "2 × PL1 = 410 W sustained",
            fontsize=8.5, color=LIMIT, va="bottom")
    pk = max(pw)
    ax.annotate("PL2 burst, clamped after ~1.6 s",
                xy=(tw[list(pw).index(pk)], pk), xytext=(0.52, 0.34),
                textcoords="axes fraction", fontsize=8.5, color=INK,
                arrowprops=dict(arrowstyle="->", lw=0.9, color=INK))
    ax.set_xlim(*CLAMP_WINDOW)
    ax.set_xlabel("time (s)"); ax.set_ylabel("package power (W)")
    ax.set_title("RAPL power capping during HPL: burst, then clamp",
                 loc="left", fontsize=11, fontweight="bold")
    fig.tight_layout(); fig.savefig(FIG / "rapl_clamp.png"); plt.close(fig)
    print("  rapl_clamp.png")


def rapl_rate():
    """What a 1 Hz poller would have seen.

    Redfish tops out near 1 Hz, so this decimates the measured 10 Hz trace to
    1 Hz rather than plotting a separate capture -- same event, same axes, the
    only difference is the sampling rate being argued about."""
    t, p = load(RUNS_DIR / "hpl_baseline.csv")
    tw, pw = _window(t, p, *CLAMP_WINDOW)
    td, pd = list(tw)[::10], list(pw)[::10]

    fig, ax = plt.subplots(figsize=(7.0, 2.9))
    ax.plot(tw, pw, lw=1.0, color=C["baseline"], label="RAPL, 10 Hz", zorder=2)
    ax.plot(td, pd, lw=1.4, color=LIMIT, marker="o", ms=3.2,
            label="decimated to 1 Hz (Redfish's ceiling)", zorder=3)
    ax.set_xlim(*CLAMP_WINDOW)
    ax.set_xlabel("time (s)"); ax.set_ylabel("package power (W)")
    ax.legend(loc="lower right", frameon=True, framealpha=0.9, fontsize=8.5)
    ax.set_title("Why 10 Hz: at 1 Hz the ramp rate is unmeasurable",
                 loc="left", fontsize=11, fontweight="bold")
    fig.tight_layout(); fig.savefig(FIG / "rapl_rate.png"); plt.close(fig)
    print("  rapl_rate.png")


# Idle power as the observability stack is brought up one layer at a time.
# Transcribed from the "InfluxDB Cost by stack layer" table in the monitoring
# section; the last row is the same stack torn back down to InfluxDB alone.
OBSERVER_LAYERS = [
    ("nothing\nrunning",      196.38),
    ("+ InfluxDB\n(no writers)", 202.38),
    ("+ RAPL sampler\n10 Hz",    202.53),
    ("+ Grafana",              206.97),
    ("InfluxDB\nalone again",  205.84),
]


def observer_cost():
    """What observing the machine costs, as deltas rather than absolutes.

    Plotting 196-207 W on a zero-based axis shows nothing, and truncating the
    axis to make the bars visible would exaggerate them. Charting the delta
    against the quiet baseline keeps a true zero and still makes the point: the
    sampler is free, the database and the dashboards are not."""
    base = OBSERVER_LAYERS[0][1]
    labels = [n for n, _ in OBSERVER_LAYERS]
    deltas = [w - base for _, w in OBSERVER_LAYERS]
    # the sampler -- the layer everyone assumes is the expensive one
    hi = [i for i, (n, _) in enumerate(OBSERVER_LAYERS) if "sampler" in n]

    fig, ax = plt.subplots(figsize=(7.0, 3.1))
    cols = [LIMIT if i in hi else "#8c8c8c" for i in range(len(deltas))]
    bars = ax.bar(range(len(deltas)), deltas, color=cols, alpha=0.85, width=0.62)
    for i, (b, d) in enumerate(zip(bars, deltas)):
        step = d - (deltas[i - 1] if i else 0.0)
        lab = f"+{d:.2f} W" if i == 0 else f"{step:+.2f} W"
        ax.text(b.get_x() + b.get_width() / 2, d + 0.22, lab, ha="center",
                va="bottom", fontsize=9, color=INK,
                fontweight="bold" if i in hi else "normal")
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, fontsize=8.6, linespacing=1.3)
    ax.set_ylabel("\u0394 idle power (W)")
    ax.set_ylim(0, max(deltas) * 1.22)
    ax.set_title("Cost of observation: the sampler is free, the stack is not",
                 loc="left", fontsize=11, fontweight="bold")
    fig.tight_layout(); fig.savefig(FIG / "observer_cost.png"); plt.close(fig)
    print(f"  observer_cost.png  (peak +{max(deltas):.2f} W over {base} W)")


if __name__ == "__main__":
    observer_cost()
    workload_traces()
    run_matrix()
    schematic()
    rampc_profile()
    mitigation_profile("powersmoother",
                       "Power smoother on AI Sim 2: baseline vs smoothed power")
    mitigation_profile("usagegov",
                       "Usage governor on AI Sim 2: baseline vs governed power")
    rapl_clamp()
    rapl_rate()
