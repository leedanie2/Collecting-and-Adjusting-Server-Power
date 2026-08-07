#!/bin/bash
# Sync latest model from local trainer to mycroft via SCP.
# Run this after detectors.random_forest.trainer completes a successful refit.
#
# Usage: ./scripts/sync_model_to_mycroft.sh [local_model_dir] [remote_dest]
# Default: mycroft:/home/dlee/code/Model/analysis/data/models/
# ("mycroft" is the ~/.ssh/config alias -> User dlee, HostName mycroft)
# (detectors.random_forest.scorer reads core.telemetry.DATA_DIR/models)

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
ANALYSIS_DIR=$(cd "$SCRIPT_DIR/.." && pwd)

TRAINER_MODEL_DIR="${1:-$ANALYSIS_DIR/data/models}"
REMOTE_DEST="${2:-mycroft:/home/dlee/code/Model/analysis/data/models/}"
REMOTE_HOST=$(echo "$REMOTE_DEST" | cut -d: -f1)

if [[ ! -d "$TRAINER_MODEL_DIR" ]]; then
    echo "Error: trainer model dir not found: $TRAINER_MODEL_DIR"
    exit 1
fi

if [[ ! -L "$TRAINER_MODEL_DIR/spike_model_current" ]]; then
    echo "Error: no symlink at $TRAINER_MODEL_DIR/spike_model_current"
    exit 1
fi

# Get the target of the symlink (versioned model file)
MODEL_FILE=$(readlink "$TRAINER_MODEL_DIR/spike_model_current")
if [[ ! -f "$TRAINER_MODEL_DIR/$MODEL_FILE" ]]; then
    echo "Error: model file not found: $TRAINER_MODEL_DIR/$MODEL_FILE"
    exit 1
fi

echo "Syncing $MODEL_FILE to $REMOTE_DEST ..."
scp "$TRAINER_MODEL_DIR/$MODEL_FILE" "$REMOTE_DEST"
if [[ $? -ne 0 ]]; then
    echo "Error: SCP failed"
    exit 1
fi

# Update the remote symlink
echo "Updating remote symlink..."
ssh "$REMOTE_HOST" "cd $(echo $REMOTE_DEST | cut -d: -f2) && ln -sf $MODEL_FILE spike_model_current"
if [[ $? -ne 0 ]]; then
    echo "Warning: failed to update remote symlink, but file was copied"
    exit 1
fi

echo "✓ Model synced successfully"
