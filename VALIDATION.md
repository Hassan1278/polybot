# Validation Checklist

Run **top-to-bottom** before flipping `TRADING_MODE=live`. Every box must be green.

---

## 0. Environment

- [ ] `python --version` ≥ 3.12
- [ ] `docker --version` ≥ 24
- [ ] `node --version` ≥ 20 (only if dev'ing dashboard outside docker)
- [ ] `.env` exists and contains no committed defaults (`change_me`, empty key, etc.)
- [ ] `PRIVATE_KEY` is for a **fresh** wallet, separate from any personal funds
- [ ] `MAX_POSITION_USDC` is small (≤ 25 USDC for first live run)

```bash
python scripts/validate.py env
```

---

## 1. Connectivity

- [ ] Gamma API reachable: `curl https://gamma-api.polymarket.com/markets?limit=1` returns JSON
- [ ] CLOB API reachable: `curl https://clob.polymarket.com/markets?limit=1` returns JSON
- [ ] Data API reachable: `curl https://data-api.polymarket.com/positions?user=0x...&limit=1` returns 200
- [ ] Polygon RPC reachable: `cast block-number --rpc-url $POLYGON_RPC_URL` returns recent block
- [ ] Goldsky subgraph reachable: introspection query succeeds

```bash
python scripts/validate.py connectivity
```

---

## 2. Database

- [ ] Postgres container up: `docker compose ps postgres` → healthy
- [ ] Timescale extension present: `SELECT extname FROM pg_extension WHERE extname='timescaledb';`
- [ ] All migrations applied: `alembic current` matches `head`
- [ ] `wallets`, `trades`, `markets`, `signals`, `fills`, `audit_log` tables exist
- [ ] `trades` is a hypertable: `SELECT * FROM timescaledb_information.hypertables;`

```bash
python scripts/validate.py db
```

---

## 3. Ingest

- [ ] `discover_wallets` populated ≥ 20 wallets per active category
- [ ] `backfill_trades` produced ≥ 1 000 rows in `trades`
- [ ] Live trade listener has produced rows in the last 5 min (assuming a market is active)
- [ ] No rate-limit errors in `docker compose logs ingest` over the last hour

```bash
python scripts/validate.py ingest
```

---

## 4. Signals

- [ ] `wallet_stats` has a row per tracked wallet, `computed_at` < 24 h ago
- [ ] Correlation matrix dimensions match number of active wallets
- [ ] A test signal can be force-fired and persists to `signals` table
- [ ] Each gate (`liquidity`, `risk_reward`, `timeframe`, `correlation_score`) returns pass/fail with a reason

```bash
python scripts/validate.py signals
```

---

## 5. Executor — paper-mode

- [ ] `TRADING_MODE=paper` honoured (executor refuses live orders)
- [ ] Synthetic signal → simulated fill written to `fills` (mode=paper)
- [ ] Fill price respects current orderbook depth (no infinite slippage)
- [ ] `pnl_snapshots` updates every minute
- [ ] After N paper trades, dashboard PnL chart populates

```bash
python scripts/validate.py executor-paper
```

---

## 6. Executor — live-mode (only after § 1–5 pass)

> Do this with **the minimum capital** you're emotionally fine losing. ≤ 25 USDC recommended for the first run.

- [ ] Funder wallet holds expected USDC.e balance: dashboard reads it correctly
- [ ] Allowance set for CTF Exchange contract (one-time on-chain tx)
- [ ] CLOB API creds derived: `client.create_or_derive_api_creds()` succeeds
- [ ] Place + cancel a **GTC limit far from market** — no risk, just to prove signing
- [ ] Kill-switch works: invoke and confirm all orders cancel within 3 s

```bash
python scripts/validate.py executor-live --dry
```

---

## 7. Risk controls

- [ ] `MAX_POSITION_USDC` enforced (try a too-big signal → rejected)
- [ ] `MAX_DAILY_LOSS_USDC` enforced (simulate loss → executor flips to SAFE_HOLD)
- [ ] `MAX_OPEN_POSITIONS` enforced
- [ ] Cool-down between fills per market enforced
- [ ] Kill-switch state survives a container restart

```bash
python scripts/validate.py risk
```

---

## 8. Observability

- [ ] Logs are JSON, one line per event
- [ ] `audit_log` has rows for: wallet discovery, signal fired, signal gated-out, fill, kill-switch
- [ ] Dashboard reflects state in real time (open a trade → see fill within ≤ 2 s)
- [ ] (optional) Telegram alert received for a forced signal

```bash
python scripts/validate.py observability
```

---

## 9. Disaster recovery

- [ ] `pg_dump` backup runs nightly via cron
- [ ] You have actually restored a backup to a sandbox once
- [ ] Containers come back up after `docker compose down && docker compose up -d` with no data loss
- [ ] `KILL_SWITCH=1` survives executor restart

---

## Sign-off

| Operator | Date | Mode unlocked |
|---|---|---|
|   |   | paper |
|   |   | live (≤ 25 USDC) |
|   |   | live (full cap) |

If **any** box above is unchecked, do not promote to live.
