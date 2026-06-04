# Polymarket API cheat-sheet

Quick reference for every endpoint we touch.

| Surface | Base URL | Auth | What we use it for |
|---|---|---|---|
| Gamma | `https://gamma-api.polymarket.com` | none | `/markets`, `/events`, `/tags`, `/public-search` — market metadata and category mapping |
| Data | `https://data-api.polymarket.com` | none | `/leaderboard/{window}/{metric}`, `/positions`, `/trades`, `/activity`, `/holders/{mkt}` — wallet-level history |
| CLOB | `https://clob.polymarket.com` | mixed | `/book`, `/midpoint`, `/price`, `/prices-history`, `/trades` (public); order placement (signed) |
| CLOB WS | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | none | live orderbook + trade prints per `assets_ids` |
| Subgraph (Goldsky) | `https://api.goldsky.com/api/public/<id>/subgraphs/polymarket-positions/<ver>/gn` | none | historical trade backfill via GraphQL |
| Polygon RPC | `https://polygon-rpc.com` (or a paid provider) | none | last-resort on-chain check / nonce / balance |

## Rate limits (observed, not officially documented)

| Surface | Rough cap | Polybot strategy |
|---|---|---|
| Gamma `/markets` | ~ 30 req/s | ingest batches 200 per page, polls every 5 min |
| Data API | ~ 10 req/s per IP | leaderboard scraper limits concurrency to 5 |
| CLOB public | ~ 30 req/s | liquidity gate caches book per (market, 5 s) |
| CLOB signed | per-key ≤ 30 orders/min | risk.yaml enforces `max_orders_per_minute=6` |
| Goldsky | ~ 5 req/s public, more on paid | only used for one-shot backfills |

## V2 migration notes

- The `py-clob-client` (V1) repo was archived on **2026-05-11**. Use `py-clob-client-v2`.
- V2 cutover happened **2026-04-28**. Pre-cutover open orders were wiped.
- Order signing changed — V2 SDK is not back-compat. Polybot uses V2 only.

## Useful third-party analytics (read-only sources you can also poll)

| Site | What |
|---|---|
| [polymarketanalytics.com](https://polymarketanalytics.com/traders) | per-wallet PnL / WR dashboards |
| [polyburg.com/polymarket-top-traders](https://polyburg.com/polymarket-top-traders) | live leaderboard with WR |
| [polysmartwallet.com](https://polysmartwallet.com/) | "score = PNL × WR × backtest × slippage" |
| [polymarket.com/leaderboard](https://polymarket.com/leaderboard) | official leaderboard |
| [apify.com/logiover/polymarket-top-wallets-leaderboard](https://apify.com/logiover/polymarket-top-wallets-leaderboard) | scraper API for the official leaderboard |

We don't depend on any of these — the framework owns its data. They're useful sanity checks for the wallet ranking we compute ourselves.
