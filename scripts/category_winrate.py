"""category_winrate.py — per-category win-rate AND net P&L since live deployment.

Win-rate ≠ profitability. A category can win most of its bets and still lose money
(many small wins, rare big losses — the favorite-fade fat tail). This reports BOTH,
per category, from venue truth (the data-api activity feed).

Accounting is done per MARKET (conditionId), via CASH FLOW:
    net = sells + redemptions − buys      (summed over all of a market's tokens)
This is robust to a Polymarket quirk that breaks naive per-token accounting: a
REDEEM (held-to-resolution win) is logged under a DIFFERENT token id than the
original buy, so token-keyed accounting dumps every redeemed winner into an
"unknown" bucket and makes real categories look like pure losers. Grouping by
conditionId (== Market.market_id) puts winners back in their category with cost
subtracted.

A market counts toward win-rate once it's SETTLED (no live position left):
  WIN  = realized cash net > 0;  LOSS = net < 0.
Markets with an open position are excluded (not yet decided). Category comes from
the local Market table keyed by conditionId.

Usage (on the VPS):
  docker compose -f docker-compose.yml -f docker-compose.prod.yml exec executor \
      python -m scripts.category_winrate
"""

from __future__ import annotations

import asyncio

from polybot.clients import DataClient
from polybot.db import session_scope
from polybot.models import Market
from sqlalchemy import select

from scripts.live_pnl import _f, _fetch_activity, _fetch_positions
from services.executor.equity_guard import _deposit_wallet

_EPS = 0.01            # ignore sub-cent net noise


def bet_outcome(net: float, eps: float = _EPS) -> str:
    """WIN / LOSS / FLAT from a settled market's net realized P&L. Pure / tested."""
    if net > eps:
        return "win"
    if net < -eps:
        return "loss"
    return "flat"


def settled_market_nets(activity: list[dict], open_conds: set[str]) -> dict[str, float]:
    """Per-market (conditionId) realized cash net = sells + redeems − buys, for
    markets with NO live position left (settled). Robust to the redeem-token-id
    mismatch because it groups by conditionId, not token. Pure / tested."""
    flows: dict[str, dict] = {}
    for e in activity:
        cond = str(e.get("conditionId") or e.get("market") or "")
        if not cond:
            continue
        typ = str(e.get("type") or "").upper()
        side = str(e.get("side") or "").upper()
        usdc = _f(e.get("usdcSize"))
        if usdc <= 0:
            usdc = _f(e.get("size")) * _f(e.get("price"))
        if usdc <= 0:
            continue
        f = flows.setdefault(cond, {"buys": 0.0, "sells": 0.0, "redeems": 0.0})
        if typ == "TRADE" and side == "BUY":
            f["buys"] += usdc
        elif typ == "TRADE" and side == "SELL":
            f["sells"] += usdc
        elif typ == "REDEEM":
            f["redeems"] += usdc
    return {cond: f["sells"] + f["redeems"] - f["buys"]
            for cond, f in flows.items() if cond not in open_conds}


def aggregate_by_category(items: list[tuple[str, str, float]]) -> dict[str, dict]:
    """items = [(category, outcome, net_usdc)]. Returns per-category
    {win, loss, flat, n, net, winrate} where winrate excludes flats. Pure / tested."""
    out: dict[str, dict] = {}
    for cat, outcome, net in items:
        c = out.setdefault(cat or "unknown",
                           {"win": 0, "loss": 0, "flat": 0, "n": 0, "net": 0.0, "winrate": None})
        c[outcome] += 1
        c["n"] += 1
        c["net"] += net
    for c in out.values():
        decided = c["win"] + c["loss"]
        c["winrate"] = (c["win"] / decided) if decided else None
    return out


async def _cond_category_map() -> dict[str, str | None]:
    """conditionId (== Market.market_id) -> category, from the local Market table."""
    async with session_scope() as s:
        rows = (await s.execute(select(Market.market_id, Market.category))).all()
    return {str(mid): cat for mid, cat in rows if mid}


async def main() -> None:
    wallet = await _deposit_wallet()
    if not wallet:
        print("No deposit wallet configured. Nothing to read.")
        return

    client = DataClient()
    try:
        raw_activity, hit_cap = await _fetch_activity(client, wallet)
        raw_positions = await _fetch_positions(client, wallet)
    finally:
        await client.close()

    open_conds = {str(p.get("conditionId") or "") for p in raw_positions
                  if _f(p.get("size")) > 0 and p.get("conditionId")}
    nets = settled_market_nets(raw_activity, open_conds)
    cond2cat = await _cond_category_map()

    unknown_cat = sum(1 for cond in nets if cond not in cond2cat)
    items = [(cond2cat.get(cond) or "unknown", bet_outcome(net), net)
             for cond, net in nets.items()]
    agg = aggregate_by_category(items)
    if not agg:
        print("No settled live markets found in the activity window.")
        return

    rows = sorted(agg.items(), key=lambda kv: kv[1]["net"])   # worst net first
    print(f"\n{'='*82}")
    print(f"PER-CATEGORY WIN-RATE & NET P&L  (settled markets, cashflow)   wallet={wallet[:10]}…")
    print('='*82)
    print(f"{'category':<16}{'mkts':>6}{'win':>6}{'loss':>6}{'win-rate':>10}{'net $':>12}  verdict")
    print('-'*82)
    tot_w = tot_l = 0
    tot_net = 0.0
    for cat, c in rows:
        wr = c["winrate"]
        wr_s = f"{wr*100:.0f}%" if wr is not None else "n/a"
        prof = c["net"] > 0
        pos_wr = wr is not None and wr > 0.5
        verdict = "PROFITABLE" if prof else "loses $"
        if pos_wr and not prof:
            verdict += "  ⚠ +WR but unprofitable"
        print(f"{cat[:15]:<16}{c['n']:>6}{c['win']:>6}{c['loss']:>6}{wr_s:>10}"
              f"{c['net']:>+12.2f}  {verdict}")
        tot_w += c["win"]
        tot_l += c["loss"]
        tot_net += c["net"]
    print('-'*82)
    tot_wr = (tot_w / (tot_w + tot_l)) if (tot_w + tot_l) else 0.0
    print(f"{'ALL':<16}{tot_w+tot_l:>6}{tot_w:>6}{tot_l:>6}{tot_wr*100:>9.0f}%{tot_net:>+12.2f}")

    profitable = [c for c, v in rows if v["net"] > 0]
    pos_wr = [c for c, v in rows if v["winrate"] is not None and v["winrate"] > 0.5]
    pos_wr_but_loss = [c for c, v in rows
                       if v["winrate"] is not None and v["winrate"] > 0.5 and v["net"] <= 0]
    print(f"\n{'='*82}")
    print(f"Positive win-rate (>50%): {', '.join(pos_wr) if pos_wr else 'none'}")
    print(f"Actually PROFITABLE (net>$0): {', '.join(profitable) if profitable else 'NONE'}")
    if pos_wr_but_loss:
        print(f"⚠ Win most markets but LOSE money (fat tail): {', '.join(pos_wr_but_loss)}")
    print('='*82)
    notes = []
    if open_conds:
        notes.append(f"{len(open_conds)} market(s) still open, excluded (not yet decided)")
    if unknown_cat:
        notes.append(f"{unknown_cat} settled market(s) not in the local Market table (→ unknown)")
    if hit_cap:
        notes.append("hit the activity scan cap — older markets may be missing")
    if notes:
        print("\nnote: " + "; ".join(notes) + ".")
    print("Net is realized cashflow per market (sells+redeems−buys), so redeemed\n"
          "winners count in their real category. High win-rate + negative net =\n"
          "many small wins, rare big losses.")


if __name__ == "__main__":
    asyncio.run(main())
