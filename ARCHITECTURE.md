# Architecture

## High-level data-flow

```
                   ┌─────────────────────────────────────────────┐
                   │                Polymarket                   │
                   │  Gamma API   CLOB v2 (REST+WS)   Data API   │
                   └──────┬──────────┬──────────────┬────────────┘
                          │          │              │
                          ▼          ▼              ▼
   ┌──────────────────────────────────────────────────────────┐
   │                       INGEST SERVICE                     │
   │  • leaderboard_scraper  (top wallets per category)       │
   │  • trade_ingest         (per-wallet historical trades)   │
   │  • live_trade_listener  (CLOB WS + Goldsky subgraph)     │
   │  • market_ingest        (active markets, liquidity, vol) │
   └────────────────────────────┬─────────────────────────────┘
                                │ writes
                                ▼
              ┌───────────────────────────────────┐
              │  Postgres + TimescaleDB           │
              │  wallets, trades, markets,        │
              │  positions, signals, fills, pnl   │
              └────────────────┬──────────────────┘
                               │ reads
              ┌────────────────┼─────────────────────────┐
              │                │                         │
              ▼                ▼                         ▼
   ┌──────────────────┐  ┌─────────────────┐  ┌──────────────────┐
   │ SIGNALS SERVICE  │  │   API SERVICE   │  │  DASHBOARD       │
   │ • correlation    │  │  FastAPI REST   │  │  Next.js 14      │
   │ • clustering     │  │  + WS pub-sub   │  │  • bubble map    │
   │ • gate engine    │  │                 │  │  • heatmap       │
   │   ↓ fires        │  │                 │  │  • kill switch   │
   │ signal_bus       │  └────────┬────────┘  │  • signals feed  │
   └────────┬─────────┘           │           └────────┬─────────┘
            │ Redis pub/sub       │ WS                 │
            ▼                     ▼                    │
   ┌──────────────────────────────────────────────────────────┐
   │                    EXECUTOR SERVICE                      │
   │  • paper_executor  (virtual USDC, real orderbook)        │
   │  • live_executor   (real USDC via py-clob-client-v2)     │
   │  • risk_manager    (max pos, max draw, kill switch)      │
   └──────────────────────────────────────────────────────────┘
```

## Why these services, not a monolith

| Concern | Service | Reason for isolation |
|---|---|---|
| Pulling tons of data, can crash on rate-limits | **ingest** | Restart freely without losing a live order |
| Heavy CPU (numpy correlation matrices) | **signals** | Schedule on its own core, don't block I/O |
| Holds the private key, needs minimal blast radius | **executor** | Smallest container, smallest set of deps |
| User-facing, must stay responsive | **api + dashboard** | Independent of compute load |

All four talk through **Postgres** (durable state) + **Redis pub/sub** (live events). No service imports another — only `packages/polybot` (the shared lib).

## Data model (simplified)

```sql
-- wallet directory + scoring
wallets          (address, label, category, first_seen, last_seen)
wallet_stats     (address, window, pnl, win_rate, sharpe, trade_count, computed_at)

-- raw history (TimescaleDB hypertable on ts)
trades           (tx_hash, ts, wallet, market_id, side, size, price, fee, source)
markets          (market_id, slug, question, category, end_date, resolved, outcome)
positions        (wallet, market_id, outcome, size, avg_price, updated_at)

-- signal pipeline
signals          (id, ts, market_id, direction, wallet_count, wallets[], correlation_score, gate_result, executed)
fills            (id, signal_id, ts, mode, side, size, price, fee, status)
pnl_snapshots    (ts, mode, equity, realised, unrealised)
```

## Signal lifecycle

```
1. ingest writes a trade           ──► trades table + Redis "trade:new"
2. signals listens on "trade:new"
   ├─ updates per-wallet recent-trade window (last 5 min)
   ├─ computes correlation matrix over active top-wallets
   ├─ when ≥ N wallets converge on same (market_id, side):
   │      build candidate signal
   └─ runs candidate through gate chain:
        ├─ category_match     (is it a category we trade?)
        ├─ wallet_quality     (avg win-rate of converging wallets ≥ X?)
        ├─ liquidity          (book depth ≥ Y at desired size?)
        ├─ risk_reward        (price ≤ MAX_PRICE for desired upside?)
        ├─ timeframe          (market resolves in [MIN, MAX] hours?)
        └─ correlation_score  (≥ threshold?)
3. signal passes  ──► persisted, published on "signal:new"
4. executor consumes "signal:new", checks risk caps, places order
5. fill / partial / reject  ──► persisted, published on "fill:new"
```

## Why paper-mode first

- Polymarket V2 uses **real money on Polygon**. There is no shared "testnet liquidity".
- We therefore simulate fills locally against the *real* orderbook (so spread, depth, fee are honest), with virtual USDC.
- The `paper_executor` and `live_executor` share the same code-path; only the final `submit_order` call differs.

See `services/executor/paper.py` and `services/executor/live.py`.

## Failure modes we explicitly handle

| Failure | Detection | Action |
|---|---|---|
| CLOB WS drops | heartbeat > 30 s | reconnect, replay missed snapshots |
| Gamma rate-limit (429) | `httpx` response | exponential backoff, no retry storm |
| Stale wallet ranking | `wallet_stats.computed_at > 24 h` | refuse signals from that wallet set |
| Postgres down | sqlalchemy error | executor goes to `SAFE_HOLD`, no new orders |
| PnL drawdown > cap | `pnl_snapshots` trigger | `kill_switch.activate()` — cancel all, halt |
| Private-key leak | n/a | use a *segregated* funder wallet, cap balance |

## Repo conventions

- All times **UTC**, all sizes **USDC** (6-decimal), all prices in **probability** (0.0 – 1.0).
- `polybot.config` reads `.env` once; nothing else does.
- Every async task uses `polybot.logging.get_logger(__name__)` — JSON logs to stdout, picked up by Loki/Promtail if you want.
- Tests are pytest-only; no async fixtures needed because the clients have sync wrappers.
