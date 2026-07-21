function run_simulation(rapl_csv)
% run_simulation('aisim2_baseline.csv')   % worst case (all N_servers in-phase)
%
% Worst-case aggregation is the only mode. Pass just the filename; the function
% resolves it against data/traces/ automatically. Output -> results/<basename>_worst/.

if nargin < 1 || isempty(rapl_csv), rapl_csv = 'rapl_data.csv'; end

sim_dir  = fileparts(mfilename('fullpath'));
root_dir = fileparts(sim_dir);

if ~contains(rapl_csv, filesep)
    rapl_csv = fullfile(root_dir, 'data', 'traces', rapl_csv);
end
if ~isfile(rapl_csv)
    error('File not found: %s', rapl_csv);
end

[~, base, ~] = fileparts(rapl_csv);
run_name = sprintf('%s_worst', base);

out_dir = fullfile(root_dir, 'results', run_name);
if ~exist(out_dir, 'dir'), mkdir(out_dir); end

RAPL_CSV          = rapl_csv;
LOAD_SIGNALS_PATH = fullfile(root_dir, 'data', 'load_signals.csv');
run(fullfile(sim_dir, 'readscript.m'));

% ── Smoothing placeholder ─────────────────────────────────────────────────────
% Apply workload-based power smoothing to facility_load_ts here before
% passing to the grid model. Replace this comment with the smoothing call:
%
%   facility_load_ts = smooth_power(facility_load_ts, params);
%
% ─────────────────────────────────────────────────────────────────────────────

ts = facility_load_ts;
facility_load_pq = [ts.Time, ts.Data(:,1), ts.Data(:,2)];
assignin('base', 'facility_load_pq', facility_load_pq);

load_system(fullfile(sim_dir, 'microgrid_phasor'));
set_param('microgrid_phasor', 'StopTime', num2str(ts.Time(end)));

out   = sim('microgrid_phasor');
S     = out.S_PCC;
t_s   = S.Time;
P_W   = real(S.Data);
Q_var = imag(S.Data);

writematrix([t_s, P_W, Q_var], fullfile(out_dir, 'S_PCC.csv'));

% Frequency deviation from swing equation (Hz relative to 60 Hz).
% NB: out is a Simulink.SimulationOutput, not a struct — isfield() is
% always false on it and silently skips the write; use who(out).
if ismember('freq_dev', who(out))
    fd = out.freq_dev;
    writematrix([fd.Time, fd.Data], fullfile(out_dir, 'freq_dev.csv'));
else
    warning('run_simulation:noFreqDev', ...
        'freq_dev missing from sim output — stale .slx without the swing-eq block? Rebuild via scripts/build_model.sh');
end

% PCC voltage magnitude (per-unit)
if ismember('V_PCC', who(out))
    vp = out.V_PCC;
    writematrix([vp.Time, vp.Data], fullfile(out_dir, 'V_PCC.csv'));
else
    warning('run_simulation:noVPCC', ...
        'V_PCC missing from sim output — stale .slx? Rebuild via scripts/build_model.sh');
end

load_signals_src = fullfile(root_dir, 'data', 'load_signals.csv');
copyfile(load_signals_src, fullfile(out_dir, 'load_signals.csv'));
save(fullfile(out_dir, 'sim_results.mat'), 'out', 'LOAD', 'GRID', 'facility_load_ts');
