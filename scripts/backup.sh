#!/usr/bin/env bash
# =============================================================================
# scripts/backup.sh — daily Postgres dump for the polybot DB.
#
# Run by cron inside the `backup` sidecar container (see docker-compose.prod.yml).
# Writes gzipped dumps to /var/backups/polybot/ on the host (bind mount) and
# prunes dumps older than $BACKUP_RETENTION_DAYS (default 14).
#
# Required env (provided by the backup container):
#   PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE
# Optional:
#   BACKUP_RETENTION_DAYS  (default: 14)
#   BACKUP_DIR             (default: /var/backups/polybot)
# =============================================================================
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/polybot}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"

mkdir -p "$BACKUP_DIR"

STAMP="$(date -u +%F)"
OUT="$BACKUP_DIR/pg-${STAMP}.sql.gz"
TMP="${OUT}.partial"

echo "[backup $(date -u +%FT%TZ)] dumping ${PGDATABASE} from ${PGHOST}:${PGPORT} -> ${OUT}"

# Stream pg_dump directly through gzip; write to a .partial file first so a
# crash mid-dump never leaves a truncated archive at the canonical path.
if pg_dump \
      --host="$PGHOST" \
      --port="$PGPORT" \
      --username="$PGUSER" \
      --dbname="$PGDATABASE" \
      --no-owner --no-privileges --format=plain \
   | gzip -9 > "$TMP"; then
    mv "$TMP" "$OUT"
    echo "[backup $(date -u +%FT%TZ)] wrote $(du -h "$OUT" | cut -f1) to ${OUT}"
else
    rm -f "$TMP"
    echo "[backup $(date -u +%FT%TZ)] FAILED — pg_dump exited non-zero" >&2
    exit 1
fi

# Prune anything older than retention. -mtime +N means strictly older than N days.
echo "[backup $(date -u +%FT%TZ)] pruning dumps older than ${RETENTION_DAYS} days"
find "$BACKUP_DIR" -type f -name 'pg-*.sql.gz' -mtime "+${RETENTION_DAYS}" -print -delete || true

echo "[backup $(date -u +%FT%TZ)] done"
