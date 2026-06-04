from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polybot.db import get_session
from polybot.models import Market

router = APIRouter()


@router.get("")
async def list_markets(
    *, category: str | None = None, resolved: bool = False, limit: int = 200,
    s: AsyncSession = Depends(get_session),
) -> list[dict]:
    q = select(Market).where(Market.resolved.is_(resolved)).order_by(Market.volume_24h_usdc.desc()).limit(limit)
    if category:
        q = q.where(Market.category == category)
    rows = (await s.execute(q)).scalars().all()
    return [
        {
            "market_id": m.market_id, "slug": m.slug, "question": m.question,
            "category": m.category, "end_date": m.end_date,
            "liquidity_usdc": m.liquidity_usdc, "volume_24h_usdc": m.volume_24h_usdc,
            "yes_token_id": m.yes_token_id, "no_token_id": m.no_token_id,
        }
        for m in rows
    ]
