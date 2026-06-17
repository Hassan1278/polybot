"""Recompute wallet stats every 5 min using positions + trades.

Pipeline per wallet:
  1. Pull `/positions?user=X` → ground-truth PnL (realised + mark-to-market).
  2. Load trade history from our DB → daily PnL buckets for Sharpe.
  3. Combine via `wallet_stats_from_positions`.
  4. Upsert a new wallet_stats row for windows 7d / 30d / 90d.

The window filter only affects the trade-based Sharpe; positions are a
current snapshot (Polymarket doesn't expose historical positions).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from polybot.clients import DataClient
from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.models import Trade, Wallet, WalletStats
from polybot.stats import wallet_stats_from_positions

log = get_logger(__name__)

WINDOWS = {"7d": 7, "30d": 30, "90d": 90}
PER_WALLET_DELAY = 0.1   # gentle on the data-api


async def _trades_for_window(addr: str, days: int) -> pd.DataFrame:
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
    async with session_scope() as s:
        rows = (await s.execute(
            select(Trade.ts, Trade.market_id, Trade.outcome, Trade.side,
                   Trade.size_shares, Trade.price, Trade.notional_usdc, Trade.fee_usdc)
            .where((Trade.wallet == addr) & (Trade.ts >= cutoff))
        )).all()
    if not rows:
        return pd.DataFrame(columns=[
            "ts", "market_id", "outcome", "side", "size_shares", "price",
            "notional_usdc", "fee_usdc",
        ])
    return pd.DataFrame(rows, columns=[
        "ts", "market_id", "outcome", "side", "size_shares", "price",
        "notional_usdc", "fee_usdc",
    ])


async def _compute_for_addr(addr: str, *, data: DataClient | None = None) -> None:
    own = data is None
    if own:
        data = DataClient()
    try:
        try:
            positions = await data.positions(addr, limit=500)
        except Exception as exc:  # noqa: BLE001
            log.warning("positions_fetch_failed", wallet=addr, err=str(exc))
            positions = []
        positions = positions or []

        now = datetime.now(tz=timezone.utc)
        async with session_scope() as s:
            for label, days in WINDOWS.items():
                trades_df = await _trades_for_window(addr, days)
                stats = wallet_stats_from_positions(positions, trades_df=trades_df)
                # UPSERT instead of INSERT — migration 0007 made
                # (address, window) UNIQUE. Without the on_conflict the
                # gate's SELECT averaged every historical snapshot.
                values = dict(
                    address=addr,
                    window=label,
                    pnl_usdc=stats["pnl_usdc"],
                    realized_pnl_usdc=stats["realized_pnl_usdc"],
                    roi=stats["roi"],
                    win_rate=stats["win_rate"],
                    sharpe=stats["sharpe"],
                    trade_count=stats["trade_count"],
                    avg_trade_size=stats["avg_trade_size"],
                    n_decisions=stats["n_decisions"],
                    n_open_positions=stats["n_open_positions"],
                    n_total_positions=stats["n_total_positions"],
                    n_trade_days=stats["n_trade_days"],
                    computed_at=now,
                )
                stmt = pg_insert(WalletStats).values(**values)
                await s.execute(stmt.on_conflict_do_update(
                    index_elements=["address", "window"],
                    set_={k: v for k, v in values.items() if k not in ("address", "window")},
                ))
    finally:
        if own:
            await data.close()


async def stats_loop(beacon=None) -> None:
    while True:
        try:
            if beacon is not None:
                beacon.heartbeat(loop="stats")
            async with session_scope() as s:
                addrs = [r[0] for r in (await s.execute(
                    select(Wallet.address).where(Wallet.is_active.is_(True))
                )).all()]
            log.info("stats_loop_recomputing", n=len(addrs))
            d = DataClient()
            try:
                for a in addrs:
                    try:
                        await _compute_for_addr(a, data=d)
                    except Exception:
                        log.exception("stats_compute_failed", wallet=a)
                    await asyncio.sleep(PER_WALLET_DELAY)
            finally:
                await d.close()
        except Exception:
            log.exception("stats_loop_failed")
        await asyncio.sleep(300)
