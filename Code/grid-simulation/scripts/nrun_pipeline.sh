#!/usr/bin/env bash
# nrun_pipeline.sh — turn the 4 repeat runs into the n=4 scoreboard with error bars.
#
# Assumes data/runs/run{1,2,3,4}/ hold the raw recollect.sh traces (15 cells
# each). Stages them (trims rampc to its plateau, copies the rest) under unique
# per-run basenames, simulates all 60 in ONE MATLAB session (model loaded once),
# scores each, and aggregates.
#
#   scripts/nrun_pipeline.sh
#
# R2025a only — the model uses Specialized Power Systems, removed in R2026a.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MATLAB="${MATLAB:-/usr/local/MATLAB/R2025a/bin/matlab}"
cd "$ROOT"

RUNS_DIR=data/runs
STAGED="$RUNS_DIR/staged"
CELLS=$(for w in hpl aisim2 step; do for c in baseline powersmoother rampc usagegov; do echo "${w}_${c}"; done; done)

echo "== stage (trim rampc, copy the rest) =="
mkdir -p "$STAGED"
for r in 1 2 3 4; do
  for cell in $CELLS; do
    src="$RUNS_DIR/run$r/$cell.csv"; dst="$STAGED/${cell}_r${r}.csv"
    if [[ "$cell" == *rampc* ]]; then
      python3 scripts/trim_auto.py "$src" "$dst" >/dev/null
    else
      cp "$src" "$dst"
    fi
  done
done

echo "== simulate 60 traces (one MATLAB session) =="
# ONE LINE, deliberately. R2025a's -batch takes only the FIRST line of its
# argument: a multi-line string runs line 1 and exits 0, silently skipping the
# rest, and an argument that starts with a newline is rejected outright with
# "No MATLAB command specified". Either way the simulate stage does nothing and
# the failure only surfaces later as a missing metrics.json. Verified 2026-07-28.
"$MATLAB" -batch "staged = fullfile('$ROOT','$STAGED'); d = dir(fullfile(staged,'*.csv')); cd(fullfile('$ROOT','simulation')); for i = 1:numel(d), f = fullfile(d(i).folder, d(i).name); fprintf('=== [%d/%d] %s\n', i, numel(d), d(i).name); try, run_simulation(f); catch e, fprintf('FAIL %s: %s\n', d(i).name, e.message); end; end; disp('ALL_SIMS_DONE');" | tee /tmp/nrun_matlab.$$.log

# MATLAB exits 0 even when it ran nothing, so trust the marker, not the status.
if ! grep -q ALL_SIMS_DONE /tmp/nrun_matlab.$$.log; then
  echo "ERROR: MATLAB did not reach ALL_SIMS_DONE -- simulate stage did nothing." >&2
  echo "       (see the -batch note above; log kept at /tmp/nrun_matlab.$$.log)" >&2
  exit 1
fi
rm -f /tmp/nrun_matlab.$$.log

echo "== score each results dir =="
for d in results/*_r[1234]_worst; do ( cd "$d" && python3 ../../analysis/grid_metrics.py >/dev/null ); done

echo "== aggregate =="
python3 analysis/summarize_n4.py
