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
# A held leg at/under this implied price with ~no market value is a market that
# resolved AGAINST us — a decided loss, not open risk.
_RESOLVED_MARK = 0.02


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


def cashflow_totals(events: list[dict]) -> dict:
    """Total cash IN / OUT across all activity, double-count-proof.

    Account PnL doesn't need buys matched to their sells/redeems: every dollar
    that left for a BUY is cash out, every dollar from a SELL or REDEEM is cash
    in, and whatever's still held is current open value. So
        net = (sells + redeems + open_value) - buys
    holds regardless of how the venue keys redeems vs the original buy tokens
    (the mismatch that breaks per-token matching). Pure / unit tested."""
    buys = sells = redeems = 0.0
    for e in events:
        typ, side, usdc = e["type"], e["side"], e["usdc"]
        if usdc <= 0:
            continue
        if typ == "TRADE" and side == "BUY":
            buys += usdc
        elif typ == "TRADE" and side == "SELL":
            sells += usdc
        elif typ == "REDEEM":
            redeems += usdc
    return {"buys": buys, "sells": sells, "redeems": redeems}


def windowed_realized(events: list[dict], since_ts: int) -> dict:
    """Realized PnL booked by SELL vs REDEEM events at/after ``since_ts``.

    Replays the FULL history to carry the correct average cost into the window
    (a leg bought yesterday and sold today must realise against yesterday's
    cost), but only TALLIES realized for events inside the window. This is the
    causal split for "what changed recently": SELL realized = the active exit
    logic doing work (the old exit path was a no-op, so this is attributable to
    it); REDEEM realized = positions that merely settled on their own. Pure."""
    st: dict[str, dict] = {}
    sell_realized = redeem_realized = 0.0
    n_sells = n_redeems = 0
    for ev in sorted(events, key=lambda e: e["ts"]):
        asset = ev["asset"]
        if not asset:
            continue
        s = st.setdefault(asset, {"shares": 0.0, "cost": 0.0})
        typ, side, shares, usdc = ev["type"], ev["side"], ev["shares"], ev["usdc"]
        if shares <= 0:
            continue
        if typ == "TRADE" and side == "BUY":
            s["shares"] += shares
            s["cost"] += usdc
            continue
        is_sell = typ == "TRADE" and side == "SELL"
        if not (is_sell or typ == "REDEEM"):
            continue                                 # ignore SPLIT/MERGE/REWARD/etc.
        avg = (s["cost"] / s["shares"]) if s["shares"] > 1e-9 else 0.0
        removed = min(shares, s["shares"]) if s["shares"] > 0 else shares
        realized = usdc - avg * removed
        s["shares"] = max(0.0, s["shares"] - removed)
        s["cost"] = max(0.0, s["cost"] - avg * removed)
        if ev["ts"] >= since_ts:
            if is_sell:
                sell_realized += realized
                n_sells += 1
            else:
                redeem_realized += realized
                n_redeems += 1
    return {"sell_realized": sell_realized, "redeem_realized": redeem_realized,
            "n_sells": n_sells, "n_redeems": n_redeems}


async def _fetch_activity(client: DataClient, wallet: str) -> tuple[list[dict], bool]:
    """Returns (events, hit_cap). hit_cap=True means we stopped at _MAX_EVENTS
    with more history available — older BUYs may be missing, so the cashflow
    total would read optimistically and the caller should warn."""
    out: list[dict] = []
    offset = 0
    hit_cap = False
    while True:
        if len(out) >= _MAX_EVENTS:
            hit_cap = True
            break
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
    return out, hit_cap


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
        raw_activity, hit_cap = await _fetch_activity(client, wallet)
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

    # ── windowed attribution: what moved the account in the last N hours ─────
    if since_h > 0:
        w = windowed_realized(events, cutoff_ts)
        booked = w["sell_realized"] + w["redeem_realized"]
        print(f"\nLAST {since_h:g}h — what the bot actually BOOKED (realized cash)")
        print(f"  by SELLING  (the new exit logic at work):  ${w['sell_realized']:+.2f}"
              f"   over {w['n_sells']} close(s)")
        print(f"  by REDEEM   (positions settling on their own): ${w['redeem_realized']:+.2f}"
              f"   over {w['n_redeems']} settlement(s)")
        print(f"  {'-'*44}")
        print(f"  total realized in window:                  ${booked:+.2f}")
        print("  → SELLING is attributable to OUR changes (the old exit path never\n"
              "    fired). REDEEM + any further equity move is positions playing out\n"
              "    / open legs re-marking (unrealized drift), NOT the exit changes.")

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

    # ── bucket 3: positions still in the wallet ──────────────────────────────
    # Split GENUINELY-OPEN (still trading, has market value) from RESOLVED-
    # AGAINST-US (market settled, this side -> 0, shares now worthless but not
    # yet cleared). The data API flags both with size>0; the worthless ones show
    # curPrice~0 / value~0 (and usually redeemable). Lumping them as "unrealized"
    # (the bug in v1 of this script) makes decided losses look like open risk.
    live_open, resolved_lost = [], []
    open_value = open_upnl = resolved_loss = 0.0
    for p in raw_positions:
        sz = _f(p.get("size"))
        if sz <= 0:
            continue
        row = {
            "title": str(p.get("title") or p.get("slug") or "?"),
            "outcome": str(p.get("outcome") or ""),
            "size": sz, "avg": _f(p.get("avgPrice")), "mark": _f(p.get("curPrice")),
            "value": _f(p.get("currentValue")), "upnl": _f(p.get("cashPnl")),
            "redeemable": bool(p.get("redeemable")),
        }
        decided = row["redeemable"] or (row["mark"] <= _RESOLVED_MARK and row["value"] <= 0.01)
        if decided and row["upnl"] < 0:
            resolved_lost.append(row)
            resolved_loss += row["upnl"]
        else:
            live_open.append(row)
            open_value += row["value"]
            open_upnl += row["upnl"]

    live_open.sort(key=lambda x: x["upnl"])
    print(f"\n3) STILL OPEN — genuinely live, marked now (UNREALIZED)  —  {len(live_open)} leg(s)")
    if live_open:
        print(f"{'-'*84}")
        print(f"{'market':<44}{'shares':>8}{'avg':>7}{'mark':>7}{'value':>9}{'uPnL':>9}")
        for o in live_open:
            print(f"{_fmt_title(o['title'], o['outcome'], 44)}"
                  f"{o['size']:>8.1f}{o['avg']:>7.3f}{o['mark']:>7.3f}"
                  f"{o['value']:>9.2f}{o['upnl']:>+9.2f}")
        print(f"{'-'*84}")
        print(f"  NET UNREALIZED (open risk) = ${open_upnl:+.2f}   "
              f"(marked value ${open_value:.2f})")
    else:
        print("  (no genuinely-open positions)")

    # ── bucket 4: resolved against us (already-decided losses) ───────────────
    resolved_lost.sort(key=lambda x: x["upnl"])
    print(f"\n4) RESOLVED AGAINST US — already-decided losses (NOT open risk)  —  "
          f"{len(resolved_lost)} leg(s)")
    if resolved_lost:
        print(f"{'-'*84}")
        print(f"{'market':<52}{'shares':>8}{'cost/loss':>12}")
        for o in resolved_lost[:25]:
            print(f"{_fmt_title(o['title'], o['outcome'], 52)}"
                  f"{o['size']:>8.1f}{o['upnl']:>+12.2f}")
        if len(resolved_lost) > 25:
            print(f"     … and {len(resolved_lost) - 25} more")
        print(f"{'-'*84}")
        print(f"  REALIZED LOSS FROM RESOLVED POSITIONS = ${resolved_loss:+.2f}")
    else:
        print("  (none)")

    # ── grand total: clean all-cashflow method (double-count-proof) ──────────
    cf = cashflow_totals(events)
    net = cf["sells"] + cf["redeems"] + open_value - cf["buys"]
    oldest = min((e["ts"] for e in events), default=0)
    print(f"\n{'='*84}")
    print("BOTTOM LINE  (cash-flow method — immune to the redeem/buy keying issue)")
    print(f"  cash OUT (buys)              -${cf['buys']:.2f}")
    print(f"  cash IN  (sells)            +${cf['sells']:.2f}")
    print(f"  cash IN  (redeems/settle)   +${cf['redeems']:.2f}")
    print(f"  value of still-open legs    +${open_value:.2f}")
    print(f"  {'-'*44}")
    print(f"  NET P&L on traded capital    ${net:+.2f}")
    print('='*84)
    if hit_cap:
        print(f"\n⚠  Hit the {_MAX_EVENTS}-event scan cap — older BUYs may be missing, so the\n"
              "   NET above reads OPTIMISTICALLY. Raise _MAX_EVENTS for a full-history total.")
    print(f"\n{len(events)} activity events scanned"
          + (f"; oldest ts={oldest}" if oldest else "") + ".")
    print("Buckets 1–4 are detail (note: the venue keys REDEEM under a different token\n"
          "than the original BUY, so bucket-2 wins can also surface as a buy with no\n"
          "matching sell — the cash-flow BOTTOM LINE avoids that and is authoritative).")
    print("All figures are the venue's ACTUAL executions, not the local Fill ledger\n"
          "(which records quoted prices and is never reconciled in live mode).")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
