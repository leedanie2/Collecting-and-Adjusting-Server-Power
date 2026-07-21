#!/usr/bin/env python3
"""Golden-trace exporter for the C KF port (models/kalman/native/kf_predict.c).

Usage:
    .venv/bin/python kf_export.py [--out PATH] [--q Q] [--r R] [--paritycheck]

Runs kalmannet.ClassicalKF over a deterministic synthetic power trace (calm
idle noise, an HPL-style plateau step with PL2-shaped overshoot, a ramp, and a
single-sample outlier -- the steps exercise the innovation gate) and writes
every step's (z, pred_next, level, slope, innov, q, R) at full precision.
--paritycheck exports both adapt modes and drives `kf_predict --verify`.
"""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

from models.kalman.kalman_filter import ClassicalKF

DEFAULT_OUT = Path("data/kf_golden.csv")


def make_trace(n=3000):
    rng = np.random.default_rng(0)
    z = 200.0 + rng.normal(0, 0.4, n)           # calm idle floor
    z[500:900] += 240.0                         # HPL plateau step
    z[500:516] += 40.0                          # PL2-style overshoot head
    z[1500:1560] += np.linspace(0, 120, 60)     # ramp
    z[2200] += 600.0                            # single-sample outlier
    return z


def golden(out, q, r, adapt):
    z = make_trace()
    kf = ClassicalKF(q, r, z0=z[0], adapt=adapt)
    rows = []
    for zt in z:
        pred, lvl, innov = kf.step(zt)
        rows.append(f"{zt:.17g},{pred:.17g},{lvl:.17g},{kf.m[1]:.17g},"
                    f"{innov:.17g},{kf.q:.17g},{kf.R:.17g}")
    Path(out).write_text(
        f"kf_golden v1 q {q:.17g} r {r:.17g} adapt {int(adapt)} n {len(z)}\n"
        + "\n".join(rows) + "\n")
    print(f"wrote {out}: n={len(z)} q={q} r={r} adapt={adapt}")


def paritycheck(q, r, cbin):
    rc = 0
    for adapt in (False, True):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
            path = f.name
        golden(path, q, r, adapt)
        p = subprocess.run([cbin, "--verify", path],
                           capture_output=True, text=True)
        print(p.stdout.strip() or p.stderr.strip())
        rc = rc or p.returncode
    sys.exit(rc)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument("--q", type=float, default=0.1)
    p.add_argument("--r", type=float, default=100.0)
    p.add_argument("--adapt", action="store_true")
    p.add_argument("--paritycheck", action="store_true")
    p.add_argument("--cbin", default="./models/kalman/native/kf_predict")
    a = p.parse_args()
    if a.paritycheck:
        paritycheck(a.q, a.r, a.cbin)
    else:
        golden(a.out, a.q, a.r, a.adapt)


if __name__ == "__main__":
    main()
