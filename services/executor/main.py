"""Executor entrypoint.

Subscribes to `signal:new` on Redis. For each signal:
  1. Look up category & risk-check.
  2. If paper-mode → simulate fill.
  3. If live-mode → call live executor.
  4. Persist + publish `fill:new`.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from polybot import alerts
from polybot.config import settings
from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.models import AuditLog, Market
from polybot.redis_bus import publish, subscribe
from services.executor.live import place_live
from services.executor.paper import simulate_fill
from services.executor.pnl_loop import pnl_loop
from services.executor.risk import RiskRejection, preflight

log = get_logger(__name__)


async def handle(sig: dict) -> None:
    sid = sig["id"]
    market_id = sig["market_id"]
    outcome = sig.get("outcome", "YES")
    side = sig["side"]
    size_usdc = float(sig.get("size_usdc", settings.max_position_usdc))
    score = float(sig.get("score", 0.0))

    async with session_scope() as s:
        row = (await s.execute(
            select(Market.category).where(Market.market_id == market_id)
        )).first()
    category = row[0] if row else None

    try:
        await preflight(mode=settings.trading_mode, market_id=market_id,
                        category=category, side=side, size_usdc=size_usdc, score=score)
    except RiskRejection as rej:
        log.warning("risk_rejected", signal=sid, reason=str(rej))
        async with session_scope() as s:
            s.add(AuditLog(actor="executor", event="risk_rejected",
                           payload={"signal_id": sid, "reason": str(rej)}))
        try:
            await alerts.risk_rejected_alert(reason=str(rej), signal_id=sid)
        except Exception:  # noqa: BLE001
            log.exception("alerts_risk_rejected_failed")
        return

    if settings.trading_mode == "paper":
        result = await simulate_fill(
            signal_id=sid, market_id=market_id, outcome=outcome,
            side=side, size_usdc=size_usdc,
        )
    else:
        if not settings.can_sign:
            log.error("live_mode_no_creds")
            return
        result = await place_live(
            signal_id=sid, market_id=market_id, outcome=outcome,
            side=side, size_usdc=size_usdc,
        )
    await publish("fill:new", {"signal_id": sid, "result": result, "mode": settings.trading_mode})

    if isinstance(result, dict) and result.get("status") in ("filled", "submitted", "partial"):
        try:
            await alerts.fill_alert(result)
        except Exception:  # noqa: BLE001
            log.exception("alerts_fill_failed")


async def signal_consumer() -> None:
    log.info("executor_consumer_starting", mode=settings.trading_mode)
    async for sig in subscribe("signal:new"):
        try:
            await handle(sig)
        except Exception:
            log.exception("executor_handle_failed", payload=sig)


async def main() -> None:
    log.info("executor_starting", mode=settings.trading_mode, can_sign=settings.can_sign)
    await asyncio.gather(
        signal_consumer(),
        pnl_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
