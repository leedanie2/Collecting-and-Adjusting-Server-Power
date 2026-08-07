#!/bin/bash
# Daily Path B retrain + sync (laptop cron, 8am). The laptop only reaches the
# lab intermittently, so this is a silent no-op unless mycroft's InfluxDB
# answers. Trainer gates promotion itself (champion-challenger); syncing the
# current symlink target is idempotent either way.
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
ANALYSIS_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
cd "$ANALYSIS_DIR" || exit 1

curl -sf -m 5 http://mycroft:8086/health > /dev/null || exit 0

echo "=== $(date -u +%FT%TZ) train_cycle ==="
.venv/bin/python -m detectors.random_forest.trainer --once --start=-7d || exit 1
./scripts/sync_model_to_mycroft.sh
