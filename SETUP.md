# Setup

Two paths: **Local (Docker Compose)** for development and **Production (Contabo VPS)** for 24/7.

---

## 1. Local development (Docker Compose)

### 1.1 Prerequisites

| Tool | Min version | Why |
|---|---|---|
| Docker Desktop | 4.30 | runs the whole stack |
| Node | 20.x | only if you want to dev the dashboard outside Docker |
| Python | 3.12 | only if you want to dev services outside Docker |
| A Polygon wallet | — | for live-mode. **Use a fresh wallet** with capped USDC.e |

### 1.2 Clone & configure

```bash
git clone <this repo> polybot
cd polybot
cp .env.example .env
```

Edit `.env`:

```bash
TRADING_MODE=paper                  # paper | live — keep paper until you trust it
PAPER_STARTING_USDC=10000

POLYGON_RPC_URL=https://polygon-rpc.com           # or your Chainstack/Alchemy URL
POLYMARKET_CLOB_URL=https://clob.polymarket.com
POLYMARKET_GAMMA_URL=https://gamma-api.polymarket.com
POLYMARKET_DATA_URL=https://data-api.polymarket.com
GOLDSKY_SUBGRAPH_URL=https://api.goldsky.com/api/public/<id>/subgraphs/<name>/<ver>/gn

# only needed for live-mode
POLYMARKET_PRIVATE_KEY=             # 0x... private key of FUNDER wallet
POLYMARKET_FUNDER_ADDRESS=          # the address that holds the USDC.e
POLYMARKET_SIGNATURE_TYPE=1         # 0 = EOA (metamask), 1 = email/magic, 2 = browser wallet

POSTGRES_USER=polybot
POSTGRES_PASSWORD=change_me
POSTGRES_DB=polybot
DATABASE_URL=postgresql+psycopg://polybot:change_me@postgres:5432/polybot
REDIS_URL=redis://redis:6379/0

DASHBOARD_API_URL=http://localhost:8000
```

### 1.3 Boot

```bash
docker compose up -d
docker compose logs -f api ingest signals executor
```

Healthchecks:

```bash
curl http://localhost:8000/health      # api
open  http://localhost:3000            # dashboard
```

### 1.4 First-time bootstrap

```bash
# 1. apply DB migrations + seed default categories
docker compose exec api alembic upgrade head
docker compose exec api python -m scripts.seed_categories

# 2. discover the initial top-wallet set (20-50 per category)
docker compose exec ingest python -m scripts.discover_wallets --top 30

# 3. backfill 90 days of trades for those wallets
docker compose exec ingest python -m scripts.backfill_trades --days 90

# 4. compute the first correlation snapshot
docker compose exec signals python -m scripts.compute_correlations
```

Now the dashboard at http://localhost:3000 should show wallets, a bubble-map, and a heatmap.

---

## 2. Production (Contabo VPS L)

### 2.1 Provision

1. Order **Contabo VPS L** — €8.49/mo, 6 vCPU, 16 GB RAM, 400 GB NVMe, location **EU (Nuremberg)**.
   - Reason: Polymarket's matching engine fronts on Cloudflare (US-East), but the heavy work is local (correlation, ingest). 80–120 ms RTT from EU is fine for non-arb strategies.
   - For latency-sensitive strategies, pick a US-East provider (Vultr NJ, Linode NJ).
2. Ubuntu 24.04 LTS, SSH key only, disable password login.
3. Open ports 22, 80, 443 in the Contabo firewall.

### 2.2 First-boot hardening

```bash
ssh root@<vps-ip>
bash <(curl -fsSL https://raw.githubusercontent.com/<you>/polybot/main/infra/deploy/contabo-setup.sh)
```

(That script is in this repo at `infra/deploy/contabo-setup.sh` — review it before piping into bash.)

### 2.3 Deploy

```bash
git clone <repo> /opt/polybot
cd /opt/polybot
cp .env.example .env && nano .env       # fill prod secrets, TRADING_MODE=paper to start
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Caddy / Nginx terminates TLS at port 443 and proxies to:
- `dashboard.<domain>` → Next.js (3000)
- `api.<domain>`       → FastAPI (8000)

### 2.4 Going live (real money)

**Do not skip these steps.**

1. `VALIDATION.md` — run **every** check.
2. Fund a *fresh* MetaMask wallet with a small amount of USDC.e on Polygon (e.g. 100 USDC).
3. Move that wallet's private key into the VPS's `.env` (file mode 600, root-only).
4. Set `TRADING_MODE=live` and `MAX_POSITION_USDC=25` (cap small).
5. `docker compose restart executor`.
6. Watch `docker compose logs -f executor` and the dashboard's signal feed.
7. Only raise caps after **N consecutive winning sessions** that match paper performance.

### 2.5 Kill-switch (always test it first)

```bash
# from anywhere:
curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" https://api.<domain>/admin/kill
# or from inside the VPS:
docker compose exec executor python -m scripts.kill_switch
```

This:
1. Cancels all open orders via CLOB v2.
2. Flips `KILL_SWITCH=1` in Redis — executor refuses any new signal.
3. Persists the event to `audit_log`.

---

## 3. Alternative hostings (decision matrix)

| Option | Monthly | Pros | Cons |
|---|---|---|---|
| **Contabo VPS L** ⭐ | €8.49 | best $/RAM, EU/US locations, real root | manual ops, no managed pg |
| **Hetzner CCX13** | €13 | premium net, EU only | only 8 GB RAM at that price |
| **Fly.io** | ~$25 | git-push deploy, multi-region | Postgres extra, no Timescale by default |
| **Railway** | ~$20 | easiest UX, autoscaling | priciest per resource, lock-in |
| **Self-host (homelab)** | €0 | full control | ISP downtime = trade gap |
| **AWS t3.medium + RDS** | ~$60 | enterprise-grade | overkill, RDS no Timescale |

For a personal bot: **Contabo + Docker Compose** is the right answer. Scale up only if you start running multiple strategies.

---

## 4. What's NOT installed by default (you may want it)

| Tool | Purpose | Add via |
|---|---|---|
| Grafana + Promtail | dashboards for ingest lag, signal throughput | `docker-compose.observability.yml` template in `infra/` |
| Sentry | exception alerting | `SENTRY_DSN` env-var, code already calls `sentry_sdk.init` if set |
| Telegram bot | push alerts for fills + kill-switch | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` in `.env` |
