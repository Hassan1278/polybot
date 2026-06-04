# Polybot — Polymarket Smart-Money Mirror Framework

A research & execution framework for Polymarket that:

1. **Discovers** the top 20–50 wallets per category (sorted by win-rate / risk-adjusted PnL).
2. **Tracks** their past *and* live trades in real time (CLOB WS + Polygon on-chain).
3. **Visualises** their behaviour as a bubble-map (à la axiom.trade) and a correlation heatmap.
4. **Correlates** wallet activity → fires a *signal* when N top wallets do the same thing.
5. **Gates** that signal through configurable conditions (liquidity, R:R, time-frame, slippage).
6. **Executes** orders in either **paper-mode** (default — virtual USDC against real orderbooks) or **live-mode** (real USDC on the V2 CLOB).
7. **Logs / Audits** every signal, every gate decision, every fill — so you can iterate on the strategy.

> **This repo is the framework, not the strategy.**
> The gate-config (`config/gates.yaml`) is the only place where you (or a future ML model) decide *when* a correlation actually becomes a trade.

---

## Quick links

| Doc | What |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | System diagram, data-flow, why each service exists |
| [SETUP.md](SETUP.md) | Step-by-step local + production setup |
| [VALIDATION.md](VALIDATION.md) | Checklist to verify every layer works before you risk a cent |
| [docs/api-cheatsheet.md](docs/api-cheatsheet.md) | All Polymarket endpoints we touch |
| [docs/hosting.md](docs/hosting.md) | Hosting decision matrix (Contabo vs Hetzner vs Fly.io vs self-host) |

---

## Tech stack (decision summary)

| Layer | Choice | Reason |
|---|---|---|
| Lang (backend) | **Python 3.12** | `py-clob-client-v2` + `polymarket-apis` + pandas/numpy for correlation |
| Lang (frontend) | **TypeScript + Next.js 14** | Mature React ecosystem, easy to dockerise |
| DB | **Postgres 16 + TimescaleDB** | Trades are time-series; hypertables make wallet-history queries cheap |
| Cache / Queue | **Redis 7** | Pub-sub for the signal bus + Celery broker |
| Scheduler | **Celery + Celery-Beat** | Periodic leaderboard / trade ingestion |
| Charts | **Nivo + D3 + visx** | Bubble-map, heatmap, network-graph all covered |
| RPC | **Chainstack / Alchemy / public Polygon** | Read on-chain events for true settlement |
| Subgraph | **Goldsky (Polymarket hosted)** | Free historical trade indexing |
| Hosting (prod) | **Contabo VPS L (€8.49/mo)** | Cheapest box that runs the whole stack 24/7 |
| Hosting (dev) | **Docker Compose** | One command, identical to prod |

---

## One-line up

```bash
cp .env.example .env       # fill in PRIVATE_KEY (use a fresh wallet) and RPC_URL
docker compose up -d       # postgres, redis, api, ingest, signals, executor, dashboard
open http://localhost:3000 # dashboard
```

The bot **starts in paper-mode**. To go live, set `TRADING_MODE=live` in `.env` *and* unlock the kill-switch in the dashboard.

---

## Repository layout

```
.
├── services/         # 4 independent services (api, ingest, signals, executor)
├── packages/polybot/ # shared lib (clients, models, db, config)
├── dashboard/        # Next.js 14 dashboard
├── infra/            # docker, nginx, deploy scripts
├── data/migrations/  # Alembic
├── scripts/          # one-shot CLIs (discover, backfill, kill-switch, validate)
├── config/           # categories.yaml, gates.yaml, risk.yaml
└── tests/            # pytest
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the why.
