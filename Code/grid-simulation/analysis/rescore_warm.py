#!/usr/bin/env python3
"""Rescore the n=4 matrix and the fleet-size sweep without the model's start-up.

Two properties of microgrid_phasor.slx dominate several published grid numbers:

  1. Cold start. The Dynamic Load starts every run at a fixed 4.104 MW (the
     load-flow init baked at build time), whatever the trace's real level, so
     every S_PCC begins with a multi-MW step. The frequency nadir lands at
     t ~ 3.3 s in all twelve reference cells.
  2. Stale operating point. Const_P0 -- the point the swing equation deviates
     from -- is also baked at build time (4.4655 MW, fitted below), not set to
     each trace's mean as the comment in build_microgrid.m intends. Every run
     therefore carries a permanent phantom load step of (mean load - 4.47 MW),
     which grows with fleet size: ~10 MW at N=20,000, hence the 56.56 Hz nadir.

The model downstream of the UPS is linear, so it is replicated here in Python:
the readscript.m facility chain (x N, +300 W, variable PUE/PF, 15 s UPS
low-pass), the swing-equation transfer function from build_microgrid.m, and the
Thevenin source (25 kV, 100 MVA, X/R 7) for PCC voltage. --selfcheck asserts the
replica reproduces the checked-in Simulink outputs (results/<cell>_worst/) before
anything is rescored: CV ratio 1.00 after the warm-up, |V| within 1e-3 pu, and
frequency within 2 mHz RMS once Simulink's own P0 is used.

Rescoring then (a) sets P0 to each trace's own mean, (b) starts the swing
equation settled at the trace's first value (no cold start), and (c) discards the
first WARMUP_S seconds (2 x tau_ups), which also removes the one-time job start
that the UPS stretches over ~3 tau. 45 s is reported alongside as a sensitivity
check. Runtime/energy/Gflops are unaffected and stay in summarize_n4.py.

    python3 analysis/rescore_warm.py            # -> data/summary/rescored/
    python3 analysis/rescore_warm.py --selfcheck
"""
import argparse, csv, json, sys
from pathlib import Path

import numpy as np
from scipy import signal

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from trim_auto import load, detect_window, trim  # noqa: E402

RUNS = [1, 2, 3, 4]
WORKLOADS = ["hpl", "aisim2", "step"]
SMOOTHERS = ["powersmoother", "rampc", "usagegov"]
OUT = ROOT / "data" / "summary" / "rescored"

WARMUP_S = 30.0                 # 2 x tau_ups
TAU_UPS = 15.0                  # readscript.m
H, D, R_DROOP, TAU_GOV = 2.5, 1.0, 0.05, 20.0   # build_microgrid.m / readscript.m
F0, S_BASE = 60.0, 50e6
V_LL, SCL, XR = 25e3, 100e6, 7.0
SIMULINK_P0 = 4.4655e6          # fitted Const_P0 baked into the checked-in .slx
DT = 0.01

_Z = V_LL**2 / SCL
_RS = _Z / np.sqrt(1 + XR**2)
_XS = _RS * XR
SWING = signal.lti([-TAU_GOV, -1.0], [2*H*TAU_GOV, 2*H + D*TAU_GOV, D + 1/R_DROOP])


def facility(t, p, n_servers=10000):
    """readscript.m steps 3-6: facility P and Q seen by the grid, post-UPS."""
    agg = n_servers * (p + 300.0)
    frac = agg / agg.max()
    raw = agg * (1.60 - 0.45 * frac)
    out = np.empty_like(raw)
    out[0] = raw[0]
    for k in range(1, len(t)):
        a = np.exp(-(t[k] - t[k-1]) / TAU_UPS)
        out[k] = a * out[k-1] + (1 - a) * raw[k]
    return out, out * np.tan(np.arccos(0.85 + 0.12 * frac))


def freq_dev(t, P, p0, settle_s=0.0):
    """Swing-equation frequency deviation (Hz) on a uniform DT grid.
    settle_s > 0 holds the first value that long first, so the run starts at
    equilibrium instead of from a step."""
    tg = np.arange(t[0], t[-1], DT)
    u = (np.interp(tg, t, P) - p0) / S_BASE * F0
    n = int(settle_s / DT)
    u = np.r_[np.full(n, u[0]), u]
    _, y, _ = signal.lsim(SWING, u, np.arange(len(u)) * DT)
    return tg, y[n:]


def v_pcc(P, Q):
    """|V| (pu) at a constant-PQ load behind Rs + jXs from a stiff 1 pu source."""
    S = P + 1j * Q
    V = np.full(len(P), V_LL, dtype=complex)
    for _ in range(60):
        V = V_LL - (_RS + 1j * _XS) * np.conj(S / V)
    return np.abs(V) / V_LL


def cv_after(t, P, skip):
    tg = np.arange(t[0] + skip, t[-1], 0.1)
    x = np.interp(tg, t, P)
    return float(x.std() / x.mean())


def trace(workload, cond, run):
    rows = load(ROOT / "data" / "runs" / f"run{run}" / f"{workload}_{cond}.csv")
    if cond == "rampc":   # score the plateau, as summarize_n4.py does
        t0, t1, *_ = detect_window(rows)
        rows = trim(rows, t0, t1)
    a = np.array(rows)
    return a[:, 0], a[:, 1]


def score(t, p, n_servers=10000, skip=WARMUP_S):
    P, Q = facility(t, p, n_servers)
    tg = np.arange(t[0], t[-1], 0.1)
    Pg, Qg = np.interp(tg, t, P), np.interp(tg, t, Q)
    tf, df = freq_dev(tg, Pg, p0=Pg.mean(), settle_s=200.0)
    k, kf = tg >= tg[0] + skip, tf >= tf[0] + skip
    V = v_pcc(Pg, Qg)
    return {
        "cv": float(Pg[k].std() / Pg[k].mean()),
        "nadir_hz": float(F0 + df[kf].min()),
        "rocof_hz_s": float(np.abs(np.gradient(df[kf], tf[kf])).max()),
        "min_v_pu": float(V[k].min()),
        "mean_MW": float(Pg[k].mean() / 1e6),
    }


def selfcheck():
    res = ROOT / "results"
    for w in WORKLOADS:
        for m in ["baseline"] + SMOOTHERS:
            d = res / f"{w}_{m}_worst"
            s = np.loadtxt(d / "S_PCC.csv", delimiter=",")
            f = np.loadtxt(d / "freq_dev.csv", delimiter=",")
            v = np.loadtxt(d / "V_PCC.csv", delimiter=",")
            rows = load(ROOT / "data" / "traces" / f"{w}_{m}.csv")
            if m == "rampc":
                t0, t1, *_ = detect_window(rows)
                tr = trim(rows, t0, t1)
                if t1 - t0 < 0.9 * (rows[-1][0] - rows[0][0]):
                    rows = tr
            a = np.array(rows)
            P, _ = facility(a[:, 0], a[:, 1])
            ratio = cv_after(a[:, 0], P, WARMUP_S) / cv_after(s[:, 0], s[:, 1], WARMUP_S)
            assert abs(ratio - 1) < 0.06, f"{w}_{m}: CV ratio {ratio:.3f}"
            tg, y = freq_dev(s[:, 0], s[:, 1], p0=SIMULINK_P0)
            rms = np.sqrt(np.mean((np.interp(tg, f[:, 0], f[:, 1]) - y) ** 2))
            assert rms < 2e-3, f"{w}_{m}: freq rms {rms*1e3:.2f} mHz"
            k = s[:, 0] > 1
            dv = np.abs(np.interp(s[:, 0], v[:, 0], v[:, 1]) - v_pcc(s[:, 1], s[:, 2]))[k].max()
            assert dv < 1e-3, f"{w}_{m}: |dV| {dv:.4f} pu"
            print(f"  {w}_{m:14s} CV ratio {ratio:.3f}  freq rms {rms*1e3:.2f} mHz  max|dV| {dv:.4f} pu")
    print("selfcheck OK: replica reproduces the checked-in Simulink outputs")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selfcheck", action="store_true")
    if ap.parse_args().selfcheck:
        return selfcheck()

    OUT.mkdir(parents=True, exist_ok=True)
    matrix, board = [], []
    for skip in (WARMUP_S, 45.0):
        per = {}
        for r in RUNS:
            for w in WORKLOADS:
                b = score(*trace(w, "baseline", r), skip=skip)
                for m in SMOOTHERS:
                    s = score(*trace(w, m, r), skip=skip)
                    row = {"warmup_s": skip, "run": r, "workload": w, "mitigation": m,
                           "cv_pct": round(100 * (s["cv"] / b["cv"] - 1), 2),
                           "min_v_delta_pu": round(s["min_v_pu"] - b["min_v_pu"], 5)}
                    matrix.append(row)
                    per.setdefault((w, m), []).append(row["cv_pct"])
        for m in SMOOTHERS:
            for w in WORKLOADS:
                v = per[(w, m)]
                board.append({"warmup_s": skip, "mitigation": m, "workload": w,
                              "cv_pct_mean": round(np.mean(v), 1), "cv_pct_sd": round(np.std(v, ddof=1), 1)})
            # mean over workloads per run, then mean +/- SD across runs (summarize_n4.py convention)
            runs = np.mean([per[(w, m)] for w in WORKLOADS], axis=0)
            board.append({"warmup_s": skip, "mitigation": m, "workload": "mean",
                          "cv_pct_mean": round(runs.mean(), 1), "cv_pct_sd": round(runs.std(ddof=1), 1)})

    sweep = []
    for n in (1000, 5000, 10000, 20000):
        s = [score(*trace(w, "baseline", r), n_servers=n) for w in WORKLOADS for r in RUNS]
        sweep.append({"N_servers": n,
                      "min_v_pu": round(min(x["min_v_pu"] for x in s), 4),
                      "nadir_hz": round(min(x["nadir_hz"] for x in s), 3),
                      "rocof_hz_s": round(max(x["rocof_hz_s"] for x in s), 4),
                      "mean_MW": round(np.mean([x["mean_MW"] for x in s]), 3)})

    for name, rows in (("matrix.csv", matrix), ("scoreboard.csv", board), ("sweep.csv", sweep)):
        with open(OUT / name, "w", newline="") as fh:
            wr = csv.DictWriter(fh, fieldnames=list(rows[0]))
            wr.writeheader()
            wr.writerows(rows)
    json.dump({"warmup_s": WARMUP_S, "simulink_p0_W": SIMULINK_P0, "tau_ups_s": TAU_UPS},
              open(OUT / "params.json", "w"), indent=2)
    for r in board:
        if r["workload"] == "mean":
            print(f"warmup {r['warmup_s']:>4.0f}s  {r['mitigation']:14s} CV {r['cv_pct_mean']:+6.1f} +/- {r['cv_pct_sd']:.1f}%")
    for r in sweep:
        print(f"N={r['N_servers']:>6}  min V {r['min_v_pu']:.4f} pu  nadir {r['nadir_hz']:.3f} Hz  ROCOF {r['rocof_hz_s']:.4f} Hz/s")
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
