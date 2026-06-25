"""side_bias.py — where does the YES/NO lean come from? (input → signals → fills)

The bot enters weather (and other) markets almost always on NO. These markets
are ~coin-flips (YES at 40–60c), so it's NOT a longshot-fade. This diagnostic
follows the side bias through the three stages of the pipeline to localize where
it's introduced — DB-only, no network:

  LAYER 1  sharp trade flow      (Trade ⋈ active Wallet ⋈ Market) — the INPUT the
                                  clusterer sees: what side are the tracked sharps
                                  actually trading?
  LAYER 2  signals generated     (Signal ⋈ Market) — the clustering/consensus
                                  OUTPUT: what side did the bot decide to act on?
  LAYER 3  live entries          (Fill mode=live, BUY ⋈ Market) — what the bot
                                  actually bought.

Reading it:
  * NO-lean already high in LAYER 1  → the tracked sharps themselves lean NO; the
    bot is mirroring real flow (a who-you-follow problem, not a bug).
  * LAYER 1 balanced but LAYER 2 NO-heavy → the CLUSTERING introduces it
    (NO trades cluster; scattered YES never reaches min_wallets).
  * LAYER 2 balanced but LAYER 3 NO-heavy → the skew enters at execution/gating.

Usage (on the VPS):
  docker compose -f docker-compose.yml -f docker-compose.prod.yml exec executor \
      python -m scripts.side_bias                # default: weather, last 14d
  docker compose ... exec executor python -m scripts.side_bias crypto 7
  docker compose ... exec executor python -m scripts.side_bias all 30
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from datetime import datetime, timedelta, timezone

from polybot.db import session_scope
from polybot.models import Fill, Market, Signal, Trade, Wallet
from sqlalchemy import distinct, func, select

_OPEN = ("filled", "submitted", "partial")


def _norm_outcome(o: str | None) -> str:
    """Collapse an outcome label to YES / NO / OTHER (multi-outcome markets)."""
    s = str(o or "").strip().upper()
    if s == "YES":
        return "YES"
    if s == "NO":
        return "NO"
    return "OTHER"


def no_pct(yes: float, no: float) -> float | None:
    """NO share of the YES+NO total, or None when there's no YES/NO volume."""
    tot = yes + no
    return (no / tot) if tot > 0 else None


def locate_skew(flow_no: float | None, sig_no: float | None,
                fill_no: float | None) -> str:
    """Pure verdict: which stage introduces the NO-lean. Each arg is a NO-fraction
    in [0,1] (by the headline measure) or None when that stage had no YES/NO data."""
    if fill_no is None:
        return ("No live YES/NO entries in range — can't assess the entry bias. "
                "Widen the window or pick an active category.")
    if fill_no < 0.55:
        return (f"Entries aren't materially NO-leaning (NO={fill_no:.0%}). "
                "Nothing to localize — the bias may be category- or window-specific.")
    stages = [
        ("sharp BUY flow (input)", flow_no),
        ("signal generation (clustering)", sig_no),
        ("entry execution (gates/fills)", fill_no),
    ]
    avail = [(name, v) for name, v in stages if v is not None]
    if len(avail) == 1:
        return (f"Only entry data available (NO={fill_no:.0%}); no upstream sharp-flow "
                "or signal data in range to trace where it enters. Widen the window.")
    first_name, first_v = avail[0]
    if first_v >= 0.60:
        return (f"The NO-lean is ALREADY in {first_name} (NO={first_v:.0%}) and carries "
                "through — the bot is mirroring real sharp flow, not creating it. "
                "A who-you-follow problem, not a pipeline bug.")
    prev, prev_name = first_v, first_name
    for name, v in avail[1:]:
        if v - prev >= 0.12:                     # a material jump enters here
            return (f"The NO-lean ENTERS at {name}: NO goes {prev:.0%} → {v:.0%} "
                    f"(was {prev_name}). That stage is the culprit.")
        prev, prev_name = v, name
    return (f"The NO-lean accumulates gradually (no single >12pt jump): first stage "
            f"NO={first_v:.0%} → entries {fill_no:.0%}. Diffuse, no single culprit.")


async def _layer1_flow(s, cat: str | None, cutoff) -> dict:
    """Active-wallet trade flow by (outcome, side): count, notional, distinct wallets."""
    q = (select(Trade.outcome, Trade.side,
                func.count(), func.coalesce(func.sum(Trade.notional_usdc), 0.0),
                func.count(distinct(Trade.wallet)))
         .join(Wallet, Wallet.address == Trade.wallet)
         .join(Market, Market.market_id == Trade.market_id)
         .where(Wallet.is_active.is_(True), Trade.ts >= cutoff)
         .group_by(Trade.outcome, Trade.side))
    if cat:
        q = q.where(Market.category == cat)
    rows = (await s.execute(q)).all()
    agg: dict = {}
    for outcome, side, n, notional, wallets in rows:
        key = (_norm_outcome(outcome), str(side or "").upper())
        c = agg.setdefault(key, {"n": 0, "notional": 0.0, "wallets": 0})
        c["n"] += int(n)
        c["notional"] += float(notional or 0.0)
        c["wallets"] += int(wallets)            # approx (distinct per group)
    return agg


async def _layer2_signals(s, cat: str | None, cutoff) -> dict:
    q = (select(Signal.outcome, Signal.side, Signal.gate_pass, func.count())
         .join(Market, Market.market_id == Signal.market_id)
         .where(Signal.ts >= cutoff)
         .group_by(Signal.outcome, Signal.side, Signal.gate_pass))
    if cat:
        q = q.where(Market.category == cat)
    rows = (await s.execute(q)).all()
    agg: dict = {}
    for outcome, side, gate_pass, n in rows:
        key = (_norm_outcome(outcome), str(side or "").upper(), bool(gate_pass))
        agg[key] = agg.get(key, 0) + int(n)
    return agg


async def _layer3_fills(s, cat: str | None, cutoff) -> dict:
    q = (select(Fill.outcome, func.count(), func.coalesce(func.sum(Fill.notional_usdc), 0.0))
         .join(Market, Market.market_id == Fill.market_id)
         .where(Fill.mode == "live", Fill.side == "BUY",
                Fill.status.in_(_OPEN), Fill.ts >= cutoff)
         .group_by(Fill.outcome))
    if cat:
        q = q.where(Market.category == cat)
    rows = (await s.execute(q)).all()
    agg: dict = {}
    for outcome, n, notional in rows:
        oc = _norm_outcome(outcome)
        c = agg.setdefault(oc, {"n": 0, "notional": 0.0})
        c["n"] += int(n)
        c["notional"] += float(notional or 0.0)
    return agg


def _yn(agg: dict, field: str, side: str | None = None) -> tuple[float, float]:
    """Sum `field` for YES and NO (optionally filtered to one side). Keys are
    either (outcome, side[, ...]) tuples or bare outcome strings."""
    yes = no = 0.0
    for key, val in agg.items():
        oc = key[0] if isinstance(key, tuple) else key
        if side is not None and (not isinstance(key, tuple) or key[1] != side):
            continue
        v = val if isinstance(val, (int, float)) else val.get(field, 0)
        if oc == "YES":
            yes += float(v)
        elif oc == "NO":
            no += float(v)
    return yes, no


def _line(label: str, yes: float, no: float, unit: str = "") -> str:
    p = no_pct(yes, no)
    pct = f"NO {p:.0%}" if p is not None else "no YES/NO data"
    return f"  {label:<26} YES {yes:>10,.0f}{unit}  |  NO {no:>10,.0f}{unit}   → {pct}"


async def main(argv: list[str]) -> None:
    cat_arg = (argv[0].lower() if argv else "weather")
    cat = None if cat_arg == "all" else cat_arg
    days = 14.0
    if len(argv) > 1:
        with contextlib.suppress(ValueError):
            days = float(argv[1])
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)

    async with session_scope() as s:
        l1 = await _layer1_flow(s, cat, cutoff)
        l2 = await _layer2_signals(s, cat, cutoff)
        l3 = await _layer3_fills(s, cat, cutoff)

    print(f"\n{'='*78}")
    print(f"SIDE BIAS — where does the YES/NO lean enter?   "
          f"category={cat or 'ALL'}  last {days:g}d")
    print('='*78)

    # LAYER 1 — sharp flow (the clustering input). BUY is what entries mirror.
    print("\nLAYER 1 — sharp trade flow  (active wallets — the clustering INPUT)")
    by_n = _yn(l1, "notional", "BUY")
    by_c = _yn(l1, "n", "BUY")
    print(_line("BUY by notional", *by_n, unit="$"))
    print(_line("BUY by trade count", *by_c))
    s_yes, s_no = _yn(l1, "notional", "SELL")
    print(_line("(SELL by notional, ctx)", s_yes, s_no, unit="$"))
    flow_no = no_pct(*by_n)
    if flow_no is None:
        flow_no = no_pct(*by_c)            # fall back to count if no notional

    # LAYER 2 — signals generated (clustering output). Focus on BUY entries.
    print("\nLAYER 2 — signals generated  (clustering / consensus OUTPUT, side=BUY)")
    buy_all = {k: v for k, v in l2.items() if k[1] == "BUY"}
    g_yes = sum(v for k, v in buy_all.items() if k[0] == "YES")
    g_no = sum(v for k, v in buy_all.items() if k[0] == "NO")
    print(_line("all BUY signals", g_yes, g_no))
    p_yes = sum(v for k, v in buy_all.items() if k[0] == "YES" and k[2])
    p_no = sum(v for k, v in buy_all.items() if k[0] == "NO" and k[2])
    print(_line("gate-PASSED BUY signals", p_yes, p_no))
    sig_no = no_pct(p_yes, p_no)
    if sig_no is None:
        sig_no = no_pct(g_yes, g_no)

    # LAYER 3 — live entries actually placed.
    print("\nLAYER 3 — live entries placed  (Fill mode=live, side=BUY)")
    f_yes, f_no = _yn(l3, "notional")
    fc_yes, fc_no = _yn(l3, "n")
    print(_line("entries by notional", f_yes, f_no, unit="$"))
    print(_line("entries by count", fc_yes, fc_no))
    fill_no = no_pct(f_yes, f_no)
    if fill_no is None:
        fill_no = no_pct(fc_yes, fc_no)

    other = l3.get("OTHER", {}).get("n", 0)
    if other:
        print(f"  (+{other} multi-outcome entries excluded from YES/NO)")

    print(f"\n{'='*78}")
    print(f"VERDICT: {locate_skew(flow_no, sig_no, fill_no)}")
    print('='*78)
    print("\nNO% at each stage — input → clustering → entries:")
    def _f(x):
        return f"{x:.0%}" if x is not None else "n/a"
    print(f"  sharp BUY flow {_f(flow_no)}   →   signals {_f(sig_no)}   →   "
          f"entries {_f(fill_no)}")
    print("\n(SELL-YES flow ≈ bearish-YES sentiment; shown as context. All DB-sourced,\n"
          "active wallets only — the same population the clusterer uses.)")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
