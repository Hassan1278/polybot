from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polybot.db import get_session
from polybot.models import Wallet, WalletStats

router = APIRouter()


@router.get("")
async def list_wallets(
    *,
    category: str | None = None,
    window: str = "30d",
    limit: int = 300,
    s: AsyncSession = Depends(get_session),
) -> list[dict]:
    q = (
        select(
            Wallet.address, Wallet.label, Wallet.category, Wallet.is_active,
            WalletStats.pnl_usdc, WalletStats.realized_pnl_usdc, WalletStats.roi,
            WalletStats.win_rate, WalletStats.sharpe,
            WalletStats.trade_count, WalletStats.avg_trade_size,
            WalletStats.n_decisions, WalletStats.n_open_positions,
            WalletStats.n_total_positions, WalletStats.n_trade_days,
        )
        .join(
            WalletStats,
            (WalletStats.address == Wallet.address) & (WalletStats.window == window),
            isouter=True,
        )
        .where(Wallet.is_active.is_(True))
        .order_by(WalletStats.realized_pnl_usdc.desc().nulls_last())
        .limit(limit)
    )
    if category:
        q = q.where(Wallet.category == category)
    rows = (await s.execute(q)).all()
    return [
        {
            "address":           r[0],
            "label":             r[1],
            "category":          r[2],
            "active":            r[3],
            "pnl_usdc":          r[4],
            "realized_pnl_usdc": r[5],
            "roi":               r[6],
            "win_rate":          r[7],
            "sharpe":            r[8],
            "trade_count":       r[9],
            "avg_trade_size":    r[10],
            "n_decisions":       r[11],
            "n_open_positions":  r[12],
            "n_total_positions": r[13],
            "n_trade_days":      r[14],
        }
        for r in rows
    ]


@router.get("/{address}")
async def get_wallet(address: str, s: AsyncSession = Depends(get_session)) -> dict:
    w = (await s.execute(select(Wallet).where(Wallet.address == address))).scalar_one_or_none()
    stats = (
        await s.execute(
            select(WalletStats)
            .where(WalletStats.address == address)
            .order_by(WalletStats.computed_at.desc())
            .limit(20)
        )
    ).scalars().all()
    return {
        "wallet": {"address": w.address, "label": w.label, "category": w.category} if w else None,
        "stats": [
            {
                "window":            ws.window,
                "pnl_usdc":          ws.pnl_usdc,
                "realized_pnl_usdc": ws.realized_pnl_usdc,
                "roi":               ws.roi,
                "win_rate":          ws.win_rate,
                "sharpe":            ws.sharpe,
                "trade_count":       ws.trade_count,
                "n_decisions":       ws.n_decisions,
                "n_open_positions":  ws.n_open_positions,
                "computed_at":       ws.computed_at,
            }
            for ws in stats
        ],
    }
