#!/usr/bin/env python3
"""Does a power-spike onset have any *leading* structure, or is it a pure step?

The cheapest test of the high-freq track's core question (rapl_track open Q#3):
before each load onset, do the samples leading INTO it creep/trend upward (=>
something a model could use for lead time), or is the dip floor flat noise right
up to a single-sample jump (=> reactive, no usable lead time at this rate)?

Run:  .venv/bin/python onset_leading_structure.py [capture.csv]   (default rapl_power.csv)
Self:  .venv/bin/python onset_leading_structure.py --selfcheck

Verdict on rapl_power.csv (62 s ai_load.sh cycle, 10 Hz): pure step. The two
clean dip->busy onsets have a flat dip floor (noise std ~1 W) with pre-jump
slope ~0 W/s, then a single-sample +60 W jump. No leading structure at 100 ms.

Upstream extension (session 2026-07-02): power itself is settled-reactive, so
the same question is now asked of the CAUSALLY UPSTREAM signals -- run-queue
depth (10 Hz), ctxt_per_s / procs_running / mem (1 Hz): do THEY move before the
power step?  Plus a 10 Hz walk-forward RF trained on upstream features ONLY
(no power-derived features), scored per-onset against TRUE step onsets.

  --hf-pull [--hours H]   pull raw 10 Hz power + nr_running (+1 Hz upstream)
                          from InfluxDB onto a 100 ms grid -> data/hf_upstream.csv
  --upstream [CSV]        model-free per-onset lead of each upstream signal,
                          then the upstream-only RF (--start/--end to slice,
                          e.g. to separate daemon-contaminated hours)
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ANALYSIS_DIR = Path(__file__).resolve().parents[1]
HF_CACHE = ANALYSIS_DIR / "data" / "hf_upstream.csv"
DEFAULT_CAPTURE = ANALYSIS_DIR / "fixtures" / "rapl_power.csv"
HF_DT = 0.1                  # the 100 ms grid
ONSET_MIN_STEP = 25.0        # W per 100 ms sample; ~25 sigma of the 1 W floor
ONSET_REFRACTORY = 50        # samples (5 s): one onset per distinct load arrival
HORIZON_SAMPLES = 50         # RF label: true onset within next 5 s
UPSTREAM_1HZ = ["ctxt_per_s", "procs_running", "mem_used", "mem_available"]


def load(path):
    df = pd.read_csv(path)
    if {"time_s", "power_W"} <= set(df.columns):
        return df["time_s"].to_numpy(float), df["power_W"].to_numpy(float)
    if "mono_s" in df.columns:  # full rapl.py schema
        return df["mono_s"].to_numpy(float), df["watts"].to_numpy(float)
    raise ValueError(f"unrecognised columns: {list(df.columns)}")


def find_onsets(z, busy=190.0, dip_max=160.0, min_step=40.0, refractory=10):
    """Sharp upward jumps OUT of a low (dip/idle) state into the busy plateau.
    Returns indices i where z[i] is the last low sample before the jump."""
    d = np.diff(z)
    onsets = []
    for i in range(2, len(z) - 1):
        if z[i] < dip_max and z[i + 1] >= busy - 20 and d[i] > min_step:
            if not onsets or i - onsets[-1] > refractory:
                onsets.append(i)
    return onsets


def pre_jump_trend(z, i, n=8):
    """Causal: slope (W/sample) and residual noise std of the n samples STRICTLY
    before the jump at i (excludes the jump sample i itself). RISING if the slope
    clears half the noise band; otherwise flat (no warning)."""
    if i - n < 0:
        return None
    seg = z[i - n:i]
    x = np.arange(len(seg))
    coef = np.polyfit(x, seg, 1)
    slope = coef[0]
    resid_std = float(np.std(seg - np.polyval(coef, x)))
    rising = slope > resid_std / 2
    return slope, resid_std, rising


def report(path):
    t, z = load(path)
    onsets = find_onsets(z)
    print(f"{path}: {len(z)} samples, dt~{np.median(np.diff(t))*1000:.0f} ms, "
          f"power {z.min():.0f}-{z.max():.0f} W")
    print(f"onsets: {[f'{t[i]:.1f}s ({z[i]:.0f}->{z[i+1]:.0f}W)' for i in onsets]}\n")
    any_warning = False
    for i in onsets:
        tr = pre_jump_trend(z, i)
        if tr is None:
            continue
        slope, noise, rising = tr
        any_warning |= rising
        print(f"  {t[i]:6.1f}s: pre-jump slope {slope*10:+.1f} W/s, "
              f"noise std {noise:.1f} W -> "
              f"{'RISING (possible lead)' if rising else 'flat (no warning)'}")
    print("\nverdict:", "leading structure present -- investigate" if any_warning
          else "pure step, no usable lead time at this sample rate.")
    return any_warning


# ---------------------------------------------------------------------------
# upstream causal signals at 10 Hz (2026-07-02)
# ---------------------------------------------------------------------------
def pull_hf(hours=8.0, out=HF_CACHE):
    """Pull raw (un-aggregated) 10 Hz cpu_power + run_queue.nr_running plus the
    1 Hz upstream signals for the last `hours`, aligned on a 100 ms grid.
    1 Hz signals are ffilled onto the grid (<=1.2 s) -- they cannot show
    sub-second lead by construction, only the 10 Hz nr_running can."""
    from core import telemetry as common
    from influxdb_client import InfluxDBClient

    org, token = common.load_org(), common.load_token()
    now = pd.Timestamp.now(tz="UTC")
    start = now - pd.Timedelta(hours=hours)
    edges = list(pd.date_range(start, now, freq="2h"))
    if edges[-1] < now:
        edges.append(now)

    def iso(t):
        return t.isoformat().replace("+00:00", "Z")

    parts = []
    client = InfluxDBClient(url=common.INFLUX_URL, token=token, org=org,
                            timeout=300_000)
    try:
        qa = client.query_api()
        for a, b in zip(edges[:-1], edges[1:]):
            flux = (
                f'from(bucket: "{org}") |> range(start: {iso(a)}, stop: {iso(b)}) '
                '|> filter(fn: (r) => (r._measurement == "cpu_power" and r._field == "watts") '
                'or (r._measurement == "run_queue" and r._field == "nr_running")) '
                '|> keep(columns: ["_time", "_measurement", "_value"])')
            d = qa.query_data_frame(flux)
            if isinstance(d, list):
                d = pd.concat(d, ignore_index=True)
            if not d.empty:
                parts.append(d[["_time", "_measurement", "_value"]])
            print(f"  {iso(a)} -> {iso(b)}: {0 if d.empty else len(d)} raw pts")
    finally:
        client.close()

    raw = pd.concat(parts, ignore_index=True)
    raw["_time"] = pd.to_datetime(raw["_time"]).dt.round("100ms")
    wide = raw.pivot_table(index="_time", columns="_measurement",
                           values="_value", aggfunc="mean")
    wide = wide.rename(columns={"cpu_power": "power_W", "run_queue": "nr_running"})
    grid = pd.date_range(wide.index.min(), wide.index.max(), freq="100ms")
    # ffill(limit=2): bridge lone missing ticks without inventing data across
    # real outages; leftover NaN rows are dropped (rare on the steady streams).
    df = wide.reindex(grid).ffill(limit=2)

    up = common.query_upstream(start=iso(start), stop=iso(now))
    up = up[[c for c in UPSTREAM_1HZ if c in up.columns]]
    df = df.join(up.reindex(grid, method="ffill", tolerance=pd.Timedelta("1.2s")))

    n0 = len(df)
    df = df.dropna(subset=["power_W", "nr_running"])
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index_label="time")
    print(f"saved {len(df)} rows ({n0 - len(df)} grid ticks dropped) -> {out}")
    print(f"  span {df.index.min()} -> {df.index.max()}, cols {list(df.columns)}")
    return df


def load_hf(path=HF_CACHE, start=None, end=None):
    df = pd.read_csv(path, index_col="time")
    # explicit ISO8601: mixed fractional-second formats defeat parse_dates
    df.index = pd.to_datetime(df.index, format="ISO8601", utc=True)
    df = df.sort_index()
    if start:
        df = df[df.index >= pd.Timestamp(start)]
    if end:
        df = df[df.index < pd.Timestamp(end)]
    return df


def hf_onsets(power):
    """TRUE onset indices on the 100 ms grid: single-sample jumps, level gates
    off (mycroft's idle floor ~200 W sits above the old capture's busy gate)."""
    return find_onsets(power.to_numpy(dtype=float), busy=-np.inf, dip_max=np.inf,
                       min_step=ONSET_MIN_STEP, refractory=ONSET_REFRACTORY)


def signal_step_lag(sig, i, pre=50, base_end=10, k=3.0, floor=1.0):
    """When did `sig` first move off its pre-onset baseline, relative to the
    power jump landing at sample i+1?  Baseline = sig[i-pre : i-base_end]
    (median + max(k*std, floor) threshold).  Returns lag in SAMPLES relative
    to i+1: negative = the signal moved BEFORE power (real lead), 0 = same
    sample, positive = after. None if no move in [i-pre, i+10] or short data."""
    if i - pre < 0 or i + 11 > len(sig):
        return None
    base = sig[i - pre:i - base_end]
    if not np.isfinite(base).all():
        return None
    thr = np.median(base) + max(k * np.std(base), floor)
    win = sig[i - pre:i + 11]
    above = np.nonzero(win > thr)[0]
    if len(above) == 0:
        return None
    return int((i - pre) + above[0] - (i + 1))


def upstream_report(df):
    """Model-free: for each TRUE power onset, when did each upstream signal
    first move? The decisive number is nr_running's (the only 10 Hz signal)."""
    onsets = hf_onsets(df["power_W"])
    print(f"{len(df)} samples ({df.index.min()} -> {df.index.max()}), "
          f"{len(onsets)} true onsets (>{ONSET_MIN_STEP:.0f} W / 100 ms jump)")
    if not onsets:
        return
    gaps = np.diff([df.index[i].value for i in onsets]) / 1e9
    print(f"inter-onset gaps: median {np.median(gaps):.0f} s, "
          f"p10 {np.percentile(gaps, 10):.0f} s, p90 {np.percentile(gaps, 90):.0f} s\n")
    sigs = ["nr_running"] + [c for c in UPSTREAM_1HZ if c in df.columns]
    print(f"{'signal':<16}{'moved@n':>9}{'led':>5}{'coincident':>11}{'lagged':>8}"
          f"{'median lag':>12}  (negative = before power; 1 Hz signals quantized +/-1 s)")
    for s in sigs:
        arr = df[s].to_numpy(dtype=float)
        lags = [signal_step_lag(arr, i) for i in onsets]
        lags = [l for l in lags if l is not None]
        if not lags:
            print(f"{s:<16}{'0':>9}")
            continue
        lags = np.array(lags)
        print(f"{s:<16}{len(lags):>9}{(lags < 0).sum():>5}{(lags == 0).sum():>11}"
              f"{(lags > 0).sum():>8}{np.median(lags) * HF_DT * 1000:>+10.0f} ms")


def hf_features(df):
    """Strictly causal, upstream-ONLY features on the 100 ms grid. No power,
    no KF-of-power -- power-derived features are settled-reactive."""
    nr = df["nr_running"]
    cols = {}
    for lag in (0, 1, 2, 5, 10):
        cols[f"nr_lag{lag}"] = nr.shift(lag)
    cols["nr_d10"] = nr - nr.shift(10)   # 1 s trailing delta
    cols["nr_d30"] = nr - nr.shift(30)   # 3 s trailing delta
    for s in UPSTREAM_1HZ:
        if s in df.columns:
            cols[s] = df[s]
            cols[f"{s}_d30"] = df[s] - df[s].shift(30)
    return pd.DataFrame(cols, index=df.index)


def hf_label(df, horizon=HORIZON_SAMPLES):
    """1 iff a TRUE onset lands within the next `horizon` samples (strictly
    after the current one) -- same forward-window shape as the 1 Hz label but
    measured against the raw step, not the frozen label's confirmation."""
    on = pd.Series(False, index=df.index)
    on.iloc[[i + 1 for i in hf_onsets(df["power_W"])]] = True
    fwd = on.shift(-1)[::-1].rolling(horizon, min_periods=1).max()[::-1]
    return fwd.fillna(0).astype(int)


def rf_upstream(df, train_h=3.0, test_h=1.0, horizon=HORIZON_SAMPLES, quiet=False):
    """Walk-forward RF on upstream-only features at 10 Hz, judged per-onset
    against TRUE onsets: PR-AUC, detection rate, and the honest lead
    distribution. Alarm budget = one flag per true onset in the fold."""
    from validation import evaluation as ev
    from sklearn.ensemble import RandomForestClassifier
    from validation.stress_test import align

    X = hf_features(df)
    y = hf_label(df, horizon)
    keep = X.notna().all(axis=1)
    X, y = X[keep], y[keep]
    onset_ts = pd.DatetimeIndex(df.index.to_numpy()[[i + 1 for i in hf_onsets(df["power_W"])]])

    train_td = pd.Timedelta(hours=train_h)
    test_td = pd.Timedelta(hours=test_h)
    emb = pd.Timedelta(seconds=horizon * HF_DT)
    yt, ys, flag_ts, covered = [], [], [], []
    t = X.index[0] + train_td
    while t + test_td <= X.index[-1] + pd.Timedelta(seconds=1):
        tr = X.index < t - emb
        te = (X.index >= t) & (X.index < t + test_td)
        t += test_td
        if y[tr].sum() < 50 or y[te].sum() < 1:
            continue
        m = RandomForestClassifier(n_estimators=200, min_samples_leaf=20,
                                   class_weight="balanced", n_jobs=-1, random_state=0)
        m.fit(X[tr].to_numpy(), y[tr].to_numpy())
        proba = m.predict_proba(X[te].to_numpy())[:, 1]
        # alarm budget = ONE flag per true onset in the fold (exact top-K, so
        # background-proba ties can't flood the fold with spurious early flags)
        te_idx = X.index[te]
        k = int(((onset_ts >= te_idx[0]) & (onset_ts <= te_idx[-1])).sum())
        fl = te_idx[np.argsort(-proba)[:max(k, 1)]]
        yt.append(y[te].to_numpy()); ys.append(proba)
        flag_ts.append(fl.to_numpy()); covered.append((X.index[te][0], X.index[te][-1]))
    if not yt:
        print("rf_upstream: no scorable folds")
        return None
    yt = np.concatenate(yt).astype(float)
    ys = np.concatenate(ys)
    flags = pd.DatetimeIndex(np.concatenate(flag_ts))
    lo = min(a for a, _ in covered)
    test_onsets = onset_ts[(onset_ts >= lo + pd.Timedelta(seconds=horizon * HF_DT))
                           & (onset_ts <= max(b for _, b in covered))]
    # match_lag_s=0: a flag on the onset sample counts as detected with 0 lead;
    # anything later does not count at all. lead>0 is the only genuine warning.
    det = align(test_onsets, flags, lead_max_s=horizon * HF_DT, match_lag_s=0)
    res = {
        "rate": float(yt.mean()),
        "pr_auc": ev._pr_auc(yt, ys),
        "n_onsets": det["n_bursts"],
        "detected": det["n_detected"],
        "median_lead_s": det["median_lead_s"],
        "frac_early": det["frac_early"],
        "leads": det["leads"],
        "n_flags": int(len(flags)),
    }
    if not quiet:
        print(f"\n=== upstream-only RF @ 10 Hz (train {train_h} h / test {test_h} h, "
              f"horizon {horizon * HF_DT:.0f} s) ===")
        print(f"  test rows             : {len(yt)}  (label rate {res['rate']:.4f})")
        print(f"  PR-AUC                : {res['pr_auc']:.4f}  "
              f"(lift {res['pr_auc'] / res['rate']:.2f}x over random)")
        print(f"  true onsets in test   : {res['n_onsets']}")
        print(f"  detected (flag <= onset): {res['detected']} "
              f"({res['detected'] / res['n_onsets']:.1%})" if res["n_onsets"] else "")
        print(f"  median lead           : {res['median_lead_s']:+.1f} s  "
              f"(+ = flag BEFORE the step)")
        print(f"  fraction with lead>0  : {res['frac_early']:.1%}")
        if res["leads"]:
            print(f"  lead distribution (s) : {np.percentile(res['leads'], [10, 50, 90]).round(1)}")
    return res


def run_upstream(path=HF_CACHE, start=None, end=None):
    df = load_hf(path, start, end)
    print("=== model-free: who moves first at each TRUE power onset? ===")
    upstream_report(df)
    rf_upstream(df)


def _selfcheck():
    # synthetic: flat dip floor + clean step => must read 'no warning'
    rng = np.random.default_rng(0)
    flat = np.r_[np.full(40, 100.0) + rng.normal(0, 1, 40), np.full(40, 204.0)]
    i = 39
    slope, noise, rising = pre_jump_trend(flat, i)
    assert not rising, f"flat floor misread as rising: slope={slope:.2f}"
    assert i in find_onsets(flat), "clean onset not detected"

    # synthetic: a real ramp INTO the jump => must read 'rising'
    ramp = np.r_[np.linspace(100, 150, 40), np.full(40, 204.0)]
    slope, noise, rising = pre_jump_trend(ramp, 39)
    assert rising, f"real pre-onset ramp misread as flat: slope={slope:.2f}"

    # signal_step_lag: a signal stepping 3 samples BEFORE the power jump must
    # read lag=-3; one stepping WITH it reads 0; a flat signal reads None
    power = np.r_[np.full(100, 200.0), np.full(100, 280.0)]  # jump lands at 100
    i = 99
    early = np.r_[np.full(97, 1.0), np.full(103, 60.0)]
    with_it = np.r_[np.full(100, 1.0), np.full(100, 60.0)]
    assert signal_step_lag(early, i) == -3, signal_step_lag(early, i)
    assert signal_step_lag(with_it, i) == 0, signal_step_lag(with_it, i)
    assert signal_step_lag(np.full(200, 1.0), i) is None

    # hf_label: 1 exactly in the `horizon` samples strictly before the onset
    idx = pd.date_range("2026-01-01", periods=200, freq="100ms")
    dfs = pd.DataFrame({"power_W": power}, index=idx)
    lbl = hf_label(dfs, horizon=10)
    assert lbl.iloc[90:100].all() and lbl.iloc[100:].sum() == 0 == lbl.iloc[:90].sum(), \
        lbl.to_numpy().nonzero()

    # end-to-end rf_upstream on a synthetic 10 Hz stream where nr_running fills
    # across the whole 5 s horizon BEFORE each power step (and drains as the
    # work starts executing): the RF must rank the planted precursor well above
    # random and any detected flags must be early. Exact top-K per-onset coverage
    # is intentionally not asserted; the RF scores often tie/cluster by fold.
    rng = np.random.default_rng(1)
    n = 45 * 600                      # 45 min at 10 Hz
    p = 200.0 + rng.normal(0, 0.5, n)
    nr = np.full(n, 1.0)
    for k0 in range(1200, n - 700, 1200):     # an event every 120 s
        nr[k0 - HORIZON_SAMPLES:k0] = 80.0    # queue fills throughout horizon
        p[k0:k0 + 100] += 90.0                # power follows
    idx = pd.date_range("2026-01-01", periods=n, freq="100ms")
    dfs = pd.DataFrame({"power_W": p, "nr_running": nr}, index=idx)
    res = rf_upstream(dfs, train_h=0.25, test_h=0.25, quiet=True)
    assert res is not None and res["n_onsets"] >= 5, res
    assert res["pr_auc"] > 0.9, res
    assert res["detected"] > 0, res
    assert res["frac_early"] == 1.0, res
    assert 0.1 <= res["median_lead_s"] <= HORIZON_SAMPLES * HF_DT, \
        f"planted lead not recovered: {res}"

    print("selfcheck OK: flat floor -> no warning; ramp -> rising; "
          f"step_lag -3/0/None; planted upstream lead ranked PR-AUC "
          f"{res['pr_auc']:.3f} and recovered "
          f"{res['median_lead_s']:+.1f} s @ {res['detected']}/{res['n_onsets']} detected")


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", default=None,
                    help="power capture for the original per-onset report")
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--hf-pull", action="store_true",
                    help="pull 10 Hz power+nr_running (+1 Hz upstream) -> data/hf_upstream.csv")
    ap.add_argument("--hours", type=float, default=8.0, help="window for --hf-pull")
    ap.add_argument("--upstream", action="store_true",
                    help="upstream lead report + upstream-only RF on the hf cache")
    ap.add_argument("--start", default=None, help="slice for --upstream (ISO)")
    ap.add_argument("--end", default=None, help="slice for --upstream (ISO)")
    args = ap.parse_args()

    if args.selfcheck:
        _selfcheck()
    elif args.hf_pull:
        pull_hf(hours=args.hours)
    elif args.upstream:
        run_upstream(args.csv or HF_CACHE, args.start, args.end)
    else:
        report(args.csv or DEFAULT_CAPTURE)


if __name__ == "__main__":
    main()
