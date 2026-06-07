"""Listens to `trade:new`, builds a rolling window per category and
periodically clusters → produces candidates → engine.process_candidate.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import and_, select

from polybot.config import settings
from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.models import Trade, Wallet
from polybot.redis_bus import publish, subscribe
from polybot.stats import cluster_active_wallets
from services.signals.engine import process_candidate

log = get_logger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("correlation_env_parse_error", var=name, value=raw, default=default)
        return default


async def _recent_trades_df(minutes: int) -> pd.DataFrame:
    cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=minutes)
    async with session_scope() as s:
        rows = (await s.execute(
            select(Trade.ts, Trade.wallet, Trade.market_id, Trade.outcome,
                   Trade.side, Trade.size_shares, Trade.price, Trade.notional_usdc)
            .join(Wallet, Wallet.address == Trade.wallet)
            .where(and_(Wallet.is_active.is_(True), Trade.ts >= cutoff))
        )).all()
    return pd.DataFrame(rows, columns=[
        "ts", "wallet", "market_id", "outcome", "side", "size_shares", "price", "notional_usdc",
    ])


async def correlation_loop(beacon=None) -> None:
    # `beacon` is an optional polybot.health_server.HealthBeacon. When
    # passed, the loop bumps it on every iteration so the /health endpoint
    # can report liveness even when there are zero trades to process.
    # Time-decay / scoring knobs — read straight from env so we don't have to
    # bloat Settings for tuning experiments. Defaults match polybot.stats.
    half_life_seconds = _env_float("CORRELATION_HALF_LIFE_SECONDS", 300.0)
    k_wallets = _env_float("CORRELATION_K_WALLETS", 2.5)
    k_notional = _env_float("CORRELATION_K_NOTIONAL", 2000.0)

    # Debounce: short when things are happening, long when they're not.
    debounce_busy_s = _env_float("CORRELATION_DEBOUNCE_BUSY_S", 5.0)
    debounce_idle_s = _env_float("CORRELATION_DEBOUNCE_IDLE_S", 30.0)
    heartbeat_interval_s = _env_float("CORRELATION_HEARTBEAT_S", 60.0)

    log.info(
        "correlation_loop_starting",
        window_min=settings.correlation_window_minutes,
        min_wallets=settings.correlation_min_wallets,
        half_life_seconds=half_life_seconds,
        k_wallets=k_wallets,
        k_notional=k_notional,
        debounce_busy_s=debounce_busy_s,
        debounce_idle_s=debounce_idle_s,
        heartbeat_interval_s=heartbeat_interval_s,
    )

    # Trigger a recompute on every "trade:new" message — but debounce: at most
    # one pass per `min_interval` seconds; back off when the last pass found
    # nothing.
    min_interval = debounce_busy_s
    last = 0.0
    pending = asyncio.Event()

    async def listener() -> None:
        async for _ in subscribe("trade:new"):
            pending.set()

    asyncio.create_task(listener())

    # Heartbeat bookkeeping — covers a rolling heartbeat_interval_s window.
    hb_last = asyncio.get_event_loop().time()
    hb_trades_seen = 0
    hb_candidates_found = 0
    hb_passes = 0

    while True:
        await pending.wait()
        pending.clear()
        now = asyncio.get_event_loop().time()
        if now - last < min_interval:
            await asyncio.sleep(min_interval - (now - last))
        last = asyncio.get_event_loop().time()
        if beacon is not None:
            beacon.heartbeat()

        df = await _recent_trades_df(settings.correlation_window_minutes)
        trade_count = 0 if df.empty else len(df)
        hb_trades_seen += trade_count
        hb_passes += 1

        cands: list[dict] = []
        if not df.empty:
            cands = cluster_active_wallets(
                df,
                window_minutes=settings.correlation_window_minutes,
                min_wallets=settings.correlation_min_wallets,
                half_life_seconds=half_life_seconds,
                k_wallets=k_wallets,
                k_notional=k_notional,
            )

        n_cands = len(cands)
        hb_candidates_found += n_cands

        # Smart debounce: stay snappy while there's signal, back off when idle.
        min_interval = debounce_busy_s if n_cands > 0 else debounce_idle_s

        if n_cands:
            log.info("correlation_candidates", n=n_cands)
            for c in cands:
                # Publish raw cluster so the dashboard can observe candidates
                # independent of whether the gate eventually passes them.
                try:
                    await publish("candidate:new", c)
                except Exception:
                    log.exception("correlation_publish_failed",
                                  market_id=c.get("market_id"))
                # Per-candidate try/except: one bad market must not nuke the loop.
                try:
                    await process_candidate(c)
                except Exception:
                    log.exception("correlation_process_candidate_failed",
                                  market_id=c.get("market_id"))

        # Heartbeat: emit at least every `heartbeat_interval_s`, regardless of
        # activity. Critical for ops to know the loop is alive.
        hb_now = asyncio.get_event_loop().time()
        if hb_now - hb_last >= heartbeat_interval_s:
            log.info(
                "correlation_heartbeat",
                interval_s=round(hb_now - hb_last, 2),
                passes=hb_passes,
                trades_seen=hb_trades_seen,
                candidates_found=hb_candidates_found,
                next_debounce_s=min_interval,
                window_min=settings.correlation_window_minutes,
            )
            hb_last = hb_now
            hb_trades_seen = 0
            hb_candidates_found = 0
            hb_passes = 0
