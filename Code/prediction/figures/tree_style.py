#!/usr/bin/env python3
"""Shared print styling for the RF tree schematics (paper Figures 4 and 6).

Both schematics used to draw at figsize 16-20in with 11-13pt text. Dropped into
a paper's ~6.4in text column that scales the type down to roughly 4.5pt, which
is unreadable in print. Drawing the same tree in a smaller box with bigger type
fixes it, but then long feature names overflow plot_tree's fixed leaf slots and
the sibling boxes collide -- so names wrap and huge thresholds get shortened.
"""
import re

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Patch
from sklearn.tree import plot_tree

FIGSIZE = (16, 7.5)   # ~2.46:1 -- at 6.4in wide the type lands near 6.5pt
FONTSIZE = 16
NAME_LIMIT = 13       # chars before a feature name wraps


def wrap_name(name, limit=NAME_LIMIT):
    """Break a long feature name at the underscore nearest its middle."""
    cuts = [i for i, c in enumerate(name) if c == "_"]
    if len(name) <= limit or not cuts:
        return name
    i = min(cuts, key=lambda c: abs(c - len(name) / 2))
    return name[:i + 1] + "\n" + name[i + 1:]  # keep the "_" so it still reads


def short(value):
    """1055685214208.0 is a box-widening eyesore; 1.056e+12 is the same number."""
    try:
        f = float(value)
    except ValueError:
        return value
    return f"{f:.4g}" if abs(f) >= 1e5 else value


def render(est, cols, out, depth=3, figsize=FIGSIZE, fontsize=FONTSIZE):
    """Draw the top `depth` levels of one estimator, print-legible, to `out`."""
    fig, ax = plt.subplots(figsize=figsize)
    annots = plot_tree(est, max_depth=depth, feature_names=list(cols),
                       class_names=["no spike", "spike"], filled=True,
                       rounded=True, impurity=False, proportion=True,
                       fontsize=fontsize, ax=ax)

    # Strip the reweighted samples/value lines -- balanced class weights make
    # them read as "spikes are 78% of the data", the opposite of the rare-event
    # framing. A schematic should show the SPLITS and the leaf CLASS, nothing
    # else; the legend below carries the colour -> class mapping.
    for a in annots:
        kept = [ln for ln in a.get_text().splitlines()
                if not ln.strip().startswith(("samples", "value"))]
        kept = [ln.replace("class = ", "") for ln in kept]
        lines = []
        for ln in kept:
            m = re.match(r"^(.*?) (<=|<|>=|>) (.*)$", ln)
            lines.append(f"{wrap_name(m.group(1))}\n{m.group(2)} {short(m.group(3))}"
                         if m else ln)
        a.set_text("\n".join(lines))

    # The "(...)" stubs below the last drawn level carry no information and just
    # read as a broken tree in print -- hide them (hides their arrows too).
    for a in annots:
        if a.get_text().strip() == "(...)":
            a.set_visible(False)

    ax.legend(
        handles=[Patch(facecolor="#e58139", edgecolor="#2b2b2b", label='predicts "no spike"'),
                 Patch(facecolor="#399de5", edgecolor="#2b2b2b", label='predicts "spike"')],
        loc="lower center", ncol=2, frameon=False, fontsize=fontsize,
        title="box shade = how one-sided the node is", title_fontsize=fontsize - 1,
        bbox_to_anchor=(0.5, -0.04),
    )
    # hiding the stub row leaves an empty band; crop the axes to what is drawn
    ys = [a.get_position()[1] for a in annots if a.get_visible()]
    if ys:
        ax.set_ylim(min(ys) - 0.10, max(ys) + 0.06)

    fig.tight_layout()
    fig.savefig(out, dpi=200, bbox_inches="tight")
    plt.close(fig)

    # plot_tree reserves a row for the hidden stubs; collapse the blank band it
    # leaves so the legend sits under the tree instead of a field of white.
    from PIL import Image
    a = np.array(Image.open(out).convert("RGB"))
    blank = (a > 250).all(axis=(1, 2))
    keep, run = np.ones(len(blank), bool), 0
    for i, b in enumerate(blank):
        run = run + 1 if b else 0
        if run > 40:
            keep[i] = False
    Image.fromarray(a[keep]).save(out)
    return out
