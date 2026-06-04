from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polybot.db import get_session
from polybot.models import Signal

router = APIRouter()


@router.get("")
async def list_signals(
    *, only_pass: bool = False, limit: int = 100,
    s: AsyncSession = Depends(get_session),
) -> list[dict]:
    q = select(Signal).order_by(Signal.ts.desc()).limit(limit)
    if only_pass:
        q = q.where(Signal.gate_pass.is_(True))
    rows = (await s.execute(q)).scalars().all()
    return [
        {
            "id": x.id, "ts": x.ts, "market_id": x.market_id, "side": x.side,
            "outcome": x.outcome, "wallet_count": x.wallet_count, "wallets": x.wallets,
            "avg_win_rate": x.avg_win_rate, "correlation_score": x.correlation_score,
            "target_price": x.target_price, "target_size_usdc": x.target_size_usdc,
            "gate_results": x.gate_results, "gate_pass": x.gate_pass, "executed": x.executed,
        }
        for x in rows
    ]
