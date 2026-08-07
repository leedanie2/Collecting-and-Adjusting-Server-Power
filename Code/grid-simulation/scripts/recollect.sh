#!/usr/bin/env bash
# recollect.sh — re-capture the 3x4 trace matrix on a quiesced mycroft.
#
# Run AFTER `sudo quiesce.sh stop`. Writes time_s,power_W CSVs into a fresh
# directory so the contaminated originals in data/traces/ stay put for
# comparison. Nothing is committed.
#
#   sudo ./recollect.sh                      # all 12 cells
#   sudo ./recollect.sh hpl                  # one workload, all 4 conditions
#   sudo ./recollect.sh hpl baseline         # one cell
#   ./recollect.sh --list                    # print the matrix, run nothing
#
# Cells: {hpl, aisim2, step} x {baseline, powersmoother, rampc, usagegov}
#   usagegov = usage_edge -> rapl_capper (ceiling on rises, ballast pre-burn
#              on drops)
#
# slewgov (rapl_capper --slew alone, no detector) was RETIRED 2026-07-28 and is
# no longer collected. Its case block below is kept only so the archived n=4
# slewgov traces in git history stay reproducible; pass CONDITIONS=slewgov to
# run it deliberately.
set -uo pipefail

# Resolved against this repository so the matrix runs from a clone; each is
# overridable from the environment for a different deployment.
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
PYTHON=${PYTHON:-python3}
OUT=${OUT:-$HOME/traces_clean}
COLLECT="$REPO/measurement/collect_rapl.py"
SMOOTHER_DIR="$REPO/mitigation/power-smoother"
GOV_DIR="$REPO/mitigation/slew-governor"
WORKLOAD_DIR="$REPO/workloads"
WAVEFORM=${WAVEFORM:-step.waveform}
# HPL builds outside the tree: it needs an MPI toolchain and a 51.2 GB working
# set. Point this at the directory holding xhpl, HPL.dat and ai_load.sh.
HPL_DIR=${HPL_DIR:-$HOME/hpl-2.3/bin/Linux}
RAMP_BIN=${RAMP_BIN:-$OUT/ramp_seq}
# This ramps cores SEQUENTIALLY -- one fully up before the next starts -- which
# is the shape wanted here; a parallel ramp cannot produce a per-core
# engagement rate.
RAMP_SRC="$REPO/mitigation/ramp/ramp.c"
# 124 virtual cores (0-123). ramp.c pins the workload to exactly this list, so
# a truncated list starves it -- "0:63" previously parsed to the single core 0
# and left 128 HPL ranks sharing one core at idle power.
RAMP_CORES=${RAMP_CORES:-0:123}
# Sequential ramp: each core takes 100/rate seconds, so 200 %/s = 0.5 s/core
# = 2 cores/s = 62 s across 124 cores, both directions.
RAMP_UP=${RAMP_UP:-200}
RAMP_DOWN=${RAMP_DOWN:-200}
SETTLE=${SETTLE:-20}     # idle seconds between cells so thermals/power decay
# ai_sim_2.py draws a random REST/PREFILL schedule from SystemRandom() when
# --seed is omitted, so every cell would run a DIFFERENT workload and the
# baseline-vs-smoother comparison would confound smoother effect with schedule
# luck. Pin it. 3555822270 is the seed used for the committed runs.
# hpl (ai_load.sh) and step (fixed waveform file) are already deterministic.
AISIM2_SEED=${AISIM2_SEED:-3555822270}

# Both overridable from the env, so a broken cell can be excluded without
# editing the script:  sudo CONDITIONS="baseline usagegov" ./recollect.sh
read -ra WORKLOADS  <<< "${WORKLOADS:-hpl aisim2 step}"
read -ra CONDITIONS <<< "${CONDITIONS:-baseline powersmoother rampc usagegov}"

# ramp.c takes an argv, not a shell string, so dir and argv stay separate.
workload_dir() {
  case "$1" in
    hpl)    echo "$HPL_DIR" ;;
    aisim2) echo "$WORKLOAD_DIR" ;;
    step)   echo "$WORKLOAD_DIR" ;;
  esac
}
workload_argv() {
  case "$1" in
    hpl)    echo "bash ./ai_load.sh" ;;
    aisim2) echo "$PYTHON ai_sim_2.py --seed $AISIM2_SEED" ;;
    step)   echo "./load 124 $WAVEFORM" ;;
  esac
}

# Sets COND_PAT (a pkill pattern), never echoes. Capturing a background PID via
# `$(...)` deadlocks: the backgrounded subshell inherits the substitution's
# stdout pipe and holds it open until the daemon exits, so `$( )` never returns.
COND_PAT=""
start_condition() {
  COND_PAT=""
  local WCELL="${2:-cell}"   # workload name, for per-cell artifact filenames
  case "$1" in
    baseline|rampc) ;;   # rampc wraps the workload instead, see run_cell
    powersmoother)
      ( cd "$SMOOTHER_DIR" && exec "$PYTHON" power_smoother_16_2.py ) >/tmp/smoother.log 2>&1 &
      COND_PAT='power_smoother_16_2\.py' ;;
    slewgov)
      # Reactive control arm: the slew governor alone, hugging MEASURED power.
      # No detector -- --risk-file points at a flag nothing writes, and the
      # capper fails open on a stale/missing flag, so this is purely reactive.
      # That is the point: it is the denominator for usagegov below.
      # Exactly the gov16 ship config -- this string matches the NOPASSWD
      # sudoers entry on the deployment host, so do not reformat it.
      ( cd "$GOV_DIR" && exec "$PYTHON" -u ./rapl_capper.py \
          --slew --ballast --ballast-w-per-core 3.26 --ballast-max-cores 64 \
          --no-engage-on-dips \
          --risk-file "$OUT/spike_risk.flag" \
          --events "$OUT/${WCELL}_slewgov.events.jsonl" ) >"$OUT/${WCELL}_slewgov.gov.log" 2>&1 &
      COND_PAT='rapl_capper\.py' ;;
    usagegov)
      # PREDICTIVE arm: usage_edge (/proc/stat, leads power) drives the same
      # governor. Rising edges arm the RAPL ceiling, falling edges pre-burn
      # ballast so the fill is already running when load falls away.
      # Two processes -- the detector must be up before the capper polls.
      ( cd "$GOV_DIR" && exec "$PYTHON" -u ./usage_edge.py \
          --risk-file "$OUT/usage_spike_risk.flag" \
          --drop-risk-file "$OUT/usage_drop_risk.flag" \
          --json ) >"$OUT/${WCELL}_usagegov.detector.jsonl" 2>&1 &
      sleep 1
      ( cd "$GOV_DIR" && exec "$PYTHON" -u ./rapl_capper.py \
          --slew --ballast --ballast-w-per-core 3.26 --ballast-max-cores 64 \
          --no-engage-on-dips \
          --risk-file "$OUT/usage_spike_risk.flag" \
          --drop-risk-file "$OUT/usage_drop_risk.flag" \
          --events "$OUT/${WCELL}_usagegov.events.jsonl" ) >"$OUT/${WCELL}_usagegov.gov.log" 2>&1 &
      # Both get killed: the pattern matches either module path.
      COND_PAT='(rapl_capper|usage_edge)\.py' ;;
  esac
}

# The smoother forks ~128 workers; killing the parent PID alone strands them and
# they would pollute every later cell. Match on cmdline instead.
stop_condition() {
  [[ -z "$COND_PAT" ]] && return 0
  pkill -f "$COND_PAT" 2>/dev/null
  sleep 3
  pkill -9 -f "$COND_PAT" 2>/dev/null
  return 0
}

run_cell() {
  local w=$1 c=$2 csv="$OUT/${1}_${2}.csv"
  # Resume: a 30-min matrix should not restart from zero after one bad cell.
  if [[ -s "$csv" ]] && (( $(wc -l < "$csv") > 10 )); then
    echo "=== $w x $c -- already have $(wc -l < "$csv") rows, skipping"
    return 0
  fi
  echo "=== $w x $c -> $csv"

  start_condition "$c" "$w"
  [[ -n "$COND_PAT" ]] && { echo "    condition up, settling 5s"; sleep 5; }

  "$PYTHON" "$COLLECT" > "$csv" &
  local rapl_pid=$!
  sleep 1

  # Capture the workload's own output. HPL prints its sustained Gflops there
  # and ai_sim_2.py prints the schedule seed it used -- both went to the
  # terminal before, which is why the clean set has no Gflops and why the
  # unseeded aisim2 runs were only caught after the fact.
  local log="$OUT/${w}_${c}.log"
  if [[ "$c" == rampc ]]; then
    # All three workloads go through ramp.c now. composite_ramp is the same
    # ramping logic generalised to multiple workloads; with one
    # workload it buys nothing, and routing step through it made step_rampc
    # incomparable to the other two cells.
    ( cd "$(workload_dir "$w")" && "$RAMP_BIN" "$RAMP_CORES" "$RAMP_UP" "$RAMP_DOWN" $(workload_argv "$w") ) >"$log" 2>&1
  else
    ( cd "$(workload_dir "$w")" && $(workload_argv "$w") ) >"$log" 2>&1
  fi
  # Surface the two lines worth seeing live; the rest stays in the log.
  grep -hE "WR[0-9]+.*Gflops|Schedule seed" "$log" 2>/dev/null | sed "s/^/    /" || true

  kill "$rapl_pid" 2>/dev/null; wait "$rapl_pid" 2>/dev/null
  stop_condition

  echo "    $(wc -l < "$csv") rows, settling ${SETTLE}s"
  sleep "$SETTLE"
}

if [[ "${1:-}" == --list ]]; then
  for w in "${WORKLOADS[@]}"; do for c in "${CONDITIONS[@]}"; do echo "$w x $c"; done; done
  exit 0
fi

[[ $EUID -eq 0 ]] || { echo "must be root (RAPL energy_uj): sudo $0 $*" >&2; exit 1; }
mkdir -p "$OUT"
# Always rebuild -- a stale binary from the wrong ramp.c is exactly the failure
# that produced a 27-minute idle trace.
cc -O2 -Wall -o "$RAMP_BIN" "$RAMP_SRC" -lpthread -lm || exit 1
"$RAMP_BIN" --selfcheck >/dev/null 2>&1 || { echo "ramp selfcheck failed" >&2; exit 1; }

sel_w=("${@:1:1}"); [[ -z "${1:-}" ]] && sel_w=("${WORKLOADS[@]}")
sel_c=("${@:2:1}"); [[ -z "${2:-}" ]] && sel_c=("${CONDITIONS[@]}")
for w in "${sel_w[@]}"; do
  for c in "${sel_c[@]}"; do
    run_cell "$w" "$c"
  done
done
echo "done -> $OUT"
