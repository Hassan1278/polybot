"""weather_paper.py — forward paper run on weather ladders with REAL prices.

Backtests can only see historical mids; they can't see the spread you actually cross.
This logs the live order book (best bid/ask of every bucket) for each open weather ladder,
settles on the real resolved high, and then scores every candidate strategy from the
*real forward prices* — so "favorite-YES", "85c high-conviction", "fade rank-2", "fade
longshots" are all post-hoc cuts on the same captured data. No strategy is committed up
front; we just record reality and slice it.

Workflow (run on the VPS; the store persists between calls):
    # capture EVERY open ladder's real book — cron every 15 min to trace the price path:
    #   */15 * * * * cd ~/polybot && docker compose exec -T executor \
    #       python -m scripts.weather_paper snap >> ~/wpaper.log 2>&1
    docker compose exec -T executor python -m scripts.weather_paper snap
    # settle + scorecard + $2000 paper book (favorite-YES, tiny sizing, full breadth):
    docker compose exec -T executor python -m scripts.weather_paper report

NB: the store defaults to /tmp/weather_paper.json — fine while the container stays up;
point --store at a mounted volume if you rebuild mid-run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import time

from scripts.weather_pnl import parse_q
from scripts.weather_recon import is_weather

_STORE = "/tmp/weather_paper.json"


# ── pure core (unit-tested) ──────────────────────────────────────────────────


def best_bid_ask(book):
    """From a CLOB /book dict {'bids':[{price,size}],'asks':[...]}, return (bid, ask):
    highest bid, lowest ask. None for an empty side. Robust to ordering."""
    def _edge(levels, fn):
        ps = []
        for lv in levels or []:
            try:
                ps.append(float(lv["price"]))
            except (KeyError, TypeError, ValueError):
                continue
        return fn(ps) if ps else None
    return _edge(book.get("bids"), max), _edge(book.get("asks"), min)


def mid_of(bid, ask):
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return bid if bid is not None else ask


def rank_by_mid(buckets):
    """buckets: [{label, bid, ask, mid}]. Drop those without a mid; sort by mid desc."""
    return sorted((b for b in buckets if b.get("mid") is not None),
                  key=lambda b: b["mid"], reverse=True)


def yes_pnl(ask, won):
    """Buy YES at the ask, settle 1 if it won. P&L per $1 notional. None if no ask."""
    return None if ask is None else (1.0 if won else 0.0) - ask


def no_pnl(bid, won):
    """Buy NO by hitting the YES bid (NO costs 1−bid), settle 1 if YES LOST.
    P&L = (1−won) − (1−bid) = bid − won. None if no bid."""
    return None if bid is None else bid - (1.0 if won else 0.0)


def evaluate_ladder(buckets, winner_label, *, hi_conv=0.85, longshot_max=0.10):
    """Score the candidate strategies on ONE resolved ladder snapshot.
    buckets: [{label, bid, ask, mid}]; winner_label: the bucket that resolved YES.
    Returns {strategy: pnl_per_$1}. Strategies that don't apply are omitted."""
    ranked = rank_by_mid(buckets)
    if not ranked:
        return {}
    fav = ranked[0]
    out = {}
    fav_won = fav["label"] == winner_label
    p = yes_pnl(fav["ask"], fav_won)
    if p is not None:
        out["fav_yes"] = p
        if fav["mid"] >= hi_conv:
            out["fav_yes_85c"] = p          # the high-conviction subset
    if len(ranked) >= 2:                      # fade the 2nd-favorite (buy NO)
        r2 = ranked[1]
        q = no_pnl(r2["bid"], r2["label"] == winner_label)
        if q is not None:
            out["fade_rank2_no"] = q
    longs = [no_pnl(b["bid"], b["label"] == winner_label)
             for b in ranked if b["mid"] <= longshot_max]
    longs = [x for x in longs if x is not None]
    if longs:                                 # fade the cheap tail (avg over its buckets)
        out["fade_longshots_no"] = sum(longs) / len(longs)
    return out


def agg(pnls):
    """Mean P&L, 2·se, n over a list of per-bet P&Ls."""
    xs = [x for x in pnls if x is not None]
    n = len(xs)
    if not n:
        return {"n": 0}
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / (n - 1) if n > 1 else 0.0
    se = math.sqrt(var / n) if n > 1 else None
    return {"n": n, "mean": mean, "se": se}


def simulate_portfolio(bets, bankroll, bet_size):
    """Realistic paper book. bets: list of (ask, won) for the favorite entries. Each bet
    buys bet_size$ of YES at the real ask -> bet_size/ask shares, each worth $1 iff it won.
    Sizing is tiny vs bankroll (full breadth, hundreds of small bets), so no binding
    constraint is modeled — we roll up realized P&L, ROI on staked, and final equity."""
    staked = pnl = 0.0
    n = 0
    for ask, won in bets:
        if ask is None or ask <= 0:
            continue
        shares = bet_size / ask
        pnl += (shares if won else 0.0) - bet_size
        staked += bet_size
        n += 1
    return {"n": n, "staked": staked, "pnl": pnl,
            "roi": (pnl / staked) if staked else None, "final_equity": bankroll + pnl}


# ── store ──────────────────────────────────────────────────────────────────


def _load(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"snaps": []}


def _save(path, store):
    with open(path, "w") as f:
        json.dump(store, f)


# ── I/O ──────────────────────────────────────────────────────────────────────


async def _open_weather_ladders(within_hours):
    """Open weather ladders resolving within the window, grouped by (city,date,kind).
    Returns {key: [{label, cond_id, yes_token, end_ts}]}."""
    from collections import defaultdict
    from datetime import datetime, timedelta, timezone

    from polybot.db import session_scope
    from polybot.models import Market
    from sqlalchemy import select
    now = datetime.now(tz=timezone.utc)
    async with session_scope() as s:
        rows = (await s.execute(
            select(Market.market_id, Market.question, Market.yes_token_id, Market.end_date).where(
                Market.question.op("~*")(r"temperature"),
                Market.yes_token_id.is_not(None),
                Market.end_date.is_not(None),
                Market.end_date > now,
                Market.end_date <= now + timedelta(hours=within_hours),
            ))).all()
    ladders = defaultdict(list)
    for r in rows:
        if not is_weather(r.question):
            continue
        kind, city, bucket, date = parse_q(r.question)
        if not (city and date and bucket):
            continue
        ladders[f"{city}|{date}|{kind}"].append({
            "label": bucket, "cond_id": r.market_id, "yes_token": str(r.yes_token_id),
            "end_ts": int(r.end_date.timestamp())})
    return ladders


async def snap(store_path, within_hours, conc):
    from polybot.clients import ClobClient
    ladders = await _open_weather_ladders(within_hours)
    if not ladders:
        print("no open weather ladders in window")
        return
    n_buckets = sum(len(v) for v in ladders.values())
    print(f"snapshotting {len(ladders)} open ladders / {n_buckets} buckets at real bid/ask…")
    clob = ClobClient()
    sem = asyncio.Semaphore(conc)

    async def book(tok):
        async with sem:
            return await clob.book(tok)

    snap_ts = int(time.time())
    store = _load(store_path)
    try:
        new = 0
        for key, legs in ladders.items():
            books = await asyncio.gather(*[book(leg["yes_token"]) for leg in legs])
            buckets = []
            for leg, bk in zip(legs, books, strict=True):
                bid, ask = best_bid_ask(bk or {})
                buckets.append({"label": leg["label"], "cond_id": leg["cond_id"],
                                "bid": bid, "ask": ask, "mid": mid_of(bid, ask)})
            end_ts = legs[0]["end_ts"]
            store["snaps"].append({
                "key": key, "snap_ts": snap_ts, "end_ts": end_ts,
                "hrs_to_end": round((end_ts - snap_ts) / 3600, 1),
                "resolved": False, "winner": None, "buckets": buckets})
            new += 1
    finally:
        await clob.close()
        _save(store_path, store)
    print(f"recorded {new} ladder snapshots (store now {len(store['snaps'])} rows)")


async def _settle(store, conc):
    """Resolve matured, still-unsettled snapshots via gamma (batched condition_ids)."""
    from polybot.clients import GammaClient

    from scripts.weather_truth import _yes_won
    now = int(time.time())
    pending = [s for s in store["snaps"] if not s["resolved"] and s["end_ts"] < now]
    if not pending:
        return 0
    cids = sorted({b["cond_id"] for s in pending for b in s["buckets"]})
    g = GammaClient()
    sem = asyncio.Semaphore(conc)

    async def chunk(ids):
        async with sem:
            try:
                return await g.get("/markets", params={"condition_ids": list(ids), "closed": "true",
                                                        "limit": len(ids)}) or []
            except Exception:  # noqa: BLE001
                return []
    try:
        parts = await asyncio.gather(*[chunk(cids[i:i + 20]) for i in range(0, len(cids), 20)])
    finally:
        await g.close()
    won = {}
    for part in parts:
        for m in part:
            cid = (m.get("conditionId") or m.get("condition_id") or "").lower()
            if cid:
                won[cid] = _yes_won(m)
    settled = 0
    for s in pending:
        winner = next((b["label"] for b in s["buckets"] if won.get(b["cond_id"].lower()) is True), None)
        # only mark resolved once every bucket has a verdict (else a partial gamma read
        # could wrongly conclude "no winner" and lock in a bad settlement)
        verdicts = [won.get(b["cond_id"].lower()) for b in s["buckets"]]
        if all(v is not None for v in verdicts):
            s["resolved"], s["winner"], settled = True, winner, settled + 1
    return settled


async def report(store_path, conc, lead_hours, spread_tier, bankroll, bet_size):
    store = _load(store_path)
    settled = await _settle(store, conc)
    _save(store_path, store)
    done = [s for s in store["snaps"] if s["resolved"] and s["winner"]]
    print(f"settled {settled} newly; {len(done)} resolved ladders in store "
          f"(of {len(store['snaps'])} snapshots)\n")
    if not done:
        print("nothing resolved yet — run `snap` over a few days, then `report` again")
        return

    # per ladder-key, pick ONE snapshot: nearest to the target lead (for the price plays),
    # and the EARLIEST snapshot whose favorite crossed 85c (for the trigger play).
    from collections import defaultdict
    by_key = defaultdict(list)
    for s in done:
        by_key[s["key"]].append(s)

    rows = defaultdict(list)            # strategy -> [pnl]
    liq = defaultdict(lambda: defaultdict(list))   # tier -> strategy -> [pnl]
    port_bets = []                       # (entry_ts, ask, won) for the bankroll sim
    n85 = 0
    for _key, snaps in by_key.items():
        winner = snaps[0]["winner"]
        at_lead = min(snaps, key=lambda s: abs(s["hrs_to_end"] - lead_hours))
        ev = evaluate_ladder(at_lead["buckets"], winner)
        # 85c trigger: earliest snapshot whose favorite mid >= 0.85
        crossed = sorted((s for s in snaps if rank_by_mid(s["buckets"])
                          and rank_by_mid(s["buckets"])[0]["mid"] >= 0.85),
                         key=lambda s: s["snap_ts"])
        if crossed:
            n85 += 1
            ev85 = evaluate_ladder(crossed[0]["buckets"], winner)
            if "fav_yes" in ev85:
                rows["fav_yes_85c_trigger"].append(ev85["fav_yes"])
        fav = rank_by_mid(at_lead["buckets"])
        if fav and fav[0]["ask"] is not None:
            port_bets.append((at_lead["snap_ts"], fav[0]["ask"], fav[0]["label"] == winner))
        tier = "liquid (spread<=3c)" if (fav and fav[0]["ask"] is not None
               and fav[0]["bid"] is not None and (fav[0]["ask"] - fav[0]["bid"]) <= spread_tier) \
               else "illiquid (spread>3c)"
        for strat, pnl in ev.items():
            rows[strat].append(pnl)
            if strat == "fav_yes":
                liq[tier]["fav_yes"].append(pnl)

    def line(name, pnls):
        s = agg(pnls)
        if not s.get("n"):
            return
        twose = f"±{2 * s['se']:.3f}" if s["se"] else "(n=1)"
        verdict = ("+EV" if s["se"] and s["mean"] - 2 * s["se"] > 0
                   else "NEGATIVE" if s["se"] and s["mean"] + 2 * s["se"] < 0 else "noise")
        print(f"  {name:<24} P&L {s['mean']:+.3f}/$1 {twose:<9} (n={s['n']}) -> {verdict}")

    print(f"===== WEATHER PAPER SCORECARD (real prices, lead≈{lead_hours}h) =====")
    for strat in ("fav_yes", "fav_yes_85c", "fav_yes_85c_trigger", "fade_rank2_no", "fade_longshots_no"):
        line(strat, rows.get(strat, []))
    print(f"\n  favorites reaching 85c (any snapshot): {n85}/{len(by_key)} ladders")
    print("\n  favorite-YES by liquidity (real spread at entry):")
    for tier in ("liquid (spread<=3c)", "illiquid (spread>3c)"):
        line(tier, liq[tier]["fav_yes"])

    # how the favorite edge evolves as you enter closer to resolution (the "important hours")
    print("\n  favorite-YES by ENTRY lead (does entering later — sharper price — help?):")
    for lead in (24, 12, 6, 3, 1):
        pnls = []
        for _key, snaps in by_key.items():
            near = min(snaps, key=lambda s: abs(s["hrs_to_end"] - lead))
            if abs(near["hrs_to_end"] - lead) <= max(0.5 * lead, 1.5):
                ev = evaluate_ladder(near["buckets"], snaps[0]["winner"])
                if "fav_yes" in ev:
                    pnls.append(ev["fav_yes"])
        line(f"entry @{lead:>2}h ", pnls)

    # the actual $ book: favorite-YES, tiny size, full breadth
    port_bets.sort(key=lambda b: b[0])
    port = simulate_portfolio([(a, w) for _ts, a, w in port_bets], bankroll, bet_size)
    print(f"\n===== PAPER PORTFOLIO — favorite-YES, ${bet_size:.0f}/bet, ${bankroll:.0f} bankroll, "
          f"entry≈{lead_hours:.0f}h =====")
    if port["n"]:
        print(f"  bets {port['n']}  staked ${port['staked']:.0f}  realized P&L "
              f"${port['pnl']:+.2f}  ROI {port['roi']:+.1%}  ->  equity ${port['final_equity']:.2f}")
    print("\n  +EV needs mean-2se>0; expect a week to show SPREADS+mechanics, not significance.")


def main():
    for _n in ("httpx", "httpcore"):
        logging.getLogger(_n).setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description="Forward paper run on weather ladders with real prices")
    ap.add_argument("mode", choices=["snap", "report"])
    ap.add_argument("--store", default=_STORE)
    ap.add_argument("--within-hours", type=int, default=36, help="snap ladders resolving within N hours")
    ap.add_argument("--lead-hours", type=float, default=24.0, help="score at the snapshot nearest this lead")
    ap.add_argument("--spread-tier", type=float, default=0.03, help="liquid/illiquid split on favorite spread")
    ap.add_argument("--bankroll", type=float, default=2000.0, help="paper bankroll for the portfolio sim")
    ap.add_argument("--bet-size", type=float, default=5.0, help="$ staked per favorite bet (tiny vs bankroll)")
    ap.add_argument("--conc", type=int, default=8, help="concurrent CLOB/gamma calls")
    args = ap.parse_args()
    if args.mode == "snap":
        asyncio.run(snap(args.store, args.within_hours, args.conc))
    else:
        asyncio.run(report(args.store, args.conc, args.lead_hours, args.spread_tier,
                           args.bankroll, args.bet_size))


if __name__ == "__main__":
    main()
