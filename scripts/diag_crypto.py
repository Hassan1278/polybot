"""diag_crypto.py — why aren't crypto signals firing?

Runs on the VPS (needs network + DB). Inspects BOTH sides of the crypto path:

  A) INPUT (our DB): recent Trade rows on crypto markets by our TRACKED
     wallets, and whether any crypto market has >=2 tracked wallets in the
     15-min correlation window — i.e. a cluster the engine SHOULD turn into a
     signal. The correlation loop only ever sees this table.

  B) LIVE (Polymarket): the busiest live crypto markets (the "daily crypto
     bets") and their recent distinct traders, cross-referenced against our
     tracked roster — to see whether the people actually trading crypto are
     even in our wallet set.

Verdict:
  - B busy (many traders) but 'tracked' ~0  -> DISCOVERY GAP: we track the
    wrong / too few crypto wallets; the daily-crypto crowd isn't in our roster.
  - B shows >=2 tracked on a market but A's 15m clusters = 0 -> INGEST gap/lag
    (their trades aren't landing in our Trade table fast enough).
  - B crypto markets few / low volume -> genuinely QUIET (regime).
  - A has 15m clusters but no signals fired -> engine/gate bug (escalate).

Usage:
  docker compose -f docker-compose.yml -f docker-compose.prod.yml exec ingest \
      python -m scripts.diag_crypto
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, select

from polybot.clients import DataClient, GammaClient
from polybot.db import session_scope
from polybot.models import Market, Trade, Wallet

WINDOW_MIN = 15            # matches settings.correlation_window_minutes default
MIN_WALLETS = 2            # matches settings.correlation_min_wallets default
TOP_CRYPTO_MARKETS = 40
TRADES_PER_MARKET = 500

# Fallback crypto detector for markets the gamma 'crypto' tag misses.
_CRYPTO_KW = (
    "bitcoin", "btc", "ethereum", "eth", "solana", "crypto", "dogecoin", "xrp",
    "ripple", "cardano", "binance", "bnb", "coinbase", "memecoin", "altcoin",
    "litecoin", "microstrategy", "stablecoin",
)


def _is_crypto_text(m: dict) -> bool:
    text = f"{m.get('question','')} {m.get('slug','')}".lower()
    return any(k in text for k in _CRYPTO_KW)


async def _section_a() -> set[str]:
    now = datetime.now(tz=timezone.utc)
    cut_window = now - timedelta(minutes=WINDOW_MIN)
    cut_24h = now - timedelta(hours=24)
    async with session_scope() as s:
        crypto_mids = {r[0] for r in (await s.execute(
            select(Market.market_id).where(Market.category == "crypto")
        )).all()}
        n_tracked = len((await s.execute(
            select(Wallet.address).where(Wallet.is_active.is_(True))
        )).all())
        rows = (await s.execute(
            select(Trade.ts, Trade.wallet, Trade.market_id)
            .join(Wallet, Wallet.address == Trade.wallet)
            .where(and_(
                Wallet.is_active.is_(True),
                Trade.market_id.in_(crypto_mids) if crypto_mids else False,
                Trade.ts >= cut_24h,
            ))
        )).all() if crypto_mids else []

    by_24h: dict[str, set] = defaultdict(set)
    by_window: dict[str, set] = defaultdict(set)
    for ts, w, mid in rows:
        by_24h[mid].add(w)
        if ts >= cut_window:
            by_window[mid].add(w)
    clusters_24h = {m: ws for m, ws in by_24h.items() if len(ws) >= MIN_WALLETS}
    clusters_now = {m: ws for m, ws in by_window.items() if len(ws) >= MIN_WALLETS}

    print("=== A) OUR DB (what the correlation engine actually sees) ===")
    print(f"  tracked active wallets (all categories): {n_tracked}")
    print(f"  crypto markets classified in DB:         {len(crypto_mids)}")
    print(f"  tracked-wallet trades on crypto mkts (24h): {len(rows)}")
    print(f"  crypto markets w/ >=2 tracked wallets, 24h:        {len(clusters_24h)}")
    print(f"  crypto markets w/ >=2 tracked wallets, last {WINDOW_MIN}m: {len(clusters_now)}  <-- engine fires on this")
    return crypto_mids


async def _section_b(crypto_mids: set[str]) -> None:
    g = GammaClient()
    d = DataClient()
    try:
        async with session_scope() as s:
            tracked = {r[0].lower() for r in (await s.execute(
                select(Wallet.address).where(Wallet.is_active.is_(True))
            )).all()}

        # Primary: gamma 'crypto' tag. Fallback: global top markets filtered by
        # crypto keywords, in case the tag is sparse.
        tagged = await g.markets(limit=TOP_CRYPTO_MARKETS, tag="crypto",
                                 order="volume24hr", active=True, closed=False)
        glob = await g.markets(limit=150, order="volume24hr", active=True, closed=False)
        seen: set[str] = set()
        mkts: list[dict] = []
        for m in (tagged or []) + [x for x in (glob or []) if _is_crypto_text(x)]:
            cid = m.get("conditionId")
            if cid and cid not in seen:
                seen.add(cid)
                mkts.append(m)
        mkts = mkts[:TOP_CRYPTO_MARKETS]

        print("\n=== B) LIVE on Polymarket (the daily crypto bets) ===")
        print(f"  live crypto markets found: {len(mkts)}")
        if not mkts:
            print("  none — crypto genuinely dormant right now (regime).")
            return

        print(f"\n  {'24h_vol':>9} {'traders':>7} {'OURS':>5} {'inDB':>5}  market")
        live_clusters = 0
        any_ours = 0
        for m in mkts:
            cid = m.get("conditionId")
            q = (m.get("question") or "")[:46]
            vol = float(m.get("volume24hr") or 0)
            try:
                trades = await d.market_trades(cid, limit=TRADES_PER_MARKET)
            except Exception:
                trades = []
            traders, ours = set(), set()
            for t in trades:
                w = (t.get("proxyWallet") or "").lower()
                if not w:
                    continue
                traders.add(w)
                if w in tracked:
                    ours.add(w)
            if len(ours) >= MIN_WALLETS:
                live_clusters += 1
            if ours:
                any_ours += 1
            indb = "yes" if cid in crypto_mids else "NO"
            print(f"  {vol:>9.0f} {len(traders):>7} {len(ours):>5} {indb:>5}  {q}")
            await asyncio.sleep(0.05)

        print(f"\n  crypto markets where ANY of our wallets traded:   {any_ours}/{len(mkts)}")
        print(f"  crypto markets where >=2 of our wallets traded:   {live_clusters}/{len(mkts)}  <-- bot-fireable")
    finally:
        await g.close()
        await d.close()


async def main() -> None:
    crypto_mids = await _section_a()
    await _section_b(crypto_mids)
    print("\n=== READ ME ===")
    print("  B busy but OURS~0           -> DISCOVERY gap: daily-crypto wallets not in our roster")
    print("  B has >=2 OURS but A 15m=0  -> INGEST lag: their trades not in our Trade table yet")
    print("  B few/low-volume            -> REGIME: crypto just quiet this week")
    print("  A 15m>0 but no signals      -> engine/gate bug (tell the assistant)")


if __name__ == "__main__":
    asyncio.run(main())
