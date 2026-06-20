#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_FILE="${PROJECT_DIR}/logs/restore.log"

mkdir -p "$(dirname "$LOG_FILE")"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# Determine backup source
BACKUP_FILE="${1:-}"
RESTORE_FROM_RESTIC="${2:-}"

if [ -z "$BACKUP_FILE" ] && [ "$RESTORE_FROM_RESTIC" != "restic" ]; then
    echo "Usage: $0 <backup_file.dump>"
    echo "   or: $0 <restic_snapshot_id> restic"
    echo ""
    echo "Examples:"
    echo "  $0 /path/to/db_backup_20260620.dump"
    echo "  $0 latest restic"
    exit 1
fi

log "Starting restore..."

if [ "$RESTORE_FROM_RESTIC" = "restic" ]; then
    log "Restoring from restic snapshot: $BACKUP_FILE"
    RESTORE_DIR="/tmp/restic_restore_$$"
    mkdir -p "$RESTORE_DIR"

    export RESTIC_REPOSITORY RESTIC_PASSWORD_FILE
    restic restore "$BACKUP_FILE" --target "$RESTORE_DIR"
    BACKUP_FILE=$(find "$RESTORE_DIR" -name "*.dump" -type f | head -1)

    if [ -z "$BACKUP_FILE" ]; then
        log "ERROR: No .dump file found in restic restore"
        rm -rf "$RESTORE_DIR"
        exit 1
    fi
    log "Restored dump from restic: $BACKUP_FILE"
fi

log "Restoring database from $BACKUP_FILE"

# Stop the bot during restore
log "Stopping bot..."
docker compose -f "${PROJECT_DIR}/docker-compose.yml" stop downloader-bot

# Drop and recreate database
log "Recreating database..."
docker exec downloader-postgres psql -U bot_user -d postgres -c "DROP DATABASE IF EXISTS downloader_bot;"
docker exec downloader-postgres psql -U bot_user -d postgres -c "CREATE DATABASE downloader_bot OWNER bot_user;"

# Restore
log "Running pg_restore..."
docker cp "$BACKUP_FILE" downloader-postgres:/tmp/backup.dump
docker exec downloader-postgres pg_restore -U bot_user -d downloader_bot /tmp/backup.dump 2>&1 | tee -a "$LOG_FILE"
docker exec downloader-postgres rm -f /tmp/backup.dump

# Fix ownership (dynamic — works for any table)
log "Fixing table ownership..."
docker exec downloader-postgres psql -U bot_user -d downloader_bot -c "
DO \$\$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
        EXECUTE 'ALTER TABLE ' || quote_ident(r.tablename) || ' OWNER TO bot_user';
    END LOOP;
    FOR r IN SELECT sequencename FROM pg_sequences WHERE schemaname = 'public' LOOP
        EXECUTE 'ALTER SEQUENCE ' || quote_ident(r.sequencename) || ' OWNER TO bot_user';
    END LOOP;
END \$\$;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO bot_user;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO bot_user;
"

# Verify data
log "Verifying restored data..."
docker exec downloader-postgres psql -U bot_user -d downloader_bot -c "
SELECT 'users' AS table_name, COUNT(*) AS count FROM users
UNION ALL SELECT 'downloaded_files', COUNT(*) FROM downloaded_files
UNION ALL SELECT 'analytics_events', COUNT(*) FROM analytics_events
UNION ALL SELECT 'settings', COUNT(*) FROM settings;
"

# Start bot
log "Starting bot..."
docker compose -f "${PROJECT_DIR}/docker-compose.yml" start downloader-bot

# Cleanup restore directory if used
if [ "$RESTORE_FROM_RESTIC" = "restic" ]; then
    rm -rf "$RESTORE_DIR"
fi

log "Restore completed successfully"
