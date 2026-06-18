from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polybot.chain import read_balances
from polybot.db import get_session
from polybot.models import Wallet, WalletCredential, WalletStats
from services.api.deps import require_admin

router = APIRouter()


@router.get("/onchain", dependencies=[Depends(require_admin)])
async def list_onchain_balances(s: AsyncSession = Depends(get_session)) -> dict:
    """Read live on-chain POL + USDC.e balances for every bot wallet
    credential in the DB.

    Admin-only because (1) it reveals the bot wallet address(es) and
    (2) it hits the Polygon RPC, which we don't want unauth'd callers
    to spam. Each balance read is ~80 ms (single eth_call), so we run
    them in parallel via asyncio.to_thread.
    """
    rows = (
        await s.execute(
            select(WalletCredential).where(WalletCredential.is_active.is_(True))
        )
    ).scalars().all()
    if not rows:
        return {"wallets": [], "note": "no active wallet credentials configured"}

    # CRITICAL: read the FUNDER address, not the signer `address`.
    #
    # On Polymarket the USDC collateral lives in the *funder* (proxy) wallet
    # for signature_type 1 (email/magic) and 2 (browser) — which is the
    # default. The signer `address` is only the key that signs orders and
    # holds essentially no USDC (gas is paid by Polymarket's relayer for
    # proxy wallets). Only for a pure EOA (sig_type 0) is funder == signer.
    #
    # Reading `w.address` here showed USDC.e == $0.00 for every proxy wallet
    # even when the account was fully funded — i.e. "something is wrong with
    # the USDC". Read the funder so the dashboard reflects the balance the
    # venue actually trades against. Fall back to the signer if funder is
    # somehow unset (older/EOA rows).
    targets = [(w, (w.funder_address or w.address)) for w in rows]
    results = await asyncio.gather(
        *(asyncio.to_thread(read_balances, addr) for _, addr in targets),
        return_exceptions=False,
    )
    return {
        "wallets": [
            {
                "id":             w.id,
                "label":          w.label,
                "address":        w.address,
                "funder_address": w.funder_address,
                # which address the balances below were actually read from
                # (the funder/collateral wallet for proxy sig-types).
                "balance_of":     addr,
                "is_active":      w.is_active,
                "balances":       bal,
            }
            for (w, addr), bal in zip(targets, results)
        ],
    }


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
