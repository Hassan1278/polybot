"""Bubble + heatmap payloads for the dashboard.

`bubble`  — every active wallet as a node:
  x = win-rate (null → skipped from x axis), y = sharpe, r = trade_count,
  color = category, plus realized_pnl / n_decisions for the tooltip.

`heatmap` — pairwise Jaccard similarity on markets-touched-in-N-days.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polybot.db import get_session
from polybot.models import Trade, Wallet, WalletStats
from polybot.stats import jaccard_matrix

router = APIRouter()


@router.get("/bubble")
async def bubble(s: AsyncSession = Depends(get_session)) -> dict:
    rows = (await s.execute(
        select(
            Wallet.address, Wallet.category,
            WalletStats.win_rate, WalletStats.sharpe,
            WalletStats.trade_count, WalletStats.pnl_usdc,
            WalletStats.realized_pnl_usdc, WalletStats.roi,
            WalletStats.n_decisions, WalletStats.n_open_positions,
        )
        .join(WalletStats,
              (WalletStats.address == Wallet.address) & (WalletStats.window == "30d"))
        .where(Wallet.is_active.is_(True))
    )).all()
    return {
        "nodes": [
            {
                "id":               r[0][:8],
                "address":          r[0],
                "category":         r[1] or "other",
                "win_rate":         r[2],     # nullable
                "sharpe":           r[3],     # nullable
                "trade_count":      r[4] or 0,
                "pnl":              r[5] or 0.0,
                "realized_pnl":     r[6] or 0.0,
                "roi":              r[7] or 0.0,
                "n_decisions":      r[8] or 0,
                "n_open_positions": r[9] or 0,
            }
            for r in rows
        ]
    }


@router.get("/heatmap")
async def heatmap(*, days: int = 7, s: AsyncSession = Depends(get_session)) -> dict:
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
    rows = (await s.execute(
        select(Trade.wallet, Trade.market_id)
        .join(Wallet, Wallet.address == Trade.wallet)
        .where((Wallet.is_active.is_(True)) & (Trade.ts >= cutoff))
    )).all()
    sets: dict[str, set[str]] = {}
    for w, m in rows:
        sets.setdefault(w, set()).add(m)
    if not sets:
        return {"labels": [], "matrix": []}
    labels, m = jaccard_matrix(sets)
    return {
        "labels":    [l[:8] for l in labels],
        "addresses": labels,
        "matrix":    m.tolist(),
    }
