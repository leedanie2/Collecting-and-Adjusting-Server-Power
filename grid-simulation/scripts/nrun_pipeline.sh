#!/usr/bin/env bash
# nrun_pipeline.sh — regenerate the n=3 scoreboard from the three repeat runs.
#
# The aggregated result is already checked in (data/summary/, plus the per-run
# results/<cell>_r<N>_worst/metrics.json), so `python3 analysis/summarize_n3.py`
# alone reproduces the scoreboard with no MATLAB. This script regenerates the
# simulation outputs feeding it, and needs MATLAB R2025a + Simscape Electrical.
#
#   scripts/nrun_pipeline.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MATLAB="${MATLAB:-/usr/local/MATLAB/R2025a/bin/matlab}"
cd "$ROOT"

STAGED=data/runs/staged
CELLS=$(for w in hpl aisim2 step; do for c in baseline powersmoother rampc usagegov; do echo "${w}_${c}"; done; done)

echo "== stage (trim ramp.c to its plateau, copy the rest) =="
mkdir -p "$STAGED"
for r in 1 2 3; do
  for cell in $CELLS; do
    src="data/runs/run$r/$cell.csv"; dst="$STAGED/${cell}_r${r}.csv"
    if [[ "$cell" == *rampc* ]]; then
      python3 scripts/trim_auto.py "$src" "$dst" >/dev/null
    else
      cp "$src" "$dst"
    fi
  done
done

echo "== simulate 36 traces (one MATLAB session, model loaded once) =="
"$MATLAB" -batch "
staged = fullfile('$ROOT','$STAGED'); d = dir(fullfile(staged,'*.csv'));
cd(fullfile('$ROOT','simulation'));
for i = 1:numel(d)
  f = fullfile(d(i).folder, d(i).name);
  fprintf('=== [%d/%d] %s\n', i, numel(d), d(i).name);
  try, run_simulation(f); catch e, fprintf('FAIL %s: %s\n', d(i).name, e.message); end
end
disp('ALL_SIMS_DONE');"

echo "== score each results dir =="
for d in results/*_r[123]_worst; do ( cd "$d" && python3 ../../analysis/grid_metrics.py >/dev/null ); done

echo "== aggregate =="
python3 analysis/summarize_n3.py
