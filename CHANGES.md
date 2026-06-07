# Changes — Resilience Hardening + Security Fixes (2026-06-07)

This session: implemented the full Polybot resilience plan (Phase A + B from
`~/.claude/plans/serene-seeking-puffin.md`), plus the security and bug findings
from a parallel investigation workflow.

## Resilience — Phase A (minimum viable)

| Change | File(s) | Tested |
|---|---|---|
| DB connection retry + `pool_recycle=1800` + `pool_timeout=10` | `packages/polybot/db.py` | `docker compose restart postgres` mid-traffic → executor recovered in <5 s |
| Fill `signal_id` partial UNIQUE index + executor dedup | `data/migrations/versions/0004_fill_signal_unique.py`, `services/executor/main.py:handle()` | Republished signal_id 12713 → `executor_dedup_skip` fired |
| `/health/deep` endpoint querying DB + Redis | `services/api/routes/health.py` | `curl localhost:8000/health/deep` returns 200 with checks payload; would return 503 if any dep down |
| Tighter docker-compose healthchecks (postgres: query + isready, redis: ping + write, api+ingest+signals+executor: HTTP) | `docker-compose.yml` | All 7 containers show `(healthy)` |

## Resilience — Phase B (structural)

| Change | File(s) | Tested |
|---|---|---|
| Redis Streams + DLQ for `signal:new` (xpublish, xconsume, xack, xdlq, xautoclaim) | `packages/polybot/redis_bus.py`, `services/executor/main.py`, `services/signals/engine.py` | Poison test message routed to `signal:new:dlq` with error trace; critical alert fired |
| `/health` endpoints on ingest, signals, executor (port 8081, aiohttp + HealthBeacon) | `packages/polybot/health_server.py`, `services/ingest/main.py`, `services/signals/main.py`, `services/executor/main.py`, `services/signals/correlation_loop.py`, `services/signals/stats_loop.py` | All three return `{"ok": true, "lag_seconds": N}` |
| TimescaleDB hypertable for `trades` + 180-day retention policy | `data/migrations/versions/0005_trades_retention.py` | 27 chunks created, retention job scheduled every 24h |
| Off-host backup to private GitHub repo | `scripts/backup.sh`, new `scripts/push_backup_to_github.sh`, `.env.example` | Script syntax checked; runtime requires `GITHUB_BACKUP_TOKEN` |
| Manual restore procedure | new `scripts/restore.sh` | Bash -n passed; prompts before destructive action |

## Security Fixes

| Bug | Severity | Fix |
|---|---|---|
| B15 — GET /admin/kill allowed unauth read | HIGH | Added `Depends(require_admin)` to the GET handler |
| CORS wildcard + credentials | HIGH | `CORS_ORIGINS` env var, default localhost only, methods locked to GET/POST |
| ADMIN_TOKEN default `change_me` | HIGH | Documented in config.py + .env.example (validate.py already rejects in live mode) |
| B16 — watermark race | MEDIUM | `_set_watermark` moved inside the session_scope context, atomic with DB commit |

## Configuration Changes (user-requested)

- `max_per_category_usdc: 400 → 1000` (more headroom during OPS test)
- `max_orders_per_minute: 20 → 10000` (effectively unlimited; CLOB hard-caps ~30/min anyway)

## New Files

```
packages/polybot/health_server.py       # HealthBeacon + aiohttp /health server
data/migrations/versions/0004_fill_signal_unique.py
data/migrations/versions/0005_trades_retention.py
scripts/restore.sh                      # manual pg_dump restore procedure
scripts/push_backup_to_github.sh        # off-host backup helper
CHANGES.md                              # this file
```

## Migrations Applied

```
0003_market_outcomes               (previous)
0004_fill_signal_unique            (new — partial UNIQUE on fills.signal_id)
0005_trades_retention              (new — hypertable + 180d retention)
```

## Verified Live

- DB retry: `docker restart postgres` mid-traffic → graceful recovery
- Dedup: republished signal → `executor_dedup_skip` log line + no duplicate fill row
- Deep health: all dep checks return ok
- Streams + DLQ: poison message captured in `signal:new:dlq` + critical alert fired
- Healthchecks: all containers report healthy
- ops_check: 0 critical failures

## Deferred (documented for live-mode prep)

These are real production gaps but appropriate to defer for paper-mode:

- Float→Decimal for money columns (large refactor, requires data migration)
- Audit log immutability via Postgres RULE / trigger
- Risk-param input validation in Settings (gt/lt constraints)
- Real secret-manager integration (Vault / AWS Secrets) — currently .env
- Ingest service split into 3 containers (paper-mode fine; reconsider on Hetzner)
- HA / streaming-replication Postgres (paper-mode fine; switch to managed PG when live)
