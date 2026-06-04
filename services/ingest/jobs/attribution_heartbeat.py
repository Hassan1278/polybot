"""Zero-attribution heartbeat — silent-failure detector.

The whole pipeline depends on `data-api.polymarket.com/trades` returning
rows with a `proxyWallet` field. If Polymarket renames that field (or
splits maker/taker into separate rows, which they've done elsewhere), our
attribution silently drops to zero — we keep polling, the WS keeps
subscribing, the dashboard shows 0 trades/min, and nobody notices for
hours.

This loop is the canary:

- Every CHECK_INTERVAL_S seconds (default 300 = 5 min)
- Count trades in the last LOOKBACK_MIN minutes (default 30)
- If 0 AND ≥ MIN_WALLETS_REQUIRED active wallets exist, the heartbeat
  failed → fire ONE critical alert
- Track state in Redis so we don't re-alert until attribution recovers
  (edge-triggered, not level-triggered)

We do NOT fire if there are no active wallets to attribute against —
that's an unrelated problem (run discover_wallets) and would just be
noise from this loop.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from polybot import alerts
from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.models import Trade, Wallet
from polybot.redis_bus import client as redis_client

log = get_logger(__name__)

CHECK_INTERVAL_S = 300          # how often we poke
LOOKBACK_MIN = 30               # window we expect to see ≥1 attributed trade in
MIN_WALLETS_REQUIRED = 10       # don't alert when we have almost no wallets

# Redis key: presence == "we are currently in a no-attribution window".
# Without this we'd re-alert every CHECK_INTERVAL_S seconds — annoying.
# Edge-trigger: set when we first detect 0 trades, clear when we see ≥1.
_DOWN_KEY = "polybot:attribution:zero_since"
_DOWN_TTL_S = 24 * 3600         # safety: state expires after a day


async def _count_recent_trades(minutes: int) -> int:
    cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=minutes)
    async with session_scope() as s:
        return int(
            (await s.execute(
                select(func.count(Trade.id)).where(Trade.ts >= cutoff)
            )).scalar_one()
        )


async def _count_active_wallets() -> int:
    async with session_scope() as s:
        return int(
            (await s.execute(
                select(func.count(Wallet.address)).where(Wallet.is_active.is_(True))
            )).scalar_one()
        )


async def _check_once() -> None:
    n_wallets = await _count_active_wallets()
    n_trades = await _count_recent_trades(LOOKBACK_MIN)
    rds = redis_client()
    log.info(
        "attribution_heartbeat",
        active_wallets=n_wallets,
        trades_last_min=n_trades,
        window_min=LOOKBACK_MIN,
    )

    if n_wallets < MIN_WALLETS_REQUIRED:
        # Not enough wallets to make this signal meaningful. Reset state so the
        # next time we DO have wallets we don't double-alert from stale state.
        await rds.delete(_DOWN_KEY)
        return

    if n_trades > 0:
        # Healthy. If we were previously in a down state, fire a recovery info
        # alert and clear the flag.
        was_down = await rds.delete(_DOWN_KEY)
        if was_down:
            await alerts.notify(
                "info",
                "Attribution recovered",
                f"trades_last_{LOOKBACK_MIN}m={n_trades} wallets={n_wallets}",
                tags={"event": "attribution_recovered"},
            )
        return

    # n_trades == 0 — broken. Edge-trigger: only alert once until recovery.
    first_down = await rds.set(
        _DOWN_KEY, datetime.now(tz=timezone.utc).isoformat(),
        nx=True, ex=_DOWN_TTL_S,
    )
    if not first_down:
        # Already alerted; don't spam.
        return

    await alerts.notify(
        "critical",
        "Attribution dropped to zero",
        f"No trades in the last {LOOKBACK_MIN} min across {n_wallets} tracked "
        f"wallets. The /data-api/trades poll is probably broken — check the "
        f"`proxyWallet` field shape, ingest service logs, and the rate-limit "
        f"counter. Pipeline-health endpoint should show trades_per_min_15m=0.",
        tags={
            "event": "attribution_zero",
            "wallets": str(n_wallets),
            "window_min": str(LOOKBACK_MIN),
        },
    )


async def run_attribution_heartbeat() -> None:
    """Single-shot wrapper so the ingest scheduler can call us via `_every`."""
    await _check_once()
