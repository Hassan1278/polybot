"""Every minute, snapshot equity / realized / unrealized PnL.

Also runs market settlement for paper-mode: any open PAPER position whose
underlying market has resolved is closed at $1 (winning outcome) or $0
(losing outcome). Without this, paper positions sit in unrealized forever
because the bot has no SELL-signal generator.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import case, func, select

from polybot import alerts
from polybot.clients import ClobClient
from polybot.config import settings
from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.models import Fill, Market, PnLSnapshot, Position
from polybot.redis_bus import client as _redis_client
from services.executor.paper import settle_resolved_markets

_DRAWDOWN_FLAG_KEY = "polybot:alerts:pnl_drawdown_15"
_DRAWDOWN_FLAG_TTL = 3600  # 1 hour

log = get_logger(__name__)


async def _equity_paper() -> tuple[float, float, float, int]:
    """Returns (equity, realized, unrealized, open_count) for paper mode."""
    async with session_scope() as s:
        rows = (await s.execute(
            select(Position.market_id, Position.outcome, Position.size_shares, Position.avg_price,
                   Position.realized_pnl_usdc, Market.yes_token_id, Market.no_token_id)
            .join(Market, Market.market_id == Position.market_id)
            .where(Position.wallet == "PAPER")
        )).all()
        # Net cash spent on positions (BUYs reduce bank, SELLs grow it).
        # Use SQL CASE so the sign is evaluated per-row in the DB. The old
        # version used a Python ternary on `Fill.side == "BUY"` (a SQLAlchemy
        # Column expression) which evaluates `__bool__` once at query build
        # time → always False → the sign collapsed to -1 for every row,
        # systematically overstating paper equity by 2× the BUY total.
        cash_used = (await s.execute(
            select(func.coalesce(
                func.sum(case(
                    (Fill.side == "BUY",  Fill.notional_usdc + Fill.fee_usdc),
                    else_=(-Fill.notional_usdc + Fill.fee_usdc),
                )),
                0.0,
            ))
            .where(Fill.mode == "paper")
        )).scalar_one()

    realized = sum(r[4] for r in rows)
    unrealized = 0.0
    open_n = 0
    if rows:
        c = ClobClient()
        try:
            for mid, oc, sz, avg, _, yt, nt in rows:
                if abs(sz) < 1e-6:
                    continue
                open_n += 1
                tid = yt if oc.upper() == "YES" else nt
                if not tid:
                    continue
                try:
                    mark = await c.midpoint(str(tid))
                except Exception:  # noqa: BLE001
                    mark = avg
                unrealized += (mark - avg) * sz
        finally:
            await c.close()

    equity = settings.paper_starting_usdc - cash_used + realized + unrealized
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
