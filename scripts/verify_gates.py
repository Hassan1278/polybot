"""Fact-check signal-gate rejections against the LIVE Polymarket CLOB.

For each market_id, pulls the live orderbook (per outcome token) + the market's
closed/active status from Gamma, and prints what the liquidity / risk_reward
gates actually see — so you can compare against the rejection reasons in the
signals feed and confirm they're real, not hallucinated.

Usage (on the VPS — needs network + DB):
  docker compose -f docker-compose.yml -f docker-compose.prod.yml exec signals \
      python -m scripts.verify_gates 0xca3df7... 0xfec5... 0xe6e4...
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from polybot.clients import ClobClient, GammaClient
from polybot.db import session_scope
from polybot.models import Market


def _summ(levels: list) -> tuple[int, float, float | None]:
    levels = levels or []
    usd = 0.0
    prices = []
    for lv in levels:
        try:
            p = float(lv["price"]); sz = float(lv["size"])
        except (KeyError, TypeError, ValueError):
            continue
        usd += p * sz
        prices.append(p)
    best = (max(prices) if prices else None)
    return len(levels), usd, best


async def main(market_ids: list[str]) -> None:
    if not market_ids:
        print("usage: python -m scripts.verify_gates <market_id> [more...]")
        return
    c = ClobClient()
    g = GammaClient()
    try:
        for mid in market_ids:
            async with session_scope() as s:
                m = (await s.execute(
                    select(Market).where(Market.market_id == mid)
                )).scalar_one_or_none()
            print(f"\n=== {mid} ===")
            if not m:
                print("  (not in our DB)")
            else:
                print(f"  q:        {(m.question or '')[:72]}")
                print(f"  category={m.category}  resolved={m.resolved}  outcomes={m.outcomes}")
            try:
                gm = await g.market_by_condition_id(mid)
                if gm:
                    print(f"  gamma:    closed={gm.get('closed')} active={gm.get('active')} "
                          f"vol24h={gm.get('volume24hr')}")
            except Exception as exc:  # noqa: BLE001
                print(f"  gamma:    lookup failed: {exc}")

            tokens = []
            if m and m.yes_token_id:
                tokens.append(("outcome0", m.yes_token_id))
            if m and m.no_token_id:
                tokens.append(("outcome1", m.no_token_id))
            for label, tok in tokens:
                try:
                    book = await c.book(tok)
                except Exception as exc:  # noqa: BLE001
                    print(f"  [{label}] book error: {exc}")
                    continue
                bids, asks = book.get("bids") or [], book.get("asks") or []
                nb, bd, best_bid = _summ(bids)
                na, ad, best_ask = _summ(asks)
                if not bids and not asks:
                    print(f"  [{label}] BOOK EMPTY  ->  matches 'book_empty' / 'no_side_levels'")
                else:
                    # BUY lifts the asks; the liquidity gate sums that side's depth.
                    print(f"  [{label}] bids {nb}L ${bd:,.0f} (best {best_bid})  |  "
                          f"asks {na}L ${ad:,.0f} (best {best_ask})  "
                          f"<- a BUY sees the ASK side")
    finally:
        await c.close()
        await g.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
