"""Pull recent trades for every active tracked wallet via the Data API.

Watermark optimisation:

The Data API has no `since=` parameter, so we always pull `limit=200` newest
trades per wallet. To avoid re-publishing the same 200 trades on every 60-second
run, we keep a per-wallet HIGH WATERMARK (epoch seconds of the latest trade
we've already ingested, persisted in Redis with 7-day TTL):

  - Skip trades whose timestamp is `<= watermark` (already seen).
  - Skip the entire wallet if the API's newest trade is `<= watermark`
    (wallet hasn't traded since last poll — save the DB writes AND the
    redundant `trade:new` publishes that wake the correlation loop).
  - Bump the watermark to the newest seen timestamp after the run.

Without this, every single run we'd publish ~200 trades × 110 wallets ≈ 22 000
events the correlation engine has already processed — and at a 60-second cadence
that redundancy would be relentless. With it, we publish only genuinely new prints.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from polybot.clients import DataClient
from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.models import Trade, Wallet
from polybot.redis_bus import client as _redis_client
from polybot.redis_bus import publish

log = get_logger(__name__)

# Watermark Redis key + TTL. 7 days >> our 60-second poll interval, so a wallet
# can go dormant for almost a week and still be deduplicated correctly.
_WM_KEY = "polybot:trade_ingest:wm:{addr}"
_WM_TTL = 7 * 24 * 3600


async def _wallet_addresses() -> list[str]:
    async with session_scope() as s:
        rows = (await s.execute(
            select(Wallet.address).where(Wallet.is_active.is_(True))
        )).all()
    return [r[0] for r in rows]


async def _get_watermark(addr: str) -> int:
    raw = await _redis_client().get(_WM_KEY.format(addr=addr))
    try:
        return int(raw) if raw else 0
    except (TypeError, ValueError):
        return 0


async def _set_watermark(addr: str, ts: int) -> None:
    await _redis_client().set(_WM_KEY.format(addr=addr), str(int(ts)), ex=_WM_TTL)


async def _ingest_wallet(d: DataClient, addr: str, *, max_trades: int = 200) -> int:
    """Pull this wallet's most recent trades and persist anything newer than
    its watermark. Returns the number of *new* trades ingested (not the API
    response size)."""
    try:
        rows = await d.trades(addr, limit=max_trades)
    except Exception as exc:  # noqa: BLE001
        log.warning("trade_fetch_failed", wallet=addr, err=str(exc))
        return 0
    if not rows:
        return 0

    wm = await _get_watermark(addr)
    # Quick path: if the newest trade is older-or-equal to the watermark,
    # nothing new — bail without touching the DB.
    try:
        newest_ts = max(int(r["timestamp"]) for r in rows)
    except (KeyError, TypeError, ValueError):
        newest_ts = 0
    if wm and newest_ts <= wm:
        return 0

    n = 0
    newest_seen = wm
    async with session_scope() as s:
        for t in rows:
            try:
                t_ts = int(t["timestamp"])
            except (KeyError, TypeError, ValueError):
                continue
            if wm and t_ts <= wm:
                continue            # already processed in a prior run

            tx = t.get("transactionHash")
            size = float(t.get("size", 0))
            price = float(t.get("price", 0))
            market_id = t.get("conditionId") or t.get("market") or ""
            side = (t.get("side") or "BUY").upper()
            ts_dt = datetime.fromtimestamp(t_ts, tz=timezone.utc)

            stmt = pg_insert(Trade).values(
                tx_hash=tx, ts=ts_dt, wallet=addr,
                market_id=market_id,
                outcome=(t.get("outcome") or "YES").upper(),
                side=side, size_shares=size, price=price,
                notional_usdc=size * price,
                fee_usdc=float(t.get("fee", 0)),
                source="data_api",
            )
            if tx:
                # Migration 0005 (TimescaleDB hypertable) requires the
                # partition column `ts` to be in every UNIQUE constraint.
                # The old `(tx_hash)` index was replaced with a composite
                # `(tx_hash, ts)`. The ON CONFLICT clause must reference
                # ALL columns of the target index, otherwise Postgres
                # raises "there is no unique or exclusion constraint
                # matching the ON CONFLICT specification" — which then
                # propagates through asyncio.gather, closes the shared
                # DataClient in the finally block, and cascade-fails the
                # remaining wallets with "client has been closed". That
                # bug silently broke trade_ingest for 9+ days.
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["tx_hash", "ts"],
                    index_where=text("tx_hash IS NOT NULL"),
                )
            await s.execute(stmt)
            n += 1
            newest_seen = max(newest_seen, t_ts)
            # Wake the correlation loop with a genuinely new event.
            await publish("trade:new", {
                "wallet": addr, "market_id": market_id, "side": side,
                "size": size, "price": price, "ts": t_ts,
            })

    # The watermark update HAS to happen AFTER the `async with`
    # session_scope exits successfully — that's when the DB commit
    # actually happens. The previous in-block ordering set the watermark
    # FIRST and then committed, so a commit failure (transient
    # OperationalError, hard failure, etc.) left us with a bumped Redis
    # watermark but un-persisted trades = silent data loss until the
    # next manual replay. The B16 comment had been rationalising the
    # broken order; flipped here. ON CONFLICT DO NOTHING still
    # idempotently dedupes if we re-ingest the same range.
    if newest_seen > wm:
        await _set_watermark(addr, newest_seen)
    return n


async def run_trade_ingest(*, concurrency: int = 8) -> None:
    addrs = await _wallet_addresses()
    if not addrs:
        log.warning("trade_ingest_no_wallets")
        return

    d = DataClient()
    sem = asyncio.Semaphore(concurrency)

    async def _run(a: str) -> int:
        async with sem:
            return await _ingest_wallet(d, a)

    try:
        counts = await asyncio.gather(*[_run(a) for a in addrs])
        active_wallets = sum(1 for c in counts if c > 0)
        log.info(
            "trade_ingest_done",
            wallets_polled=len(addrs),
            wallets_with_new_trades=active_wallets,
            new_trades=sum(counts),
        )
    finally:
        await d.close()
