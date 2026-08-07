#!/bin/bash
# AI-training-style power load: long busy phase, brief checkpoint dip, repeat.
set -e

BUSY_S=18
DIP_S=3
STAGGER_S=0.003   # spread stop/resume across ranks, avoids sync step-load spike

mpirun -np 128 --oversubscribe ./xhpl 2>/dev/null | tee results.txt &
XHPL_PID=$!

while kill -0 "$XHPL_PID" 2>/dev/null; do
  sleep "$BUSY_S"
  kill -0 "$XHPL_PID" 2>/dev/null || break

  # -x excludes mpirun (its cmdline also contains "xhpl")
  for pid in $(pgrep -x xhpl); do
    kill -STOP "$pid" 2>/dev/null || true
    sleep "$STAGGER_S"
  done

  sleep "$DIP_S"

  for pid in $(pgrep -x xhpl); do
    kill -CONT "$pid" 2>/dev/null || true
    sleep "$STAGGER_S"
  done
done

wait "$XHPL_PID"
