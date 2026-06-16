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
from polybot.health_server import HealthBeacon, run_health_server
from polybot.logging import get_logger
from polybot.models import AuditLog, Fill, Market
from polybot.redis_bus import publish, xack, xautoclaim, xconsume, xdlq
from services.executor.live import place_live
from services.executor.paper import simulate_fill
from services.executor.pnl_loop import pnl_loop
from services.executor.risk import RiskRejection, preflight

log = get_logger(__name__)

# Executor is consumer-driven (signal:new from Redis) AND has the pnl_loop
# heartbeat every 60s. 10-min window catches both a stuck consumer AND a
# stalled pnl_loop without false positives during quiet trading hours.
_BEACON = HealthBeacon(name="executor", stale_after_seconds=600)


async def handle(sig: dict) -> None:
    sid = sig["id"]
    market_id = sig["market_id"]
    outcome = sig.get("outcome", "YES")
    side = sig["side"]
    size_usdc = float(sig.get("size_usdc", settings.max_position_usdc))
    score = float(sig.get("score", 0.0))

    # B14 follow-up: idempotency check. Multiple delivery paths can hand the
    # same signal_id to the executor — Redis pub/sub re-delivery during a
    # subscriber crash, manual replay via redis-cli, or a future Streams
    # XAUTOCLAIM. Without this check, the same signal could write multiple
    # Fill rows, double-counting the position. The DB also enforces this
    # via the partial UNIQUE index `uq_fills_signal_id` (migration 0004).
    async with session_scope() as s:
        existing = (await s.execute(
            select(Fill.id).where(Fill.signal_id == sid)
        )).scalar()
    if existing is not None:
        log.info(
            "executor_dedup_skip",
            signal=sid, existing_fill=existing,
            reason="signal already processed",
        )
        return

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
            # Audit-trail fix: previously this branch silently returned,
            # leaving no Fill row and no alert — operator had to read
            # executor logs to figure out why orders weren't flowing.
            # Now record a rejected Fill so the dashboard shows it.
            log.error("live_mode_no_creds", signal=sid)
            from datetime import datetime, timezone

            from polybot.models import Fill
            async with session_scope() as s:
                s.add(Fill(
                    signal_id=sid,
                    ts=datetime.now(tz=timezone.utc),
                    mode="live",
                    market_id=market_id,
                    outcome=outcome,
                    side=side,
                    size_shares=0.0, price=0.0, notional_usdc=0.0, fee_usdc=0.0,
                    status="rejected", error="live_mode_no_creds",
                ))
            try:
                await alerts.notify(
                    "critical",
                    "Live signal dropped: no signing credential",
                    f"signal_id={sid} market={market_id[:18]} — add a wallet "
                    "via /admin/settings/wallet or set POLYMARKET_PRIVATE_KEY",
                )
            except Exception:  # noqa: BLE001
                log.exception("alerts_no_creds_failed")
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


_STREAM = "signal:new"
_GROUP = "executors"
# Per-process consumer name — should be unique per running executor. We use
# the container hostname (= container id prefix). This is what XAUTOCLAIM
# uses to identify "messages held by consumer X that crashed".
import os as _os
_CONSUMER = _os.environ.get("HOSTNAME", "executor-local")


async def signal_consumer() -> None:
    """Consume gate-passed signals via Redis Streams (B1, durable delivery).

    Acks after handle() returns. If handle() raises, the message is
    written to `signal:new:dlq` and ack'd so we don't retry-loop on a
    poison message. (A2's signal_id-dedup will also catch a re-delivery
    if XAUTOCLAIM moves a pending entry to us after a peer crashes.)

    The pub/sub `signal:new` channel is still used by the dashboard SSE
    endpoint for live observation — engine.py publishes to BOTH.
    """
    log.info(
        "executor_consumer_starting",
        mode=settings.trading_mode, stream=_STREAM,
        group=_GROUP, consumer=_CONSUMER,
    )
    async for msg_id, sig in xconsume(_STREAM, _GROUP, _CONSUMER):
        _BEACON.heartbeat(loop="signal_consumer")
        try:
            await handle(sig)
        except Exception as exc:  # noqa: BLE001
            log.exception("executor_handle_failed", payload=sig, msg_id=msg_id)
            try:
                await xdlq(_STREAM, sig, f"{type(exc).__name__}:{exc}", _msg_id=msg_id)
                await alerts.notify(
                    "critical",
                    "Signal moved to DLQ",
                    f"stream={_STREAM} msg_id={msg_id} signal_id={sig.get('id')} "
                    f"err={type(exc).__name__}",
                )
            except Exception:  # noqa: BLE001
                log.exception("executor_dlq_write_failed", msg_id=msg_id)
            continue
        try:
            await xack(_STREAM, _GROUP, msg_id)
        except Exception:  # noqa: BLE001
            log.exception("executor_xack_failed", msg_id=msg_id)


async def _autoclaim_loop() -> None:
    """Periodically reclaim messages from crashed peer consumers.

    Without this, a crashed consumer's in-flight messages stay in its
    PEL forever (until the consumer name reappears, which it won't on
    container restart since HOSTNAME changes). Running every 60s means
    a crashed consumer's messages get re-tried within ~2 min.
    """
    while True:
        try:
            n = await xautoclaim(_STREAM, _GROUP, _CONSUMER, min_idle_ms=60_000)
            if n:
                log.info("autoclaim_reassigned", count=n)
        except Exception:  # noqa: BLE001
            log.exception("autoclaim_failed")
        await asyncio.sleep(60)


async def _pnl_loop_with_beacon():
    """Periodic heartbeat ticker matching pnl_loop's 60 s cadence — the
    health-server beacon goes stale if either loop stops ticking."""
    while True:
        _BEACON.heartbeat(loop="pnl_loop_tick")
        await asyncio.sleep(60)


async def main() -> None:
    log.info("executor_starting", mode=settings.trading_mode, can_sign=settings.can_sign)
    _BEACON.heartbeat(state="warming_up")
    await asyncio.gather(
        signal_consumer(),
        pnl_loop(),
        _pnl_loop_with_beacon(),
        _autoclaim_loop(),
        run_health_server(_BEACON, port=8081),
    )


if __name__ == "__main__":
    asyncio.run(main())
