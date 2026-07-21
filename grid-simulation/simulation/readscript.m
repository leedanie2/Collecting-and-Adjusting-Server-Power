%% readscript.m  --  RAPL telemetry -> Specialized Power Systems load signal
%
% Produces workspace variables consumed by build_microgrid.m / Simulink:
%   facility_load_ts : timeseries, Data=[P_net(W)  Q_net(var)], Time=t(s)
%   LOAD             : struct  (Vnom_LL, f, P0, Q0)
%   GRID             : struct  (Vrms_LL, f, SCL_VA, XR, S_base_grid)
%
% Also writes load_signals.csv for grid_metrics.py:
%   columns: time_s | P_pre_ups_W | P_fac_W | Q_fac_var | PUE | PF
%
% Worst-case aggregation only: all N_servers run the measured trace in-phase
% (maximum grid stress). Diverse and sync-fraction modes were removed.

%% 1. Ingest RAPL telemetry ---------------------------------------------------
if ~exist('RAPL_CSV', 'var'), RAPL_CSV = 'rapl_data.csv'; end
data  = readmatrix(RAPL_CSV, 'NumHeaderLines', 1);
if size(data, 2) < 2
    error('readscript:badTrace', ...
        '%s: expected >=2 columns (time_s,power_W), got %d — wrong file or missing header?', ...
        RAPL_CSV, size(data, 2));
end
data  = data(all(isfinite(data(:, 1:2)), 2), 1:2);   % drop truncated rows
t     = data(:, 1);
P_cpu = data(:, 2);

if any(P_cpu < 0)
    warning('readscript:negPower', ...
        '%s: %d negative power samples clamped to 0 (bad capture?)', ...
        RAPL_CSV, nnz(P_cpu < 0));
    P_cpu = max(P_cpu, 0);
end

t = t - t(1);
[t, iu] = unique(t);
P_cpu = P_cpu(iu);
n       = numel(t);
if n < 2
    error('readscript:emptyTrace', ...
        '%s: fewer than 2 usable rows after dropping non-finite/duplicate-time samples', RAPL_CSV);
end
dt_mean = mean(diff(t));

%% 2. Parameters ---------------------------------------------------------------
N_servers       = 10000;
P_base_hardware = 300;     % W/server — non-CPU draw

PUE_idle = 1.60;
PUE_full = 1.15;

PF_min = 0.85;
PF_max = 0.97;

%% 3. Workload aggregation (worst case) ----------------------------------------
% All N_servers run the measured trace simultaneously — no decorrelation.
% Maximum possible grid stress for this workload type.
P_cpu_agg = N_servers * (P_cpu + P_base_hardware);

%% 4. Variable PUE -------------------------------------------------------------
if max(P_cpu_agg) <= 0
    error('readscript:zeroTrace', '%s: aggregated power is all-zero — bogus trace', RAPL_CSV);
end
P_it_frac = P_cpu_agg / max(P_cpu_agg);
PUE_t     = PUE_idle - (PUE_idle - PUE_full) * P_it_frac;
P_fac_raw = P_cpu_agg .* PUE_t;   % total facility active power before UPS

%% 5. Variable power factor ----------------------------------------------------
PF_t = PF_min + (PF_max - PF_min) * P_it_frac;

%% 6. UPS buffer (double-conversion) ------------------------------------------
% A double-conversion UPS draws from AC through a rectifier-charger whose
% output is controlled independently of the instantaneous IT load.  From the
% grid's perspective the facility demand follows the charger setpoint, which
% is a low-pass-filtered version of the IT load.
% Time constant ~15 s: conservative estimate for a large flywheel/battery UPS.
% Line-interactive UPS installations should set tau_ups = 0 to bypass this.
tau_ups = 15;   % seconds
if t(end) < tau_ups
    warning('readscript:shortTrace', ...
        '%s: trace (%.1fs) is shorter than tau_ups (%gs) — the UPS filter transient dominates a window this short; metrics are unreliable', ...
        RAPL_CSV, t(end), tau_ups);
end
% Per-step alpha (not one mean-dt alpha): captured traces can be irregularly
% sampled (the influx sampler merges sub-50ms windows), and a fixed alpha
% under/over-filters across gaps. Identical to the old code for uniform dt.
dt_k    = diff(t);
P_fac_W = zeros(n, 1);
P_fac_W(1) = P_fac_raw(1);
for k = 2:n
    a_k = exp(-dt_k(k-1) / tau_ups);
    P_fac_W(k) = a_k * P_fac_W(k-1) + (1 - a_k) * P_fac_raw(k);
end

Q_fac_var = P_fac_W .* tan(acos(PF_t));

%% 7. Simulink-ready timeseries -----------------------------------------------
facility_load_ts = timeseries([P_fac_W, Q_fac_var], t, 'Name', 'facility_PQ');

%% 8. Block-mask structs -------------------------------------------------------
LOAD.Vnom_LL = 25e3;
LOAD.f       = 60;
LOAD.P0      = mean(P_fac_W);
LOAD.Q0      = mean(Q_fac_var);

GRID.Vrms_LL    = 25e3;
GRID.f          = 60;
GRID.SCL_VA     = 100e6;
GRID.XR         = 7;
% Weak/islanded-microgrid scenario (2026-07-13 retune; citations in
% SOURCES.md "Grid stiffness"). The old regional-grid pairing
% (S_base 10 GW, H=6 s) left the swing equation inert (~59.995 Hz nadir
% for a ~5 MW facility transient) — it could not distinguish a smoother
% from none. H≈2-3 s is the low-inertia/high-renewable operating range;
% 50 MW base puts the facility at ~10% of system load (weak-grid hosting).
GRID.S_base_grid = 50e6;   % islanded/weak microgrid base (was 10e9 regional)
GRID.H_sys       = 2.5;    % low-inertia H (was 6; build_microgrid default)

%% 9. Export load signals for grid_metrics.py --------------------------------
% P_pre_ups_W is the raw facility demand before UPS filtering; included for
% diagnosing how much smoothing the UPS model is contributing.
if ~exist('LOAD_SIGNALS_PATH', 'var'), LOAD_SIGNALS_PATH = 'load_signals.csv'; end
writematrix([t, P_fac_raw, P_fac_W, Q_fac_var, PUE_t, PF_t], ...
    LOAD_SIGNALS_PATH, 'Delimiter', ',');
