"""weather_verify.py — INDEPENDENT cross-check of our weather trades.

Trust nothing from our own DB: pull our wallet's ACTUAL weather trades + positions
straight from Polymarket's Data API (data-api.polymarket.com — on-chain truth, with
Polymarket's OWN realized-P&L per position) and put them side-by-side with what our
`fills` table claims. If they disagree, the DB is corrupted and the step-2a P&L is junk.

Uses settings.polymarket_funder_address by default; override with --user 0x… if our
positions live under a different proxy wallet.

Run on the VPS:
    docker compose exec -T executor python -m scripts.weather_verify
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from scripts.weather_recon import is_weather


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _notional(t):
    """USDC notional of a Data API trade record (prefer usdcSize, else size×price)."""
    if t.get("usdcSize") is not None:
        return _f(t["usdcSize"])
    return _f(t.get("size")) * _f(t.get("price"))


def tally(items):
    """items: list of (market_id, side, outcome, notional). Pure summary used for both
    the Data API and the DB so the comparison is apples-to-apples."""
    markets = set()
    buy = sell = 0.0
    no_n = yes_n = 0
    for mid, side, outcome, notional in items:
        markets.add(mid)
        if (side or "").upper() == "SELL":
            sell += notional
        else:
            buy += notional
        if (outcome or "").upper() == "NO":
            no_n += 1
        elif (outcome or "").upper() == "YES":
            yes_n += 1
    return {"n": len(items), "markets": markets, "buy": buy, "sell": sell,
            "no_n": no_n, "yes_n": yes_n}


def _mask(addr):
    return f"{addr[:6]}…{addr[-4:]}" if addr and len(addr) > 12 else str(addr)


async def _dataapi_weather(dc, user):
    """All weather TRADES and POSITIONS for `user` from the Data API."""
    trades, offset = [], 0
    while offset < 20000:
        batch = await dc.trades(user, limit=500, offset=offset) or []
        if not batch:
            break
        trades.extend(batch)
        if len(batch) < 500:
            break
        offset += 500
    wt = [t for t in trades if is_weather(t.get("title", ""))]
    positions = await dc.positions(user, limit=500, size_threshold=0.0) or []
    wp = [p for p in positions if is_weather(p.get("title", ""))]
    return wt, wp


async def run(*, user_override):
    from polybot.clients import DataClient
    from polybot.config import settings
    from polybot.db import session_scope
    from polybot.models import Fill, Market
    from sqlalchemy import select

    user = user_override or settings.polymarket_funder_address
    if not user:
        print("no wallet address (set polymarket_funder_address or pass --user 0x…)")
        return
    print(f"wallet: {_mask(user)}   source: {settings.polymarket_data_url}")

    # independent: Polymarket Data API
    dc = DataClient()
    try:
        wt, wp = await _dataapi_weather(dc, user)
    finally:
        await dc.close()
    api_items = [(t.get("conditionId"), t.get("side"), t.get("outcome"), _notional(t)) for t in wt]
    api = tally(api_items)
    api_pnl = sum(_f(p.get("realizedPnl")) for p in wp)
    api_cash = sum(_f(p.get("cashPnl")) for p in wp)

    # our DB
    async with session_scope() as s:
        rows = (await s.execute(
            select(Fill.market_id, Fill.side, Fill.outcome, Fill.notional_usdc,
                   Fill.size_shares, Market.question)
            .join(Market, Fill.market_id == Market.market_id)
            .where(Market.question.op("~*")(r"temperature"))
        )).all()
    db_rows = [r for r in rows if is_weather(r.question) and r.size_shares > 0]
    db = tally([(r.market_id, r.side, r.outcome, r.notional_usdc) for r in db_rows])

    api_noyes = f"{api['no_n']}/{api['yes_n']}"
    db_noyes = f"{db['no_n']}/{db['yes_n']}"
    print("\n===== INDEPENDENT VERIFICATION: Polymarket Data API vs our DB =====")
    print(f"{'':<22}{'DATA API (truth)':>20}{'OUR DB':>16}")
    print(f"{'weather trades':<22}{api['n']:>20}{db['n']:>16}")
    print(f"{'distinct markets':<22}{len(api['markets']):>20}{len(db['markets']):>16}")
    print(f"{'BUY notional $':<22}{api['buy']:>20,.2f}{db['buy']:>16,.2f}")
    print(f"{'SELL notional $':<22}{api['sell']:>20,.2f}{db['sell']:>16,.2f}")
    print(f"{'NO/YES trades':<22}{api_noyes:>20}{db_noyes:>16}")

    only_db = db["markets"] - api["markets"]
    only_api = api["markets"] - db["markets"]
    both = db["markets"] & api["markets"]
    print(f"\nmarket overlap: both={len(both)}  only-in-DB={len(only_db)}  only-in-API={len(only_api)}")
    print(f"Polymarket's OWN weather P&L (open positions): realized ${api_pnl:+,.2f}  "
          f"cash ${api_cash:+,.2f}   [DB step-2a said realized −$70.42]")

    if only_db:
        print(f"\n⚠ {len(only_db)} markets our DB claims but the Data API has NO trade for — "
              "these would be fabricated/corrupted rows:")
        qof = {r.market_id: r.question for r in db_rows}
        for mid in list(only_db)[:12]:
            print(f"    {qof.get(mid, mid)[:78]}")

    verdict = ("DB MATCHES the chain — step-2a P&L is real" if not only_db and
               abs(api["buy"] - db["buy"]) < max(50.0, 0.1 * api["buy"])
               else "DB DIVERGES from the chain — treat step-2a as suspect, use the Data API")
    print(f"\nVERDICT: {verdict}")


def main():
    for _n in ("httpx", "httpcore"):
        logging.getLogger(_n).setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description="Independent verification of our weather trades")
    ap.add_argument("--user", default=None, help="override wallet/proxy address (0x…)")
    args = ap.parse_args()
    asyncio.run(run(user_override=args.user))


if __name__ == "__main__":
    main()
