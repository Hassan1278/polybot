"""Price/PnL + asset-level sharp-sentiment exits for the short-dated crypto book.

The cluster-dissolution exit (``exit_mirror``) is a confirmed no-op on hours-long
daily strike markets: no tracked sharp wallets trade those *exact* markets, so the
entry cluster is empty and it silently holds. This module adds exits that DO fit
that book:

  * **price/PnL** — per leg, on the live mark: take-profit, stop-loss, thesis
    invalidation (the market-implied win-prob has collapsed), and a near-expiry
    flatten. Independent of sharp wallets.
  * **asset-level sharp sentiment** — widen the smart-money premise from "same
    market" to "same asset": exit when our tracked *active* sharps are net AGAINST
    our direction on the underlying (quality-weighted), even though they aren't in
    our exact market.

Reuses ``exit_loop.do_close`` (venue-truth sizing, one close per (mode, market,
outcome) per cooldown, per-trigger live gating). Every evaluation emits an
``exit_rule_eval`` log, so — unlike exit_mirror — this is never silent. Price
exits close live by default; sentiment is shadow-first (logs only, no live close)
until validated via the logs.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from polybot.asset_direction import asset_of, direction
from polybot.clients import ClobClient
from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.market_resolver import token_for_outcome
from polybot.models import Fill, Market, Trade, Wallet, WalletStats
from polybot.runtime_config import merged_risk
from sqlalchemy import func, select

from services.executor.exit_loop import _HELD_STATUS, _held_outcomes, do_close

log = get_logger(__name__)

_DEFAULT_WEIGHT = 0.5            # quality weight when a wallet's win_rate is unknown


async def _rules_cfg() -> dict:
    """Effective exit_rules config (live-merged — same path as exit_mirror)."""
    return (await merged_risk("live")).get("exit_rules", {}) or {}


# ── pure decisions (DB-less; unit-tested in isolation) ───────────────────────

def price_exit_reason(*, avg_entry: float, mark: float,
                      hrs_to_expiry: float | None, cfg: dict) -> str | None:
    """Why to close this leg now, or None. ``mark`` is the market-implied P(win)
    (0-1); ``avg_entry`` the average fill price. Take-profit fires before the loss
    cuts, so a leg that has run is locked in rather than round-tripped."""
    if mark <= 0.0 or avg_entry <= 0.0:
        return None
    pnl = (mark - avg_entry) / avg_entry
    tp = cfg.get("take_profit_pct")
    tim = cfg.get("thesis_invalidation_mark")
    sl = cfg.get("stop_loss_pct")
    fbe = cfg.get("flatten_before_expiry_hours")
    if tp is not None and pnl >= float(tp):
        return "take_profit"
    if tim is not None and mark <= float(tim):
        return "thesis_invalidated"
    if sl is not None and pnl <= -float(sl):
        return "stop_loss"
    if fbe is not None and hrs_to_expiry is not None and hrs_to_expiry <= float(fbe):
        return "near_expiry"
    return None


def sentiment_breached(*, weighted_against: float, n_sharps: int,
                       stats_fresh: bool, cfg: dict) -> bool:
    """True if tracked sharps are net AGAINST our side enough to exit. Requires a
    minimum number of positioned sharps and fresh quality stats (no acting on a
    stale or thin signal — the guard the exit path otherwise lacks)."""
    if not stats_fresh:
        return False
    if n_sharps < int(cfg.get("sentiment_min_sharps", 5)):
        return False
    return weighted_against >= float(cfg.get("sentiment_against_threshold", 0.65))


# ── data assembly ────────────────────────────────────────────────────────────

async def _position_view(s, clob, market_id: str, outcome: str) -> dict | None:
    """A held LIVE leg marked at the current CLOB price, or None if we don't hold
    it / can't price it. Mirrors scripts/btc_position_analysis.py's leg model."""
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
        select(Market.question, Market.slug, Market.end_date,
               Market.yes_token_id, Market.no_token_id, Market.outcomes).where(
            Market.market_id == market_id))).first()
    if not mrow:
        return None
    q, slug, end, yes_t, no_t, outs = mrow
    mkt = SimpleNamespace(yes_token_id=yes_t, no_token_id=no_t, outcomes=outs)
    token = token_for_outcome(mkt, outcome)
    mark = float((await clob.best_mark(token)) or 0.0) if token else 0.0
    hrs = None
    if end is not None:
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        hrs = (end - datetime.now(tz=timezone.utc)).total_seconds() / 3600.0
    return {
        "shares": shares, "notional": notional,
        "avg_entry": (notional / shares) if shares else 0.0,
        "mark": mark,
        "asset": asset_of(q, slug),
        "my_dir": direction(q, slug, outcome, "BUY"),
        "hrs": hrs,
    }


async def _weights_fresh(s, wallets: list[str], cfg: dict) -> tuple[dict[str, float], bool]:
    """(win_rate weights, stats_fresh). ``stats_fresh`` is False when the newest
    WalletStats row in the window is older than sentiment_max_stats_age_hours."""
    window = str(cfg.get("quality_window", "30d"))
    max_age_h = float(cfg.get("sentiment_max_stats_age_hours", 36))
    lw = [w.lower() for w in wallets]
    rows = (await s.execute(
        select(WalletStats.address, WalletStats.win_rate, WalletStats.computed_at).where(
            func.lower(WalletStats.address).in_(lw),
            WalletStats.window == window))).all()
    wr = {str(a).lower(): w for a, w, _ in rows}
    newest = max((c for _, _, c in rows if c is not None), default=None)
    fresh = False
    if newest is not None:
        if newest.tzinfo is None:
            newest = newest.replace(tzinfo=timezone.utc)
        fresh = (datetime.now(tz=timezone.utc) - newest) <= timedelta(hours=max_age_h)
    wmap = {w: (float(wr[w]) if wr.get(w) is not None else _DEFAULT_WEIGHT) for w in lw}
    return wmap, fresh


async def _asset_sharp_sentiment(s, asset: str, my_dir: str, cfg: dict
                                 ) -> tuple[float, int, bool]:
    """(weighted_against, n_sharps, stats_fresh) for ``asset`` vs our ``my_dir``.

    Net each active sharp's signed notional (BUY/SELL + bull/bear → signed bull
    exposure) across the asset's crypto markets over the lookback, weight by
    win_rate, and return the quality-weighted fraction positioned AGAINST us."""
    lookback_h = float(cfg.get("sentiment_lookback_hours", 24))
    since = datetime.now(tz=timezone.utc) - timedelta(hours=lookback_h)
    rows = (await s.execute(
        select(Trade.wallet, Trade.outcome, Trade.side,
               func.sum(Trade.notional_usdc), Market.question, Market.slug)
        .join(Wallet, func.lower(Wallet.address) == func.lower(Trade.wallet))
        .join(Market, Market.market_id == Trade.market_id)
        .where(Wallet.is_active.is_(True), Trade.ts >= since,
               Market.category == "crypto")
        .group_by(Trade.wallet, Trade.market_id, Trade.outcome, Trade.side,
                  Market.question, Market.slug))).all()

    # Net signed BULL exposure per wallet on this asset (>0 net-bull, <0 net-bear).
    bull_by_wallet: dict[str, float] = {}
    for wallet, outcome, side, notional, q, slug in rows:
        if asset_of(q, slug) != asset:
            continue
        d = direction(q, slug, outcome, side)      # already applies the SELL flip
        if d is None:
            continue
        signed = float(notional or 0.0) * (1.0 if d == "bull" else -1.0)
        w = str(wallet).lower()
        bull_by_wallet[w] = bull_by_wallet.get(w, 0.0) + signed

    stances = {w: v for w, v in bull_by_wallet.items() if abs(v) > 1e-9}
    if not stances:
        return 0.0, 0, True
    wmap, stats_fresh = await _weights_fresh(s, list(stances), cfg)
    against_dir = "bull" if my_dir == "bear" else "bear"
    total = against = 0.0
    for w, v in stances.items():
        weight = wmap.get(w, _DEFAULT_WEIGHT)
        if weight <= 0:
            continue
        total += weight
        if ("bull" if v > 0 else "bear") == against_dir:
            against += weight
    frac = (against / total) if total > 0 else 0.0
    return frac, len(stances), stats_fresh


# ── evaluation + sweep ───────────────────────────────────────────────────────

async def _evaluate_rules(market_id: str, outcome: str, clob, *,
                          cfg: dict, sentiment_cache: dict) -> None:
    """Evaluate price + sentiment exits for one held LIVE leg. Always logs
    ``exit_rule_eval`` (visibility); closes via the shared do_close on a trigger."""
    async with session_scope() as s:
        view = await _position_view(s, clob, market_id, outcome)
        if view is None:
            return
        if view["notional"] < float(cfg.get("min_close_notional_usdc", 2.0)):
            return                                  # don't churn dust legs (fees)
        asset, my_dir = view["asset"], view["my_dir"]
        sent_against, sent_n, sent_fresh = 0.0, 0, True
        if asset and my_dir:                        # range/ambiguous legs → price-only
            ck = (asset, my_dir)
            if ck not in sentiment_cache:           # one query per asset+dir per sweep
                sentiment_cache[ck] = await _asset_sharp_sentiment(s, asset, my_dir, cfg)
            sent_against, sent_n, sent_fresh = sentiment_cache[ck]

    reason = price_exit_reason(avg_entry=view["avg_entry"], mark=view["mark"],
                               hrs_to_expiry=view["hrs"], cfg=cfg)
    sent_breach = bool(asset and my_dir) and sentiment_breached(
        weighted_against=sent_against, n_sharps=sent_n, stats_fresh=sent_fresh, cfg=cfg)
    pnl = ((view["mark"] - view["avg_entry"]) / view["avg_entry"]) if view["avg_entry"] else None

    log.info("exit_rule_eval", market=market_id, outcome=outcome, asset=asset,
             my_dir=my_dir, mark=round(view["mark"], 4),
             avg_entry=round(view["avg_entry"], 4),
             pnl=(round(pnl, 4) if pnl is not None else None),
             hrs=(round(view["hrs"], 2) if view["hrs"] is not None else None),
             sentiment_against=round(sent_against, 3), n_sharps=sent_n,
             price_reason=reason, sentiment_breach=sent_breach)

    cooldown = int(cfg.get("cooldown_seconds", 300))
    if reason is not None:
        log.info("exit_price_trigger", market=market_id, outcome=outcome, reason=reason,
                 mark=round(view["mark"], 4), avg_entry=round(view["avg_entry"], 4),
                 pnl=(round(pnl, 4) if pnl is not None else None))
        await do_close(market_id, outcome, notes=f"exit_{reason}",
                       live_ok=bool(cfg.get("price_live_enabled", True)),
                       cooldown=cooldown, skip_event="exit_rule_skip_live")
        return
    if sent_breach:
        log.info("exit_sentiment_trigger", market=market_id, outcome=outcome,
                 asset=asset, against=round(sent_against, 3), n_sharps=sent_n, my_dir=my_dir)
        await do_close(market_id, outcome, notes="exit_sentiment",
                       live_ok=bool(cfg.get("sentiment_live_enabled", False)),
                       cooldown=cooldown, skip_event="exit_rule_skip_live")


async def _sweep_rules(cfg: dict) -> None:
    async with session_scope() as s:
        pairs = await _held_outcomes(s, "live")     # price/sentiment target the live book
    pairs = sorted(set(pairs))
    if not pairs:
        return
    clob = ClobClient()
    sentiment_cache: dict = {}
    try:
        for mid, oc in pairs:
            try:
                await _evaluate_rules(mid, oc, clob, cfg=cfg, sentiment_cache=sentiment_cache)
            except Exception:  # noqa: BLE001
                log.exception("exit_rule_eval_failed", market=mid, outcome=oc)
    finally:
        await clob.close()


async def rules_sweep_loop() -> None:
    """Entry point (added to the executor's main gather). Periodically evaluates
    the price/sentiment exits at its own cadence (faster than exit_mirror's sweep —
    short-dated marks move fast). No-ops while exit_rules.enabled is false."""
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
