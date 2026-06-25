"""category_winrate.py — per-category win-rate AND net P&L since live deployment.

Win-rate ≠ profitability. A category can win most of its bets and still lose money
(many small wins, rare big losses — the favorite-fade fat tail). This reports BOTH,
per category, from venue truth (the data-api activity feed + open positions), so you
can see which categories are actually worth trading.

A "settled bet" = a token the bot took live and that has resolved or been closed:
  WIN  = net realized (sells + redemptions) > 0, or redeemed for a payout
  LOSS = net realized < 0, or held to a worthless resolution (mark→0)
Genuinely-open positions (still trading) are EXCLUDED from win-rate (not yet decided).
Category comes from the local Market table, joined by token id (yes/no_token_id).

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

from scripts.live_pnl import (
    _f,
    _fetch_activity,
    _fetch_positions,
    _norm,
    realized_from_activity,
)
from services.executor.equity_guard import _deposit_wallet

_EPS = 0.01            # ignore sub-cent realized noise (cancels, dust)
_RESOLVED_MARK = 0.02  # held leg at/under this implied price + ~no value = decided


def bet_outcome(net: float, eps: float = _EPS) -> str:
    """WIN / LOSS / FLAT from a settled bet's net realized P&L. Pure / tested."""
    if net > eps:
        return "win"
    if net < -eps:
        return "loss"
    return "flat"


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


async def _token_category_map() -> dict[str, str | None]:
    """token_id -> category, from the local Market table (both YES and NO tokens)."""
    async with session_scope() as s:
        rows = (await s.execute(
            select(Market.yes_token_id, Market.no_token_id, Market.category)
        )).all()
    m: dict[str, str | None] = {}
    for yt, nt, cat in rows:
        if yt:
            m[str(yt)] = cat
        if nt:
            m[str(nt)] = cat
    return m


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

    events = [_norm(e) for e in raw_activity]
    book = realized_from_activity(events)
    tok2cat = await _token_category_map()

    items: list[tuple[str, str, float]] = []
    seen: set[str] = set()
    unknown_cat = 0

    # Settled-by-trade: any token with realized sells/redemptions.
    for token, s in book.items():
        if s["sold_shares"] <= 1e-6 and s["redeemed_shares"] <= 1e-6:
            continue
        net = s["sold_realized"] + s["redeemed_realized"]
        cat = tok2cat.get(token)
        if cat is None:
            unknown_cat += 1
        items.append((cat or "unknown", bet_outcome(net), net))
        seen.add(token)

    # Resolved-against-us held legs (never sold — expired worthless) = losses.
    open_excluded = 0
    for p in raw_positions:
        if _f(p.get("size")) <= 0:
            continue
        asset = str(p.get("asset") or "")
        if asset in seen:
            continue
        mark, val, upnl = _f(p.get("curPrice")), _f(p.get("currentValue")), _f(p.get("cashPnl"))
        decided = bool(p.get("redeemable")) or (mark <= _RESOLVED_MARK and val <= 0.01)
        if decided and upnl < 0:
            cat = tok2cat.get(asset)
            if cat is None:
                unknown_cat += 1
            items.append((cat or "unknown", "loss", upnl))
            seen.add(asset)
        else:
            open_excluded += 1

    agg = aggregate_by_category(items)
    if not agg:
        print("No settled live bets found in the activity window.")
        return

    rows = sorted(agg.items(), key=lambda kv: kv[1]["net"])   # worst net first
    print(f"\n{'='*82}")
    print(f"PER-CATEGORY WIN-RATE & NET P&L  (settled live bets)   wallet={wallet[:10]}…")
    print('='*82)
    print(f"{'category':<16}{'bets':>6}{'win':>6}{'loss':>6}{'win-rate':>10}{'net $':>12}  verdict")
    print('-'*82)
    tot_w = tot_l = 0
    tot_net = 0.0
    for cat, c in rows:
        wr = c["winrate"]
        wr_s = f"{wr*100:.0f}%" if wr is not None else "n/a"
        prof = c["net"] > 0
        pos_wr = wr is not None and wr > 0.5
        verdict = ("PROFITABLE" if prof else "loses $") + ("" if pos_wr == prof else
                   "  ⚠ +WR but unprofitable" if pos_wr and not prof else "")
        print(f"{cat[:15]:<16}{c['n']:>6}{c['win']:>6}{c['loss']:>6}{wr_s:>10}"
              f"{c['net']:>+12.2f}  {verdict}")
        tot_w += c["win"]
        tot_l += c["loss"]
        tot_net += c["net"]
    print('-'*82)
    tot_wr = (tot_w / (tot_w + tot_l)) if (tot_w + tot_l) else 0.0
    print(f"{'ALL':<16}{tot_w+tot_l:>6}{tot_w:>6}{tot_l:>6}{tot_wr*100:>9.0f}%{tot_net:>+12.2f}")

    # ── direct answers ────────────────────────────────────────────────────────
    profitable = [c for c, v in rows if v["net"] > 0]
    pos_wr = [c for c, v in rows if v["winrate"] is not None and v["winrate"] > 0.5]
    pos_wr_but_loss = [c for c, v in rows
                       if v["winrate"] is not None and v["winrate"] > 0.5 and v["net"] <= 0]
    print(f"\n{'='*82}")
    print(f"Positive win-rate (>50%): {', '.join(pos_wr) if pos_wr else 'none'}")
    print(f"Actually PROFITABLE (net>$0): {', '.join(profitable) if profitable else 'NONE'}")
    if pos_wr_but_loss:
        print(f"⚠ Win most bets but LOSE money (fat tail): {', '.join(pos_wr_but_loss)}")
    print('='*82)
    notes = []
    if open_excluded:
        notes.append(f"{open_excluded} still-open position(s) excluded (not yet decided)")
    if unknown_cat:
        notes.append(f"{unknown_cat} bet(s) had no category in the local Market table")
    if hit_cap:
        notes.append("hit the activity scan cap — older bets may be missing")
    if notes:
        print("\nnote: " + "; ".join(notes) + ".")
    print("Win-rate counts settled bets; profitability is net realized $ (venue truth).\n"
          "A high win-rate with negative net = many small wins, rare big losses.")


if __name__ == "__main__":
    asyncio.run(main())
