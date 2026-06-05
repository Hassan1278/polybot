"""One-shot: cross-check our top winners/losers against Polymarket directly."""

import json
import sys

import requests


def main() -> None:
    positions = requests.get("http://api:8000/positions", timeout=10).json()
    winners = sorted(
        [p for p in positions if (p.get("mark_to_market_usdc") or 0) > 5],
        key=lambda p: -p["mark_to_market_usdc"],
    )[:6]
    losers = sorted(
        [p for p in positions if (p.get("mark_to_market_usdc") or 0) < -3],
        key=lambda p: p["mark_to_market_usdc"],
    )[:5]

    def check(p, label):
        mid = p["market_id"]
        print(
            f'\n── {label}: {p["outcome"][:28]:28s}  paid {p["avg_price"]:.3f}  '
            f'mark {p.get("mark_price")}  MTM ${p.get("mark_to_market_usdc"):+.2f}'
        )
        print(f'   market_id: {mid[:18]}...')
        for q in ("closed=true", "closed=false"):
            try:
                r = requests.get(
                    f"https://gamma-api.polymarket.com/markets?condition_ids={mid}&{q}",
                    timeout=8,
                ).json()
            except Exception as e:
                print(f"   gamma {q}: ERR {e}")
                continue
            if not r:
                continue
            m = r[0]
            outcomes_raw = m.get("outcomes") or "[]"
            prices_raw = m.get("outcomePrices") or "[]"
            try:
                outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                prices = [
                    float(x)
                    for x in (json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw)
                ]
            except Exception:
                outcomes, prices = [], []
            winner = None
            if prices and outcomes:
                wi = max(range(len(prices)), key=lambda i: prices[i])
                winner = outcomes[wi] if wi < len(outcomes) else None
            print(
                f'   gamma  closed={m.get("closed")}  closedTime={m.get("closedTime")}'
            )
            print(f'     Q: {(m.get("question") or "")[:75]}')
            print(f'     outcomes: {outcomes}')
            print(f'     prices:   {prices}')
            print(f'     winner:   {winner!r}')
            print(f'     bot bet:  {p["outcome"]!r}')
            if winner is not None and winner.upper() == p["outcome"].upper():
                print(f'     VERDICT:  BOT RIGHT')
            elif winner is not None:
                print(f'     VERDICT:  BOT WRONG ! ! !')
            else:
                print(f'     VERDICT:  NO WINNER YET (still open)')
            return
        print("   gamma: market not found in either filter")

    for p in winners:
        check(p, "WIN ")
    print("\n" + "=" * 70)
    for p in losers:
        check(p, "LOSE")


if __name__ == "__main__":
    main()
