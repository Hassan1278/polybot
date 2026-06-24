"""exit_quality.py — did the bot's EXITS add value, or did it sell too early?

Answers "did we flip a profit because the exits followed smart money OUT (a
real scalp), or because the positions were just good and we sold too early?".

For every position the bot closed by SELLING, it compares our sell price to what
that same token is worth NOW — best_mark = live midpoint, or for a settled
market the resolution-reveal last trade (~0.999 if it won, ~0.001 if it lost).
The counterfactual edge of having sold vs simply holding is:

    exit_edge = (avg_out - mark_now) * shares_sold

  > 0  selling BEAT holding  (the leg fell / went on to lose)   -> the scalp worked
  < 0  holding would have paid MORE (the leg kept rising / won)  -> sold too early

Sum across all closes:
  * net edge > 0  -> the EXITS made the money (timing added value)
  * net edge < 0  -> the POSITIONS made the money; the bot left it on the table

Usage (on the VPS):
  docker compose -f docker-compose.yml -f docker-compose.prod.yml exec executor \
      python -m scripts.exit_quality
  # only sells from the last N hours:
  docker compose ... exec executor python -m scripts.exit_quality 24
"""

from __future__ import annotations

import asyncio
import sys

from polybot.clients import ClobClient

from scripts.live_pnl import DataClient as _DataClient
from scripts.live_pnl import (
    _f,
    _fetch_activity,
    _fmt_title,
    _norm,
    realized_from_activity,
)
from services.executor.equity_guard import _deposit_wallet

_MIN_SHARES = 1.0
_WON = 0.95          # mark at/above this = resolved (or near-certain) WIN
_LOST = 0.05         # mark at/below this = resolved (or near-dead) LOSS


def exit_assessment(avg_in: float, avg_out: float, mark: float, shares: float) -> dict:
    """Counterfactual of the exit vs holding to ``mark``. Pure / unit tested.

    realized = what selling booked; hold_pnl = what holding to mark would book;
    edge = realized - hold_pnl = (avg_out - mark) * shares (>0 selling won)."""
    realized = (avg_out - avg_in) * shares
    hold_pnl = (mark - avg_in) * shares
    return {"realized": realized, "hold_pnl": hold_pnl, "edge": realized - hold_pnl}


def outcome_label(mark: float) -> str:
    """Where the sold leg ended up: 'won' / 'lost' (resolved or near-certain),
    'live' (still trading), or 'unknown' (mark unavailable)."""
    if mark < 0:
        return "unknown"
    if mark >= _WON:
        return "won"
    if mark <= _LOST:
        return "lost"
    return "live"


async def main(argv: list[str]) -> None:
    since_h = _f(argv[0]) if argv else 0.0

    wallet = await _deposit_wallet()
    if not wallet:
        print("No deposit wallet configured. Nothing to read.")
        return

    dclient = _DataClient()
    try:
        raw, _hit_cap = await _fetch_activity(dclient, wallet)
    finally:
        await dclient.close()

    events = [_norm(e) for e in raw]
    cutoff = 0
    if since_h > 0 and events:
        cutoff = max(e["ts"] for e in events) - int(since_h * 3600)

    book = realized_from_activity(events)
    sold_assets = {a for a, s in book.items() if s["sold_shares"] > _MIN_SHARES}
    if since_h > 0:
        recent = {ev["asset"] for ev in events
                  if ev["type"] == "TRADE" and ev["side"] == "SELL" and ev["ts"] >= cutoff}
        sold_assets &= recent

    if not sold_assets:
        print("No closed-by-selling legs in range.")
        return

    # Mark every sold token NOW (resolved -> ~0/1, still trading -> live prob).
    clob = ClobClient()
    legs = []
    try:
        for a in sorted(sold_assets):
            s = book[a]
            try:
                mark = await clob.best_mark(a)
            except Exception:  # noqa: BLE001
                mark = -1.0
            a_in, a_out, sh = s["avg_in"], s["avg_out"], s["sold_shares"]
            asm = exit_assessment(a_in, a_out, mark, sh)
            legs.append({
                "title": s["title"], "outcome": s["outcome"], "shares": sh,
                "avg_in": a_in, "avg_out": a_out, "mark": mark,
                "label": outcome_label(mark), **asm,
            })
    finally:
        await clob.close()

    legs.sort(key=lambda x: x["edge"])           # most "sold too early" first

    print(f"\n{'='*92}")
    print("EXIT QUALITY — did selling beat holding?   (edge = (sold_price - mark_now) * shares)")
    if since_h > 0:
        print(f"(sells in the last {since_h:g}h)")
    print('='*92)
    print(f"{'market':<40}{'shares':>7}{'in':>6}{'sold':>6}{'now':>6}{'end':>7}"
          f"{'realized':>10}{'edge':>9}")
    print('-'*92)
    for x in legs:
        mk = f"{x['mark']:.3f}" if x["mark"] >= 0 else "  ?  "
        print(f"{_fmt_title(x['title'], x['outcome'], 40)}{x['shares']:>7.1f}"
              f"{x['avg_in']:>6.2f}{x['avg_out']:>6.2f}{mk:>6}{x['label']:>7}"
              f"{x['realized']:>+10.2f}{x['edge']:>+9.2f}")

    realized_total = sum(x["realized"] for x in legs)
    edge_total = sum(x["edge"] for x in legs if x["mark"] >= 0)
    hold_total = sum(x["hold_pnl"] for x in legs if x["mark"] >= 0)
    left_on_table = sum(x["edge"] for x in legs if x["mark"] >= 0 and x["edge"] < 0)
    saved = sum(x["edge"] for x in legs if x["mark"] >= 0 and x["edge"] > 0)
    n_premature = sum(1 for x in legs if x["mark"] >= 0 and x["edge"] < 0)
    n_good = sum(1 for x in legs if x["mark"] >= 0 and x["edge"] > 0)
    n_unknown = sum(1 for x in legs if x["mark"] < 0)
    n_zero = sum(1 for x in legs if 0.0 <= x["mark"] <= 1e-9)

    # The killer case to expose: legs cut at a LOSS that then went on to WIN.
    cut_loss_that_won = [x for x in legs if x["label"] == "won" and x["avg_out"] <= x["avg_in"]]

    print('-'*92)
    print(f"  legs assessed: {len(legs)}  (won={sum(1 for x in legs if x['label']=='won')} "
          f"lost={sum(1 for x in legs if x['label']=='lost')} "
          f"live={sum(1 for x in legs if x['label']=='live')} unknown={n_unknown})")
    print(f"\n  what SELLING booked (realized):           ${realized_total:+.2f}")
    print(f"  what HOLDING-to-now would have booked:    ${hold_total:+.2f}")
    print(f"  {'-'*52}")
    print(f"  EXIT EDGE (selling - holding):            ${edge_total:+.2f}")
    print(f"     ├─ money the exits SAVED  ({n_good} legs that fell after we sold): ${saved:+.2f}")
    print(f"     └─ money LEFT ON TABLE   ({n_premature} legs that rose after we sold): ${left_on_table:+.2f}")
    if cut_loss_that_won:
        worst = sum(x["edge"] for x in cut_loss_that_won)
        print(f"\n  ⚠ {len(cut_loss_that_won)} leg(s) were CUT AT A LOSS but went on to WIN "
              f"(panic-sold winners): ${worst:+.2f}")
        for x in sorted(cut_loss_that_won, key=lambda y: y["edge"])[:5]:
            print(f"      {_fmt_title(x['title'], x['outcome'], 52)}  "
                  f"sold {x['avg_out']:.2f} → now {x['mark']:.2f}  edge ${x['edge']:+.2f}")
    if n_zero or n_unknown:
        print(f"\n  note: {n_zero} leg(s) marked exactly 0 (treated as resolved-to-0); "
              f"{n_unknown} had no mark (excluded from edge).")

    # ── verdict ──────────────────────────────────────────────────────────────
    print(f"\n{'='*92}")
    if abs(edge_total) < 0.15 * max(abs(realized_total), 1.0):
        verdict = ("WASH — the exits roughly matched holding. The profit is the POSITIONS, "
                   "not the timing.")
    elif edge_total < 0:
        verdict = (f"SOLD TOO EARLY — holding would have made ${-edge_total:.2f} MORE. "
                   "The positions worked out on their own; the exits left money on the table.")
    else:
        verdict = (f"THE SCALP WORKED — selling beat holding by ${edge_total:.2f}. "
                   "The exits got out before reversals; the timing added value.")
    print(f"VERDICT: {verdict}")
    print('='*92)
    print("\nmark_now = best_mark (live midpoint, or the resolution last-trade ~0.999/~0.001\n"
          "for settled markets). 'live' legs are provisional — their mark can still move.")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
