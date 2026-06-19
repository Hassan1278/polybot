"""btc_position_analysis.py — is the current (possibly two-sided) crypto book
mathematically worth holding, and where do you actually make money?

Reads OPEN live positions (live BUY fills on still-open crypto markets), groups
them by underlying asset and price direction, marks every leg at the current
CLOB price, then prints:

  * every open leg: outcome, direction, shares, avg entry, current mark
    (= market-implied P(win)), current value, unrealized P&L, strike, hrs left
  * per asset: total cost, current mark-to-market, and the two directional
    resolution scenarios (asset UP world / asset DOWN world) with net P&L and
    the market-implied probability of each.

The scenario corners treat all same-asset bull legs as winning together and all
bear legs as winning together — the realistic correlated view for one underlying
over one period. Net>0 in a corner = a world where you profit; its probability
is the implied prob of that side. If BOTH corners are negative you've locked a
guaranteed loss (an over-priced hedge); if both positive it's a free arb.

Usage (on the VPS):
  docker compose -f docker-compose.yml -f docker-compose.prod.yml exec executor \
      python -m scripts.btc_position_analysis
  # optional: restrict to one asset (default shows BTC in full + others in brief)
  docker compose ... exec executor python -m scripts.btc_position_analysis BTC
"""

from __future__ import annotations

import asyncio
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import select

from polybot.asset_direction import asset_of, direction
from polybot.clients import ClobClient
from polybot.db import session_scope
from polybot.market_resolver import token_for_outcome
from polybot.models import Fill, Market

_OPEN = ("filled", "submitted", "partial")
_STRIKE_RE = re.compile(r"\$\s?([0-9][0-9,]*\.?[0-9]*)\s*([kKmM]?)")


def _strike(q: str | None) -> str:
    if not q:
        return "-"
    m = _STRIKE_RE.search(q)
    if not m:
        return "-"
    return f"${m.group(1)}{m.group(2)}".replace(",", "")


def _hrs(end, now) -> float | None:
    if end is None:
        return None
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return (end - now).total_seconds() / 3600.0


async def main(argv: list[str]) -> None:
    only = argv[0].upper() if argv else None
    now = datetime.now(tz=timezone.utc)

    async with session_scope() as s:
        rows = (await s.execute(
            select(
                Fill.market_id, Fill.outcome, Fill.size_shares, Fill.notional_usdc,
                Market.question, Market.slug, Market.end_date,
                Market.yes_token_id, Market.no_token_id, Market.outcomes,
            )
            .join(Market, Market.market_id == Fill.market_id)
            .where(
                Fill.mode == "live",
                Fill.side == "BUY",
                Fill.status.in_(_OPEN),
                Market.category == "crypto",
                Market.end_date > now,
            )
        )).all()

    # Aggregate fills by (asset, market_id, outcome): sum shares + cost.
    agg: dict[tuple, dict] = {}
    for (mid, outcome, shares, notional, q, slug, end, yes_t, no_t, outs) in rows:
        asset = asset_of(q, slug)
        if asset is None:
            continue
        key = (asset, mid, outcome)
        g = agg.setdefault(key, {
            "asset": asset, "mid": mid, "outcome": outcome, "q": q, "slug": slug,
            "end": end, "yes_t": yes_t, "no_t": no_t, "outs": outs,
            "shares": 0.0, "cost": 0.0,
        })
        g["shares"] += float(shares or 0.0)
        g["cost"] += float(notional or 0.0)

    if not agg:
        print("No open live crypto positions on the book right now.")
        return

    clob = ClobClient()
    legs_by_asset: dict[str, list[dict]] = defaultdict(list)
    try:
        for g in agg.values():
            mkt = SimpleNamespace(yes_token_id=g["yes_t"], no_token_id=g["no_t"],
                                  outcomes=g["outs"])
            token = token_for_outcome(mkt, g["outcome"])
            mark = await clob.best_mark(token) if token else 0.0
            shares, cost = g["shares"], g["cost"]
            g.update({
                "dir": direction(g["q"], g["slug"], g["outcome"], "BUY"),
                "mark": mark,
                "avg_entry": (cost / shares) if shares else 0.0,
                "value": shares * mark,
                "upnl": shares * mark - cost,
                "hrs": _hrs(g["end"], now),
                "strike": _strike(g["q"]),
            })
            legs_by_asset[g["asset"]].append(g)
    finally:
        await clob.close()

    assets = sorted(legs_by_asset, key=lambda a: -sum(l["cost"] for l in legs_by_asset[a]))
    for asset in assets:
        legs = legs_by_asset[asset]
        brief = only is not None and asset != only
        _print_asset(asset, legs, brief=brief)


def _wavg(legs: list[dict], field: str) -> float:
    sh = sum(l["shares"] for l in legs)
    return (sum(l[field] * l["shares"] for l in legs) / sh) if sh else 0.0


def _print_asset(asset: str, legs: list[dict], *, brief: bool) -> None:
    bulls = [l for l in legs if l["dir"] == "bull"]
    bears = [l for l in legs if l["dir"] == "bear"]
    unknown = [l for l in legs if l["dir"] not in ("bull", "bear")]

    cost = sum(l["cost"] for l in legs)
    value = sum(l["value"] for l in legs)
    bull_payoff = sum(l["shares"] for l in bulls)   # $1/share if up-world wins
    bear_payoff = sum(l["shares"] for l in bears)
    net_up = bull_payoff - cost
    net_down = bear_payoff - cost
    p_up = _wavg(bulls, "mark") if bulls else 0.0
    p_down = _wavg(bears, "mark") if bears else 0.0

    twosided = bool(bulls) and bool(bears)
    flag = "  <-- TWO-SIDED" if twosided else ""
    print(f"\n{'='*78}\n{asset}: {len(legs)} open leg(s) | bull={len(bulls)} "
          f"bear={len(bears)} | cost=${cost:.2f} mark=${value:.2f} "
          f"uPnL=${value-cost:+.2f}{flag}")
    if brief:
        print(f"  (brief — pass `{asset}` as arg for the full per-leg table)")
        return

    print(f"{'-'*78}")
    hdr = f"{'dir':<5}{'outcome':<9}{'strike':>9}{'shares':>9}{'entry':>8}{'mark':>7}{'cost':>9}{'value':>9}{'uPnL':>9}{'hrs':>6}"
    print(hdr)
    for l in sorted(legs, key=lambda x: (x["dir"] or "z", -x["cost"])):
        hrs = f"{l['hrs']:.1f}" if l["hrs"] is not None else "-"
        mk = f"{l['mark']:.3f}" if l["mark"] else "n/a"
        print(f"{(l['dir'] or '?'):<5}{str(l['outcome'])[:8]:<9}{l['strike']:>9}"
              f"{l['shares']:>9.1f}{l['avg_entry']:>8.3f}{mk:>7}{l['cost']:>9.2f}"
              f"{l['value']:>9.2f}{l['upnl']:>+9.2f}{hrs:>6}")
        print(f"      {str(l['q'])[:88]}")

    print(f"{'-'*78}\nRESOLUTION SCENARIOS for {asset} (held to expiry, $1/winning share):")
    print(f"  {asset} UP world   -> bulls win: payoff ${bull_payoff:.2f} "
          f"- cost ${cost:.2f} = NET ${net_up:+.2f}   (implied P(up)~{p_up:.2f})")
    print(f"  {asset} DOWN world -> bears win: payoff ${bear_payoff:.2f} "
          f"- cost ${cost:.2f} = NET ${net_down:+.2f}   (implied P(down)~{p_down:.2f})")
    if twosided:
        ev = p_up * net_up + p_down * net_down
        print(f"  blended EV (corr.) ~= {p_up:.2f}*({net_up:+.2f}) + "
              f"{p_down:.2f}*({net_down:+.2f}) = ${ev:+.2f}")
        if net_up < 0 and net_down < 0:
            print("  >> BOTH corners negative: this pair LOCKS IN A LOSS no matter "
                  "where BTC goes.")
        elif net_up > 0 and net_down > 0:
            print("  >> BOTH corners positive: free arb (check for stale marks).")
        else:
            win = "UP" if net_up > 0 else "DOWN"
            p_win = p_up if net_up > 0 else p_down
            print(f"  >> You only profit in the {asset} {win} world "
                  f"(implied ~{p_win:.0%} likely). The other world is a loss.")
    if unknown:
        print(f"  note: {len(unknown)} leg(s) had unparseable direction "
              "(excluded from scenarios).")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
