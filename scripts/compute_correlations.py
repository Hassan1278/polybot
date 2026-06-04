"""Manual one-shot recomputation of the correlation snapshot.

Useful for the first run / debugging — the signals service does this
automatically every time a new trade arrives.
"""

from __future__ import annotations

import asyncio

import pandas as pd
from sqlalchemy import and_, select

from polybot.config import settings
from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.models import Trade, Wallet
from polybot.stats import cluster_active_wallets
from services.signals.engine import process_candidate

log = get_logger(__name__)


async def main() -> None:
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=settings.correlation_window_minutes)
    async with session_scope() as s:
        rows = (await s.execute(
            select(Trade.ts, Trade.wallet, Trade.market_id, Trade.outcome,
                   Trade.side, Trade.size_shares, Trade.price, Trade.notional_usdc)
            .join(Wallet, Wallet.address == Trade.wallet)
            .where(and_(Wallet.is_active.is_(True), Trade.ts >= cutoff))
        )).all()
    df = pd.DataFrame(rows, columns=[
        "ts", "wallet", "market_id", "outcome", "side", "size_shares", "price", "notional_usdc"])
    cands = cluster_active_wallets(df,
                                   window_minutes=settings.correlation_window_minutes,
                                   min_wallets=settings.correlation_min_wallets)
    log.info("compute_correlations_candidates", n=len(cands))
    for c in cands:
        await process_candidate(c)


if __name__ == "__main__":
    asyncio.run(main())
