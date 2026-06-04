from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polybot.db import get_session
from polybot.models import PnLSnapshot

router = APIRouter()


@router.get("")
async def equity_curve(
    *, mode: str = "paper", limit: int = 1440,
    s: AsyncSession = Depends(get_session),
) -> list[dict]:
    rows = (await s.execute(
        select(PnLSnapshot).where(PnLSnapshot.mode == mode)
        .order_by(PnLSnapshot.ts.desc()).limit(limit)
    )).scalars().all()
    return [
        {"ts": p.ts, "equity": p.equity_usdc, "realized": p.realized_usdc,
         "unrealized": p.unrealized_usdc, "open": p.open_positions}
        for p in reversed(rows)
    ]
