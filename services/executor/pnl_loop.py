"""Every minute, snapshot equity / realized / unrealized PnL.

Also runs market settlement for paper-mode: any open PAPER position whose
underlying market has resolved is closed at $1 (winning outcome) or $0
(losing outcome). Without this, paper positions sit in unrealized forever
because the bot has no SELL-signal generator.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import func, select

from polybot import alerts
from polybot.clients import ClobClient
from polybot.config import settings
from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.market_resolver import token_for_outcome
from polybot.models import Market, PnLSnapshot, Position
from polybot.redis_bus import client as _redis_client
from services.executor.paper import settle_resolved_markets

_DRAWDOWN_FLAG_KEY = "polybot:alerts:pnl_drawdown_15"
_DRAWDOWN_FLAG_TTL = 3600  # 1 hour

log = get_logger(__name__)


async def _equity_paper() -> tuple[float, float, float, int]:
    """Returns (equity, realized, unrealized, open_count) for paper mode.

    Equity model:
        equity = starting_cash + sum(realized) + sum(unrealized)

    where:
        realized   = sum(Position.realized_pnl_usdc) — already includes fees
                     (subtracted per fill via realized_delta = -fee) and the
                     PnL of fully-settled positions ((settle_px - avg) × size).
        unrealized = sum((mark - avg) × size) for still-open positions —
                     i.e. the mark-to-market GAIN on open positions, on top
                     of their cost basis.

    Previously the formula subtracted `cash_used` (the total BUY notional+fee
    minus SELL proceeds) on top of realized+unrealized. That double-counts
    the cost basis of open positions: bank cash is decreased by BUY cost,
    but the position's current market value (cost + unrealized) is never
    added back. With $346 of open cost basis the formula under-stated
    equity by ~$346.

    Verification on a toy case: starting=$100, BUY 100×$0.30 (cost=$30),
    mark moves to $0.40 (unrealized=$10), no fees:
        - bank cash:        $70
        - position value:   $40  (= 100 × $0.40)
        - true equity:      $110

        OLD formula:        100 - 30 + 0 + 10 = $80   (off by $30 = cost)
        NEW formula:        100 + 0 + 10 = $110       ✓
    """
    # Compute realized from ALL positions (no Market join). A briefly-missing
    # Market row (ingest race, market_id rename, retention drop) would
    # otherwise drop that position's realized PnL from the equity total —
    # equity would shrink spuriously, drawdown alerts could mis-fire.
    async with session_scope() as s:
        realized_total = (await s.execute(
            select(func.coalesce(func.sum(Position.realized_pnl_usdc), 0.0))
            .where(Position.wallet == "PAPER")
        )).scalar_one()
        rows = (await s.execute(
            select(Position.market_id, Position.outcome, Position.size_shares, Position.avg_price,
                   Position.realized_pnl_usdc, Market.yes_token_id, Market.no_token_id,
                   Market.outcomes)
            .join(Market, Market.market_id == Position.market_id, isouter=True)
            .where(Position.wallet == "PAPER")
        )).all()

    realized = float(realized_total or 0.0)
    unrealized = 0.0
    open_n = 0
    if rows:
        c = ClobClient()
        try:
            for mid, oc, sz, avg, _, yt, nt, outcomes in rows:
                if abs(sz) < 1e-6:
                    continue
                open_n += 1
                # Centralised token-id lookup. See
                # packages/polybot/market_resolver.py:token_for_outcome —
                # correctly handles multi-outcome markets via outcomes[]
                # mapping, falls back to yes_token_id for legacy rows.
                # SimpleNamespace shim because tuple rows don't have attrs.
                from types import SimpleNamespace
                row_shim = SimpleNamespace(
                    yes_token_id=yt, no_token_id=nt, outcomes=outcomes,
                    market_id=mid,
                )
                tid = token_for_outcome(row_shim, oc)
                if not tid:
                    continue
                try:
                    # best_mark = midpoint, fallback to last-trade-price
                    # for resolved-but-pending markets. Without the fallback,
                    # ~half of our open positions show mark=0 (= treated as
                    # avg → 0 unrealized contribution), making aggregate
                    # unrealized PnL severely under-reported.
                    mark = await c.best_mark(str(tid))
                    if mark <= 0:
                        mark = avg
                except Exception:  # noqa: BLE001
                    mark = avg
                unrealized += (mark - avg) * sz
        finally:
            await c.close()

    # See docstring above — cash_used was double-subtracting cost basis.
    equity = settings.paper_starting_usdc + realized + unrealized
    return equity, realized, unrealized, open_n


async def pnl_loop() -> None:
    while True:
        try:
            # Settle any resolved markets before the snapshot so today's
            # realized line is current. Paper-mode only — live positions
            # settle on-chain.
            if settings.trading_mode == "paper":
                try:
                    settled = await settle_resolved_markets()
                    if settled:
                        log.info("paper_settlements_applied", n=len(settled))
                        for s_ in settled:
                            await alerts.notify(
                                "info",
                                "Paper position settled",
                                f"market={s_['market_id'][:18]} outcome={s_['outcome']} "
                                f"settled_price={s_['settled_price']} shares={s_['shares']:.0f}",
                            )
                except Exception:  # noqa: BLE001
                    log.exception("paper_settle_failed")

                equity, realised, unrealised, n = await _equity_paper()
            else:
                # Live equity is read from the chain — for now, mirror realized fills
                async with session_scope() as s:
                    r = (await s.execute(
                        select(func.coalesce(func.sum(Position.realized_pnl_usdc), 0.0))
                    )).scalar_one()
                equity = float(r)
                realised = float(r)
                unrealised = 0.0
                n = 0
            async with session_scope() as s:
                s.add(PnLSnapshot(
                    ts=datetime.now(tz=timezone.utc),
                    mode=settings.trading_mode,
                    equity_usdc=equity, realized_usdc=realised,
                    unrealized_usdc=unrealised, open_positions=n,
                ))
            log.info("pnl_snapshot", mode=settings.trading_mode, equity=round(equity, 2),
                     realized=round(realised, 2), unrealized=round(unrealised, 2), open=n)

            try:
                starting_equity = float(getattr(settings, "paper_starting_usdc", 0.0) or 0.0)
                if starting_equity > 0 and equity < starting_equity * 0.85:
                    rds = _redis_client()
                    # SET NX with 1h TTL — only the first writer fires the alert
                    # within the drawdown window.
                    acquired = await rds.set(
                        _DRAWDOWN_FLAG_KEY, "1", nx=True, ex=_DRAWDOWN_FLAG_TTL
                    )
                    if acquired:
                        await alerts.notify(
                            "critical",
                            "PnL drawdown 15%",
                            f"equity={equity:.2f}",
                        )
            except Exception:  # noqa: BLE001
                log.exception("pnl_drawdown_alert_failed")
        except Exception:
            log.exception("pnl_loop_failed")
        await asyncio.sleep(60)
