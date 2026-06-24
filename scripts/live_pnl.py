"""live_pnl.py — did the bot's buys and sells make or lose money? (venue truth)

The local Fill ledger CAN'T answer this: the live path is fire-and-forget, so a
Fill stores the price we *quoted* and a placement-time status that's never
reconciled to what actually executed (a "submitted" BUY may be resting or
filled). Summing Fills therefore mixes phantom resting orders in at quoted
prices — useless for PnL. (This is also why pnl_loop shows ~$0 realized in live
mode: it reads the Position table, which the live path never writes.)

This script ignores the local ledger and reads the wallet's ACTUAL executed
trades from the Polymarket data API (/activity) — the same source the venue and
the /live/account card trust. It does proper average-cost accounting per token
and splits the result into three honest buckets:

  1. Round-trips the bot CLOSED BY SELLING (BUY -> SELL): the literal
     "bot sells and buys" — each shown with avg in/out price and WIN/LOSS.
  2. Settlements (REDEEM): positions held to resolution and redeemed.
  3. Still-open positions: marked-to-market now (unrealized), from /positions.

Plus a reconciliation note for shares that were bought but later vanished
without a SELL or REDEEM — i.e. resolved worthless (a realized loss the activity
feed shows only as a disappearance).

Usage (on the VPS):
  docker compose -f docker-compose.yml -f docker-compose.prod.yml exec executor \
      python -m scripts.live_pnl
  # optional: only show round-trips/settlements since N hours ago
  docker compose ... exec executor python -m scripts.live_pnl 24
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from polybot.clients import DataClient

from services.executor.equity_guard import _deposit_wallet

# Activity is paged; pull until a short page or this many events (safety cap).
_PAGE = 500
_MAX_EVENTS = 8000


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _norm(e: dict) -> dict:
    """Normalise one /activity row into the fields we account on. Falls back to
    size*price when usdcSize is absent so a missing notional never reads as $0."""
    typ = str(e.get("type") or "").upper()
    side = str(e.get("side") or "").upper()
    shares = _f(e.get("size"))
    usdc = _f(e.get("usdcSize"))
    price = _f(e.get("price"))
    if usdc <= 0 and shares > 0 and price > 0:
        usdc = shares * price
    return {
        "type": typ,
        "side": side,
        "shares": shares,
        "usdc": usdc,
        "price": price,
        "asset": str(e.get("asset") or e.get("conditionId") or ""),
        "title": str(e.get("title") or e.get("slug") or e.get("conditionId") or "?"),
        "outcome": str(e.get("outcome") or ""),
        "ts": int(_f(e.get("timestamp"))),
    }


def realized_from_activity(events: list[dict]) -> dict[str, dict]:
    """Average-cost realized PnL per token from normalised activity rows.

    Walks events oldest-first maintaining (shares, cost) per token. A SELL or
    REDEEM realises proceeds against the average cost of the shares it removes:
    realized += proceeds - avg_cost * shares_removed. Pure (no I/O) so it's unit
    tested. Returns per-token aggregates keyed by token id."""
    st: dict[str, dict] = {}

    def _slot(asset: str, title: str, outcome: str) -> dict:
        s = st.get(asset)
        if s is None:
            s = st[asset] = {
                "title": title, "outcome": outcome,
                "shares": 0.0, "cost": 0.0,           # running open lot (avg cost)
                "sold_shares": 0.0, "sold_proceeds": 0.0, "sold_cost": 0.0,
                "redeemed_shares": 0.0, "redeemed_proceeds": 0.0, "redeemed_cost": 0.0,
                "n_buys": 0, "n_sells": 0, "n_redeems": 0,
            }
        return s

    for ev in sorted(events, key=lambda e: e["ts"]):
        asset = ev["asset"]
        if not asset:
            continue
        s = _slot(asset, ev["title"], ev["outcome"])
        typ, side, shares, usdc = ev["type"], ev["side"], ev["shares"], ev["usdc"]
        if shares <= 0:
            continue
        if typ == "TRADE" and side == "BUY":
            s["shares"] += shares
            s["cost"] += usdc
            s["n_buys"] += 1
        elif typ == "TRADE" and side == "SELL":
            avg = (s["cost"] / s["shares"]) if s["shares"] > 1e-9 else 0.0
            removed = min(shares, s["shares"]) if s["shares"] > 0 else shares
            cost_removed = avg * removed
            s["sold_shares"] += removed
            s["sold_proceeds"] += usdc
            s["sold_cost"] += cost_removed
            s["shares"] = max(0.0, s["shares"] - removed)
            s["cost"] = max(0.0, s["cost"] - cost_removed)
            s["n_sells"] += 1
        elif typ == "REDEEM":
            avg = (s["cost"] / s["shares"]) if s["shares"] > 1e-9 else 0.0
            removed = min(shares, s["shares"]) if s["shares"] > 0 else shares
            cost_removed = avg * removed
            s["redeemed_shares"] += removed
            s["redeemed_proceeds"] += usdc
            s["redeemed_cost"] += cost_removed
            s["shares"] = max(0.0, s["shares"] - removed)
            s["cost"] = max(0.0, s["cost"] - cost_removed)
            s["n_redeems"] += 1

    for s in st.values():
        s["sold_realized"] = s["sold_proceeds"] - s["sold_cost"]
        s["redeemed_realized"] = s["redeemed_proceeds"] - s["redeemed_cost"]
        s["avg_in"] = (s["sold_cost"] / s["sold_shares"]) if s["sold_shares"] > 1e-9 else 0.0
        s["avg_out"] = (s["sold_proceeds"] / s["sold_shares"]) if s["sold_shares"] > 1e-9 else 0.0
        s["open_shares"] = s["shares"]
        s["open_cost"] = s["cost"]
    return st


async def _fetch_activity(client: DataClient, wallet: str) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while len(out) < _MAX_EVENTS:
        try:
            rows = await client.activity(wallet, limit=_PAGE, offset=offset)
        except Exception as exc:  # noqa: BLE001
            print(f"  (activity fetch failed at offset {offset}: {exc})")
            break
        if not rows:
            break
        out.extend(r for r in rows if isinstance(r, dict))
        if len(rows) < _PAGE:
            break
        offset += _PAGE
    return out


async def _fetch_positions(client: DataClient, wallet: str) -> list[dict]:
    try:
        rows = await client.positions(wallet, limit=500, size_threshold=0.0)
    except Exception as exc:  # noqa: BLE001
        print(f"  (positions fetch failed: {exc})")
        return []
    return [r for r in rows if isinstance(r, dict)]


def _fmt_title(title: str, outcome: str, width: int) -> str:
    s = f"{title} ({outcome})" if outcome else title
    return s[:width].ljust(width)


async def main(argv: list[str]) -> None:
    since_h = _f(argv[0]) if argv else 0.0

    wallet = await _deposit_wallet()
    if not wallet:
        print("No deposit wallet configured (POLYMARKET_FUNDER_ADDRESS / active "
              "credential). Nothing to read.")
        return

    client = DataClient()
    try:
        raw_activity = await _fetch_activity(client, wallet)
        raw_positions = await _fetch_positions(client, wallet)
    finally:
        await client.close()

    events = [_norm(e) for e in raw_activity]
    # PnL accounting must see the FULL history (a window would orphan SELLs from
    # their BUYs); `since_h` only filters which closed legs we DISPLAY.
    cutoff_ts = 0
    if since_h > 0 and events:
        newest = max(e["ts"] for e in events)
        cutoff_ts = newest - int(since_h * 3600)

    book = realized_from_activity(events)
    open_assets = {str(p.get("asset") or "") for p in raw_positions if _f(p.get("size")) > 0}

    # ── bucket 1: round-trips the bot closed by SELLING ──────────────────────
    sold = [s for s in book.values() if s["sold_shares"] > 1e-6]
    if since_h > 0:
        last_sell = {}  # approximate display filter: keep legs with recent activity
        for ev in events:
            if ev["type"] == "TRADE" and ev["side"] == "SELL" and ev["ts"] >= cutoff_ts:
                last_sell[ev["asset"]] = True
        sold = [s for a, s in book.items() if a in last_sell]
    sold.sort(key=lambda s: s["sold_realized"])

    print(f"\n{'='*84}")
    print(f"LIVE REALIZED PnL  —  venue truth (data-api /activity)   wallet={wallet[:10]}…")
    if since_h > 0:
        print(f"(showing closed legs with activity in the last {since_h:g}h; "
              f"PnL still computed over full history)")
    print('='*84)

    print(f"\n1) ROUND-TRIPS THE BOT CLOSED BY SELLING  (BUY → SELL)  —  {len(sold)} leg(s)")
    if sold:
        print(f"{'-'*84}")
        print(f"{'market':<44}{'shares':>8}{'avg_in':>8}{'avg_out':>8}{'realized':>10}  W/L")
        wins = losses = 0
        net_sold = 0.0
        for s in sold:
            r = s["sold_realized"]
            net_sold += r
            wl = "WIN " if r > 0 else ("LOSS" if r < 0 else "flat")
            if r > 0:
                wins += 1
            elif r < 0:
                losses += 1
            print(f"{_fmt_title(s['title'], s['outcome'], 44)}"
                  f"{s['sold_shares']:>8.1f}{s['avg_in']:>8.3f}{s['avg_out']:>8.3f}"
                  f"{r:>+10.2f}  {wl}")
        n = wins + losses
        wr = (wins / n * 100.0) if n else 0.0
        print(f"{'-'*84}")
        print(f"  {wins} win / {losses} loss  ({wr:.0f}% win-rate)   "
              f"NET REALIZED ON SELLS = ${net_sold:+.2f}")
    else:
        print("  (none — the bot hasn't closed any position by selling yet)")

    # ── bucket 2: settlements (held to resolution, redeemed) ─────────────────
    redeemed = [s for s in book.values() if s["redeemed_shares"] > 1e-6]
    redeemed.sort(key=lambda s: s["redeemed_realized"])
    print(f"\n2) SETTLEMENTS  (held to resolution, REDEEM)  —  {len(redeemed)} leg(s)")
    if redeemed:
        print(f"{'-'*84}")
        print(f"{'market':<44}{'shares':>8}{'cost':>10}{'payout':>10}{'realized':>10}")
        net_red = 0.0
        for s in redeemed:
            r = s["redeemed_realized"]
            net_red += r
            print(f"{_fmt_title(s['title'], s['outcome'], 44)}"
                  f"{s['redeemed_shares']:>8.1f}{s['redeemed_cost']:>10.2f}"
                  f"{s['redeemed_proceeds']:>10.2f}{r:>+10.2f}")
        print(f"{'-'*84}")
        print(f"  NET SETTLED = ${net_red:+.2f}")
    else:
        print("  (none redeemed)")

    # ── bucket 3: still-open positions, marked now (unrealized) ──────────────
    opens = []
    net_unreal = 0.0
    for p in raw_positions:
        sz = _f(p.get("size"))
        if sz <= 0:
            continue
        cash_pnl = _f(p.get("cashPnl"))
        net_unreal += cash_pnl
        opens.append({
            "title": str(p.get("title") or p.get("slug") or "?"),
            "outcome": str(p.get("outcome") or ""),
            "size": sz, "avg": _f(p.get("avgPrice")), "mark": _f(p.get("curPrice")),
            "value": _f(p.get("currentValue")), "upnl": cash_pnl,
            "redeemable": bool(p.get("redeemable")),
        })
    opens.sort(key=lambda x: x["upnl"])
    print(f"\n3) STILL OPEN  (marked-to-market now, UNREALIZED)  —  {len(opens)} leg(s)")
    if opens:
        print(f"{'-'*84}")
        print(f"{'market':<42}{'shares':>8}{'avg':>7}{'mark':>7}{'value':>9}{'uPnL':>9}  flag")
        for o in opens:
            flag = "redeemable" if o["redeemable"] else ""
            print(f"{_fmt_title(o['title'], o['outcome'], 42)}"
                  f"{o['size']:>8.1f}{o['avg']:>7.3f}{o['mark']:>7.3f}"
                  f"{o['value']:>9.2f}{o['upnl']:>+9.2f}  {flag}")
        print(f"{'-'*84}")
        print(f"  NET UNREALIZED = ${net_unreal:+.2f}")
    else:
        print("  (no open positions)")

    # ── reconciliation: bought, then vanished with no SELL/REDEEM = lost ─────
    ghosts = [s for a, s in book.items()
              if s["open_shares"] > 1e-6 and a not in open_assets]
    ghost_loss = sum(s["open_cost"] for s in ghosts)
    if ghosts:
        print(f"\n⚠  {len(ghosts)} leg(s) were bought but are GONE with no sell/redeem "
              f"— resolved worthless.")
        print(f"   Estimated realized LOSS (cost basis written off): -${ghost_loss:.2f}")
        for s in sorted(ghosts, key=lambda x: -x["open_cost"])[:8]:
            print(f"     {_fmt_title(s['title'], s['outcome'], 50)}  "
                  f"{s['open_shares']:.1f} sh  cost ${s['open_cost']:.2f}")

    # ── grand total ──────────────────────────────────────────────────────────
    net_sold_all = sum(s["sold_realized"] for s in book.values())
    net_red_all = sum(s["redeemed_realized"] for s in book.values())
    total_realized = net_sold_all + net_red_all - ghost_loss
    print(f"\n{'='*84}")
    print(f"TOTAL  realized(sells) ${net_sold_all:+.2f}  |  settled ${net_red_all:+.2f}  |  "
          f"expired-loss ${-ghost_loss:+.2f}")
    print(f"       => NET REALIZED ${total_realized:+.2f}   "
          f"+ open/unrealized ${net_unreal:+.2f}   "
          f"= ${total_realized + net_unreal:+.2f}")
    print('='*84)
    print("\nNote: prices/PnL are the venue's ACTUAL executions, not the local Fill\n"
          "ledger (which records quoted prices and is never reconciled in live mode).")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
