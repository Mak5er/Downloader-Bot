#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_FILE="${PROJECT_DIR}/logs/backup.log"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DUMP_FILE="/tmp/db_backup_${TIMESTAMP}.dump"

mkdir -p "$(dirname "$LOG_FILE")"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

cleanup() {
    rm -f "$DUMP_FILE"
}
trap cleanup EXIT

log "Starting backup..."

# Dump database via Docker
log "Dumping database..."
docker exec downloader-postgres pg_dump -Fc -U bot_user downloader_bot > "$DUMP_FILE"

DUMP_SIZE=$(ls -lh "$DUMP_FILE" | awk '{print $5}')
log "Dump completed: $DUMP_SIZE"

# Upload to restic (if configured)
if [ -n "${RESTIC_REPOSITORY:-}" ] && [ -f "${RESTIC_PASSWORD_FILE:-/dev/null}" ]; then
    log "Uploading to restic repository..."
    export RESTIC_REPOSITORY RESTIC_PASSWORD_FILE
    restic backup "$DUMP_FILE" --tag "db-backup" 2>&1 | tee -a "$LOG_FILE"
    log "Restic upload completed"

    # Prune old backups
    log "Pruning old backups..."
    restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune 2>&1 | tee -a "$LOG_FILE"
    log "Prune completed"
else
    log "WARNING: RESTIC_REPOSITORY not configured, backup saved locally at $DUMP_FILE"
    # Copy to project backups directory as fallback
    BACKUP_DIR="${PROJECT_DIR}/backups"
    mkdir -p "$BACKUP_DIR"
    cp "$DUMP_FILE" "${BACKUP_DIR}/db_backup_${TIMESTAMP}.dump"
    log "Backup saved to ${BACKUP_DIR}/db_backup_${TIMESTAMP}.dump"

    # Keep only last 7 local backups
    ls -t "${BACKUP_DIR}"/db_backup_*.dump 2>/dev/null | tail -n +8 | xargs -r rm -f
fi

log "Backup completed successfully"
