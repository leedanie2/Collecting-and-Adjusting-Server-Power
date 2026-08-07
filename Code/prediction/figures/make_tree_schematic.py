#!/usr/bin/env python3
"""Render a paper schematic of the shipped RF: the top 3 levels of one real tree.

The champion is 200 fully-grown trees (max_depth=None) -- a whole tree is
unreadable. plot_tree(..., max_depth=3) shows the top splits of a single real
estimator, which is what a schematic should be: honest structure, not a toy.

Run from prediction/ (it needs data/models/ on the path):

  .venv/bin/python figures/make_tree_schematic.py            # tree 0
  .venv/bin/python figures/make_tree_schematic.py 7          # another estimator

Writes rf_tree.png next to this script and prints the same tree as text.
"""
import os
import pickle
import sys

import matplotlib
matplotlib.use("Agg")
from sklearn.tree import export_text

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tree_style

MODEL = "data/models/spike_model_current"
DEPTH = 3  # levels to show; the real trees go far deeper

which = int(sys.argv[1]) if len(sys.argv) > 1 else 0
d = pickle.load(open(MODEL, "rb"))
rf, cols = d["model"], d["cols"]
est = rf.estimators_[which]

# text version -- easy to eyeball / recreate by hand
print(export_text(est, feature_names=list(cols), max_depth=DEPTH,
                  show_weights=True))

# No title: this is a captioned figure in the paper, and a second title on the
# image just eats vertical space.
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rf_tree.png")
tree_style.render(est, cols, out, depth=DEPTH)
print(f"\nwrote {out}")
