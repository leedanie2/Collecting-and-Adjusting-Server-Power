#!/usr/bin/env python3
"""A classical Kalman filter for CPU power, with online q/r noise adaptation.

What this is
------------
A 2-state (level+slope) KF whose job is to hand *clean* kf_level/kf_slope
regime-awareness features to the RF daemons (`lane_a.feature_frame`, kf=True).
With adapt=True it re-estimates its noise model (q, r) online from the
innovation sequence, so the features get cleaner as more diverse load data
accumulates instead of relying on a one-time tune -- item-1 of the 2026-07-01b
session. The A/B here is fixed-tune vs online-adaptive KF vs naive persistence.

History: a sklearn-MLP "KalmanNet-lite" learned-gain arm lived here (menu E2
tier-3). It was RETIRED 2026-07-01 -- on 1 Hz it only won one-step RMSE by
denoising *less* (sliding toward persistence, the opposite of what the RF
features want), and it went unstable on high-freq data. True KalmanNet (torch
BPTT) is the only honest learned path and is not worth the dependency here; the
adaptive classical KF is the principled, stable way to make the noise model
improve over time. Recover the MLP from git history if ever needed.

Three jobs, all on the 1 Hz InfluxDB cache (`common.load_telemetry()`):
  1. one-step-ahead power prediction error (RMSE / MAE),
  2. noise filtering (denoising on a calm segment),
  3. behaviour at transients / ramps (tracking error near sharp di/dt).

State-space (both filters)
--------------------------
Local-level + trend (constant-velocity) kinematic model, dt = 1 s (1 Hz):
    state x = [level, slope]
    F = [[1, dt],[0, 1]]   H = [1, 0]
    Q = q * Qc   (white-noise-acceleration discretisation, scalar q)
    R = r        (scalar measurement noise)
A 3-state (level/slope/accel) variant is a one-line F/H change; YAGNI, not built
-- 1 Hz can't see the sub-second overshoot curvature that would justify it (that
is the Phase-2 RAPL job below).

Classical KF (fixed): textbook predict/update; (q, r) grid-tuned on a *training
prefix* by one-step RMSE, then frozen and run online on a held-out suffix.

Adaptive KF (adapt=True): same predict/update, but R is re-estimated from the
innovation variance and the process-noise scale q rides a normalized-innovation
consistency check -- both from PAST innovations only, so it stays strictly
causal. This is the arm lane_a actually uses for its RF features.

Run
---
    cd analysis
    .venv/bin/python kalmannet.py --selfcheck   # fast synthetic asserts
    .venv/bin/python kalmannet.py               # real 1 Hz A/B scoreboard

Phase 2 (high-freq RAPL overshoot) is scaffolded but GATED -- see `run_phase2`
at the bottom. Do not run it here; it needs a sudo rapl.py capture under load on
the shared box.
"""

import argparse
import sys

import numpy as np

from core import telemetry as common  # read-only: load_telemetry()

# ----------------------------------------------------------------------------
# state-space constants (2-state local level + trend, dt = 1s)
# ----------------------------------------------------------------------------
DT = 1.0
F = np.array([[1.0, DT], [0.0, 1.0]])
H = np.array([[1.0, 0.0]])
# continuous white-noise-acceleration discretisation (Bar-Shalom), scaled by q
QC = np.array([[DT**3 / 3.0, DT**2 / 2.0], [DT**2 / 2.0, DT]])
I2 = np.eye(2)


# ----------------------------------------------------------------------------
# classical Kalman filter
# ----------------------------------------------------------------------------
# online noise-adaptation constants (adapt=True). Innovation-matching a la Mehra:
# a slow EWMA so the noise model converges as more diverse load data accumulates
# (the whole point of item-1), clipped so a transient can't blow up the filter.
ADAPT_ALPHA = 0.02
# R_MIN raised 0.1 -> 1.0 (2026-07-09): the old floor sat far below any real
# measurement noise (tuned r=100; calm raw diff-std ~0.4 W), and once R pinned
# there S collapsed, NIS exploded, and q ran away 140-1000 live -> pass-through.
R_MIN, R_MAX = 1.0, 1e4
Q_MIN, Q_MAX = 1e-4, 1e3
# innovation gate (2026-07-09): winsorize innov^2 at GATE_NIS*S (~3 sigma)
# before it feeds the R/q adaptation, so a real power step nudges the noise
# model boundedly instead of kicking q by x1000. A sustained genuine noise
# regime change still converges: the clip ceiling rises with S as R adapts up.
GATE_NIS = 9.0


class ClassicalKF:
    """Textbook 2-state KF. step(z) returns (pred_next, filtered_level, innov)
    where pred_next is the one-step-ahead measurement prediction made AT this
    step (i.e. the model's guess for the *next* z).

    adapt=True turns on online q/r adaptation: R is re-estimated from the
    innovation variance (the identifiable term), and the process-noise scale q
    rides a normalized-innovation-squared (NIS) consistency check. Both use only
    PAST innovations, so the filter stays strictly causal (lane_a relies on
    this). The noise model therefore *improves over time* instead of being tuned
    once and frozen -- item-1's goal for cleaner kf_level/kf_slope RF features.
    R is the identified term; the q-scale is a stable NIS heuristic,
    full joint EM is the upgrade path if this proves too coarse."""

    def __init__(self, q, r, z0=0.0, adapt=False):
        self.q, self.r = float(q), float(r)
        self.Q = self.q * QC
        self.R = self.r
        self.m = np.array([z0, 0.0])
        self.P = np.eye(2) * 1e3  # diffuse prior
        self.K_last = np.zeros(2)
        self.adapt = bool(adapt)
        self.c_innov = float(r)  # running innovation-variance estimate, seeded at r
        self.nis = 1.0           # EWMA of normalized innovation^2 (target 1.0)

    def step(self, z):
        # predict
        m_pred = F @ self.m
        P_pred = F @ self.P @ F.T + self.Q
        # update
        innov = z - (H @ m_pred)[0]
        HPHt = (H @ P_pred @ H.T)[0, 0]
        S = HPHt + self.R
        K = (P_pred @ H.T)[:, 0] / S
        self.m = m_pred + K * innov
        self.P = (I2 - np.outer(K, H[0])) @ P_pred
        self.K_last = K
        if self.adapt:
            # R <- EWMA(innov^2) - HPHt keeps predicted S consistent with the
            # observed innovation variance; q drifts geometrically toward the
            # value that makes NIS=1 (sustained NIS>1 => under-modelled dynamics).
            # innov^2 is winsorized at GATE_NIS*S: an outlier innovation (a real
            # power step, not noise) must not blow up the noise model (see the
            # GATE_NIS note above; selfcheck (e) is the regression test).
            i2 = min(innov * innov, GATE_NIS * S)
            self.c_innov = (1 - ADAPT_ALPHA) * self.c_innov + ADAPT_ALPHA * i2
            self.R = float(np.clip(self.c_innov - HPHt, R_MIN, R_MAX))
            self.nis = (1 - ADAPT_ALPHA) * self.nis + ADAPT_ALPHA * (i2 / S)
            self.q = float(np.clip(self.q * self.nis ** ADAPT_ALPHA, Q_MIN, Q_MAX))
            self.Q = self.q * QC
        # one-step-ahead measurement prediction
        pred_next = (H @ (F @ self.m))[0]
        return pred_next, self.m[0], innov


def run_classical(z, q, r, adapt=False):
    """Run a classical KF online over z. Returns dict of aligned arrays.
    pred_next[t] is the prediction for z[t+1]; valid for t in [0, n-2].
    slope[t] is the filtered trend state (kf.m[1]) after processing z[t] --
    used by lane_a's regime-awareness features (kf_level/kf_slope).
    adapt=True re-estimates q/r online from the innovation sequence."""
    kf = ClassicalKF(q, r, z0=z[0], adapt=adapt)
    n = len(z)
    pred = np.full(n, np.nan)
    lvl = np.full(n, np.nan)
    slope = np.full(n, np.nan)
    innov = np.full(n, np.nan)
    for t in range(n):
        pred[t], lvl[t], innov[t] = kf.step(z[t])
        slope[t] = kf.m[1]
    return {"pred_next": pred, "level": lvl, "slope": slope, "innov": innov, "K_last": kf.K_last}


def tune_classical(z_train, q_grid=None, r_grid=None):
    """Grid-search (q, r) minimising one-step RMSE on the training prefix."""
    if q_grid is None:
        q_grid = [1e-3, 1e-2, 1e-1, 1.0, 10.0]
    if r_grid is None:
        r_grid = [1.0, 5.0, 25.0, 100.0]
    best = (None, None, np.inf)
    for q in q_grid:
        for r in r_grid:
            out = run_classical(z_train, q, r)
            rmse = _one_step_rmse(z_train, out["pred_next"])
            if rmse < best[2]:
                best = (q, r, rmse)
    return best[0], best[1]


# ----------------------------------------------------------------------------
# metrics
# ----------------------------------------------------------------------------
def _one_step_rmse(z, pred_next):
    """RMSE of pred_next[t] vs z[t+1]."""
    e = pred_next[:-1] - z[1:]
    e = e[np.isfinite(e)]
    return float(np.sqrt(np.mean(e * e)))


def _one_step_mae(z, pred_next):
    e = np.abs(pred_next[:-1] - z[1:])
    e = e[np.isfinite(e)]
    return float(np.mean(e))


def persistence_metrics(z):
    """Naive baseline: predict z[t+1] = z[t] (carries all measurement noise)."""
    pred = np.empty(len(z))
    pred[:-1] = z[:-1]
    pred[-1] = np.nan
    return _one_step_rmse(z, pred), _one_step_mae(z, pred)


def noise_reduction(z, level, calm_quantile=0.25, win=600):
    """Denoising on the calmest window: on a near-flat segment the true signal is
    ~constant, so step-to-step variation is mostly noise. Report std of the
    filtered first-difference vs raw first-difference (lower = more noise
    rejected). Returns (raw_std, filt_std, ratio)."""
    d = np.abs(np.diff(z))
    # find the calmest contiguous window of length `win`
    if len(z) <= win:
        s, e = 0, len(z)
    else:
        roll = np.convolve(d, np.ones(win) / win, mode="valid")
        s = int(np.argmin(roll))
        e = s + win
    raw_std = float(np.std(np.diff(z[s:e])))
    filt_std = float(np.std(np.diff(level[s:e])))
    ratio = filt_std / raw_std if raw_std > 0 else np.nan
    return raw_std, filt_std, ratio


def transient_error(z, pred_next, n_events=50, half_win=3):
    """Mean abs one-step error in +/-half_win windows around the sharpest 1 Hz
    ramps (largest |di/dt|). Lower = better tracking through transients. At 1 Hz
    we only see ramps, not the sub-second overshoot (Phase 2)."""
    d = np.abs(np.diff(z))
    idx = np.argsort(d)[-n_events:]
    errs = []
    for i in idx:
        lo, hi = max(0, i - half_win), min(len(z) - 1, i + half_win)
        e = np.abs(pred_next[lo:hi] - z[lo + 1:hi + 1])
        errs.append(e[np.isfinite(e)])
    if not errs:
        return np.nan
    return float(np.mean(np.concatenate(errs)))


# ----------------------------------------------------------------------------
# data helpers
# ----------------------------------------------------------------------------
def longest_contiguous_power(df, step_s=1):
    """Return the longest run of power_watts where consecutive timestamps are
    ~step_s apart and non-null (clean dt for the kinematic model)."""
    p = df["power_watts"].dropna()
    dt = p.index.to_series().diff().dt.total_seconds().to_numpy().copy()
    dt[0] = step_s
    # split where the gap deviates from step_s
    breaks = np.where(np.abs(dt - step_s) > 0.5)[0]
    bounds = np.concatenate([[0], breaks, [len(p)]])
    best_lo, best_hi, best_len = 0, len(p), 0
    for a, b in zip(bounds[:-1], bounds[1:]):
        if b - a > best_len:
            best_lo, best_hi, best_len = a, b, b - a
    return p.iloc[best_lo:best_hi].to_numpy(dtype=float)


# ----------------------------------------------------------------------------
# 1 Hz A/B
# ----------------------------------------------------------------------------
def run_1hz(train_n=30000, test_n=60000):
    df = common.load_telemetry()
    z = longest_contiguous_power(df)
    if len(z) < train_n + 1000:
        train_n = len(z) // 2
    z_train = z[:train_n]
    z_test = z[train_n:train_n + test_n] if test_n else z[train_n:]
    print(f"Loaded {len(df)} rows; longest contiguous power run = {len(z)} samples.")
    print(f"train = {len(z_train)} samples (causal prefix), "
          f"test = {len(z_test)} samples (held-out suffix)\n")

    # tune the fixed KF on train only; the adaptive KF starts from the same seed
    q, r = tune_classical(z_train)
    print(f"Tuned classical KF: q={q}, r={r} (adaptive arm seeds from these)\n")

    # run all three on the held-out suffix
    cls = run_classical(z_test, q, r)
    adp = run_classical(z_test, q, r, adapt=True)
    p_rmse, p_mae = persistence_metrics(z_test)

    rows = [("persistence (raw z[t])", p_rmse, p_mae, "-", "-")]
    for name, out in (("classical KF (fixed)", cls), ("adaptive KF (q/r online)", adp)):
        rmse = _one_step_rmse(z_test, out["pred_next"])
        mae = _one_step_mae(z_test, out["pred_next"])
        _, _, nr = noise_reduction(z_test, out["level"])
        te = transient_error(z_test, out["pred_next"])
        rows.append((name, rmse, mae, nr, te))

    raw_std, _, _ = noise_reduction(z_test, z_test)  # raw reference
    print("=== 1 Hz one-step A/B (held-out suffix) ===")
    print(f"{'model':<26}{'RMSE(W)':>10}{'MAE(W)':>10}"
          f"{'noiseRatio':>12}{'transErr(W)':>13}")
    for name, rmse, mae, nr, te in rows:
        nr_s = f"{nr:.3f}" if isinstance(nr, float) else f"{nr:>}"
        te_s = f"{te:.3f}" if isinstance(te, float) else f"{te:>}"
        print(f"{name:<26}{rmse:>10.4f}{mae:>10.4f}{nr_s:>12}{te_s:>13}")
    print(f"\nnoiseRatio = std(diff(filtered)) / std(diff(raw)) on calmest window "
          f"(raw diff std = {raw_std:.4f} W; <1 means noise rejected).")
    print("transErr   = mean abs one-step error near the 50 sharpest ramps.\n")

    # honest verdict: this is a DENOISER for the RF's kf_level/kf_slope features,
    # not a spike predictor (forecasting is dead at 1 Hz).
    cls_nr, adp_nr = rows[1][3], rows[2][3]
    print("--- verdict ---")
    print(f"noiseRatio  fixed {cls_nr:.3f}  vs  adaptive {adp_nr:.3f} "
          "(lower = cleaner features).")
    if p_rmse <= min(rows[1][1], rows[2][1]):
        print("Persistence still ties/wins on one-step RMSE -- expected: 1 Hz power "
              "is flat plateaus punctuated by un-anticipatable jumps, so a smoother "
              "lags them. RMSE is NOT the metric that matters here; the KF earns its "
              "keep by DENOISING (noiseRatio < 1), which persistence cannot do.")
    print("Adaptive q/r converges the noise model from the innovation sequence as "
          "load diversity accumulates, so kf_level/kf_slope stay clean across "
          "regimes without a re-tune. The deciding test is the live "
          "champion-challenger gate in spike_daemon_rf_continual.py on mycroft, "
          "not this cached A/B -- here we only confirm the adaptive arm is stable "
          "and denoises at least as well as the fixed tune.")


# ----------------------------------------------------------------------------
# Phase 2: high-freq RAPL overshoot  (GATED -- do NOT run here)
# ----------------------------------------------------------------------------
def set_dt(dt):
    """Rebuild the kinematic F/QC globals for sample interval `dt` seconds.
    Phase-2 RAPL is ~10x faster than 1 Hz; leaving DT=1 would mis-scale the
    constant-velocity process model (the trend term carries `dt` seconds of
    drift per step). Call ONCE before constructing any ClassicalKF."""
    global DT, F, QC
    DT = float(dt)
    F = np.array([[1.0, DT], [0.0, 1.0]])
    QC = np.array([[DT**3 / 3.0, DT**2 / 2.0], [DT**2 / 2.0, DT]])


def load_rapl_csv(path):
    """Ingest a high-freq RAPL capture. Accepts two schemas:
      - full `Intel RAPL Code/rapl.py` output: mono_s,...,watts
      - simple 10 Hz logger:                   time_s,power_W
    Returns (t_seconds, watts) numpy arrays. The sub-second cadence resolves the
    load-onset turbo transient that the 1 Hz Nyquist floor (2 s) cannot see."""
    import pandas as pd
    df = pd.read_csv(path)
    if "mono_s" in df.columns:          # full rapl.py schema
        return df["mono_s"].to_numpy(float), df["watts"].to_numpy(float)
    if {"time_s", "power_W"} <= set(df.columns):  # simple 10 Hz logger
        return df["time_s"].to_numpy(float), df["power_W"].to_numpy(float)
    raise ValueError(f"unrecognised RAPL csv columns: {list(df.columns)}")


def run_phase2(path, train_frac=0.3):
    """Phase-2: fixed-vs-adaptive-vs-persistence A/B on a high-freq RAPL capture,
    with the process model rescaled to the real dt and the verdict focused on
    TRANSIENT (load-onset) tracking -- the whole reason high-freq matters. Split
    is causal: train on the prefix, score the suffix."""
    t, z = load_rapl_csv(path)
    dt = float(np.median(np.diff(t)))
    set_dt(dt)  # MUST precede any ClassicalKF construction
    n = len(z)
    n_train = max(50, int(n * train_frac))
    z_train, z_test = z[:n_train], z[n_train:]
    print(f"Loaded {n} samples from {path}: dt={dt*1000:.0f} ms "
          f"({1/dt:.1f} Hz), span={t[-1]-t[0]:.1f}s, "
          f"power {z.min():.0f}-{z.max():.0f} W (sharpest step "
          f"{np.abs(np.diff(z)).max():.0f} W/sample).")
    print(f"train = {len(z_train)} (causal prefix), test = {len(z_test)} (suffix)\n")

    q, r = tune_classical(z_train)
    print(f"Tuned classical KF: q={q}, r={r} (adaptive arm seeds from these)\n")

    cls = run_classical(z_test, q, r)
    adp = run_classical(z_test, q, r, adapt=True)
    p_rmse, p_mae = persistence_metrics(z_test)

    # transient window: focus on the genuine load onsets/dips, not every wiggle
    win = max(50, len(z_test) // 4)
    rows = [("persistence (raw z[t])", p_rmse, p_mae, None, None)]
    for name, out in (("classical KF (fixed)", cls), ("adaptive KF (q/r online)", adp)):
        rows.append((name,
                     _one_step_rmse(z_test, out["pred_next"]),
                     _one_step_mae(z_test, out["pred_next"]),
                     noise_reduction(z_test, out["level"], win=win)[2],
                     transient_error(z_test, out["pred_next"], n_events=8)))

    print("=== Phase-2 high-freq one-step A/B (held-out suffix) ===")
    print(f"{'model':<26}{'RMSE(W)':>10}{'MAE(W)':>10}{'noiseRatio':>12}{'transErr(W)':>13}")
    for name, rmse, mae, nr, te in rows:
        nr_s = "-" if nr is None else f"{nr:.3f}"
        te_s = "-" if te is None else f"{te:.3f}"
        print(f"{name:<26}{rmse:>10.4f}{mae:>10.4f}{nr_s:>12}{te_s:>13}")
    print("\ntransErr = mean abs one-step error near the 8 sharpest transitions "
          "(the load onsets/dips). This is the Phase-2 metric of record.")

    cls_rmse, adp_rmse = rows[1][1], rows[2][1]
    cls_te, adp_te = rows[1][4], rows[2][4]
    # adaptive-filter stability check: online q/r must stay bounded on the sharp
    # high-freq onsets (this is exactly where the retired MLP surrogate diverged).
    lo, hi = z.min(), z.max()
    span = hi - lo
    ap = adp["pred_next"][:-1]
    oob = np.mean((ap < lo - span) | (ap > hi + span))
    print("\n--- verdict ---")
    print(f"one-step RMSE: persistence {p_rmse:.3f}  fixed {cls_rmse:.3f}  "
          f"adaptive {adp_rmse:.3f} W.")
    print(f"transient err: fixed {cls_te:.3f}  adaptive {adp_te:.3f} W "
          f"({(cls_te-adp_te)/cls_te*100:+.1f}% adaptive vs fixed).")
    if oob > 0.02:
        print(f"WARNING: adaptive filter unstable -- {oob*100:.0f}% of predictions "
              f"fall outside [data range +/- one span] ({lo-span:.0f}..{hi+span:.0f} "
              "W). Tighten Q_MAX/R_MAX or slow ADAPT_ALPHA.")
    else:
        print("Adaptive filter stayed bounded on the high-freq onsets (unlike the "
              "retired MLP surrogate).")
    if p_rmse <= min(cls_rmse, adp_rmse):
        print("Persistence still competitive on bulk RMSE (plateaus dominate). The "
              "filters earn their keep only if they cut TRANSIENT error -- that is "
              "where high-freq dynamics live. Read transErr, not RMSE.")
    else:
        print("A filter beats persistence on bulk RMSE here -- the high-freq signal "
              "has enough exploitable structure between samples that smoothing pays "
              "off, unlike the 1 Hz case. Confirm it also wins transErr before "
              "believing it.")


# ----------------------------------------------------------------------------
# selfcheck
# ----------------------------------------------------------------------------
def _selfcheck():
    rng = np.random.default_rng(0)

    # (a) noise rejection on a constant signal: filtered diff-std < raw diff-std
    const = 200.0 + rng.normal(0, 3.0, 4000)
    out = run_classical(const, q=1e-2, r=25.0)
    raw_std, filt_std, ratio = noise_reduction(const, out["level"], win=1000)
    assert ratio < 0.8, f"KF failed to denoise constant+noise: ratio={ratio:.3f}"

    # (b) ramp tracking: KF one-step RMSE beats persistence on a noisy ramp
    n = 4000
    ramp = np.linspace(200, 420, n) + rng.normal(0, 3.0, n)
    out = run_classical(ramp, q=1.0, r=10.0)
    kf_rmse = _one_step_rmse(ramp, out["pred_next"])
    p_rmse, _ = persistence_metrics(ramp)
    assert kf_rmse < p_rmse, f"KF should beat persistence on ramp: {kf_rmse:.3f} vs {p_rmse:.3f}"

    # (c) adaptive q/r converges R toward the true measurement noise. Feed a
    #     constant signal with a KNOWN noise std that JUMPS partway through; the
    #     adaptive filter's R must track up toward the new innov variance while a
    #     fixed filter (seeded low) stays put. Also assert it stays finite/bounded.
    seg = np.r_[200.0 + rng.normal(0, 2.0, 3000),
                200.0 + rng.normal(0, 12.0, 3000)]
    adp = run_classical(seg, q=1e-2, r=4.0, adapt=True)  # seed r low (var=4)
    assert np.isfinite(adp["pred_next"][:-1]).all(), "adaptive filter non-finite"
    kf = ClassicalKF(1e-2, 4.0, z0=seg[0], adapt=True)
    for zt in seg:
        kf.step(zt)
    assert kf.R > 20.0, f"adaptive R did not track the noise jump: R={kf.R:.1f}"
    lo, hi = seg.min(), seg.max()
    span = hi - lo
    ap = adp["pred_next"][:-1]
    assert np.mean((ap < lo - span) | (ap > hi + span)) < 0.02, "adaptive KF unstable"

    # (e) bursty-load stability (the 2026-07-08 live failure): calm 1 W noise
    #     punctuated by 100 W square bursts must NOT run q away / pin R at the
    #     floor / degrade calm-stretch smoothing to pass-through. Live daemon
    #     showed q 140-1000 with R floored -> net smoothing 1.2x. The gate:
    #     q bounded, R off the floor in calm, calm smoothing still < 0.5.
    n = 12000
    zb = 200.0 + rng.normal(0, 1.0, n)
    burst_starts = range(500, n - 60, 300)
    for s in burst_starts:
        zb[s:s + 30] += 100.0
    kfb = ClassicalKF(1e-3, 100.0, z0=zb[0], adapt=True)
    qb, rb, lb = np.empty(n), np.empty(n), np.empty(n)
    for i, zt in enumerate(zb):
        kfb.step(zt)
        qb[i], rb[i], lb[i] = kfb.q, kfb.R, kfb.m[0]
    calm = np.ones(n, bool)
    for s in burst_starts:
        calm[s - 5:s + 40] = False
    smooth_b = float(np.std(np.diff(lb)[calm[1:]]) / np.std(np.diff(zb)[calm[1:]]))
    floor_frac = float(np.mean(rb <= R_MIN + 1e-9))
    assert qb.max() < 50.0, f"q ran away on bursty load: max q={qb.max():.1f}"
    assert floor_frac < 0.10, f"R pinned at floor {floor_frac*100:.0f}% of steps"
    assert smooth_b < 0.5, f"bursty-load calm smoothing degraded: {smooth_b:.3f}"

    # (d) Phase-2 dt rescale: set_dt rebuilds F's trend term and load_rapl_csv
    #     reads the simple schema. Restore DT=1 afterwards so order can't matter.
    set_dt(0.1)
    assert abs(F[0, 1] - 0.1) < 1e-12, f"set_dt failed to rescale F: {F[0,1]}"
    assert abs(QC[1, 1] - 0.1) < 1e-12, f"set_dt failed to rescale QC: {QC[1,1]}"
    set_dt(1.0)
    import io
    t_in, z_in = load_rapl_csv(io.StringIO("time_s,power_W\n0.1,100\n0.2,110\n"))
    assert list(z_in) == [100.0, 110.0], f"load_rapl_csv simple schema: {z_in}"

    print("selfcheck OK:")
    print(f"  (a) denoise constant+noise: diff-std ratio = {ratio:.3f} (raw {raw_std:.3f} -> {filt_std:.3f})")
    print(f"  (b) ramp one-step RMSE: classical {kf_rmse:.3f} < persistence {p_rmse:.3f}")
    print(f"  (c) adaptive R tracked noise jump (seed 4 -> R={kf.R:.1f}) and stayed bounded")
    print(f"  (d) set_dt rescales F/QC + load_rapl_csv reads simple schema")
    print(f"  (e) bursty load: max q={qb.max():.1f}, R at floor {floor_frac*100:.0f}%, "
          f"calm smoothing {smooth_b:.3f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selfcheck", action="store_true",
                    help="fast synthetic asserts, no real data")
    ap.add_argument("--phase2", metavar="CSV",
                    help="run high-freq RAPL A/B on a capture (time_s,power_W "
                         "or rapl.py schema)")
    args = ap.parse_args()
    if args.selfcheck:
        _selfcheck()
        return
    if args.phase2:
        run_phase2(args.phase2)
        return
    run_1hz()


if __name__ == "__main__":
    main()
