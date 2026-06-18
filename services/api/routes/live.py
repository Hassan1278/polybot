"""Live Polymarket account view — what's REALLY on the venue.

The dashboard's equity / open-positions cards are paper-centric: they read
the bot's synthetic ledger, so in live mode they don't reflect the money and
positions actually held on Polymarket. This route surfaces the deposit
wallet's ACTUAL CLOB V2 state:

  - pUSD collateral balance (cash) via the clob-rs sidecar — the authoritative
    balance the CLOB itself checks when sizing orders.
  - Open positions + mark-to-market value + unrealized PnL via the public
    data API (data-api.polymarket.com/positions?user=<deposit_wallet>).

Read-only and best-effort: the two sources fail independently and the route
degrades to nulls / empty rather than 500ing, so a flaky data API or a
sleeping sidecar never blanks the dashboard.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polybot.clients import ClobClient, DataClient
from polybot.config import settings
from polybot.db import get_session
from polybot.logging import get_logger
from polybot.models import WalletCredential
from services.api.deps import require_admin

log = get_logger(__name__)
router = APIRouter()


async def _deposit_wallet(s: AsyncSession) -> str | None:
    """Resolve the deposit (funder) wallet we trade from on V2.

    Prefer POLYMARKET_FUNDER_ADDRESS — that's the exact value the clob-rs
    sidecar signs/queries balances for, so the balance and the positions
    describe the same account. Fall back to the active wallet credential's
    funder (or signer, for an EOA row).
    """
    env_funder = (settings.polymarket_funder_address or "").strip()
    if env_funder:
        return env_funder
    row = (
        await s.execute(
            select(WalletCredential)
            .where(WalletCredential.is_active.is_(True))
            .order_by(WalletCredential.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row:
        return row.funder_address or row.address
    return None


def _f(v: Any) -> float:
    """Coerce data-API / sidecar decimal-strings to float, defaulting to 0."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


@router.get("/account", dependencies=[Depends(require_admin)])
async def live_account(s: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Real Polymarket account snapshot for the deposit wallet.

    Admin-only: reveals the wallet address and hits external venue APIs.
    Returns ``{"configured": false}`` when no deposit wallet is set so the
    dashboard can render a hint instead of an error.
    """
    address = await _deposit_wallet(s)
    if not address:
        return {
            "configured": False,
            "note": (
                "no deposit wallet configured — set POLYMARKET_FUNDER_ADDRESS "
                "or add an active wallet credential"
            ),
        }

    # Independent failure tracking so one dead source doesn't blank the other.
    errors: dict[str, str | None] = {"balance": None, "positions": None}

    async def _balance() -> float | None:
        c = ClobClient()
        try:
            async with asyncio.timeout(12.0):
                d = await c.balance()
            if isinstance(d, dict) and d.get("ok") and d.get("balance") is not None:
                return _f(d.get("balance"))
            errors["balance"] = (
                (d.get("error") if isinstance(d, dict) else None)
                or "clob-rs balance unavailable"
            )
            return None
        except Exception as exc:  # noqa: BLE001
            errors["balance"] = f"{type(exc).__name__}: {str(exc)[:140]}"
            return None
        finally:
            await c.close()

    async def _positions() -> list[dict[str, Any]]:
        c = DataClient()
        try:
            async with asyncio.timeout(12.0):
                rows = await c.positions(address, limit=100, size_threshold=0.0)
            return rows if isinstance(rows, list) else []
        except Exception as exc:  # noqa: BLE001
            errors["positions"] = f"{type(exc).__name__}: {str(exc)[:140]}"
            return []
        finally:
            await c.close()

    pusd, raw_positions = await asyncio.gather(_balance(), _positions())

    positions: list[dict[str, Any]] = []
    pos_value = 0.0
    unrealized = 0.0
    for p in raw_positions:
        if not isinstance(p, dict):
            continue
        size = _f(p.get("size"))
        if size <= 0:
            continue
        cur_val = _f(p.get("currentValue"))
        cash_pnl = _f(p.get("cashPnl"))
        init_val = _f(p.get("initialValue"))
        pos_value += cur_val
        unrealized += cash_pnl
        # Compute the % return ourselves (cash PnL / cost) so the scale is
        # unambiguous — the data API's `percentPnl` field's units vary. This
        # matches the fraction convention the paper /positions card uses.
        pct_change = (cash_pnl / init_val) if init_val > 0 else None
        positions.append({
            "asset": str(p.get("asset") or ""),
            "condition_id": p.get("conditionId"),
            "title": p.get("title"),
            "slug": p.get("slug"),
            "outcome": p.get("outcome"),
            "size": size,
            "avg_price": _f(p.get("avgPrice")),
            "cur_price": _f(p.get("curPrice")),
            "current_value": round(cur_val, 2),
            "initial_value": round(init_val, 2),
            "cash_pnl": round(cash_pnl, 2),
            "pct_change": round(pct_change, 4) if pct_change is not None else None,
            "redeemable": bool(p.get("redeemable")),
        })

    positions.sort(key=lambda x: x["current_value"], reverse=True)

    # Real equity = cash collateral + marked value of open positions. Only
    # meaningful when the balance read succeeded; otherwise leave null so the
    # UI doesn't imply a wrong total.
    equity = (pusd + pos_value) if pusd is not None else None

    return {
        "configured": True,
        "address": address,
        "pusd_balance": pusd,
        "positions_value": round(pos_value, 2),
        "unrealized_pnl": round(unrealized, 2),
        "equity": round(equity, 2) if equity is not None else None,
        "n_positions": len(positions),
        "positions": positions,
        "errors": errors,
    }
