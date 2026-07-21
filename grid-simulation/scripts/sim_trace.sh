#!/usr/bin/env bash
# Run one RAPL power trace through the grid simulation (wraps run_simulation.m).
# Worst-case aggregation only (all N_servers in-phase). Drop a time_s,power_W CSV
# into data/traces/, then:
#
#   scripts/sim_trace.sh aisim2_baseline.csv
#
# Pass just the filename; run_simulation.m resolves it against data/traces/ and
# writes results/<base>_worst/. Requires the model built (scripts/build_model.sh).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MATLAB="${MATLAB:-/usr/local/MATLAB/R2025a/bin/matlab}"
TRACE="${1:?usage: sim_trace.sh <trace.csv>}"

[[ -x "$MATLAB" ]] || { echo "MATLAB not found at $MATLAB (set \$MATLAB)" >&2; exit 1; }
[[ -f "$ROOT/data/traces/$TRACE" || -f "$TRACE" ]] || { echo "Trace not found in data/traces/: $TRACE" >&2; exit 1; }
[[ -f "$ROOT/simulation/microgrid_phasor.slx" ]] || { echo "Model not built — run scripts/build_model.sh first" >&2; exit 1; }

"$MATLAB" -batch "cd('$ROOT/simulation'); run_simulation('$TRACE'); disp('SIM_OK');"
