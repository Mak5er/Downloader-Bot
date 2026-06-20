#!/bin/bash
set -euo pipefail

export RESTIC_REPOSITORY="${RESTIC_REPOSITORY:-}"
export RESTIC_PASSWORD="${RESTIC_PASSWORD:-}"
export AWS_ACCESS_KEY_ID="${B2_KEY_ID:-}"
export AWS_SECRET_ACCESS_KEY="${B2_APP_KEY:-}"

if [ -z "$RESTIC_REPOSITORY" ] || [ -z "$RESTIC_PASSWORD" ]; then
    echo "[init] RESTIC_REPOSITORY or RESTIC_PASSWORD not set, skipping init"
    exit 0
fi

if restic cat config 2>/dev/null; then
    echo "[init] Repository already initialized"
else
    echo "[init] Initializing new restic repository..."
    restic init 2>&1
    echo "[init] Repository initialized successfully"
fi
