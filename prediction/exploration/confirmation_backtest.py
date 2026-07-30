#!/usr/bin/env python3
"""Confirmation-signal backtest (NEGATIVE result, kept for reproducibility).

Question (2026-07-13 pivot): can nr_running jerk + a Hawkes self-exciting
intensity, used as *simultaneous corroboration* alongside power's own EWMA
z-score (NOT as lead-time predictors -- forecasting is dead),
make the detector CONFIRM a power spike that is already happening either faster
or with fewer false positives than power-EWMA alone?

Answer: NO on both.
  * Confirmation latency has no headroom -- power-EWMA confirms the pure-step
    onset at 0.00 s median at every threshold (huge SNR: ~+25-90 W step vs ~1 W
    floor). (Latency is ~0 partly by construction, since ground-truth onsets and
    the confirmer are both the power signal -- which is the point: power suffices.)
  * Precision -- raising the power-EWMA threshold alone traces a strictly better
    precision/recall frontier (~0.66 prec @55% det, ~0.96 @20%) than any
    jerk/Hawkes AND-gate (best ~0.46 @40%); every corroboration rule is
    Pareto-dominated. Requiring corroboration mostly discards the ~60% of onsets
    that have no simultaneous run-queue disturbance -- it removes real
    detections, not false alarms.

Full write-up: docs/endeavor_summary.md sec 7.1. The scheduler signals only ever
helped as LEADING indicators (nr_running_jerk d~1.5, +3 s cached lead), which
this framing deliberately excludes.

Ground truth = the repo's own TRUE power onsets (core.onsets.hf_onsets on
power_W: the raw >=25 W / 100 ms step). Data = data/hf_upstream.csv (10 Hz power
+ nr_running; the cache lacks upstream fields, this capture has them).

Run from analysis/:
  .venv/bin/python -m exploration.confirmation_backtest            # full backtest
  .venv/bin/python -m exploration.confirmation_backtest --selfcheck
fixed-kernel Hawkes (no MLE refit), episode-based FP counting.
"""
import sys

import numpy as np
import pandas as pd

from core.anomaly import ewma_z
from core import onsets as on

HF_DT = 0.1  # 100 ms grid


def contiguous_segments(idx, max_gap_s=0.25):
    """Split a DatetimeIndex into contiguous runs (no gap > max_gap_s). Rolling
    features must not cross real outages (hf_upstream has a retention gap)."""
    dt = np.diff(idx.view("int64")) / 1e9
    breaks = np.nonzero(dt > max_gap_s)[0]
    bounds = [0, *(breaks + 1), len(idx)]
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]


def hawkes_intensity(nr, tau_s=35.0, dt=HF_DT):
    """Self-exciting intensity on run-queue arrivals: positive increments in
    nr_running are marks; lambda decays exp(-dt/tau) and jumps by the mark each
    step. tau ~35 s from the fitted Hawkes kernel (Model history: 30-40 s).
    Recursive, strictly causal. Returns an array the same length as nr."""
    arrivals = np.clip(np.diff(nr, prepend=nr[0]), 0, None)
    decay = np.exp(-dt / tau_s)
    lam = np.empty(len(nr))
    acc = 0.0
    for k in range(len(nr)):
        acc = acc * decay + arrivals[k]
        lam[k] = acc
    return lam


def nr_jerk(nr, win=50):
    """Run-queue roughness = rolling std of 1-step diffs (the jerk proxy from
    exploration/extended_precursor_screen.py). win in samples (5 s @ 10 Hz)."""
    d = pd.Series(nr).diff()
    return d.rolling(win, min_periods=win).std().to_numpy()


def build_features(df):
    """Per contiguous segment: EWMA-z of power, of nr_jerk, and of the Hawkes
    intensity. All causal. Concatenated back to one frame aligned to df.index.
    span=300 samples = 30 s time-constant, matching the frozen label's span=30
    @1 Hz."""
    n = len(df)
    p_z = np.full(n, np.nan)
    j_z = np.full(n, np.nan)
    h_z = np.full(n, np.nan)
    power = df["power_W"].to_numpy(float)
    nr = df["nr_running"].to_numpy(float)
    for a, b in contiguous_segments(df.index):
        if b - a < 100:
            continue
        p_z[a:b] = ewma_z(pd.Series(power[a:b]), span=300).to_numpy()
        j_z[a:b] = ewma_z(pd.Series(nr_jerk(nr[a:b])), span=300).to_numpy()
        h_z[a:b] = ewma_z(pd.Series(hawkes_intensity(nr[a:b])), span=300).to_numpy()
    return pd.DataFrame({"p_z": p_z, "j_z": j_z, "h_z": h_z}, index=df.index)


def onset_indices(df):
    """TRUE onset sample positions (the power step lands at i+1)."""
    return np.array([i + 1 for i in on.hf_onsets(df["power_W"])], dtype=int)


def flags_to_episodes(flag_mask, refractory=50):
    """Cluster a boolean flag stream into episodes, merging gaps < refractory
    samples. Returns the start index of each episode."""
    idx = np.nonzero(flag_mask)[0]
    if len(idx) == 0:
        return []
    splits = np.nonzero(np.diff(idx) > refractory)[0]
    return [idx[0]] + [idx[s + 1] for s in splits]


def evaluate(rule_mask, onset_pos, match_s=5.0, latency_max_s=5.0):
    """Score a boolean confirmation stream against true onsets.
    detection: onset confirmed if a flag fires in [onset, onset+latency_max];
    latency = onset -> first such flag. precision = flag episodes whose start is
    within +-match_s of any onset / total episodes (unmatched = false alarms)."""
    lat_max = int(latency_max_s / HF_DT)
    match = int(match_s / HF_DT)
    flag_idx = np.nonzero(rule_mask)[0]
    latencies, detected = [], 0
    for o in onset_pos:
        w = flag_idx[(flag_idx >= o) & (flag_idx <= o + lat_max)]
        if len(w):
            detected += 1
            latencies.append((w[0] - o) * HF_DT)
    episodes = flags_to_episodes(rule_mask)
    matched = sum(1 for e in episodes
                  if len(onset_pos) and np.min(np.abs(onset_pos - e)) <= match)
    n_ep = len(episodes)
    return {
        "n_onsets": len(onset_pos),
        "detected": detected,
        "det_rate": detected / len(onset_pos) if len(onset_pos) else float("nan"),
        "median_lat_s": float(np.median(latencies)) if latencies else float("nan"),
        "p90_lat_s": float(np.percentile(latencies, 90)) if latencies else float("nan"),
        "n_episodes": n_ep,
        "false_ep": n_ep - matched,
        "precision": matched / n_ep if n_ep else float("nan"),
    }


def _sd(a):
    """nan-safe >: NaN never crosses a threshold."""
    return np.where(np.isnan(a), -np.inf, a)


def run(df):
    feats = build_features(df)
    onset_pos = onset_indices(df)
    valid = feats["p_z"].notna().to_numpy()
    pz, jz, hz = (_sd(feats[c].to_numpy()) for c in ("p_z", "j_z", "h_z"))
    corrob = np.fmax(jz, hz)  # scheduler corroboration = max of the two z's

    rows = []

    def add(name, mask):
        rows.append((name, evaluate((mask & valid), onset_pos)))

    for k in (2.0, 3.0, 4.0, 6.0, 8.0, 12.0):          # power-only frontier
        add(f"power-EWMA  k={k}", pz > k)
    for k_lo in (1.0, 1.5, 2.0):                        # corroboration AND-gate
        for c in (1.0, 2.0):
            add(f"power>{k_lo} AND corrob>{c}", (pz > k_lo) & (corrob > c))
    add("corrob>2 (no power)", corrob > 2.0)
    return rows, onset_pos


def report(rows):
    hdr = (f"{'rule':<26}{'det%':>6}{'med_lat':>9}{'p90_lat':>9}"
           f"{'#epis':>7}{'#false':>8}{'prec':>7}")
    print(hdr)
    print("-" * len(hdr))
    for name, r in rows:
        print(f"{name:<26}{r['det_rate'] * 100:>5.0f}%{r['median_lat_s']:>9.2f}"
              f"{r['p90_lat_s']:>9.2f}{r['n_episodes']:>7}{r['false_ep']:>8}"
              f"{r['precision']:>7.2f}")
    print("\n--- Pareto test: each corroboration rule vs power-only @ ~matched det ---")
    pf = [(n, r) for n, r in rows if n.startswith("power-EWMA")]
    for name, r in rows:
        if not name.startswith("power>"):
            continue
        near = min(pf, key=lambda x: abs(x[1]["det_rate"] - r["det_rate"]))
        tag = "WINS" if r["precision"] > near[1]["precision"] else "no gain"
        print(f"{name:<26} det {r['det_rate'] * 100:>3.0f}% prec {r['precision']:.2f}"
              f"  vs {near[0].strip()} (det {near[1]['det_rate'] * 100:.0f}%"
              f" prec {near[1]['precision']:.2f}) -> {tag}")


def main(path=None):
    path = path or on.HF_CACHE
    print(f"loading {path} ...")
    df = on.load_hf(path).dropna(subset=["power_W", "nr_running"])
    span_h = (df.index[-1] - df.index[0]).total_seconds() / 3600
    rows, onset_pos = run(df)
    print(f"{len(df)} rows, span {span_h:.1f} h, {len(onset_pos)} true onsets\n")
    report(rows)


def selfcheck():
    # hawkes: lone +3 arrival then quiet -> jump by mark, decay exp(-dt/tau)
    lam = hawkes_intensity(np.array([0, 0, 3, 3, 3.0]), tau_s=1.0, dt=1.0)
    assert abs(lam[2] - 3.0) < 1e-9 and abs(lam[3] - 3 * np.exp(-1)) < 1e-9 \
        and abs(lam[4] - 3 * np.exp(-2)) < 1e-9, lam
    # episodes: big gap -> two starts; small gap -> merged
    m = np.zeros(300, bool); m[10:15] = True; m[20:25] = True; m[200:205] = True
    assert flags_to_episodes(m, refractory=50) == [10, 200], flags_to_episodes(m)
    # evaluate: onset@100, flag@103 -> detected, latency 0.3 s, precision 1
    mask = np.zeros(500, bool); mask[103:106] = True
    r = evaluate(mask, np.array([100]))
    assert r["detected"] == 1 and abs(r["median_lat_s"] - 0.3) < 1e-9 \
        and r["precision"] == 1.0, r
    # flag far from any onset -> false episode, precision 0
    mask2 = np.zeros(500, bool); mask2[400:403] = True
    r2 = evaluate(mask2, np.array([100]))
    assert r2["detected"] == 0 and r2["false_ep"] == 1 and r2["precision"] == 0.0, r2
    # segments split on a gap
    idx = pd.to_datetime(np.r_[np.arange(50), np.arange(50) + 200] * 1e8, utc=True)
    assert contiguous_segments(idx) == [(0, 50), (50, 100)]
    print("selfcheck OK: hawkes decay, episode clustering, detect/latency/precision, "
          "segment split")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selfcheck":
        selfcheck()
    else:
        main(sys.argv[1] if len(sys.argv) > 1 else None)
