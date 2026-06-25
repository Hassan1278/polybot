"""Stop-loss-only exit engine for the LIVE book.

Operator model (replaces the old TP / thesis / near-expiry / sharp-sentiment
exits — positions are NOT exited early anymore; they ride to resolution unless a
stop is hit):

  * base stop-loss at ``stop_loss_level`` (0.20): close if the mark falls to/below it.
  * a leg ENTERED below ``low_entry_threshold`` (0.20) gets a looser stop,
    ``low_entry_stop`` (0.05) — a cheap longshot needs room before it's a "loss".
  * once a leg's mark EVER exceeds ``profit_lock_trigger`` (0.75), the stop
    ratchets up to ``profit_lock_stop`` (0.43) — no matter the entry — to protect
    a winner. The "ever exceeded" high-water mark is persisted in Redis so a later
    pullback below 0.75 still uses the 0.43 stop.

There is NO take-profit: a leg above the stop is held. The only exit is the stop.
Reuses ``exit_loop.do_close`` (venue-truth sizing, one close per (mode, market,
outcome) per cooldown). Every evaluation emits an ``exit_rule_eval`` log.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from polybot.clients import ClobClient
from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.market_resolver import token_for_outcome
from polybot.models import Fill, Market
from polybot.redis_bus import client as redis_client
from polybot.runtime_config import merged_risk
from sqlalchemy import func, select

from services.executor.exit_loop import _HELD_STATUS, _held_outcomes, do_close

log = get_logger(__name__)

_HWM_KEY = "polybot:exit:hwm75:{mid}:{oc}"      # "leg ever traded above the lock trigger"


async def _rules_cfg() -> dict:
    """Effective exit_rules config (live-merged — same path as exit_mirror)."""
    return (await merged_risk("live")).get("exit_rules", {}) or {}


# ── pure decisions (DB-less; unit-tested in isolation) ───────────────────────

def stop_loss_level(*, avg_entry: float, hit_high_water: bool, cfg: dict) -> float:
    """The stop price for a leg right now (the mark at/below which we close).

    Precedence: a leg that has EVER hit the profit-lock trigger uses the
    profit-lock stop (regardless of entry); else a sub-threshold *entry* uses the
    looser low-entry stop; else the base stop."""
    if hit_high_water:
        return float(cfg.get("profit_lock_stop", 0.43))
    if avg_entry < float(cfg.get("low_entry_threshold", 0.20)):
        return float(cfg.get("low_entry_stop", 0.05))
    return float(cfg.get("stop_loss_level", 0.20))


def stop_exit_reason(*, avg_entry: float, mark: float,
                     hit_high_water: bool, cfg: dict) -> str | None:
    """``'profit_lock'`` / ``'stop_loss'`` / None. The ONLY exit trigger — there is
    no take-profit, so a leg above its stop is held to resolution. ``mark`` is the
    market-implied P(win) (0-1)."""
    if mark <= 0.0 or avg_entry <= 0.0:
        return None
    stop = stop_loss_level(avg_entry=avg_entry, hit_high_water=hit_high_water, cfg=cfg)
    if mark <= stop:
        return "profit_lock" if hit_high_water else "stop_loss"
    return None


# ── data assembly ────────────────────────────────────────────────────────────

async def _position_view(s, clob, market_id: str, outcome: str) -> dict | None:
    """A held LIVE leg marked at the current CLOB price, or None if we don't hold
    it / can't price it. Sums live BUY fills for the average entry."""
    frow = (await s.execute(
        select(func.sum(Fill.size_shares), func.sum(Fill.notional_usdc)).where(
            Fill.mode == "live", Fill.side == "BUY",
            Fill.status.in_(_HELD_STATUS), Fill.market_id == market_id,
            func.upper(Fill.outcome) == outcome.upper(),
        ))).first()
    shares = float((frow[0] if frow else 0.0) or 0.0)
    notional = float((frow[1] if frow else 0.0) or 0.0)
    if shares <= 0.0:
        return None
    mrow = (await s.execute(
        select(Market.yes_token_id, Market.no_token_id, Market.outcomes).where(
            Market.market_id == market_id))).first()
    if not mrow:
        return None
    yes_t, no_t, outs = mrow
    mkt = SimpleNamespace(yes_token_id=yes_t, no_token_id=no_t, outcomes=outs)
    token = token_for_outcome(mkt, outcome)
    mark = float((await clob.best_mark(token)) or 0.0) if token else 0.0
    return {
        "shares": shares, "notional": notional,
        "avg_entry": (notional / shares) if shares else 0.0,
        "mark": mark,
    }


# ── evaluation + sweep ───────────────────────────────────────────────────────

async def _evaluate_rules(market_id: str, outcome: str, clob, *, cfg: dict) -> None:
    """Evaluate the stop-loss for one held LIVE leg. Always logs ``exit_rule_eval``
    (visibility); closes via the shared do_close when the stop is hit."""
    async with session_scope() as s:
        view = await _position_view(s, clob, market_id, outcome)
    if view is None:
        return
    if view["notional"] < float(cfg.get("min_close_notional_usdc", 2.0)):
        return                                       # don't churn dust legs (fees)

    avg_entry, mark = view["avg_entry"], view["mark"]

    # High-water mark: has this leg EVER traded above the profit-lock trigger?
    # Persisted so a later pullback below the trigger still uses the ratcheted stop.
    r = redis_client()
    key = _HWM_KEY.format(mid=market_id, oc=outcome.upper())
    trigger = float(cfg.get("profit_lock_trigger", 0.75))
    hit_hwm = bool(await r.get(key))
    if not hit_hwm and mark > trigger:
        await r.set(key, "1", ex=int(cfg.get("hwm_ttl_seconds", 14 * 24 * 3600)))
        hit_hwm = True
        log.info("exit_profit_lock_armed", market=market_id, outcome=outcome,
                 mark=round(mark, 4), new_stop=float(cfg.get("profit_lock_stop", 0.43)))

    stop = stop_loss_level(avg_entry=avg_entry, hit_high_water=hit_hwm, cfg=cfg)
    reason = stop_exit_reason(avg_entry=avg_entry, mark=mark, hit_high_water=hit_hwm, cfg=cfg)
    pnl = ((mark - avg_entry) / avg_entry) if avg_entry else None

    log.info("exit_rule_eval", market=market_id, outcome=outcome,
             mark=round(mark, 4), avg_entry=round(avg_entry, 4), stop=round(stop, 4),
             hwm75=hit_hwm, pnl=(round(pnl, 4) if pnl is not None else None),
             reason=reason)

    if reason is not None:
        log.info("exit_stop_trigger", market=market_id, outcome=outcome, reason=reason,
                 mark=round(mark, 4), stop=round(stop, 4), avg_entry=round(avg_entry, 4))
        await do_close(market_id, outcome, notes=f"exit_{reason}",
                       live_ok=bool(cfg.get("live_enabled", True)),
                       cooldown=int(cfg.get("cooldown_seconds", 300)),
                       skip_event="exit_rule_skip_live")


async def _sweep_rules(cfg: dict) -> None:
    async with session_scope() as s:
        pairs = await _held_outcomes(s, "live")     # stop-loss targets the whole live book
    pairs = sorted(set(pairs))
    if not pairs:
        return
    clob = ClobClient()
    try:
        for mid, oc in pairs:
            try:
                await _evaluate_rules(mid, oc, clob, cfg=cfg)
            except Exception:  # noqa: BLE001
                log.exception("exit_rule_eval_failed", market=mid, outcome=oc)
    finally:
        await clob.close()


async def rules_sweep_loop() -> None:
    """Entry point (in the executor's main gather). Periodically evaluates the
    stop-loss over every held live leg. No-ops while exit_rules.enabled is false."""
    log.info("exit_rules_loop_starting")
    while True:
        interval = 60
        try:
            cfg = await _rules_cfg()
            interval = int(cfg.get("sweep_seconds", 60))
            if cfg.get("enabled", True):
                await _sweep_rules(cfg)
        except Exception:  # noqa: BLE001
            log.exception("exit_rules_sweep_failed")
        await asyncio.sleep(max(15, interval))
