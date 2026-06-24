"""Live equity-drawdown circuit breaker.

The realised-PnL daily-loss check in ``risk.preflight`` (``max_daily_loss_usdc``)
is effectively INERT in live mode: the live path writes only Fill rows and never
reconciles realised PnL onto Position rows, so the brake that's meant to halt a
bleeding day can't actually see the loss. (This is the gap that let a $380->$190
day run unchecked.)

This loop closes it. Every minute, when live mode is enabled, it reads the
wallet's REAL equity — pUSD cash + marked value of open positions, the same
sources as the ``/live/account`` dashboard card — snapshots day-start equity in
Redis, and trips the kill switch when intraday drawdown exceeds
``drawdown.max_daily_drawdown_pct``.

Fail-SAFE by construction:
  - Runs only when "live" is in the enabled mode set (no real money, no guard).
  - NEVER trips on a failed/missing equity read — a flaky data API or sleeping
    sidecar must not be able to halt trading by itself, so a bad read just skips
    the tick.
  - Trips the kill switch at most once per breach (no alert spam). The halt then
    AUTO-CLEARS once equity recovers to within ``drawdown.resume_drawdown_pct`` of
    the day's open (hysteresis vs the trip line); set that key null/0 to keep the
    old manual-clear behavior. Only the breaker's OWN kills auto-clear — a manual
    or other halt is left untouched.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from polybot import alerts
from polybot.clients import ClobClient, DataClient
from polybot.config import settings
from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.models import WalletCredential
from polybot.redis_bus import client as redis_client
from polybot.redis_bus import kill_clear, kill_set, kill_status
from polybot.runtime_config import enabled_modes, merged_risk

log = get_logger(__name__)

_CHECK_INTERVAL_S = 60
_BASELINE_KEY = "polybot:equity:day_start:{day}"
_BASELINE_TTL = 2 * 24 * 3600          # > one trading day, auto-expires


def drawdown_breached(baseline: float, equity: float, pct: float) -> bool:
    """True if equity has fallen at least ``pct`` (fraction) below ``baseline``.
    A non-positive baseline can't define a drawdown, so it never breaches."""
    if baseline <= 0:
        return False
    return (baseline - equity) / baseline >= pct


def drawdown_recovered(baseline: float, equity: float, resume_pct: float | None) -> bool:
    """True if equity has recovered to within ``resume_pct`` of ``baseline`` (or
    risen above it). Never True when resume_pct is unset/<=0 or the baseline can't
    define a drawdown — so auto-resume is off unless explicitly configured."""
    if not resume_pct or resume_pct <= 0 or baseline <= 0:
        return False
    return (baseline - equity) / baseline <= resume_pct


def breaker_action(current_kill: str | None, *, breached: bool, recovered: bool) -> str:
    """Pure per-tick decision: ``'resume'`` | ``'trip'`` | ``'noop'``.

    Only the breaker's OWN kill (reason prefixed ``equity_drawdown:``) auto-clears,
    and only once equity has ``recovered``; a foreign/manual halt is never touched,
    and we never trip on top of an existing halt."""
    ours = bool(current_kill) and str(current_kill).startswith("equity_drawdown:")
    if ours:
        return "resume" if recovered else "noop"
    if current_kill:
        return "noop"
    return "trip" if breached else "noop"


def _resume_pct(dd_cfg: dict) -> float | None:
    """The configured auto-resume threshold (fraction), or None when disabled."""
    raw = dd_cfg.get("resume_drawdown_pct")
    if raw in (None, "", 0, 0.0):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _trading_day(reset_hour: int) -> str:
    """UTC calendar day, shifted by the configured reset hour, as an ISO date."""
    now = datetime.now(tz=timezone.utc)
    return (now - timedelta(hours=reset_hour)).date().isoformat()


async def _deposit_wallet() -> str | None:
    """Same resolution as services/api/routes/live._deposit_wallet, but with its
    own session (this runs outside a request)."""
    env_funder = (settings.polymarket_funder_address or "").strip()
    if env_funder:
        return env_funder
    async with session_scope() as s:
        row = (await s.execute(
            select(WalletCredential)
            .where(WalletCredential.is_active.is_(True))
            .order_by(WalletCredential.id.desc())
            .limit(1)
        )).scalar_one_or_none()
    if row:
        return row.funder_address or row.address
    return None


async def _current_equity(address: str) -> float | None:
    """Real equity = pUSD cash + marked value of open positions. Returns None if
    the CASH balance can't be read — we refuse to compute a misleading equity
    from positions alone (that would understate equity and risk a false trip)."""
    c_clob = ClobClient()
    c_data = DataClient()
    try:
        try:
            async with asyncio.timeout(12.0):
                bal = await c_clob.balance()
        except Exception as exc:  # noqa: BLE001
            log.warning("equity_guard_balance_failed", err=str(exc))
            return None
        if not (isinstance(bal, dict) and bal.get("ok") and bal.get("balance") is not None):
            log.warning("equity_guard_balance_unavailable", resp=str(bal)[:140])
            return None
        pusd = _f(bal.get("balance"))
        try:
            async with asyncio.timeout(12.0):
                rows = await c_data.positions(address, limit=500, size_threshold=0.0)
        except Exception as exc:  # noqa: BLE001
            log.warning("equity_guard_positions_failed", err=str(exc))
            return None
        pos_value = sum(_f(p.get("currentValue"))
                        for p in (rows or []) if isinstance(p, dict))
        return pusd + pos_value
    finally:
        await c_clob.close()
        await c_data.close()


async def _tick() -> None:
    # Only protect REAL money — skip entirely unless live mode is enabled.
    if "live" not in await enabled_modes():
        return
    dd_cfg = (await merged_risk("live")).get("drawdown", {})
    raw_pct = dd_cfg.get("max_daily_drawdown_pct")
    if not raw_pct or float(raw_pct) <= 0:
        return                                       # breaker disabled
    pct = float(raw_pct)
    reset_hour = int(dd_cfg.get("daily_reset_utc_hour", 0))

    address = await _deposit_wallet()
    if not address:
        return                                       # no live wallet → nothing to guard

    equity = await _current_equity(address)
    if equity is None or equity <= 0:
        return                                       # fail-safe: never act on a bad read

    r = redis_client()
    key = _BASELINE_KEY.format(day=_trading_day(reset_hour))
    baseline_raw = await r.get(key)
    if baseline_raw is None:
        # First read of the trading day — establish the baseline, no drawdown yet.
        await r.set(key, f"{equity:.6f}", ex=_BASELINE_TTL)
        log.info("equity_guard_baseline_set", day_key=key, baseline=round(equity, 2))
        return
    baseline = _f(baseline_raw)
    if baseline <= 0:
        return                                       # can't define a drawdown
    drawdown = (baseline - equity) / baseline
    current_kill = await kill_status()
    action = breaker_action(
        current_kill,
        breached=drawdown_breached(baseline, equity, pct),
        recovered=drawdown_recovered(baseline, equity, _resume_pct(dd_cfg)),
    )

    if action == "resume":
        # Our own halt + equity recovered to within resume_drawdown_pct of the
        # day's open → lift it and let trading continue. The gap to the trip line
        # (resume_drawdown_pct < max_daily_drawdown_pct) is the anti-flap margin.
        await kill_clear(by="equity_guard_auto")
        log.info("equity_guard_auto_resume", drawdown=round(drawdown, 4),
                 baseline=round(baseline, 2), now=round(equity, 2))
        try:
            await alerts.notify(
                "warning",
                "Equity drawdown breaker auto-cleared — trading resumed",
                f"Real equity recovered to {drawdown * 100:.1f}% below today's open "
                f"(${baseline:.2f} -> ${equity:.2f}); kill switch lifted automatically.",
            )
        except Exception:  # noqa: BLE001
            log.exception("equity_guard_resume_alert_failed")
        return

    if action != "trip":
        return                                       # holding below the line, or a foreign halt

    # Breach, and nothing else has halted us → trip the kill switch ONCE.
    reason = (f"equity_drawdown:{drawdown * 100:.1f}%>={pct * 100:.0f}%:"
              f"baseline={baseline:.2f}:now={equity:.2f}")
    log.error("equity_guard_breach", reason=reason)
    await kill_set(reason, actor="equity_guard")
    try:
        await alerts.notify(
            "critical",
            "Equity drawdown breaker tripped — trading halted",
            f"Real equity fell {drawdown * 100:.1f}% today "
            f"(${baseline:.2f} -> ${equity:.2f}); kill switch is ON. It will "
            f"auto-clear once equity recovers, or clear it manually.",
        )
    except Exception:  # noqa: BLE001
        log.exception("equity_guard_alert_failed")


async def equity_guard_loop() -> None:
    log.info("equity_guard_starting", interval_s=_CHECK_INTERVAL_S)
    while True:
        try:
            await _tick()
        except Exception:  # noqa: BLE001
            log.exception("equity_guard_tick_failed")
        await asyncio.sleep(_CHECK_INTERVAL_S)
