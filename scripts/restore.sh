#!/usr/bin/env bash
# Restore the Polybot Postgres database from a pg_dump archive.
#
# This script is intentionally MANUAL — restoring a DB while services
# are running mid-transaction would corrupt state. Stop application
# services first, then run.
#
# Usage:
#   bash scripts/restore.sh /path/to/polybot_YYYYMMDD_HHMMSS.sql.gz
#
# What it does:
#   1. Sanity-check the dump file exists and is gzipped.
#   2. Pause application services (api, executor, signals, ingest).
#   3. Rename the live `polybot` database to `polybot_old_<timestamp>`
#      (preserved for rollback — drop manually after verifying restore).
#   4. Create a fresh empty `polybot` database.
#   5. Stream the dump into it via psql.
#   6. Validate row counts on critical tables.
#   7. Restart services.

set -euo pipefail

DUMP="${1:-}"
if [[ -z "$DUMP" ]]; then
  echo "usage: $0 /path/to/polybot_*.sql.gz" >&2
  exit 2
fi
if [[ ! -f "$DUMP" ]]; then
  echo "dump file not found: $DUMP" >&2
  exit 2
fi
if ! file "$DUMP" | grep -q 'gzip compressed'; then
  echo "expected a gzipped sql dump: $DUMP" >&2
  exit 2
fi

PG_USER="${POSTGRES_USER:-polybot}"
PG_DB="${POSTGRES_DB:-polybot}"
TS=$(date +%Y%m%d_%H%M%S)
OLD_DB="${PG_DB}_old_${TS}"

read -p "Restore from $DUMP into '$PG_DB' (current DB will be renamed to '$OLD_DB')? [type 'yes' to confirm] " ans
if [[ "$ans" != "yes" ]]; then
  echo "aborted"
  exit 1
fi

echo "==> pausing application services"
docker compose stop api executor signals ingest dashboard

echo "==> renaming live DB '$PG_DB' -> '$OLD_DB'"
docker compose exec -T postgres psql -U "$PG_USER" -d postgres -c "ALTER DATABASE $PG_DB RENAME TO $OLD_DB;"

echo "==> creating fresh '$PG_DB'"
docker compose exec -T postgres createdb -U "$PG_USER" "$PG_DB"

echo "==> restoring dump (this can take a minute for large dumps)"
gunzip -c "$DUMP" | docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" >/dev/null

echo "==> validating row counts"
docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -c "
  SELECT 'trades',     COUNT(*) FROM trades
  UNION ALL SELECT 'fills',      COUNT(*) FROM fills
  UNION ALL SELECT 'positions',  COUNT(*) FROM positions
  UNION ALL SELECT 'wallets',    COUNT(*) FROM wallets
  UNION ALL SELECT 'markets',    COUNT(*) FROM markets;
"

echo "==> restarting services"
docker compose start api executor signals ingest dashboard

echo ""
echo "DONE. Old DB preserved as '$OLD_DB'. After verifying the restored DB:"
echo "  docker compose exec postgres dropdb -U $PG_USER $OLD_DB"
