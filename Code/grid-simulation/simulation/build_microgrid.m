%% build_microgrid.m  (rev. for MATLAB R2025a / Specialized Power Systems)
% Programmatically builds a Phasor-mode SPS microgrid model that consumes
% the workspace variables produced by readscript.m:
%
%   facility_load_ts : timeseries, Data = [P(W) Q(var)], Time = t(s)
%   LOAD             : struct  (Vnom_LL, f, P0, Q0)
%   GRID             : struct  (Vrms_LL, f, SCL_VA, XR)
%
% Usage:
%   >> readscript            % populates facility_load_ts, LOAD, GRID
%   >> build_microgrid       % builds & saves microgrid_phasor.slx
%   >> out = sim('microgrid_phasor');
%   >> GRID.S_PCC_VA = out.S_PCC;     % complex apparent power at PCC (VA)
%
% Key revisions vs. the original script:
%   * powergui pulled from 'powerlib/powergui' (root), not powerlib_extras.
%   * SPS electrical terminals (A/B/C) are *connection ports*, not regular
%     in/outports. All electrical wiring now uses PortHandles
%     (LConn/RConn) instead of 'Block/N' strings, which silently fails or
%     errors for SPS blocks.
%   * The Three-Phase Dynamic Load with external control exposes ONE
%     Simulink input expecting a 2-element [P Q] vector — not separate P
%     and Q ports. A single From Workspace block now feeds [t P Q].
%   * Nominal values (V, f, P0, Q0, short-circuit level, X/R) are taken
%     from the GRID / LOAD structs created by readscript.m instead of
%     hard-coded 12.47 kV values that contradicted the 25 kV data prep.
%   * The script no longer clobbers the LOAD struct (the original
%     reassigned LOAD to a block-path string).
%   * StopTime is derived from the telemetry time vector (~70 s), not a
%     hard-coded 24 h.
%   * Mask parameters are set through setp(), which matches candidate
%     parameter names against the block's actual DialogParameters and, on
%     a miss, prints the valid names so a rename in a future release is a
%     one-line fix instead of a hard error.
%   * To Workspace output renamed S_PCC (timeseries) to avoid colliding
%     with GRID.SCL_VA, which is an *input* (source short-circuit level).
%
% Phasor convention note: SPS phasor measurements are PEAK-valued complex
% phasors (in SI units when per-unit output is off), so the three-phase
% complex power is S = sum(V .* conj(I)) / 2.

%% -----------------------------------------------------------------------
%  0.  Configuration (pulled from readscript.m structs when present)
% -----------------------------------------------------------------------
MDL = 'microgrid_phasor';

if exist('GRID', 'var') && isstruct(GRID)
    FREQ   = GRID.f;            % Hz
    VNOM   = GRID.Vrms_LL;      % V L-L rms (source)
    SCL_VA = GRID.SCL_VA;       % source short-circuit level (VA)
    XR     = GRID.XR;           % source X/R ratio
else
    warning('build_microgrid:noGRID', ...
        'GRID struct not found — run readscript.m first. Using defaults.');
    FREQ = 60;  VNOM = 25e3;  SCL_VA = 100e6;  XR = 7;
end

if exist('LOAD', 'var') && isstruct(LOAD)
    VLOAD = LOAD.Vnom_LL;       % V L-L rms (load nominal)
    P0    = LOAD.P0;            % W   (init for load-flow/block init)
    Q0    = LOAD.Q0;            % var
else
    warning('build_microgrid:noLOAD', ...
        'LOAD struct not found — run readscript.m first. Using defaults.');
    VLOAD = VNOM;  P0 = 1e6;  Q0 = 0.33e6;
end

% Regional grid base power for per-unit conversion in the swing equation.
% Default 10 GW matches the LOLE calculation in grid_metrics.py.
if exist('GRID', 'var') && isfield(GRID, 'S_base_grid')
    S_BASE = GRID.S_base_grid;
else
    S_BASE = 10e9;
end

% Peak phase-to-ground voltage at nominal LL voltage (SPS uses peak phasors).
V_NOM_PEAK_PHASE = VNOM / sqrt(3) * sqrt(2);

%% -----------------------------------------------------------------------
%  1.  Prepare the From Workspace signal & derive StopTime
% -----------------------------------------------------------------------
if exist('facility_load_ts', 'var')
    [tPQ, Psig, Qsig] = local_extract_pq(facility_load_ts);
else
    warning('build_microgrid:noTS', ...
        'facility_load_ts not found — using a 70 s, %.2f MW / %.2f Mvar stub.', ...
        P0/1e6, Q0/1e6);
    tPQ  = (0:0.001:70)';
    Psig = P0 * ones(size(tPQ));
    Qsig = Q0 * ones(size(tPQ));
end

% From Workspace matrix format: [time, data...] -> outputs a 2-wide [P Q]
facility_load_pq = [tPQ, Psig, Qsig];           %#ok<NASGU>  (used by model)
TSTOP = tPQ(end);

%% -----------------------------------------------------------------------
%  2.  Create / reset model
% -----------------------------------------------------------------------
if bdIsLoaded(MDL), close_system(MDL, 0); end
if exist([MDL '.slx'], 'file'), delete([MDL '.slx']); end
new_system(MDL);
open_system(MDL);

set_param(MDL, ...
    'Solver',    'ode23tb', ...
    'StartTime', '0', ...
    'StopTime',  num2str(TSTOP, '%.10g'), ...
    'RelTol',    '1e-4');                 % MaxStep left 'auto' — the ~1 ms
                                          % load telemetry needs fine steps

% Block placement grid
col = @(n) 100 + (n-1)*260;
row = @(n) 100 + (n-1)*160;
pos = @(c,r) [col(c), row(r), col(c)+90, row(r)+70];

%% -----------------------------------------------------------------------
%  3.  powergui — Phasor mode  (R2025a path: root of powerlib)
% -----------------------------------------------------------------------
PGUI = [MDL '/powergui'];
add_block('powerlib/powergui', PGUI, 'Position', pos(1,1));
setp(PGUI, 'Phasor',        'SimulationType', 'SimulationMode');
setp(PGUI, num2str(FREQ),   'Frequency', 'Fphasor', 'freq');

%% -----------------------------------------------------------------------
%  4.  Three-Phase Source — impedance from short-circuit level
% -----------------------------------------------------------------------
BLK_SRC = [MDL '/Grid_Source'];
add_block('powerlib/Electrical Sources/Three-Phase Source', BLK_SRC, ...
    'Position', pos(1,3));
setp(BLK_SRC, num2str(VNOM, '%.10g'), 'Voltage');
setp(BLK_SRC, '0',                    'Phase', 'PhaseAngle');
setp(BLK_SRC, num2str(FREQ),          'Frequency');
setp(BLK_SRC, 'Yg',                   'InternalConnection', 'Connection');

% Prefer the mask's "specify impedance using short-circuit level" option;
% if that checkbox parameter isn't found, fall back to explicit R-L
% computed from |Z| = V^2 / Ssc and the X/R ratio.
okSC = setp(BLK_SRC, 'on', ...
    'ShortCircuit', 'SpecifyImpedance', 'SpecifyShortCircuit', 'ImpedanceSpec');
if okSC
    setp(BLK_SRC, num2str(SCL_VA, '%.10g'), ...
        'Level3ph', 'SCLevel', 'ShortCircuitLevel', 'Psc');
    setp(BLK_SRC, num2str(VNOM, '%.10g'), 'BaseVoltage', 'Vbase');
    setp(BLK_SRC, num2str(XR),            'XRratio', 'X_R', 'XR');
else
    Zmag = VNOM^2 / SCL_VA;               % ohms
    Rsrc = Zmag / sqrt(1 + XR^2);
    Xsrc = Rsrc * XR;
    Lsrc = Xsrc / (2*pi*FREQ);
    setp(BLK_SRC, num2str(Rsrc, '%.10g'), 'Resistance', 'R');
    setp(BLK_SRC, num2str(Lsrc, '%.10g'), 'Inductance', 'L');
end

%% -----------------------------------------------------------------------
%  5.  Three-Phase V-I Measurement at PCC  (SI units, complex phasors)
% -----------------------------------------------------------------------
BLK_M = [MDL '/PCC_Measurement'];
add_block('powerlib/Measurements/Three-Phase V-I Measurement', BLK_M, ...
    'Position', pos(2,3));
setp(BLK_M, 'phase-to-ground', 'VoltageMeasurement');
setp(BLK_M, 'yes',             'CurrentMeasurement');
% Per-unit outputs stay OFF so Compute_S yields true VA.
% In Phasor mode the output-format option should be Complex:
setp(BLK_M, 'Complex', 'OutputSignals', 'PhasorOutput', 'OutputFormat', 'OutputType');

%% -----------------------------------------------------------------------
%  6.  Three-Phase Dynamic Load — external [P Q] control
% -----------------------------------------------------------------------
BLK_LOAD = [MDL '/Facility_Load'];     % NB: string var renamed; LOAD struct preserved
add_block('powerlib/Elements/Three-Phase Dynamic Load', BLK_LOAD, ...
    'Position', pos(3,3));
setp(BLK_LOAD, sprintf('[%.10g %.10g]', VLOAD, FREQ), ...
    'NominalVoltageFrequency', 'Vn_fn', 'VnFn', 'NomVF', 'NominalVoltage');
setp(BLK_LOAD, sprintf('[%.10g %.10g]', P0, Q0), ...
    'PQ_init', 'InitialPQ', 'PoQo', 'Po_Qo', 'ActiveReactivePowers');
setp(BLK_LOAD, 'on', 'ExternalControl', 'external', 'ExtPQ');
% ZIP voltage exponents: data centers behave as near-constant-power loads
% (exponent ≈ 0).  SPS default is constant-impedance (exponent = 2), which
% is correct for motors but wrong for switch-mode PSU loads.
% R2025a uses a single 'NpNq' parameter taking [np nq] as a 2-element vector.
setp(BLK_LOAD, '[0 0]', 'NpNq', 'Np', 'np', 'ActivePowerExponent', 'ExponentP');

%% -----------------------------------------------------------------------
%  7.  From Workspace — single block feeding the [P Q] vector
% -----------------------------------------------------------------------
BLK_FW = [MDL '/Facility_PQ'];
add_block('simulink/Sources/From Workspace', BLK_FW, ...
    'Position',              pos(2,1), ...
    'VariableName',          'facility_load_pq', ...
    'Interpolate',           'on', ...
    'OutputAfterFinalValue', 'Holding final value');

%% -----------------------------------------------------------------------
%  8.  S = sum(V .* conj(I)) / 2   (peak phasors -> VA)
% -----------------------------------------------------------------------
BLK_S = [MDL '/Compute_S'];
add_block('simulink/User-Defined Functions/MATLAB Function', BLK_S, ...
    'Position', pos(4,3));
cfg = get_param(BLK_S, 'MATLABFunctionConfiguration');   % R2019b+ API
cfg.FunctionScript = sprintf([ ...
    'function S = compute_S(V, I)\n' ...
    '%%#codegen\n' ...
    '%% V, I: 3x1 complex PEAK phasors [a;b;c] in volts / amps.\n' ...
    '%% Three-phase complex power in VA (peak-phasor convention):\n' ...
    'S = sum(V .* conj(I)) / 2;\n']);

BLK_TW = [MDL '/S_PCC_Out'];
add_block('simulink/Sinks/To Workspace', BLK_TW, ...
    'Position',     pos(5,3), ...
    'VariableName', 'S_PCC', ...
    'SaveFormat',   'Timeseries');     % keeps the time vector, unlike 'Array'

%% -----------------------------------------------------------------------
%  8b. PCC voltage magnitude (per-unit)
%
%  SPS peak phasor convention: |V_phase_peak| = V_LL_rms / sqrt(3) * sqrt(2)
%  at nominal conditions, so V_pu = mean(|V_abc|) / V_NOM_PEAK_PHASE.
% -----------------------------------------------------------------------
BLK_CV = [MDL '/Compute_V'];
add_block('simulink/User-Defined Functions/MATLAB Function', BLK_CV, ...
    'Position', pos(4,5));
cfg_v = get_param(BLK_CV, 'MATLABFunctionConfiguration');
cfg_v.FunctionScript = sprintf([ ...
    'function V_pu = compute_V(V)\n' ...
    '%%#codegen\n' ...
    '%% V: 3x1 complex PEAK phase-to-ground phasors from PCC measurement.\n' ...
    '%% Nominal peak phase voltage at %.10g V LL-rms: %.10g V peak.\n' ...
    'V_pu = mean(abs(V)) / %.10g;\n'], VNOM, V_NOM_PEAK_PHASE, V_NOM_PEAK_PHASE);

BLK_TW_V = [MDL '/V_PCC_Out'];
add_block('simulink/Sinks/To Workspace', BLK_TW_V, ...
    'Position',     pos(5,5), ...
    'VariableName', 'V_PCC', ...
    'SaveFormat',   'Timeseries');

%% -----------------------------------------------------------------------
%  8c. Grid frequency deviation — simplified swing equation + governor
%
%  Transfer function from ΔP_e (W) to Δf (Hz):
%
%    G(s) = Δω(pu) / ΔP_e(pu)
%         = -(1 + τ_gov·s) / [2H·τ_gov·s² + (2H + D·τ_gov)·s + (D + 1/R)]
%
%    Δf(Hz) = G(s) · ΔP_e(W)/S_base · f0
%
%  Parameters (single-machine equivalent; scenario set by readscript.m):
%    H     — inertia constant. Default 6 s (conventional mixed grid);
%            GRID.H_sys overrides it — the weak/islanded-microgrid retune
%            uses 2.5 s (see SOURCES.md "Grid stiffness").
%    D     = 1          — load damping (1% load relief per 1% freq drop)
%    R     = 0.05       — governor droop (5%, standard utility setting)
%    τ_gov = 20 s       — governor + turbine time constant (steam turbine)
%    f0    = 60 Hz
%
%  Numerator  = [-τ_gov, -1]
%  Denominator = [2H·τ_gov, 2H+D·τ_gov, D+1/R]
%
%  A negative Δf indicates frequency fell below 60 Hz (expected for a
%  positive load ramp). Under-frequency relays typically trip at 59.3 Hz
%  (−0.7 Hz) per NERC BAL-003.
% -----------------------------------------------------------------------
% H_sys is baked into the swing-eq coefficients at BUILD time. If it is
% absent here, the model silently takes the H=6 conventional-grid default
% and every frequency/ROCOF metric comes out for the WRONG regime with no
% error at run time (this exact footgun produced nadir 59.995/ROCOF 0.00173
% instead of the weak-grid 59.9909/0.08309). Refuse to build without an
% explicit H_sys — run readscript.m first, or set GRID.H_sys by hand.
if ~exist('GRID', 'var') || ~isfield(GRID, 'H_sys')
    error('build_microgrid:noHsys', ...
        ['GRID.H_sys is not set — refusing to bake the default H=6 ' ...
         'conventional grid into the model. Run readscript.m first (it ' ...
         'sets the weak-grid retune H_sys=2.5), or set GRID.H_sys ' ...
         'explicitly before building.']);
end
H_sys    = GRID.H_sys;    % scenario inertia constant (s), from readscript.m
D_sys    = 1;      % load damping (pu/pu)
R_droop  = 0.05;   % governor droop (pu/pu)
tau_gov  = 20;     % governor time constant (s)
f0       = FREQ;   % 60 Hz

num_G = [-tau_gov, -1];
den_G = [2*H_sys*tau_gov, 2*H_sys + D_sys*tau_gov, D_sys + 1/R_droop];

% Compute_P: extract real power (W) from phasor V, I
BLK_CP = [MDL '/Compute_P'];
add_block('simulink/User-Defined Functions/MATLAB Function', BLK_CP, ...
    'Position', pos(3,6));
cfg_p = get_param(BLK_CP, 'MATLABFunctionConfiguration');
cfg_p.FunctionScript = [ ...
    'function P_e = compute_P(V, I)' newline ...
    '%#codegen' newline ...
    'P_e = real(sum(V .* conj(I)) / 2);' newline];

% Constant P0 (mean load — operating point the swing equation deviates from)
BLK_P0 = [MDL '/Const_P0'];
add_block('simulink/Sources/Constant', BLK_P0, ...
    'Position', pos(2,6), ...
    'Value',    num2str(P0, '%.10g'));

% Sum: ΔP_e = P_e - P0
BLK_SUM = [MDL '/Sum_dP'];
add_block('simulink/Math Operations/Sum', BLK_SUM, ...
    'Position',  pos(4,6), ...
    'Inputs',    '+-', ...
    'IconShape', 'rectangular');

% Gain: 1/S_base → ΔP_e in per-unit
BLK_GSBASE = [MDL '/Gain_Sbase'];
add_block('simulink/Math Operations/Gain', BLK_GSBASE, ...
    'Position', pos(5,6), ...
    'Gain',     num2str(1/S_BASE, '%.10g'));

% Transfer function: G(s) → Δω in per-unit
BLK_TF = [MDL '/SwingEq'];
add_block('simulink/Continuous/Transfer Fcn', BLK_TF, ...
    'Position',    pos(6,6), ...
    'Numerator',   mat2str(num_G), ...
    'Denominator', mat2str(den_G));

% Gain: Δω (pu) → Δf (Hz)
BLK_GF0 = [MDL '/Gain_f0'];
add_block('simulink/Math Operations/Gain', BLK_GF0, ...
    'Position', pos(7,6), ...
    'Gain',     num2str(f0));

% To Workspace: frequency deviation (Hz)
BLK_TW_F = [MDL '/freq_dev_Out'];
add_block('simulink/Sinks/To Workspace', BLK_TW_F, ...
    'Position',     pos(8,6), ...
    'VariableName', 'freq_dev', ...
    'SaveFormat',   'Timeseries');

%% -----------------------------------------------------------------------
%  9.  Wiring — electrical conn ports via PortHandles, signals via ports
% -----------------------------------------------------------------------
hSrc  = get_param(BLK_SRC,  'PortHandles');
hMeas = get_param(BLK_M,    'PortHandles');
hLoad = get_param(BLK_LOAD, 'PortHandles');
hFW   = get_param(BLK_FW,   'PortHandles');
hCS   = get_param(BLK_S,    'PortHandles');
hTW   = get_param(BLK_TW,   'PortHandles');

srcABC  = [hSrc.RConn,  hSrc.LConn];    % terminals, whichever side populated
measIn  = hMeas.LConn;                  % A B C
measOut = hMeas.RConn;                  % a b c
loadABC = [hLoad.LConn, hLoad.RConn];

assert(numel(srcABC) >= 3 && numel(measIn) >= 3 && ...
       numel(measOut) >= 3 && numel(loadABC) >= 3, ...
       'build_microgrid:connPorts', ...
       'Unexpected SPS connection-port layout — inspect PortHandles of the SPS blocks.');

for k = 1:3
    add_line(MDL, srcABC(k),  measIn(k),  'autorouting', 'on');  % Source -> Meas
    add_line(MDL, measOut(k), loadABC(k), 'autorouting', 'on');  % Meas -> Load
end

% Phasor-mode requires a resistive shunt at the bus: a current-source load in a
% loop with only an inductive source impedance is underdetermined (SPS error).
% 10 MΩ Y-shunt draws ~62.5 W/phase @ 25 kV — 0.003% of load, negligible.
BLK_SNB = [MDL '/Bus_Snubber'];
add_block('powerlib/Elements/Three-Phase Parallel RLC Branch', BLK_SNB, ...
    'Position', pos(3,1));
setp(BLK_SNB, 'R',    'BranchType', 'Type');            % resistance-only
setp(BLK_SNB, '1e7',  'Resistance', 'R', 'Rs');         % 10 MΩ
setp(BLK_SNB, '1e9',  'Inductance', 'L', 'Ls');         % near-open if L not disabled
setp(BLK_SNB, '1e-12','Capacitance','C', 'Cs');         % near-open if C not disabled
hSnb = get_param(BLK_SNB, 'PortHandles');
% Three-Phase Parallel RLC Branch is a pass-through (LConn=in, RConn=out).
% Connect LConn→bus and RConn→ground blocks to form Y-shunt to ground.
for k = 1:3
    add_line(MDL, measOut(k), hSnb.LConn(k), 'autorouting', 'on');   % bus side
    BLK_GNDk = sprintf('%s/Snubber_Gnd%d', MDL, k);
    add_block('powerlib/Elements/Ground', BLK_GNDk, 'Position', pos(3+k,1));
    hGk = get_param(BLK_GNDk, 'PortHandles');
    add_line(MDL, hSnb.RConn(k), hGk.LConn(1), 'autorouting', 'on'); % ground side
end

% [P Q] vector into the Dynamic Load's single external-control inport
assert(~isempty(hLoad.Inport), 'build_microgrid:noPQport', ...
    ['Dynamic Load has no Simulink inport — the external-control mask ', ...
     'option was not enabled. Check the setp warnings above.']);
add_line(MDL, hFW.Outport(1), hLoad.Inport(1), 'autorouting', 'on');

% Vabc / Iabc -> Compute_S -> To Workspace (S_PCC)
add_line(MDL, hMeas.Outport(1), hCS.Inport(1), 'autorouting', 'on');
add_line(MDL, hMeas.Outport(2), hCS.Inport(2), 'autorouting', 'on');
add_line(MDL, hCS.Outport(1),   hTW.Inport(1), 'autorouting', 'on');

% Voltage magnitude: Vabc -> Compute_V -> V_PCC_Out
% Fan-out from the same V outport (Simulink allows multiple add_line calls
% from the same source port; it inserts a branch point automatically).
hCV   = get_param(BLK_CV,   'PortHandles');
hTW_V = get_param(BLK_TW_V, 'PortHandles');
add_line(MDL, hMeas.Outport(1), hCV.Inport(1), 'autorouting', 'on');
add_line(MDL, hCV.Outport(1),   hTW_V.Inport(1), 'autorouting', 'on');

% Frequency deviation chain:
%   Vabc, Iabc -> Compute_P -> Sum_dP
%                 Const_P0  -> Sum_dP
%   Sum_dP -> Gain_Sbase -> SwingEq -> Gain_f0 -> freq_dev_Out
hCP    = get_param(BLK_CP,    'PortHandles');
hP0    = get_param(BLK_P0,    'PortHandles');
hSUM   = get_param(BLK_SUM,   'PortHandles');
hGSB   = get_param(BLK_GSBASE,'PortHandles');
hTF    = get_param(BLK_TF,    'PortHandles');
hGF0   = get_param(BLK_GF0,   'PortHandles');
hTW_F  = get_param(BLK_TW_F,  'PortHandles');

add_line(MDL, hMeas.Outport(1), hCP.Inport(1), 'autorouting', 'on');   % V → Compute_P
add_line(MDL, hMeas.Outport(2), hCP.Inport(2), 'autorouting', 'on');   % I → Compute_P
add_line(MDL, hCP.Outport(1),   hSUM.Inport(1), 'autorouting', 'on');  % P_e → Sum (+)
add_line(MDL, hP0.Outport(1),   hSUM.Inport(2), 'autorouting', 'on');  % P0  → Sum (-)
add_line(MDL, hSUM.Outport(1),  hGSB.Inport(1), 'autorouting', 'on');  % ΔP_e → Gain
add_line(MDL, hGSB.Outport(1),  hTF.Inport(1),  'autorouting', 'on');  % ΔP_pu → G(s)
add_line(MDL, hTF.Outport(1),   hGF0.Inport(1), 'autorouting', 'on');  % Δω_pu → ×f0
add_line(MDL, hGF0.Outport(1),  hTW_F.Inport(1),'autorouting', 'on');  % Δf_Hz → workspace

%% -----------------------------------------------------------------------
%  10.  Save & report
% -----------------------------------------------------------------------
save_system(MDL, fullfile(fileparts(mfilename('fullpath')), MDL));

%% -----------------------------------------------------------------------
%  Optional: run immediately
% -----------------------------------------------------------------------
% out = sim(MDL);
% GRID.S_PCC_VA = out.S_PCC;
% fprintf('Peak apparent power at PCC: %.3f MVA\n', max(abs(GRID.S_PCC_VA.Data))/1e6);

%% ========================================================================
%  Local functions
% ========================================================================
function ok = setp(blk, value, varargin)
% setp  Set the first mask parameter (case-insensitive) that exists on BLK.
%   Candidate names are matched against the block's actual
%   DialogParameters; on a total miss, the valid names are printed so a
%   parameter rename between releases is a one-line fix, not a crash.
ok = false;
dps = get_param(blk, 'DialogParameters');
if isempty(dps), names = {}; else, names = fieldnames(dps); end
for c = 1:numel(varargin)
    hit = find(strcmpi(names, varargin{c}), 1);
    if ~isempty(hit)
        try
            set_param(blk, names{hit}, value);
            ok = true;
        catch ME
            warning('build_microgrid:setFailed', ...
                '%s: setting ''%s'' = ''%s'' failed: %s', ...
                blk, names{hit}, value, ME.message);
        end
        return
    end
end
warning('build_microgrid:paramNotFound', ...
    ['%s: none of {%s} exist on this block — set it manually or add the ', ...
     'right name to the candidates.\n  Valid parameters: %s'], ...
    blk, strjoin(varargin, ', '), strjoin(names, ', '));
end

function [t, P, Q] = local_extract_pq(fts)
% Normalize facility_load_ts into column vectors t, P, Q.
if isa(fts, 'timeseries')
    t = fts.Time(:);
    D = squeeze(fts.Data);
    if ~isreal(D)
        P = real(D(:));  Q = imag(D(:));
    elseif size(D, 2) >= 2
        P = D(:, 1);     Q = D(:, 2);     % readscript.m format: [P Q]
    else
        P = D(:);        Q = zeros(size(P));
    end
elseif isstruct(fts) && isfield(fts, 'time') && isfield(fts, 'signals')
    t = fts.time(:);
    P = fts.signals.values(:, 1);
    Q = fts.signals.values(:, 2);
else
    error('build_microgrid:unknownFormat', ...
        'Unrecognized facility_load_ts format (expected timeseries or struct).');
end
% Sanitize for From Workspace: start at t=0, strictly increasing time.
t = t - t(1);
[t, iu] = unique(t);      % RAPL timestamps can repeat at ~1 ms resolution
P = P(iu);  Q = Q(iu);
end