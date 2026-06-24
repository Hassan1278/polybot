"""Exit-mirror detection (Stage 3): follow smart money OUT.

When the wallets whose cluster justified one of our open positions start to
reverse (net-sell the outcome), we (a) flag the thesis as dissolving so the entry
path stops ADDING to it, and (b) close our position. The decision is the
quality-weighted "would I still enter this now?" test: if the support remaining
from the original entry cluster falls below a threshold, the thesis is gone.

Event-driven off `trade:new` (a tracked-wallet SELL on a market we hold), with a
periodic backstop sweep because pub/sub is lossy. Per-mode and idempotent: a
Redis NX marker collapses a burst of cluster-sells (and the event/sweep race)
into ONE close per (mode, market, outcome). Live closes are gated by
exit_mirror.live_enabled (default False) — paper exits shadow-run for validation.
The loop is resilient (restart-on-crash) and lives OFF the signal stream, so it
can never DLQ entry signals.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select

from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.models import Fill, Position, Signal, Trade, Wallet, WalletStats
from polybot.redis_bus import THESIS_DISSOLVING_KEY
from polybot.redis_bus import client as redis_client
from polybot.redis_bus import subscribe
from polybot.runtime_config import enabled_modes, merged_risk
from services.executor.close import close_live, close_paper

log = get_logger(__name__)

_PENDING_KEY = "polybot:exit:pending:{mode}:{mid}:{oc}"
_HELD_STATUS = ("filled", "submitted", "partial")
_NET_LOOKBACK_DAYS = 14          # how far back to net cluster trades
_DEFAULT_WEIGHT = 0.5            # quality weight when a wallet's win_rate is unknown


def weighted_support_remaining(weights: list[float], sold: list[bool]) -> float:
    """Fraction of the entry cluster's (quality-weighted) support still LONG.

    1.0 = nobody sold, 0.0 = everyone sold. With no usable weight (empty cluster
    or all weights <= 0) returns 1.0 — no signal means "don't exit"."""
    total = sum(w for w in weights if w and w > 0)
    if total <= 0:
        return 1.0
    remaining = sum(w for w, s in zip(weights, sold) if (w and w > 0) and not s)
    return remaining / total


async def _exit_cfg() -> dict:
    """Effective exit_mirror config (live-merged — that's where live_enabled lives)."""
    return (await merged_risk("live")).get("exit_mirror", {}) or {}


# ── holdings enumeration ─────────────────────────────────────────────────────

async def _held_outcomes(s, mode: str) -> list[tuple[str, str]]:
    if mode == "live":
        rows = (await s.execute(
            select(Fill.market_id, Fill.outcome).where(
                Fill.mode == "live", Fill.side == "BUY",
                Fill.status.in_(_HELD_STATUS),
            ).group_by(Fill.market_id, Fill.outcome))).all()
    else:
        rows = (await s.execute(
            select(Position.market_id, Position.outcome).where(
                Position.size_shares > 0,
            ).group_by(Position.market_id, Position.outcome))).all()
    return [(m, o) for m, o in rows if m and o]


async def _held_outcomes_for_market(s, market_id: str) -> set[str]:
    """Outcomes we hold on this market in EITHER mode (so a SELL event on the
    market wakes evaluation of every outcome we're exposed to)."""
    out: set[str] = set()
    live = (await s.execute(
        select(Fill.outcome).where(
            Fill.mode == "live", Fill.market_id == market_id, Fill.side == "BUY",
            Fill.status.in_(_HELD_STATUS)).group_by(Fill.outcome))).all()
    paper = (await s.execute(
        select(Position.outcome).where(
            Position.market_id == market_id, Position.size_shares > 0,
        ).group_by(Position.outcome))).all()
    for (o,) in list(live) + list(paper):
        if o:
            out.add(str(o))
    return out


# ── cluster recovery + scoring ───────────────────────────────────────────────

def _net_expr():
    return func.sum(case((Trade.side == "BUY", Trade.size_shares),
                         else_=-Trade.size_shares))


def _union_wallets(wallet_lists) -> list[str]:
    """Flatten BUY-signal wallet arrays into one deduped, lower-cased cluster,
    preserving first-seen (newest-signal-first) order."""
    seen: dict[str, None] = {}
    for wallets in wallet_lists:
        for w in (wallets or []):
            if w:
                seen.setdefault(str(w).lower(), None)
    return list(seen)


async def _entry_cluster(s, market_id: str, outcome: str) -> tuple[list[str], bool]:
    """(cluster_wallets, is_fallback). Recover the entry cluster from EVERY BUY
    Signal on (market, outcome) within the net-lookback window — the UNION, not
    just the most recent. A position is usually accumulated across several signals
    with different wallets; keying the exit off only the latest signal would close
    the whole position the moment that one sub-cluster reverses, even while an
    earlier (often larger) cluster is still long. Bounding the signal lookup to the
    same window we net "still long" over keeps every member's reversal test fair.
    Fall back to tracked active wallets recently net-long the outcome when no Signal
    links our position."""
    since = datetime.now(tz=timezone.utc) - timedelta(days=_NET_LOOKBACK_DAYS)
    rows = (await s.execute(
        select(Signal.wallets).where(
            Signal.market_id == market_id,
            func.upper(Signal.outcome) == outcome.upper(),
            Signal.side == "BUY",
            Signal.ts >= since,
        ).order_by(Signal.ts.desc()))).all()
    cluster = _union_wallets(r[0] for r in rows)
    if cluster:
        return cluster, False
    rows = (await s.execute(
        select(Trade.wallet, _net_expr())
        .join(Wallet, func.lower(Wallet.address) == func.lower(Trade.wallet))
        .where(Trade.market_id == market_id,
               func.upper(Trade.outcome) == outcome.upper(),
               Trade.ts >= since, Wallet.is_active.is_(True))
        .group_by(Trade.wallet))).all()
    wallets = [str(w).lower() for w, net in rows if net and float(net) > 0]
    return wallets, True


async def _net_by_wallet(s, market_id: str, outcome: str,
                         wallets: list[str]) -> dict[str, float]:
    if not wallets:
        return {}
    lw = [w.lower() for w in wallets]
    since = datetime.now(tz=timezone.utc) - timedelta(days=_NET_LOOKBACK_DAYS)
    rows = (await s.execute(
        select(Trade.wallet, _net_expr())
        .where(Trade.market_id == market_id,
               func.upper(Trade.outcome) == outcome.upper(),
               func.lower(Trade.wallet).in_(lw),
               Trade.ts >= since)
        .group_by(Trade.wallet))).all()
    return {str(w).lower(): float(net or 0.0) for w, net in rows}


async def _weights(s, wallets: list[str], window: str) -> dict[str, float]:
    if not wallets:
        return {}
    lw = [w.lower() for w in wallets]
    rows = (await s.execute(
        select(WalletStats.address, WalletStats.win_rate).where(
            func.lower(WalletStats.address).in_(lw),
            WalletStats.window == window))).all()
    wr = {str(a).lower(): w for a, w in rows}
    return {w: (float(wr[w]) if wr.get(w) is not None else _DEFAULT_WEIGHT) for w in lw}


# ── evaluation + close ───────────────────────────────────────────────────────

async def _evaluate(market_id: str, outcome: str) -> None:
    """Decide whether (market, outcome)'s entry cluster has dissolved; if so, flag
    the thesis and close the position (per enabled mode)."""
    cfg = await _exit_cfg()
    if not cfg.get("enabled", True):
        return
    window = str(cfg.get("quality_window", "30d"))
    async with session_scope() as s:
        cluster, is_fallback = await _entry_cluster(s, market_id, outcome)
        if not cluster:
            return
        nets = await _net_by_wallet(s, market_id, outcome, cluster)
        weights_map = await _weights(s, cluster, window)

    weights = [weights_map.get(w, _DEFAULT_WEIGHT) for w in cluster]
    sold = [nets.get(w, 0.0) <= 0.0 for w in cluster]   # net <= 0 → no longer long
    remaining = weighted_support_remaining(weights, sold)
    threshold = float(cfg.get(
        "fallback_support_dissolution_threshold" if is_fallback
        else "support_dissolution_threshold",
        0.35 if is_fallback else 0.5))
    if remaining >= threshold:
        return                                          # thesis still supported

    log.info("exit_thesis_dissolving", market=market_id, outcome=outcome,
             remaining=round(remaining, 3), threshold=threshold,
             cluster=len(cluster), is_fallback=is_fallback)
    await _flag_dissolving(market_id, outcome, cfg)
    await _do_close(market_id, outcome, cluster, is_fallback, cfg)


async def _flag_dissolving(market_id: str, outcome: str, cfg: dict) -> None:
    ttl = max(60, int(cfg.get("cooldown_seconds", 300)))
    try:
        await redis_client().set(
            THESIS_DISSOLVING_KEY.format(mid=market_id, oc=outcome.upper()), "1", ex=ttl)
    except Exception:  # noqa: BLE001
        log.warning("thesis_dissolving_flag_failed", market=market_id, outcome=outcome)


async def _exit_signal(market_id: str, outcome: str, cluster: list[str],
                       notes: str = "exit_mirror") -> int | None:
    """Write a SELL Signal row for provenance/PnL; returns its id (or None)."""
    try:
        async with session_scope() as s:
            sig = Signal(
                ts=datetime.now(tz=timezone.utc), market_id=market_id,
                outcome=outcome, side="SELL", wallet_count=len(cluster),
                wallets=cluster, avg_win_rate=0.0, correlation_score=0.0,
                target_price=0.0, target_size_usdc=0.0, gate_results={},
                gate_pass=True, executed=False, notes=notes,
            )
            s.add(sig)
            await s.flush()
            return sig.id
    except Exception:  # noqa: BLE001
        log.warning("exit_signal_write_failed", market=market_id, outcome=outcome)
        return None


async def do_close(market_id: str, outcome: str, *, notes: str, live_ok: bool,
                   cooldown: int, cluster: list[str] | tuple = (),
                   skip_event: str = "exit_skip_live_disabled") -> None:
    """Shared exit executor. ONE close per (mode, market, outcome) per cooldown
    window (Redis NX marker — the SAME key gates every trigger and both sweeps, so
    cluster/price/sentiment exits can never double-close). Live is gated by
    ``live_ok`` (logged as ``skip_event`` when blocked); paper always shadow-runs.
    ``notes`` tags the provenance SELL Signal (e.g. "exit_mirror", "exit_take_profit")."""
    modes = await enabled_modes()
    cooldown = max(30, int(cooldown))
    r = redis_client()
    for mode in sorted(modes):
        # Idempotency: ONE close per (mode, market, outcome) per cooldown window.
        key = _PENDING_KEY.format(mode=mode, mid=market_id, oc=outcome.upper())
        try:
            acquired = await r.set(key, "1", nx=True, ex=cooldown)
        except Exception:  # noqa: BLE001
            acquired = None
        if not acquired:
            continue
        try:
            if mode == "live":
                if not live_ok:
                    log.info(skip_event, market=market_id, outcome=outcome, notes=notes)
                    continue
                sid = await _exit_signal(market_id, outcome, list(cluster), notes=notes)
                res = await close_live(market_id=market_id, outcome=outcome, signal_id=sid)
            else:
                res = await close_paper(market_id=market_id, outcome=outcome)
            log.info("exit_close_done", mode=mode, market=market_id, outcome=outcome,
                     status=(res or {}).get("status"), notes=notes)
        except Exception:  # noqa: BLE001
            log.exception("exit_close_failed", mode=mode, market=market_id, outcome=outcome)
            try:                                        # release marker so a sweep can retry
                await r.delete(key)
            except Exception:  # noqa: BLE001
                pass


async def _do_close(market_id: str, outcome: str, cluster: list[str],
                    is_fallback: bool, cfg: dict) -> None:
    """exit_mirror's close: derive live-gating from the cluster's provenance
    (inferred clusters never auto-sell live) and delegate to do_close, preserving
    the distinct exit_skip_live_disabled / exit_skip_live_fallback observability."""
    live_enabled = bool(cfg.get("live_enabled", False))
    live_on_fallback = bool(cfg.get("live_exit_on_fallback", False))
    if not live_enabled:
        live_ok, skip_event = False, "exit_skip_live_disabled"
    elif is_fallback and not live_on_fallback:
        live_ok, skip_event = False, "exit_skip_live_fallback"
    else:
        live_ok, skip_event = True, "exit_skip_live_disabled"
    await do_close(market_id, outcome, notes="exit_mirror", live_ok=live_ok,
                   cooldown=int(cfg.get("cooldown_seconds", 300)),
                   cluster=cluster, skip_event=skip_event)


# ── loop + sweep ─────────────────────────────────────────────────────────────

async def _on_trade(ev: dict) -> None:
    if str(ev.get("side", "")).upper() != "SELL":
        return
    market_id = ev.get("market_id")
    wallet = str(ev.get("wallet", "")).lower()
    if not market_id or not wallet:
        return
    async with session_scope() as s:
        tracked = (await s.execute(
            select(Wallet.address).where(
                func.lower(Wallet.address) == wallet,
                Wallet.is_active.is_(True)).limit(1))).first()
        if not tracked:
            return
        held = await _held_outcomes_for_market(s, market_id)
    for outcome in held:
        await _evaluate(market_id, outcome)


async def _sweep() -> None:
    async with session_scope() as s:
        pairs: list[tuple[str, str]] = []
        for mode in await enabled_modes():
            pairs += await _held_outcomes(s, mode)
    for mid, oc in set(pairs):
        await _evaluate(mid, oc)


async def _sweep_loop() -> None:
    while True:
        interval = 120
        try:
            cfg = await _exit_cfg()
            interval = int(cfg.get("backstop_sweep_seconds", 120))
            if cfg.get("enabled", True):
                await _sweep()
        except Exception:  # noqa: BLE001
            log.exception("exit_sweep_failed")
        await asyncio.sleep(max(15, interval))


async def exit_loop() -> None:
    """Entry point (added to the executor's main gather). Runs the backstop sweep
    and the event-driven trade:new listener; both no-op while exit_mirror.enabled
    is false, so it's safe to always start."""
    log.info("exit_loop_starting")
    asyncio.create_task(_sweep_loop())
    while True:
        try:
            async for ev in subscribe("trade:new"):
                try:
                    await _on_trade(ev)
                except Exception:  # noqa: BLE001
                    log.exception("exit_on_trade_failed")
        except Exception:  # noqa: BLE001
            log.exception("exit_loop_subscribe_failed")
            await asyncio.sleep(5)          # reconnect after a transient pub/sub error
