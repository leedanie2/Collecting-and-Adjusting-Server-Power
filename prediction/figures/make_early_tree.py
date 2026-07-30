#!/usr/bin/env python3
"""Schematic for the *early* RF section: power + usage + KF features, NO upstream.

The champion pickle is the upstream model -- wrong figure for the section that
describes the pre-upstream RF. So train that earlier model honestly here:
feature_frame(kf=True, upstream=False), the same base+KF feature set CP3 used,
then render the top 3 levels of one tree. This is a schematic, not the shipped
model, so a small forest on a cache slice is fine.

  .venv/bin/python make_early_tree.py         # tree 0
  .venv/bin/python make_early_tree.py 5        # pick another estimator

Writes /home/daniellee/rf_tree_early.png and prints it as text.
"""
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.tree import export_text, plot_tree

import core.features as features

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

fig, ax = plt.subplots(figsize=(20, 9))
annots = plot_tree(
    est, max_depth=DEPTH, feature_names=list(X.columns),
    class_names=["no spike", "spike"], filled=True, rounded=True,
    impurity=False, proportion=True, fontsize=11, ax=ax,
)
# Drop the reweighted samples/value lines: with balanced weights they read as
# "spikes are the majority", the opposite of the rare-event framing.
for a in annots:
    kept = [ln for ln in a.get_text().splitlines()
            if not ln.strip().startswith(("samples", "value"))]
    a.set_text("\n".join(kept))

ax.set_title(f"Early random-forest spike detector (power + KF features): "
             f"top {DEPTH} levels of one of {rf.n_estimators} trees", fontsize=13)
fig.tight_layout()
out = "/home/daniellee/rf_tree_early.png"
fig.savefig(out, dpi=150, bbox_inches="tight")
print(f"wrote {out}")
