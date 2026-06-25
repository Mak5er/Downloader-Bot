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
    psql -h "$RESTORE_HOST" -U "$RESTORE_USER" -d postgres -c "DROP DATABASE IF EXISTS ${RESTORE_DB};" || {
        log "ERROR: Failed to drop database"
        return 1
    }
    psql -h "$RESTORE_HOST" -U "$RESTORE_USER" -d postgres -c "CREATE DATABASE ${RESTORE_DB} OWNER ${RESTORE_USER};" || {
        log "ERROR: Failed to create database"
        return 1
    }

    log "Restoring from dump..."
    pg_restore -h "$RESTORE_HOST" -U "$RESTORE_USER" -d "$RESTORE_DB" "$dump_file" 2>&1 || {
        log "WARNING: pg_restore reported errors (non-fatal, partial restore possible)"
    }

    log "Fixing ownership and privileges..."
    psql -h "$RESTORE_HOST" -U "$RESTORE_USER" -d "$RESTORE_DB" <<-'EOSQL' || true
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
    " || log "WARNING: Verification query failed"
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
    if restore_from_dump "$LATEST_DUMP"; then
        log "Restore from local dump completed"
        exit 0
    fi
    log "WARNING: Local dump restore failed"
fi

# Try B2
if [ -n "$B2_KEY_ID" ] && [ -n "$B2_APP_KEY" ] && [ -n "$B2_BUCKET" ]; then
    log "No local dump found, trying B2..."
    b2 authorize-account "$B2_KEY_ID" "$B2_APP_KEY" >/dev/null 2>&1 || {
        log "WARNING: B2 authorization failed"
        log "WARNING: No backup found — starting with empty database"
        exit 0
    }
    log "B2 authorized, listing backups..."
    LATEST_B2=$(b2 ls --long "b2://${B2_BUCKET}/backups/" 2>/dev/null | grep '\.dump$' | tail -1 | awk '{print $1}' || true)
    if [ -n "$LATEST_B2" ]; then
        log "Downloading backup by ID: ${LATEST_B2}"
        if b2 download-file-by-id "$LATEST_B2" > /tmp/latest_backup.dump 2>/dev/null; then
            DUMP_SIZE=$(ls -lh /tmp/latest_backup.dump 2>/dev/null | awk '{print $5}')
            log "Downloaded: ${DUMP_SIZE}"
            if restore_from_dump "/tmp/latest_backup.dump"; then
                rm -f /tmp/latest_backup.dump
                log "Restore from B2 completed"
                exit 0
            fi
            log "WARNING: B2 dump restore failed"
        else
            log "WARNING: B2 download failed"
        fi
        rm -f /tmp/latest_backup.dump
    else
        log "No .dump files found in B2 bucket backups/"
    fi
fi

log "WARNING: No backup found — starting with empty database"
log "The bot will create tables via Alembic migrations on first start."
exit 0
