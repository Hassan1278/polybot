# Live Trading on Polymarket CLOB V2 — Surgical Migration Plan

**Status:** in progress. This replaces ONLY the live order-placement layer.
Everything else (signals, gates, correlation, ingest, risk, paper executor,
dashboard, DB) is V2-agnostic and stays untouched.

## Why this is needed

On **2026-04-28** Polymarket cut over to **CLOB V2**:
- New collateral token **pUSD** (ERC-20, 1:1 backed by USDC.e). API traders
  must wrap USDC.e → pUSD via the **Collateral Onramp** (`wrap()`); the
  polymarket.com UI does this automatically.
- New V2 Exchange contracts.
- New onboarding for new API users: **deposit wallets**, signature type **3
  (POLY_1271)** — an ERC-1967 proxy smart wallet whose order signatures the
  CLOB validates via **ERC-1271** (ERC-7739-wrapped).
- The legacy raw-EOA flow (what this bot used) is **rejected** post-cutover
  → `maker address not allowed, please use the deposit wallet flow`.

The **Python** and **TypeScript** SDKs are both broken for the deposit-wallet
flow (open bugs: py-clob-client-v2 #70, clob-client-v2 #65 — L1 API-key
derivation binds the key to the EOA, not the deposit wallet, so the CLOB
rejects every order: signer != api_key). The **Rust** SDK
(`rs-clob-client-v2`) implements the ERC-1271 L1 auth correctly and is the
only official client that currently works for deposit-wallet trading.

## Strategy: keep the bot, replace only execution

```
signals ─▶ gates ─▶ engine ─▶ Redis(signal:new) ─▶ executor/main.py
                                                       │
                                          ┌────────────┴───────────┐
                                       paper.py (KEEP)        live.py (KEEP iface)
                                                                    │ place_limit()
                                                            clob.py SIGNED methods  ◀── REPLACE
                                                                    │ HTTP
                                                            clob-rs sidecar (Rust)  ◀── ADD
                                                                    │ rs-clob-client-v2 (sig type 3)
                                                              Polymarket CLOB V2
```

### KEEP (no changes)
- `services/signals/**`, `services/ingest/**` — all alpha + data.
- `services/executor/main.py`, `risk.py`, `paper.py`, `pnl_loop.py`.
- `packages/polybot/**` except the signed path in `clob.py`.
- `clob.py` **read-only** methods (`book`, `midpoint`, `price`,
  `last_trade_price`, `best_mark`, `books`) — no auth, unaffected by V2.
- `dashboard/**`, DB models, migrations, `redis_bus`, `runtime_config`.

### REPLACE
- `packages/polybot/clients/clob.py` — the **signed** methods only
  (`_signed_client`, `place_limit`, `cancel`, `cancel_all`, `open_orders`):
  drop `py_clob_client_v2`; call the `clob-rs` sidecar over HTTP. Same async
  method signatures, so `live.py` and callers are unchanged.

### ADD
- `services/clob-rs/` — small Rust HTTP service using `rs-clob-client-v2`.
  Holds the deposit-wallet signing config, derives the L1/L2 API creds once,
  exposes order endpoints.
- `docker-compose*.yml` — a `clob-rs` service; `executor` depends on it.
- Config: deposit-wallet **funder** address + controlling **signer key** +
  `signature_type=3` (sidecar env / reuse the wallet-credential row).

## One-time setup via the web UI (NO code — do this in Phase 0)
Doing this in polymarket.com removes the hardest, most error-prone code from
the bot (relayer deposit-wallet deployment + onramp wrapping + allowances):
1. Sign in to polymarket.com with the controlling wallet → it deploys your
   **deposit wallet** (note its address).
2. Deposit / move your USDC.e in → UI **wraps it to pUSD** automatically.
3. Confirm a tiny manual trade works (proves the account is V2-ready).

After this, the bot only needs to **sign + place orders** from the
already-funded deposit wallet.

## HTTP contract: clob.py  ◀──▶  clob-rs   (internal docker network)
Base: `http://clob-rs:8082`
- `GET  /health` → `{"ok": true, "address": "<deposit_wallet>"}`
- `POST /order`  ← `{token_id, side: "BUY"|"SELL", price, size, order_type: "GTC"|"FAK"}`
                 → `{status, order_id, raw}` or `{status:"rejected", error}`
- `POST /cancel` ← `{order_id}` → `{ok}`
- `POST /cancel-all` → `{ok}`
- `GET  /orders` → `[ ... open orders ... ]`

`clob.py` keeps its current method names/return shapes so nothing upstream
changes; it just calls these endpoints instead of the Python SDK.

## V2 reference facts (verified)
- V2 CTF Exchange (standard): `0xE111180000d2663C0091e4f400237545B87B996B`
- V2 NegRisk CTF Exchange:    `0xe2222d279d744050d28e00520010520000310F59`
- EIP-712 order domain version = **"2"**; **ClobAuth (L1 key derivation)
  stays version "1"** — biggest migration pitfall. (`rs-clob-client-v2`
  handles both.)
- V2 order struct: `salt, maker, signer, tokenId, makerAmount, takerAmount,
  side, signatureType, timestamp(ms), metadata, builder` (no nonce/expiration).
- pUSD / CollateralOnramp / deposit-wallet-factory addresses: **TBD** — pull
  from Polymarket V2 docs or observe them during the Phase 0 web-UI setup.
  (Not needed in the bot if the UI handles wrapping.)

## Phases
- **Phase 0 (operator, web UI):** deposit wallet + pUSD + manual trade. Capture
  the deposit-wallet address. ← unblocks everything; do first.
- **Phase 1 (code):** scaffold `clob-rs` (HTTP server + endpoints + Dockerfile)
  and wire it into compose. Stub order logic.
- **Phase 2 (code):** implement signing + order placement in `clob-rs` via
  `rs-clob-client-v2` (sig type 3). Pin the crate API first. Compile on VPS.
- **Phase 3 (code):** refactor `clob.py` signed methods → call `clob-rs`.
  Keep read methods as-is. `live.py` unchanged.
- **Phase 4 (config + test):** point the sidecar at the deposit wallet; smoke
  test (derive creds + place ONE tiny resting order that won't fill, confirm
  the venue ACCEPTS it), then enable live.

## Open items / inputs needed
- Pin `rs-clob-client-v2` public API (docs.rs / repo examples).
- Deposit-wallet address (from Phase 0).
- Rust toolchain in the `clob-rs` image (multi-stage build).
- Compile + real-order testing happens on the VPS (small amounts).

## Rollback
Live execution is isolated behind `clob.py`'s signed methods + the `clob-rs`
service. Disable by flipping the bot to paper (`enabled_modes=["paper"]`) or
stopping `clob-rs`; nothing else is affected.
