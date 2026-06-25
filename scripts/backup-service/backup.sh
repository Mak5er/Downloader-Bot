#!/bin/bash
set -euo pipefail

B2_BUCKET="${B2_BUCKET:-maxload}"
B2_KEY_ID="${B2_KEY_ID:-}"
B2_APP_KEY="${B2_APP_KEY:-}"
BACKUP_HOST="${BACKUP_HOST:-postgres}"
BACKUP_DB="${BACKUP_DB:-downloader_bot}"
BACKUP_USER="${BACKUP_USER:-bot_user}"
BACKUP_PASS="${BACKUP_PASS:-${POSTGRES_PASSWORD:-changeme}}"
export PGPASSWORD="$BACKUP_PASS"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DUMP_FILE="/tmp/db_backup_${TIMESTAMP}.dump"
LOG_PREFIX="[backup ${TIMESTAMP}]"

log() { echo "${LOG_PREFIX} $*"; }

cleanup() { rm -f "$DUMP_FILE"; }
trap cleanup EXIT

log "Starting database dump..."
if ! pg_dump -Fc -h "$BACKUP_HOST" -U "$BACKUP_USER" "$BACKUP_DB" > "$DUMP_FILE"; then
    log "ERROR: pg_dump failed"
    exit 1
fi
if [ ! -s "$DUMP_FILE" ]; then
    log "ERROR: pg_dump produced empty file"
    exit 1
fi
DUMP_SIZE=$(ls -lh "$DUMP_FILE" | awk '{print $5}')
log "Dump completed: ${DUMP_SIZE}"

# Always save locally for restore
LOCAL_FILE="/backups/db_backup_${TIMESTAMP}.dump"
cp "$DUMP_FILE" "$LOCAL_FILE"
log "Saved to ${LOCAL_FILE}"

# Keep only last 7 local backups
ls -t /backups/db_backup_*.dump 2>/dev/null | tail -n +8 | xargs -r rm -f

# Upload to B2 if configured
if [ -n "$B2_KEY_ID" ] && [ -n "$B2_APP_KEY" ] && [ -n "$B2_BUCKET" ]; then
    log "Uploading to B2 bucket: ${B2_BUCKET}..."
    if b2 upload-file "$B2_BUCKET" "$DUMP_FILE" "backups/db_backup_${TIMESTAMP}.dump" 2>&1; then
        log "Upload completed"
    else
        log "WARNING: B2 upload failed (local backup still available)"
    fi

    # Delete old backups from B2 (keep last 30)
    log "Cleaning old B2 backups..."
    EXISTING=$(b2 ls --long "b2://${B2_BUCKET}/backups/" 2>/dev/null | wc -l || true)
    if [ "$EXISTING" -gt 30 ]; then
        b2 ls --long "b2://${B2_BUCKET}/backups/" 2>/dev/null | head -n $((EXISTING - 30)) | awk '{print $1}' | while read -r file_id; do
            b2 delete-file-version "$file_id" 2>/dev/null || true
        done
        log "Cleaned old backups from B2"
    fi
else
    log "B2 not configured, local backup only"
fi

log "Backup finished"
