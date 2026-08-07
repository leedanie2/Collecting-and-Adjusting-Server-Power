#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_UNIT_DIR="${HOME}/.config/systemd/user"

usage() {
  cat <<'USAGE'
Usage:
  deploy/install-slew-governor.sh install   # install user unit + sudoers, do not start
  deploy/install-slew-governor.sh start     # start the governor (stops any capper unit)
  deploy/install-slew-governor.sh status    # show governor + detector user units
  deploy/install-slew-governor.sh verify    # selfcheck + stock-limit sanity, no root

Run on mycroft as user dlee from /home/dlee/code/Model/analysis.
The governor conflicts with rapl-capper*.service (same RAPL files).
USAGE
}

require_mycroft_layout() {
  if [[ "${ROOT}" != "/home/dlee/code/Model/analysis" ]]; then
    echo "Refusing: expected /home/dlee/code/Model/analysis, got ${ROOT}" >&2
    exit 2
  fi
}

case "${1:-}" in
  install)
    require_mycroft_layout
    mkdir -p "${USER_UNIT_DIR}"
    cp "${ROOT}/deploy/slew-governor.service" "${USER_UNIT_DIR}/slew-governor.service"
    systemctl --user daemon-reload
    sudo install -m 0440 "${ROOT}/deploy/slew-governor.sudoers" /etc/sudoers.d/slew-governor
    sudo visudo -cf /etc/sudoers.d/slew-governor
    echo "Installed but not started. Next: deploy/install-slew-governor.sh verify && ... start"
    ;;
  start)
    require_mycroft_layout
    systemctl --user stop rapl-capper.service rapl-capper-dry-run.service 2>/dev/null || true
    systemctl --user start slew-governor.service
    systemctl --user --no-pager --full status slew-governor.service
    ;;
  status)
    systemctl --user --no-pager --full status slew-governor.service spike-daemon-rf.service || true
    ;;
  verify)
    require_mycroft_layout
    "${ROOT}/.venv/bin/python" -m actuators.rapl_capper --selfcheck | tail -1
    echo "--- current RAPL limits (expect stock 205000000 / 246000000 per socket) ---"
    grep . /sys/class/powercap/intel-rapl:*/constraint_*_power_limit_uw 2>/dev/null \
      | grep -v ':[01]:' || true
    ;;
  *)
    usage
    exit 2
    ;;
esac
