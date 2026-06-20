#!/bin/bash
# Wrapper script for cron - sources env and runs backup
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Load backup environment if exists
if [ -f "${SCRIPT_DIR}/.env.backup" ]; then
    set -a
    source "${SCRIPT_DIR}/.env.backup"
    set +a
fi

exec "${SCRIPT_DIR}/backup.sh"
