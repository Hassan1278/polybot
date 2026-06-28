"""weather_trades.py — STEP 1 of the weather forecast study.

Just two things, no forecasts or analysis yet:
  • What weather have we actually traded? — our Fills (paper + live) and Positions
    joined to market metadata, filtered to weather questions.
  • What settles those markets? — the RESOLUTION SOURCE, fetched per market from gamma
    (we don't store descriptions locally).

If we turn out to have ~no weather trades (this bot has mostly traded crypto), it says
so and notes the fallback: sample resolved weather markets straight from gamma for the
later steps. Observe-only.

Run on the VPS (DB + gamma live there):
    docker compose exec -T executor python -m scripts.weather_trades
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re

from scripts.weather_recon import is_weather

# Broad server-side narrowing (substring, case-insensitive); is_weather() then does the
# precise word-boundary filter so 'ukraine' doesn't sneak in via 'rain'.
_SQL_PREFILTER = r"temperature|fahrenheit|celsius|degrees|rain|snow|hurricane|cyclone|weather|precip|°"


def _clean(s):
    return re.sub(r"<[^>]+>", " ", re.sub(r"\s+", " ", s or "")).strip()


async def run():
    from polybot.clients import GammaClient
    from polybot.db import session_scope
    from polybot.models import Fill, Market, Position
    from sqlalchemy import select

    async with session_scope() as s:
        fills = (await s.execute(
            select(Fill.mode, Fill.ts, Fill.outcome, Fill.side, Fill.size_shares,
                   Fill.price, Fill.notional_usdc, Market.market_id, Market.question,
                   Market.category, Market.end_date, Market.resolved,
                   Market.outcome.label("resolution"))
            .join(Market, Fill.market_id == Market.market_id)
            .where(Market.question.op("~*")(_SQL_PREFILTER))
        )).all()
        positions = (await s.execute(
            select(Position.wallet, Position.outcome, Position.size_shares,
                   Position.avg_price, Position.realized_pnl_usdc, Market.market_id,
                   Market.question, Market.resolved, Market.outcome.label("resolution"))
            .join(Market, Position.market_id == Market.market_id)
            .where(Market.question.op("~*")(_SQL_PREFILTER))
        )).all()

    wf = [r for r in fills if is_weather(r.question)]
    wp = [r for r in positions if is_weather(r.question)]
    qmap = {r.market_id: r.question for r in wf}
    qmap.update({r.market_id: r.question for r in wp})

    print(f"\nWEATHER FILLS (our trades): {len(wf)}  "
          f"(paper={sum(1 for r in wf if r.mode == 'paper')}, "
          f"live={sum(1 for r in wf if r.mode == 'live')})")
    print(f"WEATHER POSITIONS: {len(wp)}")
    print(f"DISTINCT WEATHER MARKETS TOUCHED: {len(qmap)}")

    if not qmap:
        print("\nNo weather trades logged. For the next steps we'll sample RESOLVED "
              "weather markets from gamma instead (bigger n, same analysis).")
        return

    print("\nOUR FILLS BY MARKET:")
    for mid, q in qmap.items():
        legs = [r for r in wf if r.market_id == mid]
        meta = next((r for r in wf if r.market_id == mid), None) or next(r for r in wp if r.market_id == mid)
        print(f"\n  • {q[:90]}")
        print(f"    end={str(meta.end_date)[:10] if hasattr(meta, 'end_date') else '?'} "
              f"resolved={meta.resolved} result={meta.resolution}")
        for r in legs:
            print(f"      [{r.mode}] {r.side} {r.outcome} {r.size_shares:.1f}sh "
                  f"@ {r.price:.3f} = ${r.notional_usdc:.2f}  {str(r.ts)[:16]}")
        for r in (r for r in wp if r.market_id == mid):
            print(f"      [position] {r.outcome} {r.size_shares:.1f}sh @ {r.avg_price:.3f} "
                  f"realized=${r.realized_pnl_usdc:.2f}")

    print("\nRESOLUTION SOURCES (from gamma — the feed our forecast must match):")
    g = GammaClient()
    try:
        for mid, q in qmap.items():
            m = await g.market_by_condition_id(mid)
            if not m:
                print(f"\n  • {q[:80]}\n    (gamma: not found)")
                continue
            print(f"\n  • {q[:80]}\n    {_clean(m.get('description'))[:420]}")
    finally:
        await g.close()


def main():
    for _n in ("httpx", "httpcore"):
        logging.getLogger(_n).setLevel(logging.WARNING)
    argparse.ArgumentParser(description="Step 1: our weather trades + resolution sources").parse_args()
    asyncio.run(run())


if __name__ == "__main__":
    main()
