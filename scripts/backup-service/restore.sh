#!/bin/bash
set -euo pipefail

B2_BUCKET="${B2_BUCKET:-maxload}"
B2_KEY_ID="${B2_KEY_ID:-}"
B2_APP_KEY="${B2_APP_KEY:-}"
RESTORE_HOST="${RESTORE_HOST:-postgres}"
RESTORE_DB="${RESTORE_DB:-downloader_bot}"
RESTORE_USER="${RESTORE_USER:-bot_user}"
RESTORE_PASS="${RESTORE_PASS:-${POSTGRES_PASSWORD:-changeme}}"
export PGPASSWORD="$RESTORE_PASS"
LOG_PREFIX="[restore]"

log() { echo "${LOG_PREFIX} $*"; }

restore_from_dump() {
    local dump_file="$1"
    log "Dropping and recreating database..."
    psql -h "$RESTORE_HOST" -U "$RESTORE_USER" -d postgres -c "DROP DATABASE IF EXISTS ${RESTORE_DB};"
    psql -h "$RESTORE_HOST" -U "$RESTORE_USER" -d postgres -c "CREATE DATABASE ${RESTORE_DB} OWNER ${RESTORE_USER};"

    log "Restoring from dump..."
    pg_restore -h "$RESTORE_HOST" -U "$RESTORE_USER" -d "$RESTORE_DB" "$dump_file" 2>/dev/null || true

    log "Fixing ownership and privileges..."
    psql -h "$RESTORE_HOST" -U "$RESTORE_USER" -d "$RESTORE_DB" <<-'EOSQL'
        DO $$
        DECLARE r RECORD;
        BEGIN
            FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
                EXECUTE 'ALTER TABLE ' || quote_ident(r.tablename) || ' OWNER TO bot_user';
            END LOOP;
            FOR r IN SELECT sequencename FROM pg_sequences WHERE schemaname = 'public' LOOP
                EXECUTE 'ALTER SEQUENCE ' || quote_ident(r.sequencename) || ' OWNER TO bot_user';
            END LOOP;
        END $$;
        GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO bot_user;
        GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO bot_user;
EOSQL

    log "Verifying restored data..."
    psql -h "$RESTORE_HOST" -U "$RESTORE_USER" -d "$RESTORE_DB" -c "
        SELECT 'users' AS t, COUNT(*) FROM users
        UNION ALL SELECT 'files', COUNT(*) FROM downloaded_files
        UNION ALL SELECT 'events', COUNT(*) FROM analytics_events
        UNION ALL SELECT 'settings', COUNT(*) FROM settings;
    "
}

# Check if database has tables
TABLE_COUNT=$(psql -h "$RESTORE_HOST" -U "$RESTORE_USER" -d "$RESTORE_DB" -t -c \
    "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public';" 2>/dev/null || echo "0")
TABLE_COUNT=$(echo "$TABLE_COUNT" | tr -d ' ')

if [ "$TABLE_COUNT" -gt 2 ]; then
    log "Database has ${TABLE_COUNT} tables — no restore needed"
    exit 0
fi

log "Database is empty (${TABLE_COUNT} tables) — attempting restore..."

# Try local dump first
LATEST_DUMP=$(find /backups -name "*.dump" -type f 2>/dev/null | sort -r | head -1)
if [ -n "$LATEST_DUMP" ]; then
    log "Found local dump: ${LATEST_DUMP}"
    restore_from_dump "$LATEST_DUMP"
    log "Restore from local dump completed"
    exit 0
fi

# Try B2
if [ -n "$B2_KEY_ID" ] && [ -n "$B2_APP_KEY" ] && [ -n "$B2_BUCKET" ]; then
    log "No local dump found, trying B2..."
    b2 authorize-account "$B2_KEY_ID" "$B2_APP_KEY" 2>/dev/null || true
    LATEST_B2=$(b2 ls --long "b2://${B2_BUCKET}/backups/" 2>/dev/null | tail -1 | awk '{print $1}')
    if [ -n "$LATEST_B2" ]; then
        b2 download-file-by-id "$LATEST_B2" > /tmp/latest_backup.dump 2>/dev/null
        restore_from_dump "/tmp/latest_backup.dump"
        rm -f /tmp/latest_backup.dump
        log "Restore from B2 completed"
        exit 0
    fi
fi

log "WARNING: No backup found — starting with empty database"
