#!/bin/bash
set -euo pipefail

echo "[backup] Starting backup service..."

B2_KEY_ID="${B2_KEY_ID:-}"
B2_APP_KEY="${B2_APP_KEY:-}"
BACKUP_CRON="${BACKUP_CRON:-0 3 * * *}"

# Authorize B2 if credentials provided
if [ -n "$B2_KEY_ID" ] && [ -n "$B2_APP_KEY" ]; then
    echo "[backup] Authorizing B2..."
    b2 authorize-account "$B2_KEY_ID" "$B2_APP_KEY"
    echo "[backup] B2 authorized"
else
    echo "[backup] WARNING: B2 credentials not set, local backups only"
fi

# Create local backup directory
mkdir -p /backups

# Write cron schedule
echo "${BACKUP_CRON} /backup.sh >> /var/log/backup.log 2>&1" > /etc/crontabs/root

echo "[backup] Cron schedule: ${BACKUP_CRON}"
echo "[backup] Starting cron daemon..."
exec crond -f -l 2
