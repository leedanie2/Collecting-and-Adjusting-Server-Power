#!/usr/bin/env python3
"""Regenerate every presentation figure under figures/ from checked-in data.

    python3 figures/make_figures.py

Reads data/summary/{scoreboard,matrix}.csv and data/runs/run1/*.csv (Python +
matplotlib only, no MATLAB). Outputs:

    scoreboard.png        ranked mitigation effect, 4 metrics, n=4 error bars
    per_workload.png      CV and energy split by workload (the averages hide a lot)
    overlays.png          9 baseline-vs-mitigation power traces; ramp.c UNTRIMMED
                          so the full di/dt ramp shows, trim window shaded
    pipeline.png          the trace -> fleet -> UPS -> microgrid -> metrics chain
"""
import csv, statistics, sys
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

ROOT = Path(__file__).resolve().parent.parent
FIG = ROOT / "figures"
sys.path.insert(0, str(ROOT / "scripts"))
from trim_auto import load as trim_load, detect_window  # noqa: E402

WORKLOADS = ["hpl", "aisim2", "step"]
MITIG = ["rampc", "powersmoother", "usagegov"]
# Okabe-Ito colourblind-safe. baseline grey, one hue per mitigation.
C = {"baseline": "#4d4d4d", "rampc": "#0072B2",
     "powersmoother": "#E69F00", "usagegov": "#009E73"}
LABEL = {"rampc": "ramp.c (di/dt shaping)", "powersmoother": "power smoother",
         "usagegov": "slew governor"}
plt.rcParams.update({"font.size": 11, "axes.grid": True, "grid.alpha": 0.25,
                     "axes.axisbelow": True, "figure.dpi": 130,
                     "savefig.bbox": "tight", "axes.spines.top": False,
                     "axes.spines.right": False})


def read_scoreboard():
    rows = {}
    for r in csv.DictReader(open(ROOT / "data/summary/scoreboard.csv")):
        rows[r["smoother"]] = r
    return rows


def read_matrix():
    """{(workload, mitigation): {metric: (mean, sd)}} across the 4 runs."""
    acc = {}
    for r in csv.DictReader(open(ROOT / "data/summary/matrix.csv")):
        k = (r["workload"], r["mitigation"])
        for m in ("cv_pct", "peak_pct", "runtime_pct", "energy_pct"):
            acc.setdefault(k, {}).setdefault(m, []).append(float(r[m]))
    out = {}
    for k, d in acc.items():
        out[k] = {m: (statistics.mean(v),
                      statistics.stdev(v) if len(v) > 1 else 0.0)
                  for m, v in d.items()}
    return out


# ── Figure 1: the scoreboard ──────────────────────────────────────────────────
def fig_scoreboard(sb):
    metrics = [("cv_pct", "CV (flatness)"), ("peak_pct", "peak-to-mean"),
               ("runtime_pct", "runtime"), ("energy_pct", "energy")]
    fig, axes = plt.subplots(1, 4, figsize=(13, 4.2))
    for ax, (col, title) in zip(axes, metrics):
        xs = range(len(MITIG))
        vals = [float(sb[m][col]) for m in MITIG]
        errs = [float(sb[m][col + "_sd"]) for m in MITIG]
        bars = ax.bar(xs, vals, yerr=errs, capsize=5,
                      color=[C[m] for m in MITIG], edgecolor="black", linewidth=0.6)
        ax.axhline(0, color="black", linewidth=0.8)
        ax.set_title(title, fontweight="bold")
        ax.set_xticks(list(xs))
        ax.set_xticklabels(["ramp.c", "smoother", "gov"], rotation=25, ha="right")
        ax.margins(x=0.15)
        ax.set_ylabel("% vs baseline")
        for b, v, e in zip(bars, vals, errs):
            off = 3 if v >= 0 else -3
            ax.annotate(f"{v:+.0f}%", (b.get_x() + b.get_width() / 2, v + (e + 1) * (1 if v >= 0 else -1)),
                        ha="center", va="bottom" if v >= 0 else "top", fontsize=9)
    axes[0].text(0.0, 1.14, "Lower is better for CV / peak / runtime · energy is a cost",
                 transform=axes[0].transAxes, fontsize=9, color="#555")
    fig.suptitle("Grid-impact of three power mitigations  (n=4 runs, mean ± SD)",
                 fontsize=14, fontweight="bold", y=1.02)
    fig.tight_layout()
    fig.savefig(FIG / "scoreboard.png"); plt.close(fig)


# ── Figure 2: per-workload breakdown ──────────────────────────────────────────
def fig_per_workload(mat):
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    for ax, (col, title, sign) in zip(
            axes, [("cv_pct", "CV change (flatness)", "lower better"),
                   ("energy_pct", "energy cost", "lower better")]):
        width = 0.25
        for i, m in enumerate(MITIG):
            xs = [j + (i - 1) * width for j in range(len(WORKLOADS))]
            vals = [mat[(w, m)][col][0] for w in WORKLOADS]
            errs = [mat[(w, m)][col][1] for w in WORKLOADS]
            ax.bar(xs, vals, width, yerr=errs, capsize=3, label=LABEL[m],
                   color=C[m], edgecolor="black", linewidth=0.5)
        ax.axhline(0, color="black", linewidth=0.8)
        ax.set_xticks(range(len(WORKLOADS)))
        ax.set_xticklabels(["HPL", "aisim2", "step"])
        ax.set_ylabel("% vs baseline")
        ax.set_title(f"{title}  ({sign})", fontweight="bold")
    axes[0].legend(loc="lower left", fontsize=9, framealpha=0.9)
    fig.suptitle("The averages hide structure: effect per workload (n=4, mean ± SD)",
                 fontsize=13, fontweight="bold", y=1.0)
    fig.tight_layout()
    fig.savefig(FIG / "per_workload.png"); plt.close(fig)


# ── Figure 3: the 9 overlays, ramp.c untrimmed ────────────────────────────────
def _series(path):
    rows = trim_load(path)
    return [t for t, _ in rows], [p for _, p in rows]


def fig_overlays(run="run1"):
    """One figure per mitigation (3 workload rows each). ramp.c is shown
    untrimmed AND time-aligned so its plateau starts where baseline starts —
    the ramp flanks then sit in negative time / past the baseline end, so the
    workloads line up for direct shape comparison."""
    rd = ROOT / "data" / "runs" / run
    for m in MITIG:
        fig, axes = plt.subplots(3, 1, figsize=(9, 9), sharex=False)
        for r, w in enumerate(WORKLOADS):
            ax = axes[r]
            bt, bp = _series(rd / f"{w}_baseline.csv")
            ax.plot(bt, bp, color=C["baseline"], lw=1.0, alpha=0.85, label="baseline")
            mt, mp = _series(rd / f"{w}_{m}.csv")
            if m == "rampc":
                # align: shift so the scored-plateau start sits at baseline t=0
                rows = trim_load(rd / f"{w}_{m}.csv")
                a, b, _, _ = detect_window(rows)
                mt = [t - a for t in mt]
                ax.plot(mt, mp, color=C[m], lw=1.1, label=LABEL[m] + " (aligned)")
                ax.axvspan(0, b - a, color=C[m], alpha=0.07)
                ax.axvline(0, color=C[m], ls="--", lw=0.8, alpha=0.6)
                ax.axvline(b - a, color=C[m], ls="--", lw=0.8, alpha=0.6)
                ax.text((b - a) / 2, ax.get_ylim()[1] * 0.99,
                        "scored plateau (aligned to baseline)", ha="center",
                        va="top", fontsize=8, color=C[m])
                ax.text(-a / 2 if a else -1, ax.get_ylim()[0] * 1.02, "ramp-up",
                        ha="center", va="bottom", fontsize=7, color=C[m], alpha=0.8)
            else:
                ax.plot(mt, mp, color=C[m], lw=1.1, label=LABEL[m])
            ax.set_ylabel(f"{w}\npower (W)", fontweight="bold")
            ax.legend(fontsize=9, loc="lower right", framealpha=0.85)
            if r == 2:
                ax.set_xlabel("time (s)")
        note = ("  (untrimmed, plateau aligned to baseline)" if m == "rampc" else "")
        fig.suptitle(f"Single-node power: baseline vs {LABEL[m]}{note}",
                     fontsize=13, fontweight="bold", y=0.997)
        fig.tight_layout()
        fig.savefig(FIG / f"overlay_{m}.png"); plt.close(fig)


# ── Figure 4: pipeline schematic ──────────────────────────────────────────────
def fig_pipeline():
    stages = [("1 server\npower trace", "#DDDDDD"),
              ("fleet aggregation\n10,000 servers\nworst-case in-phase", "#CFE8F3"),
              ("double-conversion\nUPS\n15 s low-pass", "#CFE8F3"),
              ("Simulink phasor\nmicrogrid\nswing-eq + PCC V", "#CFE8F3"),
              ("grid-risk metrics\nCV · peak · vs\nNERC/IEEE", "#D8F0DE")]
    fig, ax = plt.subplots(figsize=(14, 2.7))
    ax.set_xlim(0, len(stages) * 3); ax.set_ylim(0, 3); ax.axis("off")
    w, h, y = 2.3, 1.6, 0.7
    for i, (txt, col) in enumerate(stages):
        x = i * 3 + 0.2
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.12",
                                    fc=col, ec="black", lw=1.1))
        ax.text(x + w / 2, y + h / 2, txt, ha="center", va="center", fontsize=10)
        if i < len(stages) - 1:
            ax.add_patch(FancyArrowPatch((x + w + 0.08, y + h / 2), (x + 3 + 0.1, y + h / 2),
                                         arrowstyle="-|>", mutation_scale=22, lw=1.6,
                                         color="#333", zorder=5))
    ax.set_title("Fleet-scale grid-impact pipeline: one measured server → 10,000-server grid risk",
                 fontsize=13, fontweight="bold", y=1.02)
    fig.savefig(FIG / "pipeline.png"); plt.close(fig)


if __name__ == "__main__":
    FIG.mkdir(exist_ok=True)
    sb, mat = read_scoreboard(), read_matrix()
    fig_scoreboard(sb)
    fig_per_workload(mat)
    fig_overlays()
    fig_pipeline()
    print("wrote:", ", ".join(p.name for p in sorted(FIG.glob("*.png"))))
