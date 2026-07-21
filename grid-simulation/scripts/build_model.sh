#!/usr/bin/env bash
# Build simulation/microgrid_phasor.slx with the CORRECT grid regime baked in.
#
# build_microgrid.m bakes the swing-equation coefficients (from GRID.H_sys) into
# the .slx at BUILD time. If readscript.m hasn't populated GRID.H_sys first, the
# build takes the H=6 conventional-grid default and every frequency/ROCOF metric
# comes out for the WRONG regime with no run-time error. So we ALWAYS run
# readscript first; build_microgrid.m now also errors out if H_sys is missing.
#
# Run once, and again after any edit to build_microgrid.m or the GRID params in
# readscript.m.
#
#   scripts/build_model.sh                       # sources GRID from noctl trace
#   scripts/build_model.sh data/other_trace.csv  # any valid trace works
#
# GRID params are trace-independent — the trace is only needed so readscript can
# run; it does not affect the baked swing-eq coefficients.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MATLAB="${MATLAB:-/usr/local/MATLAB/R2025a/bin/matlab}"
TRACE="${1:-$ROOT/data/traces/rapl_hpl_noctl.csv}"

[[ -x "$MATLAB" ]] || { echo "MATLAB not found at $MATLAB (set \$MATLAB)" >&2; exit 1; }
[[ -f "$TRACE" ]]  || { echo "Trace not found: $TRACE (needed so readscript can populate GRID)" >&2; exit 1; }

"$MATLAB" -batch "cd('$ROOT/simulation'); \
  RAPL_CSV='$TRACE'; \
  LOAD_SIGNALS_PATH='$ROOT/data/load_signals.csv'; \
  run('readscript.m'); \
  fprintf('GRID.H_sys=%.3f  S_base_grid=%.4g\n', GRID.H_sys, GRID.S_base_grid); \
  build_microgrid; disp('BUILD_OK');"

echo "Built: $ROOT/simulation/microgrid_phasor.slx"
