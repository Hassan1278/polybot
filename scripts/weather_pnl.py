"""weather_pnl.py — STEP 2a: how did our live weather NO-strategy actually do?

Our DB's resolution flags are stale, so we pull each traded weather market's RESOLVED
outcome from gamma, then compute realized P&L per market and in aggregate:
  • realized P&L, capital deployed, and NO win-rate (how often our NO bet was right);
  • where the high ACTUALLY landed per city-day vs the buckets we shorted — i.e. is our
    bucket selection systematically shorting the too-likely bucket?

No forecasts here — that's step 2b. Run on the VPS:
    docker compose exec -T executor python -m scripts.weather_pnl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from collections import defaultdict

from scripts.weather_recon import is_weather

_Q_RE = re.compile(r"(highest|lowest) temperature in (.+?) be (.+?) on (.+?)\??$", re.I)


def parse_q(q):
    """(kind, city, bucket, date) from a temperature-market question, or Nones."""
    m = _Q_RE.search(q or "")
    if not m:
        return (None, None, None, None)
    kind, city, bucket, date = m.groups()
    return (kind.lower(), city.strip(), bucket.strip(), date.strip())


def market_pnl(fills, no_won):
    """fills: list of (side, shares, notional, fee), all on the NO outcome.
    no_won: True/False/None(unresolved). Realized P&L = sells + settle − buys − fees,
    where settle = remaining net shares × $1 if NO won (held to resolution)."""
    bought = sum(n for sd, _sh, n, _fe in fills if sd == "BUY")
    sold = sum(n for sd, _sh, n, _fe in fills if sd == "SELL")
    fees = sum(fe for *_rest, fe in fills)
    net = sum((sh if sd == "BUY" else -sh) for sd, sh, _n, _fe in fills)
    if no_won is None:
        return {"bought": bought, "sold": sold, "net_shares": net, "realized": None}
    settle = net * (1.0 if no_won else 0.0)
    return {"bought": bought, "sold": sold, "net_shares": net,
            "realized": sold + settle - bought - fees}


def _resolution(m):
    """no_won (bool) from a gamma market dict, or None if not resolved.
    outcomes = [Yes, No]; NO won iff the second leg paid out."""
    if not m or not m.get("closed"):
        return None
    px = m.get("outcomePrices")
    try:
        p = json.loads(px) if isinstance(px, str) else px
        return float(p[1]) > 0.5
    except (TypeError, ValueError, IndexError):
        return None


async def run():
    from polybot.clients import GammaClient
    from polybot.db import session_scope
    from polybot.models import Fill, Market
    from sqlalchemy import select

    async with session_scope() as s:
        rows = (await s.execute(
            select(Fill.market_id, Fill.outcome, Fill.side, Fill.size_shares,
                   Fill.notional_usdc, Fill.fee_usdc, Market.question)
            .join(Market, Fill.market_id == Market.market_id)
            .where(Market.question.op("~*")(r"temperature"))
        )).all()

    rows = [r for r in rows if is_weather(r.question) and r.outcome == "NO"]
    by_mkt = defaultdict(list)
    qof = {}
    for r in rows:
        by_mkt[r.market_id].append((r.side, r.size_shares, r.notional_usdc, r.fee_usdc))
        qof[r.market_id] = r.question

    print(f"resolving {len(by_mkt)} weather markets via gamma (~a minute)…")
    g = GammaClient()
    res = {}
    try:
        for mid in by_mkt:
            res[mid] = _resolution(await g.market_by_condition_id(mid))
    finally:
        await g.close()

    recs = []
    for mid, fills in by_mkt.items():
        kind, city, bucket, date = parse_q(qof[mid])
        recs.append({"mid": mid, "q": qof[mid], "city": city, "bucket": bucket,
                     "date": date, "kind": kind, "no_won": res[mid],
                     **market_pnl(fills, res[mid])})

    resolved = [r for r in recs if r["no_won"] is not None]
    unresolved = [r for r in recs if r["no_won"] is None]
    real = sum(r["realized"] for r in resolved)
    deployed = sum(r["bought"] for r in recs)
    no_wins = sum(1 for r in resolved if r["no_won"])

    print("\n===== STEP 2a: live weather NO-strategy results =====")
    print(f"markets traded: {len(recs)}   resolved: {len(resolved)}   "
          f"still open on gamma: {len(unresolved)}")
    if resolved:
        wr = no_wins / len(resolved)
        on_dep = f"  ({real / deployed:+.1%} on deployed)" if deployed else ""
        wins = [r["realized"] for r in resolved if r["realized"] > 0]
        losses = [r["realized"] for r in resolved if r["realized"] <= 0]
        aw = f"   avg win ${sum(wins) / len(wins):+.2f}" if wins else ""
        al = f"   avg loss ${sum(losses) / len(losses):+.2f}" if losses else ""
        print(f"NO win-rate: {no_wins}/{len(resolved)} = {wr:.1%}  "
              f"(NO right = the high MISSED that bucket)")
        print(f"realized P&L: ${real:+.2f}  on ${deployed:.2f} NO buys{on_dep}")
        print(f"profitable: {len(wins)}   losing: {len(losses)}{aw}{al}")

    # where did the high actually land, per city-day?
    groups = defaultdict(list)
    for r in resolved:
        groups[(r["city"], r["date"], r["kind"])].append(r)
    landed_in_ours = sum(1 for legs in groups.values() if any(not x["no_won"] for x in legs))
    print(f"\ncity-days resolved: {len(groups)}   "
          f"high landed in a bucket we shorted: {landed_in_ours} "
          f"(those cost us; the rest were clean NO wins)")

    def _tag(x):
        return f"{'⚠' if not x['no_won'] else ' '}{x['bucket']}({x['realized']:+.1f})"

    print("\nWHERE THE HIGH LANDED (city-day → bucket(s) we shorted; ⚠ = high hit it):")
    for (city, date, kind), legs in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        body = "  ".join(_tag(x) for x in sorted(legs, key=lambda x: x["bucket"]))
        print(f"  {date:>7} {city:<16} {kind:<7} {body}")

    if unresolved:
        print(f"\n(still open on gamma — no result yet: {len(unresolved)} markets, "
              f"e.g. {', '.join(sorted({r['date'] for r in unresolved if r['date']}))[:60]})")


def main():
    for _n in ("httpx", "httpcore"):
        logging.getLogger(_n).setLevel(logging.WARNING)
    argparse.ArgumentParser(description="Step 2a: realized P&L of our weather NO-strategy").parse_args()
    asyncio.run(run())


if __name__ == "__main__":
    main()
