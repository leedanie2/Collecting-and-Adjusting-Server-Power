#!/usr/bin/env python3
"""3D fleet power-CDF visualizer.

For each node, X = threshold as % of that node's own idle-max power range,
Y = fraction of elapsed sim time the node's power has been below X,
Z = count of nodes whose curve passes through each (X, Y) cell.
The surface animates as sim time advances. Nodes are k-means clustered on
their CDF vectors; cluster centroid curves are overlaid.

Two fleet sources:
  default   200 synthetic nodes, 4 duty-cycle archetypes (original prototype)
  --real    nodes derived from MEASURED mycroft power traces (the grid
            pipeline's data/rapl_*.csv): each node is a phase-shifted,
            gain/noise-jittered variant of a real trace, so the fleet
            profiles real workload shapes (HPL flat-out, throttled tiers,
            ai_load busy/dip cycling) instead of invented ones.

Launch:    .venv/bin/python fleet_cdf_viz.py           (builds, serves, opens browser)
           .venv/bin/python fleet_cdf_viz.py --real    (fleet from measured traces)
Options:   --no-browser (serve only), --selfcheck
"""

import glob as globmod
import http.server
import functools
import os
import sys
import webbrowser
from pathlib import Path

import numpy as np
from scipy.cluster.vq import kmeans2
from scipy.ndimage import gaussian_filter, gaussian_filter1d, map_coordinates

# ---- config ----
N_PER_ARCHETYPE = 50          # 4 archetypes -> N=200
T = 600                       # simulated seconds at 1 Hz
N_FRAMES = 100
N_XBINS = 80                  # X: % of own idle-max range
N_YBINS = 80                  # Y: fraction of elapsed time below X
K = 4
SEED = 42
PORT = 8047
SMOOTH_SIGMA = (1.2, 2.2, 1.8)  # gaussian smoothing (time, x_bins, y_bins): ridges + steady animation

ARCHETYPES = ["mostly-idle", "steady-busy", "bursty-cyclic", "heavy-sustained"]
# dataviz reference categorical palette (first 4 validated: CVD dE 24.2);
# extended so --real fleets with >4 source traces still get distinct colors
COLORS = ["#2a78d6", "#1baf7a", "#eda100", "#008300",
          "#b04ad6", "#d64a4a", "#4ad6cd", "#8a6d3b"]

# --real defaults: the grid pipeline's measured trace library
REAL_GLOB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "grid-simulation", "data", "traces", "*.csv")
N_PER_TRACE = 30


# ---- simulation (normalized power, 0..1 of each node's own idle-max range) ----
def gen_mostly_idle(rng, T):
    p = rng.uniform(0.08, 0.16) + rng.normal(0, 0.02, T)
    for _ in range(rng.poisson(T / 150)):
        s = rng.integers(0, T)
        p[s:s + rng.integers(3, 12)] = rng.uniform(0.7, 0.9)
    return p


def gen_steady_busy(rng, T):
    return rng.uniform(0.55, 0.75) + rng.normal(0, rng.uniform(0.02, 0.05), T)


def gen_bursty(rng, T):
    period, duty = rng.uniform(20, 90), rng.uniform(0.3, 0.6)
    on = ((np.arange(T) + rng.uniform(0, period)) % period) < duty * period
    return np.where(on, rng.uniform(0.7, 0.95), rng.uniform(0.1, 0.2)) + rng.normal(0, 0.03, T)


def gen_heavy(rng, T):
    return rng.uniform(0.85, 0.95) + rng.normal(0, 0.02, T)


GENERATORS = [gen_mostly_idle, gen_steady_busy, gen_bursty, gen_heavy]


def load_trace_nodes(csv_paths, n_per_trace, rng, t_out=T):
    """Measured time_s,power_W traces -> a fleet of normalized node rows.

    Each node is its source trace resampled to t_out ticks, circularly
    phase-shifted (diversifies the animation; the final CDF is order-
    agnostic), gain-jittered ±10% and noised — machines 'like' the measured
    one, not copies. Per-node normalization to own 2-98th percentile range
    matches the synthetic mode's X-axis semantics; note it deliberately makes
    absolute throttle LEVEL invisible — shape is what clusters."""
    power, labels, names = [], [], []
    for p in csv_paths:
        arr = np.atleast_2d(np.genfromtxt(p, delimiter=",", skip_header=1))
        if arr.ndim != 2 or arr.shape[1] < 2:
            print(f"  skip {p} (wrong shape)")
            continue
        arr = arr[np.all(np.isfinite(arr[:, :2]), axis=1)]   # drop truncated rows
        if arr.shape[0] < 10:
            print(f"  skip {p} (too short)")
            continue
        t, w = arr[:, 0], arr[:, 1]
        base = np.interp(np.linspace(t[0], t[-1], t_out), t, w)
        lo, hi = np.percentile(base, 2), np.percentile(base, 98)
        if hi - lo < 1e-9:
            print(f"  skip {p} (flat trace, no range to normalize)")
            continue
        base = np.clip((base - lo) / (hi - lo), 0.0, 1.0)
        for _ in range(n_per_trace):
            node = np.roll(base, rng.integers(0, t_out)) * rng.uniform(0.9, 1.1)
            node = node + rng.normal(0, 0.02, t_out)
            power.append(np.clip(node, 0.0, 1.0))
            labels.append(len(names))
        names.append(Path(p).stem.removeprefix("rapl_"))
    if not power:
        sys.exit("no usable traces found")
    return np.array(power), np.array(labels), names


def simulate(seed=SEED):
    rng = np.random.default_rng(seed)
    power, labels = [], []
    for a, gen in enumerate(GENERATORS):
        for _ in range(N_PER_ARCHETYPE):
            power.append(np.clip(gen(rng, T), 0.0, 1.0))
            labels.append(a)
    return np.array(power), np.array(labels)  # (N,T), (N,)


# ---- CDF curves and binned surface ----
def cdf_frames(power):
    """Y[f, n, k] = frac of time in [0, t_f] node n spent <= threshold k."""
    n_t = power.shape[1]
    thr = np.linspace(0.02, 1.0, N_XBINS)
    below = power[:, :, None] <= thr[None, None, :]           # (N, T, K)
    cum = np.cumsum(below, axis=1)
    ft = np.linspace(n_t / N_FRAMES, n_t, N_FRAMES).astype(int) - 1
    Y = cum[:, ft, :] / (ft + 1)[None, :, None]               # (N, F, K)
    return thr * 100, ft, Y.transpose(1, 0, 2)                # x in %, (F, N, K)


def bin_surface(Y):
    """Z[f, x, y] = node count per (X, Y) cell. One Y-bin per node per X-column."""
    F, N, Kx = Y.shape
    yb = np.minimum((Y * N_YBINS).astype(int), N_YBINS - 1)
    Z = np.zeros((F, Kx, N_YBINS), dtype=int)
    for f in range(F):
        for k in range(Kx):
            Z[f, k] = np.bincount(yb[f, :, k], minlength=N_YBINS)
    return Z


def smooth_surface(Z):
    """Smooth raw counts into a continuous node-density landscape.

    Smooths across (time, x, y) so the surface is a ridge landscape per frame
    AND evolves steadily frame-to-frame; rounding shrinks the HTML payload.
    """
    return np.round(gaussian_filter(Z.astype(float), SMOOTH_SIGMA), 2)


# ---- clustering ----
def cluster(Y_final, k=K, seed=SEED):
    centroids, assign = kmeans2(Y_final, k, minit="++", seed=seed)
    elbow = {}
    for kk in range(2, 9):
        c, a = kmeans2(Y_final, kk, minit="++", seed=seed)
        elbow[kk] = float(((Y_final - c[a]) ** 2).sum())
    return centroids, assign, elbow


def confusion(assign, labels, k=K, n_classes=None):
    """Map each cluster to its majority class; return (accuracy, table)."""
    if n_classes is None:
        n_classes = len(ARCHETYPES)
    table = np.zeros((k, n_classes), dtype=int)
    for c, a in zip(assign, labels):
        table[c, a] += 1
    acc = table.max(axis=1).sum() / len(labels)
    return acc, table


# ---- figure ----
def build_figure(x_pct, ft, Z, centroids, assign, labels, Y_final,
                 class_names=ARCHETYPES, source="synthetic"):
    import plotly.graph_objects as go
    k = len(centroids)

    def z_on_surface(y_curve, lift):
        coords = np.vstack([np.arange(N_XBINS),
                            np.clip(y_curve * N_YBINS - 0.5, 0, N_YBINS - 1)])
        return map_coordinates(Z[0], coords, order=1) + lift

    y_centers = (np.arange(N_YBINS) + 0.5) / N_YBINS
    zmax = float(Z.max())
    surf = dict(x=x_pct, y=y_centers, colorscale="Viridis", cmin=0, cmax=zmax,
                colorbar=dict(title="node density"), showscale=True)

    data = [go.Surface(z=Z[0].T, **surf)]
    # final-time cluster centroid curves, hovering just above the surface
    acc_table = np.zeros((k, len(class_names)), dtype=int)
    for c, a in zip(assign, labels):
        acc_table[c, a] += 1
    rng = np.random.default_rng(SEED)
    for c in range(k):
        name = f"cluster {c}: {class_names[acc_table[c].argmax()]} (n={int((assign == c).sum())})"
        yc = gaussian_filter1d(centroids[c], 2.0)
        # sample the smoothed surface along the curve (bilinear) instead of snapping to bins
        data.append(go.Scatter3d(
            x=x_pct, y=yc, z=z_on_surface(yc, zmax * 0.03), mode="lines",
            line=dict(color=COLORS[c % len(COLORS)], width=8), name=name,
            legendgroup=f"c{c}"))
        # spaghetti: a few real node curves per cluster so the four families read as families
        members = np.where(assign == c)[0]
        for i in rng.choice(members, size=min(8, len(members)), replace=False):
            data.append(go.Scatter3d(
                x=x_pct, y=Y_final[i], z=z_on_surface(Y_final[i], zmax * 0.02),
                mode="lines", line=dict(color=COLORS[c % len(COLORS)], width=2), opacity=0.35,
                legendgroup=f"c{c}", showlegend=False, hoverinfo="skip"))

    frames = [go.Frame(data=[go.Surface(z=Z[f].T, **surf)],
                       name=f"{ft[f] + 1}s") for f in range(len(ft))]

    steps = [dict(method="animate", label=fr.name,
                  args=[[fr.name], dict(mode="immediate",
                                        frame=dict(duration=0, redraw=True),
                                        transition=dict(duration=0))])
             for fr in frames]
    fig = go.Figure(data=data, frames=frames)
    fig.update_layout(
        title=f"Fleet power-CDF surface — N={len(assign)} {source} nodes, k-means k={k}",
        annotations=[dict(
            text="How to read: one line = one node's power CDF. Climbs at LOW X → idle-heavy node. "
                 "Flat until HIGH X → heavy-sustained. Mid-height plateau → bursty duty cycle. "
                 "Click a legend entry to toggle that cluster.",
            xref="paper", yref="paper", x=0.5, y=1.045, xanchor="center",
            showarrow=False, font=dict(size=12), align="center")],
        scene=dict(
            xaxis=dict(title="X: power threshold (% of node's own range)", range=[0, 100]),
            yaxis=dict(title="Y: fraction of time below X", range=[0, 1]),
            zaxis=dict(title="Z: node count", range=[0, zmax * 1.1]),
            # origin (0,0,0) at the front-left corner so curves rise away like a 2D CDF
            camera=dict(eye=dict(x=-1.6, y=-1.6, z=0.9)),
            aspectmode="cube"),
        legend=dict(x=0.01, y=0.99),
        updatemenus=[dict(
            type="buttons", direction="left", x=0.1, y=0, xanchor="right", yanchor="top",
            buttons=[
                dict(label="Play", method="animate",
                     args=[None, dict(frame=dict(duration=80, redraw=True),
                                      fromcurrent=True, transition=dict(duration=0))]),
                dict(label="Pause", method="animate",
                     args=[[None], dict(mode="immediate",
                                        frame=dict(duration=0, redraw=True))]),
            ])],
        sliders=[dict(steps=steps, x=0.1, y=0, len=0.9,
                      currentvalue=dict(prefix="sim time: "))])
    return fig


# ---- selfcheck (also serves as Step 3 quantitative validation) ----
def selfcheck():
    power, labels = simulate()
    x_pct, ft, Y = cdf_frames(power)
    assert Y.min() >= 0 and Y.max() <= 1, "Y out of [0,1]"
    assert np.all(np.diff(Y, axis=2) >= -1e-12), "CDF not monotone in X"
    assert np.allclose(Y[-1, :, -1], 1.0), "Y at X=100% must be 1"
    Z = bin_surface(Y)
    assert np.all(Z.sum(axis=2) == power.shape[0]), "each X-column must count every node once"
    centroids, assign, elbow = cluster(Y[-1])
    acc, table = confusion(assign, labels)
    print("elbow (k: within-cluster SS):",
          {k: round(v, 1) for k, v in elbow.items()})
    print("confusion (rows=cluster, cols=archetype " + str(ARCHETYPES) + "):")
    print(table)
    print(f"cluster->archetype recovery accuracy: {acc:.3f}")
    assert acc >= 0.9, f"archetypes blur together (accuracy {acc:.3f} < 0.9)"

    # --real path: two obviously different trace fixtures -> full recovery
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tt = np.arange(0, 120, 0.1)
        square = np.where((tt % 20) < 10, 300.0, 80.0)
        steady = np.full_like(tt, 250.0) + 5 * np.sin(tt)
        for fn, w in (("sq.csv", square), ("fl.csv", steady)):
            np.savetxt(os.path.join(td, fn), np.column_stack([tt, w]),
                       delimiter=",", header="time_s,power_W")
        pw, lb, nm = load_trace_nodes(
            [os.path.join(td, "sq.csv"), os.path.join(td, "fl.csv")],
            20, np.random.default_rng(0))
        assert pw.shape == (40, T) and pw.min() >= 0.0 and pw.max() <= 1.0
        assert nm == ["sq", "fl"]
        _, _, Yr = cdf_frames(pw)
        _, asg, _ = cluster(Yr[-1], k=2, seed=0)
        accr, _ = confusion(asg, lb, k=2, n_classes=2)
        assert accr >= 0.95, f"real-mode fixture recovery {accr:.3f} < 0.95"
        print(f"real-mode fixture recovery accuracy: {accr:.3f}")

    print("SELFCHECK PASS")
    return acc, table


def main():
    if "--selfcheck" in sys.argv:
        selfcheck()
        return

    if "--real" in sys.argv:
        paths = sorted(globmod.glob(REAL_GLOB))
        print(f"building fleet from {len(paths)} measured traces ({N_PER_TRACE} nodes each) ...")
        power, labels, class_names = load_trace_nodes(
            paths, N_PER_TRACE, np.random.default_rng(SEED))
        source = "measured-trace"
        print("traces:", ", ".join(class_names))
    else:
        print(f"simulating {4 * N_PER_ARCHETYPE} nodes x {T}s ...")
        power, labels = simulate()
        class_names, source = ARCHETYPES, "synthetic"

    k = len(class_names)
    x_pct, ft, Y = cdf_frames(power)
    Z = smooth_surface(bin_surface(Y))
    centroids, assign, elbow = cluster(Y[-1], k=k)
    acc, _ = confusion(assign, labels, k=k, n_classes=k)
    print(f"k-means k={k}: class recovery accuracy {acc:.3f}; "
          f"elbow SS: { {kk: round(v) for kk, v in elbow.items()} }")
    if source == "measured-trace" and acc < 0.9:
        print("  (expected for the throttle family: per-node normalization makes"
              " absolute level invisible — same-shape plateaus share a cluster)")
    fig = build_figure(x_pct, ft, Z, centroids, assign, labels, Y[-1],
                       class_names=class_names, source=source)
    here = os.path.dirname(os.path.abspath(__file__))
    html = os.path.join(here, "index.html")
    fig.write_html(html, include_plotlyjs=True, auto_play=False)
    print(f"wrote {html} ({os.path.getsize(html) // 1024} KB)")
    if "--no-browser" not in sys.argv:
        url = f"http://127.0.0.1:{PORT}/index.html"
        try:
            webbrowser.get('firefox').open(url)
        except webbrowser.Error:
            print("Firefox not found, falling back to default browser...")
            webbrowser.open(url)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=here)
    with http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler) as srv:
        print(f"serving http://127.0.0.1:{PORT}/index.html  (Ctrl-C to stop)")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
