#!/usr/bin/env bash
# diag.sh — one-shot deployment health check.
#
# Usage on the VPS:
#   cd /root/polybot
#   git pull
#   bash scripts/diag.sh
#
# Dumps everything I need to tell you why your pipeline isn't producing
# signals: container states, recent errors, DB row counts, key Redis
# keys, env-var sanity (without leaking secrets), and external reachability
# from inside the ingest container.
#
# Safe to share the output — passwords / tokens / keys are redacted.

set +e   # don't bail on a single check failing
trap '' PIPE

H() { printf '\n=== %s ===\n' "$*"; }

H "1. Containers"
docker ps --filter "name=polybot-" \
    --format 'table {{.Names}}\t{{.Status}}\t{{.RunningFor}}' 2>&1

H "2. Recent errors / warnings — each service, last 30 lines"
for svc in api ingest signals executor dashboard; do
    printf '\n--- polybot-%s ---\n' "$svc"
    docker logs "polybot-$svc" --tail 30 2>&1 \
      | grep -iE 'error|exception|traceback|critical|warn|fail|503|500|connection refused|timeout' \
      | head -20
done

H "3. Database row counts"
docker exec polybot-postgres psql -U polybot -d polybot -c "
    SELECT 'markets'        AS table, count(*) FROM markets
    UNION ALL SELECT 'wallets',         count(*) FROM wallets
    UNION ALL SELECT 'trades',          count(*) FROM trades
    UNION ALL SELECT 'signals',         count(*) FROM signals
    UNION ALL SELECT 'fills',           count(*) FROM fills
    UNION ALL SELECT 'positions',       count(*) FROM positions
    UNION ALL SELECT 'pnl_snapshots',   count(*) FROM pnl_snapshots
    UNION ALL SELECT 'wallet_credentials', count(*) FROM wallet_credentials
    UNION ALL SELECT 'audit_log',       count(*) FROM audit_log
;" 2>&1

H "4. Last 5 rows of pnl_snapshots (proves executor is writing)"
docker exec polybot-postgres psql -U polybot -d polybot -c \
    "SELECT ts, mode, equity_usdc, realized_usdc, unrealized_usdc, open_positions
     FROM pnl_snapshots ORDER BY ts DESC LIMIT 5;" 2>&1

H "5. Most recent market + wallet (proves ingest is writing)"
docker exec polybot-postgres psql -U polybot -d polybot -c \
    "SELECT max(updated_at) AS last_market_update FROM markets;" 2>&1
docker exec polybot-postgres psql -U polybot -d polybot -c \
    "SELECT max(last_active) AS last_wallet_activity FROM wallets;" 2>&1

H "6. Redis — kill switch + mode + stream lengths"
docker exec polybot-redis sh -c '
    echo "kill_switch:      $(redis-cli get polybot:kill_switch)"
    echo "mode_override:    $(redis-cli get polybot:mode_override)"
    echo "enabled_modes:    $(redis-cli get polybot:enabled_modes)"
    echo "signals_stream:   $(redis-cli xlen polybot:signals)"
    echo "signals_dlq:      $(redis-cli xlen polybot:signals_dlq)"
    echo "kill_history:     $(redis-cli llen polybot:kill_history)"
' 2>&1

H "7. .env sanity (REDACTED — only shows presence / format)"
grep -E '^(POSTGRES_PASSWORD|DATABASE_URL|ADMIN_TOKEN|WALLET_ENCRYPTION_KEY|ADMIN_WALLET_ADDRESSES|GOLDSKY_SUBGRAPH_URL|DASHBOARD_API_URL|TRADING_MODE|PAPER_STARTING_USDC|POLYGON_RPC_URL|CORS_ORIGINS)=' .env 2>&1 \
  | sed -E '
      s/(POSTGRES_PASSWORD=).*/\1<set>/;
      s|(DATABASE_URL=postgresql\+psycopg://[^:]+:)[^@]+|\1<password-set>|;
      s/(ADMIN_TOKEN=).{8,}/\1<set>/;
      s/(WALLET_ENCRYPTION_KEY=).{8,}/\1<set>/;
      s/(GOLDSKY_SUBGRAPH_URL=.*REPLACE.*)/\1   <-- *** PLACEHOLDER STILL THERE — INGEST WILL FAIL ***/;
    '

H "8. External reachability — from inside the ingest container"
docker exec polybot-ingest sh -c '
    for url in \
        https://clob.polymarket.com/ \
        https://gamma-api.polymarket.com/markets \
        https://data-api.polymarket.com/trades \
        https://polygon-rpc.com/ ; do
        code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$url")
        echo "$code  $url"
    done
' 2>&1

H "9. Goldsky reachability (only if URL is set)"
gsk=$(grep -E '^GOLDSKY_SUBGRAPH_URL=' .env | cut -d= -f2-)
if echo "$gsk" | grep -q REPLACE; then
    echo "GOLDSKY_SUBGRAPH_URL still has REPLACE placeholder — skipping"
elif [ -z "$gsk" ]; then
    echo "GOLDSKY_SUBGRAPH_URL is empty"
else
    docker exec polybot-ingest sh -c "curl -s -o /dev/null -w '%{http_code}' --max-time 5 '$gsk'" 2>&1
    echo
fi

H "10. Alembic migration version"
docker exec polybot-api alembic -c alembic.ini current 2>&1 | tail -3

H "DIAG COMPLETE — copy this entire output and paste to the assistant"
