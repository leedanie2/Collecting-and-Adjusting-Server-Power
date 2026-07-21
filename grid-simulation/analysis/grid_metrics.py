#!/usr/bin/env python3
"""
grid_metrics.py  --  Grid risk metrics for one simulation run.

Run from inside a results/<name>/ folder:
    python3 ../../analysis/grid_metrics.py [--gflops GFLOPS]

Writes metrics.json (risk, frequency, voltage, power, optional efficiency).
No dollar cost and no plots: cost is a raw energy-area ratio computed on the
raw traces by analysis/compare_pair.py; per-pair risk tables by the same.

--gflops  Sustained HPL Gflops score; adds Gflops/W when supplied.

Loads:
  S_PCC.csv          time_s, P_W, Q_var           (Simulink PCC output)
  freq_dev.csv       time_s, delta_f_Hz           (swing equation, optional)
  V_PCC.csv          time_s, V_pu                 (PCC voltage, optional)

Thresholds (see SOURCES.md):
  Ramp rate:  20 MW/min = 0.333 MW/s  (Southern Company large-load cap)
  LOLE:       0.1 days/year            (NERC 1-in-10-years adequacy standard)
  Freq nadir: -0.7 Hz (59.3 Hz)        (NERC BAL-003 under-frequency relay)
  Voltage:    0.95 pu                  (IEEE 1159 sag threshold)
"""

import sys, json, argparse
import numpy as np
from pathlib import Path

# Grid risk thresholds
RAMP_LIMIT_MW_S       = 0.333   # 20 MW/min — Southern Company large-load cap
SECONDS_PER_YEAR      = 365.25 * 24 * 3600
LOLE_TARGET_DAYS_YR   = 0.1     # NERC 1-in-10-years standard
FREQ_NADIR_LIMIT_HZ   = -0.7    # Hz below 60 Hz (NERC BAL-003 relay threshold)
VOLTAGE_SAG_LIMIT_PU  = 0.95    # IEEE 1159 sag threshold

N_SERVERS = 10_000              # modelled server count (from readscript.m)


def load_csv(path):
    return np.atleast_2d(np.loadtxt(path, delimiter=','))


def drop_nonincreasing(t, *cols):
    """Keep only strictly-increasing time samples.

    Variable-step solver output can repeat a timestamp; dt <= 0 turns every
    diff-based metric (ramp rate, RREI, ROCOF) into inf/nan silently."""
    keep = np.concatenate(([True], np.diff(t) > 0))
    return (t[keep],) + tuple(c[keep] for c in cols)


def try_load(path):
    p = Path(path)
    if not p.exists():
        return None
    try:
        return np.loadtxt(p, delimiter=',')
    except Exception:
        return None


RESAMPLE_DT_S = 0.1   # uniform metric grid; matches the RAPL telemetry cadence


def resample_uniform(t, v, dt=RESAMPLE_DT_S):
    """Interpolate onto a uniform time grid.

    The variable-step solver clusters output points at transients (dt down to
    ~1e-5 s). Diffing that raw grid measures solver micro-steps, not physical
    ramps, and any sample-count metric (RREI, sag counts) is biased by solver
    point density. Swing/UPS dynamics have multi-second time constants, so
    0.1 s loses nothing."""
    tu = np.arange(t[0], t[-1] + dt / 2, dt)
    return tu, np.interp(tu, t, v)


def sanitize_2col(arr):
    """atleast_2d, strictly-increasing time, uniform resample; None if unusable."""
    if arr is None:
        return None
    arr = np.atleast_2d(arr)
    if arr.shape[1] < 2:
        return None
    t, v = drop_nonincreasing(arr[:, 0], arr[:, 1])
    if len(t) < 2 or t[-1] - t[0] < RESAMPLE_DT_S:
        return None
    return np.column_stack(resample_uniform(t, v))


def annualize(x_per_trace, trace_duration_s):
    return x_per_trace * SECONDS_PER_YEAR / trace_duration_s


def compute_risk_metrics(P_W, t_s):
    dt      = np.diff(t_s)
    dP_MW_s = np.diff(P_W) / 1e6 / dt

    excess     = np.maximum(np.abs(dP_MW_s) - RAMP_LIMIT_MW_S, 0.0)
    exceed_idx = excess > 0
    n_exceed   = int(np.sum(exceed_idx))
    trace_s    = t_s[-1] - t_s[0]

    rrei         = annualize(n_exceed, trace_s)
    nrs_trace    = float(np.sum(excess[exceed_idx] * dt[exceed_idx]))
    nrs          = annualize(nrs_trace, trace_s)

    regional_MW  = 10_000.0
    sensitivity  = 2.0
    p_conditional = (np.max(P_W) / 1e6 / regional_MW) * sensitivity
    lole_proxy   = rrei * p_conditional / 24.0

    cv = float(np.std(P_W) / np.mean(P_W))

    return {
        'rrei_exceedances_per_year': round(rrei, 1),
        'nrs_MW_per_year':           round(nrs, 3),
        'lole_proxy_days_per_year':  round(lole_proxy, 4),
        'lole_target_days_per_year': LOLE_TARGET_DAYS_YR,
        'lole_ratio':                round(lole_proxy / LOLE_TARGET_DAYS_YR, 4),
        'cv':                        round(cv, 4),
        'peak_to_mean':              round(float(np.max(P_W) / np.mean(P_W)), 4),
        'n_exceedances_in_trace':    n_exceed,
        'ramp_limit_MW_s':           RAMP_LIMIT_MW_S,
        'trace_duration_s':          round(trace_s, 1),
    }


def compute_freq_metrics(t_f, delta_f):
    """Metrics from the swing-equation frequency deviation output (Δf in Hz)."""
    rocof = np.diff(delta_f) / np.diff(t_f)   # Hz/s
    nadir = float(60.0 + np.min(delta_f))
    n_uf  = int(np.sum(delta_f < FREQ_NADIR_LIMIT_HZ))
    return {
        'freq_nadir_hz':              round(nadir, 4),
        'freq_nadir_deviation_hz':    round(float(np.min(delta_f)), 4),
        'rocof_max_hz_per_s':         round(float(np.max(np.abs(rocof))), 5),
        'under_freq_events_per_year': round(annualize(n_uf, t_f[-1] - t_f[0]), 1),
        'under_freq_limit_hz':        60.0 + FREQ_NADIR_LIMIT_HZ,
    }


def compute_voltage_metrics(t_v, V_pu):
    """Metrics from PCC voltage magnitude (per-unit). Sag = below IEEE 1159 0.95 pu.

    voltage_sag_depth_pu (= 1 - min V) is the GRADED companion to the sag COUNT:
    it moves whenever the worst dip changes, even when neither run crosses the
    0.95 standards threshold (the count then reads 0/0 and hides the improvement).
    Lower depth = safer, matching compare_pair.py's 'lower = safer' RISK convention.
    The standards count stays untouched — this is additive, not a moved threshold."""
    min_v = float(np.min(V_pu))
    n_sag = int(np.sum(V_pu < VOLTAGE_SAG_LIMIT_PU))
    return {
        'min_voltage_pu':            round(min_v, 5),
        'voltage_sag_depth_pu':      round(1.0 - min_v, 5),
        'voltage_sag_limit_pu':      VOLTAGE_SAG_LIMIT_PU,
        'voltage_sag_events_per_yr': round(annualize(n_sag, t_v[-1] - t_v[0]), 1),
        'mean_voltage_pu':           round(float(np.mean(V_pu)), 5),
    }


def compute_efficiency_metrics(P_W, gflops):
    """Gflops/W (Green500) when an HPL score is available. No dollars."""
    PUE_MEAN = 1.35
    P_BASE_HW = 300.0  # W/server (matches readscript.m P_base_hardware)
    mean_server_cpu_W = max(1.0, float(np.mean(P_W)) / PUE_MEAN / N_SERVERS - P_BASE_HW)
    return {
        'hpl_gflops_server':      round(gflops, 4),
        'fleet_tflops_sustained': round(gflops * N_SERVERS / 1000.0, 2),
        'gflops_per_watt_server': round(gflops / mean_server_cpu_W, 6),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--gflops', type=float, default=None,
                        help='Sustained HPL Gflops score for this run')
    parser.add_argument('--selfcheck', action='store_true',
                        help='Run built-in assertions and exit')
    args = parser.parse_args()

    if args.selfcheck:
        selfcheck()
        return

    if not Path('S_PCC.csv').exists():
        sys.exit('S_PCC.csv not found. Run the MATLAB simulation pipeline first.')

    pcc = load_csv('S_PCC.csv')
    t, P_pcc = drop_nonincreasing(pcc[:, 0], pcc[:, 1])
    if len(t) < 2 or t[-1] - t[0] < RESAMPLE_DT_S:
        sys.exit('S_PCC.csv: not enough strictly-increasing time samples')
    t, P_pcc = resample_uniform(t, P_pcc)

    trapz = getattr(np, 'trapezoid', np.trapz)
    metrics = {
        'power': {
            'peak_MW':      round(float(np.max(P_pcc)) / 1e6, 4),
            'mean_MW':      round(float(np.mean(P_pcc)) / 1e6, 4),
            'trace_energy_MJ': round(float(trapz(P_pcc, t)) / 1e6, 3),
        },
        'risk': compute_risk_metrics(P_pcc, t),
        'thresholds': {
            'ramp_limit_MW_s':         RAMP_LIMIT_MW_S,
            'ramp_limit_source':       'Southern Company 20 MW/min cap (arXiv 2601.12686)',
            'lole_target_days_per_yr': LOLE_TARGET_DAYS_YR,
            'lole_source':             'NERC 1-in-10-years resource adequacy standard',
        },
    }

    fd_data = sanitize_2col(try_load('freq_dev.csv'))
    if fd_data is not None:
        metrics['frequency'] = compute_freq_metrics(fd_data[:, 0], fd_data[:, 1])

    vp_data = sanitize_2col(try_load('V_PCC.csv'))
    if vp_data is not None:
        metrics['voltage'] = compute_voltage_metrics(vp_data[:, 0], vp_data[:, 1])

    if args.gflops is not None:
        metrics['efficiency'] = compute_efficiency_metrics(P_pcc, args.gflops)

    with open('metrics.json', 'w') as f:
        json.dump(metrics, f, indent=2)


def selfcheck():
    t = np.array([0.0, 0.1, 0.1, 0.2, 0.3])
    p = np.array([1.0, 2.0, 99.0, 3.0, 4.0])
    t2, p2 = drop_nonincreasing(t, p)
    assert t2.tolist() == [0.0, 0.1, 0.2, 0.3] and p2.tolist() == [1.0, 2.0, 3.0, 4.0]

    tt = np.array([0.0, 1.0, 1.0, 2.0, 3.0])
    pp = np.array([1e6, 2e6, 2e6, 1.5e6, 1.6e6])
    tt, pp = drop_nonincreasing(tt, pp)
    r = compute_risk_metrics(pp, tt)
    assert all(np.isfinite(v) for v in r.values() if isinstance(v, (int, float)))

    fm = compute_freq_metrics(np.array([0.0, 1.0, 2.0]), np.array([0.0, -0.05, -0.02]))
    assert fm['freq_nadir_hz'] == 59.95
    assert abs(fm['rocof_max_hz_per_s'] - 0.05) < 1e-12

    # graded voltage: a 0.97 dip never breaches 0.95 (count blind), depth catches it
    vm = compute_voltage_metrics(np.array([0.0, 1.0, 2.0]), np.array([1.0, 0.97, 0.99]))
    assert vm['min_voltage_pu'] == 0.97 and vm['voltage_sag_events_per_yr'] == 0.0, vm
    assert abs(vm['voltage_sag_depth_pu'] - 0.03) < 1e-9, vm

    tm = np.array([0.0, 5.0, 5.00001, 10.0])
    pm = np.array([4e6, 4e6, 4.016e6, 4.016e6])   # 0.016 MW step over 10 us
    tu, pu = resample_uniform(tm, pm)
    assert abs(tu[1] - tu[0] - RESAMPLE_DT_S) < 1e-12 and tu[-1] >= 9.9
    rr_u = np.max(np.abs(np.diff(pu) / 1e6 / np.diff(tu)))
    assert rr_u < 0.34, f'resampled ramp {rr_u} should be ~0.16 MW/s, not 1600'

    assert sanitize_2col(None) is None
    assert sanitize_2col(np.array([0.0, 1.5])) is None
    s = sanitize_2col(np.array([[0.0, 1.0], [0.0, 9.0], [1.0, 2.0]]))
    assert s.shape == (11, 2) and s[-1, 1] == 2.0
    assert abs(s[5, 1] - 1.5) < 1e-12
    print('SELFCHECK OK')


if __name__ == '__main__':
    main()
