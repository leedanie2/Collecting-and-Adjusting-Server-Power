#!/usr/bin/env python3
"""Export the current RF spike model to a flat text forest for the C scorer.

Usage:
    .venv/bin/python rf_export.py [--model PATH] [--out PATH] [--paritycheck]

Writes data/models/spike_model_current.forest: plain-text tree arrays the C
evaluator (detectors/random_forest/native/rf_predict.c) walks. --paritycheck exports, then verifies the C
binary reproduces sklearn predict_proba on 200 random feature vectors.
"""

import argparse
import pickle
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

from core import telemetry as common

DEFAULT_MODEL = common.DATA_DIR / "models" / "spike_model_current"
DEFAULT_OUT = common.DATA_DIR / "models" / "spike_model_current.forest"


def export(model_path, out_path):
    with open(Path(model_path).resolve(), "rb") as f:
        d = pickle.load(f)
    model, cols, thr = d["model"], list(d["cols"]), float(d.get("threshold", 0.5))
    ver = Path(model_path).resolve().stem.replace("spike_model_", "")
    lines = ["rf_forest v1", f"version {ver}", f"threshold {thr:.9g}",
             f"ncols {len(cols)}", "cols " + " ".join(cols),
             f"ntrees {len(model.estimators_)}"]
    for est in model.estimators_:
        t = est.tree_
        lines.append(f"tree {t.node_count}")
        for n in range(t.node_count):
            counts = t.value[n][0]
            p1 = float(counts[1] / counts.sum()) if counts.sum() > 0 else 0.0
            lines.append(f"{t.feature[n]} {t.threshold[n]:.9g} "
                         f"{t.children_left[n]} {t.children_right[n]} {p1:.9g}")
    Path(out_path).write_text("\n".join(lines) + "\n")
    print(f"exported {ver}: {len(model.estimators_)} trees, {len(cols)} cols "
          f"-> {out_path}")
    return d, cols


def paritycheck(model_path, out_path, cbin):
    d, cols = export(model_path, out_path)
    rng = np.random.default_rng(0)
    X = rng.normal(0, 2, size=(200, len(cols)))
    want = d["model"].predict_proba(X)[:, 1]
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
        for row, p in zip(X, want):
            f.write(",".join(f"{v:.9g}" for v in row) + f",{p:.9g}\n")
        csv = f.name
    r = subprocess.run([cbin, "--verify", str(out_path), csv],
                       capture_output=True, text=True)
    print(r.stdout.strip() or r.stderr.strip())
    sys.exit(r.returncode)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--model", default=str(DEFAULT_MODEL))
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument("--paritycheck", action="store_true")
    p.add_argument("--cbin", default="./detectors/random_forest/native/rf_predict")
    a = p.parse_args()
    if a.paritycheck:
        paritycheck(a.model, a.out, a.cbin)
    else:
        export(a.model, a.out)


if __name__ == "__main__":
    main()
