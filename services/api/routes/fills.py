from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polybot.db import get_session
from polybot.models import Fill

router = APIRouter()


@router.get("")
async def list_fills(
    *, mode: str | None = None, limit: int = 100,
    s: AsyncSession = Depends(get_session),
) -> list[dict]:
    q = select(Fill).order_by(Fill.ts.desc()).limit(limit)
    if mode:
        q = q.where(Fill.mode == mode)
    rows = (await s.execute(q)).scalars().all()
    return [
        {
            "id": f.id, "signal_id": f.signal_id, "ts": f.ts, "mode": f.mode,
            "market_id": f.market_id, "outcome": f.outcome, "side": f.side,
            "size_shares": f.size_shares, "price": f.price,
            "notional_usdc": f.notional_usdc, "fee_usdc": f.fee_usdc,
            "status": f.status, "venue_order_id": f.venue_order_id, "error": f.error,
        }
        for f in rows
    ]
