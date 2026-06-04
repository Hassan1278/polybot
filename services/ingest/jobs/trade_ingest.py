"""Backfill recent trades for every active tracked wallet via the Data API."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from polybot.clients import DataClient
from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.models import Trade, Wallet
from polybot.redis_bus import publish

log = get_logger(__name__)


async def _wallet_addresses() -> list[str]:
    async with session_scope() as s:
        rows = (await s.execute(select(Wallet.address).where(Wallet.is_active.is_(True)))).all()
    return [r[0] for r in rows]


async def _ingest_wallet(d: DataClient, addr: str, *, max_trades: int = 200) -> int:
    try:
        ts = await d.trades(addr, limit=max_trades)
    except Exception as exc:  # noqa: BLE001
        log.warning("trade_fetch_failed", wallet=addr, err=str(exc))
        return 0
    if not ts:
        return 0

    n = 0
    async with session_scope() as s:
        for t in ts:
            tx = t.get("transactionHash")
            size = float(t.get("size", 0))
            price = float(t.get("price", 0))
            market_id = t.get("conditionId") or t.get("market") or ""
            side = (t.get("side") or "BUY").upper()
            ts_dt = datetime.fromtimestamp(int(t["timestamp"]), tz=timezone.utc)

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
                # The partial unique index requires matching its WHERE clause.
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["tx_hash"],
                    index_where=text("tx_hash IS NOT NULL"),
                )
            await s.execute(stmt)
            n += 1
            await publish("trade:new", {
                "wallet": addr, "market_id": market_id, "side": side,
                "size": size, "price": price, "ts": int(t["timestamp"]),
            })
    return n


async def run_trade_ingest(*, concurrency: int = 5) -> None:
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
        log.info("trade_ingest_done", wallets=len(addrs), trades=sum(counts))
    finally:
        await d.close()
