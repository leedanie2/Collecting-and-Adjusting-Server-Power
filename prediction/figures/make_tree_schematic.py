#!/usr/bin/env python3
"""Render a paper schematic of the shipped RF: the top 3 levels of one real tree.

The champion is 200 fully-grown trees (max_depth=None) -- a whole tree is
unreadable. plot_tree(..., max_depth=3) shows the top splits of a single real
estimator, which is what a schematic should be: honest structure, not a toy.

  .venv/bin/python make_tree_schematic.py            # tree 0
  .venv/bin/python make_tree_schematic.py 7          # pick a different estimator

Writes /home/daniellee/rf_tree.png and prints the same tree as text.
"""
import pickle
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from sklearn.tree import export_text, plot_tree

MODEL = "data/models/spike_model_current"
DEPTH = 3  # levels to show; the real trees go far deeper

which = int(sys.argv[1]) if len(sys.argv) > 1 else 0
d = pickle.load(open(MODEL, "rb"))
rf, cols = d["model"], d["cols"]
est = rf.estimators_[which]

# text version -- easy to eyeball / recreate by hand
print(export_text(est, feature_names=list(cols), max_depth=DEPTH,
                  show_weights=True))

fig, ax = plt.subplots(figsize=(16, 9))
annots = plot_tree(
    est,
    max_depth=DEPTH,
    feature_names=list(cols),
    class_names=["no spike", "spike"],
    filled=True,
    rounded=True,
    impurity=False,
    proportion=True,
    fontsize=11,
    ax=ax,
)
# Strip the reweighted samples/value lines -- balanced class weights make them
# read as "spikes are 78% of data", the opposite of the rare-event framing.
# A schematic should show the SPLITS and the leaf CLASS, nothing else.
for a in annots:
    kept = [ln for ln in a.get_text().splitlines()
            if not ln.strip().startswith(("samples", "value"))]
    a.set_text("\n".join(kept))
ax.set_title(f"Random-forest spike detector: top {DEPTH} levels of tree "
             f"{which} of {rf.n_estimators}", fontsize=13)
fig.tight_layout()
out = "/home/daniellee/rf_tree.png"
fig.savefig(out, dpi=150, bbox_inches="tight")
print(f"\nwrote {out}")
