#!/usr/bin/env bash
# quiesce.sh — stop/restore mycroft's on-box observability stack.
#
# The "idle power swing 195-210 W" is observer self-load, not physics: influxd
# runs on the box at ~82% of a core continuously and bursts every 5s, which is
# the ~15 W ripple sitting inside every trace we feed the MATLAB grid model.
# Baseline traces literally start at 194.5 W (see data/traces/hpl_baseline.csv).
#
# Stopping the stack means nothing can write to InfluxDB during a run, so
# traces must be captured straight to CSV by collect_rapl.py -- which is how
# instructions.md already does it. Nothing downstream changes.
#
#   sudo ./quiesce.sh stop      # tear down, record what was running
#   sudo ./quiesce.sh status    # what's still making noise
#   sudo ./quiesce.sh start     # put it all back
#   ./quiesce.sh dry-run        # print the plan, touch nothing (no root needed)
#
# Shared box: other users lose Grafana/Influx between stop and start.
set -uo pipefail

STATE=/var/tmp/quiesce.state
UNITS=(influx2.service rapl-hf-sampler.service pmlogger.service pmlogger_farm.service pmcd.service)
# desktop cruft is opt-in via DESKTOP=1 -- worth ~a few W but gdm
# takes out anyone on the physical console, so it is not the default.
DESKTOP_UNITS=(cups.service ModemManager.service avahi-daemon.service colord.service switcheroo-control.service)
# Root-owned nohup pollers + the user-owned scorer/Grafana. Matched on cmdline.
PATTERNS=(
  'python3 .*pdu_influx\.py'
  'redfish_influx\.py'
  'sys_influx\.py'
  'detectors\.random_forest\.scorer'
  'grafana server'
  'collect_rapl\.py'          # orphans from earlier runs -- 4 were live on 2026-07-22
)

[[ "${DESKTOP:-0}" == 1 ]] && UNITS+=("${DESKTOP_UNITS[@]}")

# systemd --user units. Killing their processes is useless -- systemd restarts
# them seconds later, which is exactly how spike-daemon-rf survived a quiesce
# and ran through a whole collection. They must be stopped as units.
USER_UNITS=(spike-daemon-rf.service)
USER_MGR=${USER_MGR:-${SUDO_USER:-$USER}}

uctl() { systemctl --user -M "${USER_MGR}@" "$@" 2>/dev/null; }

# A process supervised by any systemd unit must NOT be restored by hand -- the
# unit brings it back on its own, and a hand-restored copy becomes a duplicate.
unit_of_pid() {
  grep -o '[a-zA-Z0-9_.@-]*\.service' "/proc/$1/cgroup" 2>/dev/null | head -1
}

need_root() {
  [[ $EUID -eq 0 ]] || { echo "must be root: sudo $0 $*" >&2; exit 1; }
}

matching_pids() {
  # Never match this script or its own pgrep children.
  pgrep -f "$1" 2>/dev/null | grep -vx "$$" || true
}

# The InfluxDB *database* is a root-owned podman container, NOT influx2.service
# (that unit is only the Python streamer). It is the ~87%-of-a-core writer
# behind the 5s ripple, so quiescing without stopping it accomplishes nothing.
influx_containers() {
  podman ps --format '{{.Names}}' 2>/dev/null | grep -i influx || true
}

cmdline_of() { tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null | tr '\n' ' '; }

plan() {
  echo "units to stop:"
  for u in "${UNITS[@]}"; do
    # is-active exits nonzero for inactive units but still prints the state,
    # so no `|| echo unknown` fallback -- that just prints a second line.
    printf '  %-28s %s\n' "$u" "$(systemctl is-active "$u" 2>/dev/null)"
  done
  echo "systemd --user units to stop:"
  for uu in "${USER_UNITS[@]}"; do printf '  %-28s %s\n' "$uu" "$(uctl is-active "$uu")"; done
  echo "podman containers to stop:"
  local found; found=$(influx_containers)
  [[ -n "$found" ]] && sed 's/^/  /' <<<"$found" || echo "  (none visible -- needs root)"
  echo "processes to kill:"
  for p in "${PATTERNS[@]}"; do
    for pid in $(matching_pids "$p"); do
      printf '  %-8s %s\n' "$pid" "$(cmdline_of "$pid" | cut -c1-90)"
    done
  done
}

do_stop() {
  need_root stop
  : > "$STATE"
  for u in "${UNITS[@]}"; do
    if systemctl is-active --quiet "$u"; then
      echo "unit $u" >> "$STATE"
      systemctl stop "$u" && echo "stopped $u"
    fi
  done
  for uu in "${USER_UNITS[@]}"; do
    if uctl is-active --quiet "$uu"; then
      echo "user-unit $uu" >> "$STATE"
      uctl stop "$uu" && echo "stopped user unit $uu"
    fi
  done
  for cn in $(influx_containers); do
    echo "container $cn" >> "$STATE"
    podman stop -t 20 "$cn" >/dev/null && echo "stopped container $cn"
  done
  # Collect PIDs across all patterns FIRST and dedupe. One process can match two
  # patterns (the sys_influx/pdu_influx launcher matches both), and duplicates
  # already running would each be recorded and then faithfully re-spawned --
  # that is how three RF scorers accumulated over successive stop/start cycles.
  declare -A seen_cmd=()
  for pid in $(for p in "${PATTERNS[@]}"; do matching_pids "$p"; done | sort -un); do
    # cmdline_of flattens newlines -- the launcher is a multi-line `bash -c`,
    # which would otherwise corrupt the one-record-per-line state file.
    cmd=$(cmdline_of "$pid")
    [[ -z "$cmd" ]] && continue
    cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null)
    user=$(stat -c %U "/proc/$pid" 2>/dev/null)
    u=$(unit_of_pid "$pid")
    if [[ -n "$u" ]]; then
      echo "supervised by $u, not restoring by hand: $pid"
      kill "$pid" 2>/dev/null
      continue
    fi
    if [[ -z "${seen_cmd[$cmd]:-}" ]]; then
      seen_cmd[$cmd]=1
      echo "proc ${user}|${cwd}|${cmd}" >> "$STATE"
    else
      echo "duplicate, will NOT be restored: $pid ($(echo "$cmd" | cut -c1-50))"
    fi
    kill "$pid" 2>/dev/null && echo "killed $pid ($(echo "$cmd" | cut -c1-60))"
  done
  sleep 2
  # Anything that ignored SIGTERM gets SIGKILL; a straggler still writing RAPL
  # would defeat the whole point of the quiesce.
  for p in "${PATTERNS[@]}"; do
    for pid in $(matching_pids "$p"); do
      kill -9 "$pid" 2>/dev/null && echo "SIGKILLed $pid"
    done
  done
  echo "--- state saved to $STATE"
  do_status
}

do_start() {
  need_root start
  [[ -f "$STATE" ]] || { echo "no $STATE -- nothing recorded to restore" >&2; exit 1; }
  while IFS= read -r line; do
    case "$line" in
      user-unit\ *) uu=${line#user-unit }; uctl start "$uu" && echo "started user unit $uu" ;;
      container\ *) cn=${line#container }; podman start "$cn" >/dev/null && echo "started container $cn" ;;
      unit\ *) u=${line#unit }; systemctl start "$u" && echo "started $u" ;;
      proc\ *) rest=${line#proc }
               user=${rest%%|*}; rest=${rest#*|}
               cwd=${rest%%|*};  cmd=${rest#*|}
               [[ -d "$cwd" ]] || cwd=/tmp
               # collect_rapl.py orphans are garbage -- do not resurrect them.
               [[ "$cmd" == *collect_rapl.py* ]] && { echo "skipped orphan: $cmd"; continue; }
               ( cd "$cwd" && setsid runuser -u "$user" -- bash -c "nohup $cmd >> nohup.out 2>&1 &" ) \
                 && echo "restarted [$user] $(echo "$cmd" | cut -c1-60)" ;;
    esac
  done < "$STATE"
  mv "$STATE" "$STATE.restored"
}

do_status() {
  echo "=== residual load (5s sample) ==="
  python3 - <<'EOF'
import time, os
def jiffies(pid):
    try:
        f = open(f"/proc/{pid}/stat").read().rsplit(") ", 1)[1].split()
        return (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")
    except Exception:
        return None
pids = [p for p in os.listdir("/proc") if p.isdigit()]
t0 = {p: jiffies(p) for p in pids}
s0 = time.time(); time.sleep(5); s1 = time.time()
rows = []
for p in pids:
    a, b = t0.get(p), jiffies(p)
    if a is None or b is None or b - a <= 0:
        continue
    try:
        cmd = open(f"/proc/{p}/comm").read().strip()
    except Exception:
        continue
    rows.append((100 * (b - a) / (s1 - s0), p, cmd))
rows.sort(reverse=True)
if not rows:
    print("  (nothing measurable -- box is quiet)")
for pct, p, cmd in rows[:10]:
    print(f"  {pct:6.1f}% core  {p:>8}  {cmd}")
EOF
}

case "${1:-}" in
  stop)    do_stop ;;
  start)   do_start ;;
  status)  do_status ;;
  dry-run) plan ;;
  *) sed -n '2,20p' "$0"; exit 1 ;;
esac
