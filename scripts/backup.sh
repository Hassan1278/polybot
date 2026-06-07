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

# Optional off-host backup. If GITHUB_BACKUP_TOKEN + GITHUB_BACKUP_REPO are set,
# push today's dump to a private GitHub repo so a disk-failure on this host
# doesn't take both the live DB and the backups with it. Skip silently if
# unset to keep dev frictionless.
if [[ -n "${GITHUB_BACKUP_TOKEN:-}" && -n "${GITHUB_BACKUP_REPO:-}" ]]; then
    if command -v bash >/dev/null 2>&1 && [[ -x "$(dirname "$0")/push_backup_to_github.sh" ]]; then
        echo "[backup $(date -u +%FT%TZ)] pushing $OUT to github.com/${GITHUB_BACKUP_REPO}"
        "$(dirname "$0")/push_backup_to_github.sh" "$OUT" \
          || echo "[backup $(date -u +%FT%TZ)] github push FAILED (non-fatal)" >&2
    fi
fi

echo "[backup $(date -u +%FT%TZ)] done"
