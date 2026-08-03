#!/usr/bin/env python3
"""Schematic for the *early* RF section: power + usage + KF features, NO upstream.

The champion pickle is the upstream model -- wrong figure for the section that
describes the pre-upstream RF. So train that earlier model honestly here:
feature_frame(kf=True, upstream=False), the same base+KF feature set CP3 used,
then render the top 3 levels of one tree. This is a schematic, not the shipped
model, so a small forest on a cache slice is fine.

Run from prediction/ (it needs core/ on the path):

  .venv/bin/python figures/make_early_tree.py         # tree 0
  .venv/bin/python figures/make_early_tree.py 5       # pick another estimator

Writes rf_tree_early.png next to this script and prints it as text.
"""
import os
import sys

import matplotlib
matplotlib.use("Agg")
from sklearn.ensemble import RandomForestClassifier
from sklearn.tree import export_text

import core.features as features

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tree_style

DEPTH = 3
which = int(sys.argv[1]) if len(sys.argv) > 1 else 0

df = features.load()
# A slice is plenty for a schematic and keeps this quick; take a work-hours
# chunk so the tree sees real spikes, not an idle stretch.
df = df.tail(120_000)
X, y = features.build_xy(df, lane_a=False, kf=True, upstream=False)
print(f"trained on {len(X):,} rows, {int(y.sum()):,} positives, {X.shape[1]} features")
print("features:", ", ".join(X.columns))

rf = RandomForestClassifier(
    n_estimators=200, class_weight="balanced", n_jobs=-1, random_state=0,
)
rf.fit(X, y)
est = rf.estimators_[which]

print("\ntop features by importance:")
for v, c in sorted(zip(rf.feature_importances_, X.columns), reverse=True)[:8]:
    print(f"  {c:24s} {v:.4f}")
print()
print(export_text(est, feature_names=list(X.columns), max_depth=DEPTH))

# No title: this is a captioned figure in the paper, and a second title on the
# image just eats vertical space.
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rf_tree_early.png")
tree_style.render(est, X.columns, out, depth=DEPTH)
print(f"wrote {out}")
