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
from polybot.runtime_config import current_mode, enabled_modes
from services.executor.equity_guard import equity_guard_loop
from services.executor.exit_loop import exit_loop
from services.executor.exit_rules import rules_sweep_loop
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
    # Entry price (probability, 0-1) — drives the low-odds per-bet cap in
    # preflight. Falls back to 0.0 when absent (treated as "unknown odds", so the
    # full per-bet cap applies rather than the tight long-shot cap).
    price = float(sig.get("avg_price", 0.0) or 0.0)

    # Idempotency pre-check moved INSIDE the per-mode for-loop below.
    # Parallel mode (paper + live) needs to skip ONLY modes that have
    # already been filled, not the entire signal — otherwise a paper-row
    # written on a prior delivery blocks the live retry forever. Migration
    # 0008 enforces the same constraint at the DB layer with
    # uq_fills_signal_id_mode(signal_id, mode).

    async with session_scope() as s:
        row = (await s.execute(
            select(Market.category, Market.outcomes).where(Market.market_id == market_id)
        )).first()
    category = row[0] if row else None
    market_outcomes = row[1] if row else None

    # Validate that the signal's outcome actually exists on the market.
    # Without this, a corrupted/spoofed signal pushes a bogus outcome
    # through to live.place_live, which then logs token_id_missing and
    # writes a Fill row with status=rejected — wasting a slot on the
    # consumer stream and audit log for a message that should have been
    # caught at the gate. Binary markets default to YES/NO when outcomes
    # is empty.
    valid_outcomes: list[str]
    if market_outcomes:
        try:
            valid_outcomes = [str(o).upper() for o in market_outcomes]
        except Exception:  # noqa: BLE001
            valid_outcomes = ["YES", "NO"]
    else:
        valid_outcomes = ["YES", "NO"]
    if outcome.upper() not in valid_outcomes:
        log.warning("executor_bad_outcome", signal=sid,
                    outcome=outcome, valid=valid_outcomes)
        async with session_scope() as s:
            s.add(AuditLog(actor="executor", event="bad_outcome",
                           payload={"signal_id": sid, "outcome": outcome,
                                    "valid": valid_outcomes}))
        return

    # PARALLEL paper+live mode. Each enabled mode runs INDEPENDENTLY: own
    # risk preflight, own Fill row, own publish. This lets the operator
    # keep a paper-shadow running while testing live with real USDC — the
    # paper run is the control group, the live run is the experiment.
    #
    # Mode set comes from runtime_config (Redis override > default).
    # Defaults to {"paper"} so existing single-mode behavior is preserved.
    # The legacy `current_mode()` still returns "live" when live is in
    # the active set (preserves backward compat for code that needs ONE
    # mode label — e.g. health dashboard's mode badge).
    modes = await enabled_modes()
    if not modes:
        log.warning("executor_no_modes_enabled", signal=sid)
        return

    from datetime import datetime, timezone
    results: dict[str, dict] = {}
    for exec_mode in sorted(modes):
        # Per-mode try/except: a live failure must NOT propagate up and
        # DLQ the paper-success. We log + continue to the next mode.
        try:
            # MODE-SCOPED idempotency: skip ONLY this mode's existing fill,
            # not the entire signal. Without this check, a redelivered
            # signal with paper already filled would still try to insert
            # a second paper row (IntegrityError on the composite UNIQUE
            # uq_fills_signal_id_mode from migration 0008).
            async with session_scope() as s:
                existing = (await s.execute(
                    select(Fill.id).where(
                        Fill.signal_id == sid,
                        Fill.mode == exec_mode,
                    )
                )).scalar()
            if existing is not None:
                log.info("executor_dedup_skip_mode",
                         signal=sid, mode=exec_mode, existing_fill=existing)
                continue

            try:
                await preflight(mode=exec_mode, market_id=market_id,
                                category=category, side=side, size_usdc=size_usdc,
                                score=score, outcome=outcome, price=price)
            except RiskRejection as rej:
                log.warning("risk_rejected", signal=sid, mode=exec_mode, reason=str(rej))
                async with session_scope() as s:
                    s.add(AuditLog(actor="executor", event="risk_rejected",
                                   payload={"signal_id": sid, "mode": exec_mode, "reason": str(rej)}))
                try:
                    await alerts.risk_rejected_alert(reason=f"{exec_mode}:{rej}", signal_id=sid)
                except Exception:  # noqa: BLE001
                    log.exception("alerts_risk_rejected_failed")
                continue

            if exec_mode == "paper":
                result = await simulate_fill(
                    signal_id=sid, market_id=market_id, outcome=outcome,
                    side=side, size_usdc=size_usdc,
                )
            else:  # live
                if not settings.can_sign:
                    # No signing credential — record a rejected Fill so the
                    # dashboard surfaces the gap. Paper still runs in parallel.
                    log.error("live_mode_no_creds", signal=sid)
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
                    results[exec_mode] = {"status": "rejected", "error": "live_mode_no_creds"}
                    continue
                result = await place_live(
                    signal_id=sid, market_id=market_id, outcome=outcome,
                    side=side, size_usdc=size_usdc,
                )

            results[exec_mode] = result if isinstance(result, dict) else {"status": "ok"}
            await publish("fill:new", {"signal_id": sid, "result": result, "mode": exec_mode})

            if isinstance(result, dict) and result.get("status") in ("filled", "submitted", "partial"):
                try:
                    await alerts.fill_alert(result)
                except Exception:  # noqa: BLE001
                    log.exception("alerts_fill_failed")
        except Exception as exc:  # noqa: BLE001
            # Isolate per-mode failures: one mode crashing should not
            # block the others or DLQ the whole signal.
            log.exception("executor_mode_failed", signal=sid, mode=exec_mode, err=str(exc))
            results[exec_mode] = {"status": "rejected", "error": f"{type(exc).__name__}:{exc}"}


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
                await xdlq(_STREAM, sig, f"{type(exc).__name__}:{exc}",
                           _msg_id=msg_id, _group=_GROUP)
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
        equity_guard_loop(),
        exit_loop(),
        rules_sweep_loop(),
        run_health_server(_BEACON, port=8081),
    )


if __name__ == "__main__":
    asyncio.run(main())
