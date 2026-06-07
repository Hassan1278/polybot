# Changes — Dashboard Control Plane + Wallet Mgmt + Per-Mode Settings (2026-06-07)

Two sessions combined in this changelog:
1. **Resilience Phase A + B** (earlier today) — DB retry, executor idempotency, Streams+DLQ, deep health, retention, GitHub backup.
2. **Dashboard control plane** (this session) — wallet encryption, settings UI, per-mode profiles, metrics dashboard, security hardening.

---

## SESSION 2 — Dashboard Control Plane (new)

### Encrypted Wallet Storage

| Change | File(s) |
|---|---|
| AES-256-GCM helpers (encrypt/decrypt with AAD + nonce) | `packages/polybot/crypto.py` |
| `WalletCredential` ORM (label, address, funder, ciphertext, is_active) | `packages/polybot/models/wallet_credential.py` |
| Migration 0006 — `wallet_credentials` table | `data/migrations/versions/0006_wallet_credentials.py` |
| `WALLET_ENCRYPTION_KEY` config + validator | `packages/polybot/config.py` |
| `ClobClient._signed_client()` reads DB then falls back to .env | `packages/polybot/clients/clob.py` |
| .env: master key auto-generated on first run | `.env` (not committed) |

### Runtime Config Override Layer

| Change | File(s) |
|---|---|
| `runtime_config.py` — `merged_risk()`, `merged_categories()`, `merged_gates()`, `set_mode()`, `set_overrides()`, audit logging | `packages/polybot/runtime_config.py` |
| `HotConfig.get(mode)` deep-merges `defaults` + `modes[mode]` from YAML | `packages/polybot/yaml_config.py` |
| `risk.yaml` restructured to `defaults` + `modes.{paper,live}` | `config/risk.yaml` |
| `executor/risk.py.preflight()` uses `await merged_risk()` so mode switch takes effect immediately | `services/executor/risk.py` |

### API Endpoints (settings + metrics)

| Path | Verbs | Description |
|---|---|---|
| `/admin/settings/` | GET | Effective + overrides + baseline (per current mode) |
| `/admin/settings/mode` | GET, POST | Current mode + paper↔live switch (live needs `X-Live-Confirm` HMAC) |
| `/admin/settings/risk` | PATCH, DELETE | Risk-config overrides |
| `/admin/settings/categories` | POST | Add new category |
| `/admin/settings/categories/{name}` | PATCH, DELETE | Modify / soft-disable category |
| `/admin/settings/gates` | PATCH | Gate-param overrides |
| `/admin/settings/wallet` | GET, POST | List wallets / add encrypted wallet |
| `/admin/settings/wallet/{id}` | DELETE | Soft-disable wallet |
| `/metrics/categories?window=24h` | GET | Per-category winrate/profit/signals/fills/positions |

### Dashboard UI

| Page | Description |
|---|---|
| `/settings` | 5 tabs: Mode (paper↔live), Risk, Categories, Gates, Wallet |
| `/metrics` | Per-category table + Nivo bar chart, window selector 1h/24h/7d/30d |
| `dashboard/src/lib/admin.ts` | sessionStorage-only token, PATCH/POST/DELETE helpers |
| `dashboard/src/components/ConfirmModal.tsx` | Typed-confirm modal for destructive actions |
| `dashboard/src/middleware.ts` | CSP, X-Frame-Options DENY, X-Content-Type-Options, Referrer-Policy, Permissions-Policy |
| Nav updated | `/metrics` + `/settings` added to `dashboard/src/app/layout.tsx` |

### Production Security

| Concern | Fix |
|---|---|
| `/admin/*` rate-limit | New `services/api/rate_limit.py` — 60/min per IP via Redis fixed-window. Wired on all admin router includes. |
| CSP / browser headers | `dashboard/src/middleware.ts` — strict CSP with explicit connect-src for API+WS |
| Live-mode confirmation | Server requires `X-Live-Confirm: epoch:hmac` (HMAC over `switch-to-live:{epoch}`) — 60s skew window |
| Wallet form security | Controlled input, no autoComplete, no `name` attr, cleared on every outcome |
| Admin token storage | `sessionStorage` (cleared on tab close), never `localStorage` |
| AAD-bound encryption | Each ciphertext bound to its wallet's address — stolen rows can't be cross-decrypted |

### Configuration

- `pyproject.toml`: added `cryptography>=43`
- `services/_base/Dockerfile`: added `cryptography>=43` + `aiohttp>=3.10`
- `dashboard/package.json`: added `@nivo/bar@0.87.0`
- `.env.example`: documents `WALLET_ENCRYPTION_KEY`, `CORS_ORIGINS`, `GITHUB_BACKUP_*`

### Live Verification Before Commit

- DB retry: `docker restart postgres` → executor recovered <5s ✓
- A2 dedup: republished signal → `executor_dedup_skip` ✓
- B1 Streams DLQ: poison message routed + critical alert ✓
- Crypto roundtrip: encrypt(secret, aad) → decrypt OK; wrong AAD → rejected ✓
- Mode switch: paper→live changed all 6 risk caps (max_open 200→30, drawdown 50→100, sizing.anchor 0.5→0.65, etc.) ✓
- Live-confirm enforcement: missing/wrong sig → 403 ✓
- Rate-limit: 65 rapid /admin requests → 59 OK + 6× 429 ✓
- CSP header: present on every dashboard response ✓
- 7/7 containers healthy, 0 unhandled exceptions in 5 min ✓
- `ops_check`: 0 critical failures ✓
- Equity stable: $10,091 (166 fills, 42 settles, 53 open)

### Files Created

```
packages/polybot/crypto.py
packages/polybot/runtime_config.py
packages/polybot/models/wallet_credential.py
data/migrations/versions/0006_wallet_credentials.py
services/api/routes/settings.py
services/api/routes/metrics.py
services/api/rate_limit.py
dashboard/src/middleware.ts
dashboard/src/lib/admin.ts
dashboard/src/components/ConfirmModal.tsx
dashboard/src/app/settings/page.tsx
dashboard/src/app/settings/_tabs/{Mode,Risk,Categories,Gates,Wallet}Tab.tsx
dashboard/src/app/metrics/page.tsx
```

### Out of Scope (deferred / future)

- **NautilusTrader migration** — evaluated separately; stay bespoke (80% of code has no NT equivalent; migration is 150-300h with negative ROI for single-venue strategy). 2-3 patterns (batch-submit, fill-normalization) could be cherry-picked later (~5-15h).
- **Server-issued live-confirm challenge** — currently the operator runs a python one-liner to compute the HMAC. Follow-up: add `GET /admin/settings/mode/live-challenge` that returns a server-signed token to skip the manual step.
- **HTTPS** — Caddy ready in `infra/nginx/Caddyfile` with Let's Encrypt; activate on VPS deploy by setting `POLYBOT_DOMAIN` + `POLYBOT_TLS_EMAIL`.
- **Float→Decimal money columns** — large refactor; defer until live-mode capital scaling.
- **HashiCorp Vault / AWS KMS** — overkill for solo paper-mode; revisit at multi-user prod.
- **Per-mode categories.yaml + gates.yaml restructure** — only `risk.yaml` got the new `defaults`+`modes` layout; categories/gates can be per-mode via Redis overrides without yaml changes.

---

## SESSION 1 — Resilience Phase A + B (earlier today)

See the previous CHANGES.md content below this divider (preserved).

## Resilience — Phase A (minimum viable)

| Change | File(s) | Tested |
|---|---|---|
| DB connection retry + `pool_recycle=1800` + `pool_timeout=10` | `packages/polybot/db.py` | `docker compose restart postgres` mid-traffic → executor recovered in <5 s |
| Fill `signal_id` partial UNIQUE index + executor dedup | `data/migrations/versions/0004_fill_signal_unique.py`, `services/executor/main.py:handle()` | Republished signal_id 12713 → `executor_dedup_skip` fired |
| `/health/deep` endpoint querying DB + Redis | `services/api/routes/health.py` | `curl localhost:8000/health/deep` returns 200 with checks payload |
| Tighter docker-compose healthchecks | `docker-compose.yml` | All 7 containers `(healthy)` |

## Resilience — Phase B (structural)

| Change | File(s) | Tested |
|---|---|---|
| Redis Streams + DLQ for `signal:new` | `packages/polybot/redis_bus.py`, `services/executor/main.py`, `services/signals/engine.py` | Poison test → `signal:new:dlq` + alert |
| `/health` on ingest/signals/executor (port 8081) | `packages/polybot/health_server.py` + service main.py files | All 3 healthy |
| TimescaleDB hypertable + 180-day retention | `data/migrations/versions/0005_trades_retention.py` | 27 chunks, retention job scheduled daily |
| Off-host backup to GitHub | `scripts/backup.sh`, new `scripts/push_backup_to_github.sh`, `.env.example` | Script syntax checked |
| Manual restore procedure | new `scripts/restore.sh` | bash -n passed |

## Security Fixes (earlier batch)

| Bug | Severity | Fix |
|---|---|---|
| B15 — GET /admin/kill allowed unauth read | HIGH | Added `Depends(require_admin)` |
| CORS wildcard + credentials | HIGH | `CORS_ORIGINS` env, no wildcard, methods locked |
| ADMIN_TOKEN default `change_me` | HIGH | Documented in config + .env.example |
| B16 — watermark race | MEDIUM | `_set_watermark` moved inside session_scope |
