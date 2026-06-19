"""Discover candidate wallets and persist the top scorers per category.

Pipeline:
  1. Read top-volume active markets from DB.
  2. For each market, pull recent trades → aggregate participants by traded
     notional. This gives a *broad* candidate pool of active wallets.
  3. For each candidate, pull `/positions` (ground-truth PnL) + DB trades.
     Score with `wallet_stats_from_positions`.
  4. Bucket by dominant market category, rank by a multi-signal composite,
     persist the top_n per category.

Ranking (per category) combines four normalised signals, each in roughly [0, 1]:
    pnl_signal    = tanh(realized_pnl / 5000)          # 5k USDC saturates
    wr_signal     = (win_rate - 0.5) * 2                # 0 at 50%, ±1 at extremes
    sharpe_signal = tanh(sharpe / 1.5)
    depth_signal  = tanh(n_decisions / 50)
    score = 0.40*pnl + 0.30*wr + 0.15*sharpe + 0.15*depth
Wallets with < 5 realised decisions, |realised PnL| < $50, or score <= 0 are
skipped. The composite intentionally de-emphasises pure volume so a high-WR
mid-volume sharp can out-rank a sloppy whale.
"""

from __future__ import annotations

import asyncio
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import desc, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from polybot.clients import DataClient
from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.models import Market, Wallet, WalletStats
from polybot.stats import wallet_stats_from_positions
from polybot.runtime_config import merged_categories

log = get_logger(__name__)

MARKETS_TO_SCAN = 80
# Also scan the top markets WITHIN each enabled category, so a category that's
# quiet on global volume (e.g. crypto during World Cup season) still gets its
# markets scanned and its sharps discovered. Without this the candidate pool
# skews entirely to whatever's hot and quieter categories starve. 80 per
# category surfaces a deep crypto/politics roster regardless of sports volume.
MARKETS_PER_CATEGORY = 80
TRADES_PER_MARKET = 500
MAX_CANDIDATES = 1500
PER_WALLET_DELAY = 0.05
MIN_REALIZED_DECISIONS = 5
MIN_REALIZED_PNL_USDC = 50.0   # at least $50 of realised PnL to be considered


async def _market_category_map(s) -> dict[str, str | None]:
    rows = (await s.execute(select(Market.market_id, Market.category))).all()
    return {mid: cat for mid, cat in rows}


async def _top_markets(s, enabled_cats: list[str]) -> list[str]:
    """Unresolved markets to scan for candidate wallets: a GLOBAL top-by-volume
    slice (breadth) UNION the top-by-volume slice WITHIN each enabled category
    (guarantees every category is represented even when one dominates global
    volume). Deduplicated.
    """
    mids: set[str] = set()

    # Global breadth — the busiest markets overall.
    rows = (await s.execute(
        select(Market.market_id)
        .where(Market.resolved.is_(False))
        .order_by(desc(Market.volume_24h_usdc))
        .limit(MARKETS_TO_SCAN)
    )).all()
    mids.update(r[0] for r in rows)

    # Per-category guarantee — top markets inside each enabled category, so
    # crypto/macro/politics get scanned regardless of World-Cup/sports volume.
    for cat in enabled_cats:
        rows = (await s.execute(
            select(Market.market_id)
            .where(Market.resolved.is_(False), Market.category == cat)
            .order_by(desc(Market.volume_24h_usdc))
            .limit(MARKETS_PER_CATEGORY)
        )).all()
        mids.update(r[0] for r in rows)

    return list(mids)


async def _collect_candidates(d: DataClient, market_ids: list[str]) -> dict[str, float]:
    candidates: Counter[str] = Counter()
    sem = asyncio.Semaphore(5)

    async def _pull(mid: str) -> None:
        async with sem:
            try:
                trades = await d.market_trades(mid, limit=TRADES_PER_MARKET)
            except Exception as exc:  # noqa: BLE001
                log.warning("market_trades_failed", market=mid, err=str(exc))
                return
            for t in trades:
                w = (t.get("proxyWallet") or "").lower()
                if not w:
                    continue
                candidates[w] += float(t.get("size") or 0) * float(t.get("price") or 0)

    await asyncio.gather(*[_pull(m) for m in market_ids])
    log.info("candidates_collected", n=len(candidates), markets=len(market_ids))
    return dict(candidates)


def _ranking_score(stats: dict, dominant_category: str | None) -> float:
    """Composite multi-signal score in roughly [-1, 1], higher is better.

    Components (each normalised to ~[0, 1], wr can go negative):
      pnl_signal    = tanh(realized_pnl / 5000)
      wr_signal     = (win_rate - 0.5) * 2  (or 0 if win_rate is None)
      sharpe_signal = tanh(sharpe / 1.5)    (or 0 if sharpe is None)
      depth_signal  = tanh(n_decisions / 50)
    Weights: pnl 0.40, wr 0.30, sharpe 0.15, depth 0.15.
    """
    if dominant_category is None:
        return 0.0
    n_dec = int(stats.get("n_decisions", 0) or 0)
    if n_dec < MIN_REALIZED_DECISIONS:
        return 0.0
    realised = float(stats.get("realized_pnl_usdc", 0.0) or 0.0)
    if abs(realised) < MIN_REALIZED_PNL_USDC:
        return 0.0

    pnl_signal = math.tanh(realised / 5000.0)

    wr = stats.get("win_rate")
    wr_signal = (float(wr) - 0.5) * 2.0 if wr is not None else 0.0

    sharpe = stats.get("sharpe")
    sharpe_signal = math.tanh(float(sharpe) / 1.5) if sharpe is not None else 0.0

    depth_signal = math.tanh(n_dec / 50.0)

    return (
        pnl_signal * 0.40
        + wr_signal * 0.30
        + sharpe_signal * 0.15
        + depth_signal * 0.15
    )


async def _score_candidate(d: DataClient, addr: str, mkt_cat: dict[str, str | None]) -> tuple[dict, str | None] | None:
    # 1. positions (ground truth)
    try:
        positions = await d.positions(addr, limit=500)
    except Exception:
        return None
    positions = positions or []

    # 2. trade history (for dominant category + sharpe)
    try:
        trades = await d.trades(addr, limit=500)
    except Exception:
        trades = []
    if not trades:
        return None

    df = pd.DataFrame([{
        "ts": datetime.fromtimestamp(int(t["timestamp"]), tz=timezone.utc),
        "market_id": t.get("conditionId") or t.get("market") or "",
        "side": (t.get("side") or "BUY").upper(),
        "outcome": (t.get("outcome") or "YES").upper(),
        "size_shares": float(t.get("size") or 0),
        "price": float(t.get("price") or 0),
        "notional_usdc": float(t.get("size") or 0) * float(t.get("price") or 0),
        "fee_usdc": float(t.get("fee") or 0),
    } for t in trades])

    cats_seen = [mkt_cat.get(mid) for mid in df["market_id"].unique()]
    dominant = Counter([c for c in cats_seen if c]).most_common(1)
    dom_cat = dominant[0][0] if dominant else None

    stats = wallet_stats_from_positions(positions, trades_df=df)
    stats["address"] = addr
    stats["category"] = dom_cat
    return stats, dom_cat


async def run_leaderboard() -> None:
    # merged_categories applies dashboard PATCHes (e.g. disable sports_other)
    # without a container restart. Previously this read raw YAML, so
    # dashboard category toggles were silently ignored by the scraper —
    # the bot kept scraping rosters for disabled categories.
    cats_cfg = await merged_categories()
    enabled_cats = {k: v for k, v in cats_cfg.items() if v.get("enabled")}
    if not enabled_cats:
        log.warning("leaderboard_no_categories_enabled")
        return

    d = DataClient()
    try:
        async with session_scope() as s:
            mkt_cat = await _market_category_map(s)
            top_mids = await _top_markets(s, list(enabled_cats.keys()))
        if not top_mids:
            log.warning("leaderboard_no_markets")
            return

        candidates = await _collect_candidates(d, top_mids)
        if not candidates:
            return

        ranked = sorted(candidates.items(), key=lambda kv: kv[1], reverse=True)[:MAX_CANDIDATES]
        log.info("leaderboard_scoring", n=len(ranked))

        scored_by_cat: dict[str, list[dict]] = defaultdict(list)
        sem = asyncio.Semaphore(5)

        async def _one(addr: str) -> None:
            async with sem:
                res = await _score_candidate(d, addr, mkt_cat)
                await asyncio.sleep(PER_WALLET_DELAY)
                if not res:
                    return
                stats, cat = res
                # Skip wallets in categories that aren't enabled in categories.yaml
                # (e.g. dominant cat = 'entertainment' while only politics is on).
                if cat is None or cat not in enabled_cats:
                    return
                score = _ranking_score(stats, cat)
                if score <= 0:
                    return
                stats["_score"] = score
                log.debug(
                    "wallet_scored",
                    addr=stats["address"],
                    category=cat,
                    score=round(score, 4),
                    realized_pnl=stats.get("realized_pnl_usdc"),
                    win_rate=stats.get("win_rate"),
                    sharpe=stats.get("sharpe"),
                    n_decisions=stats.get("n_decisions"),
                )
                scored_by_cat[cat].append(stats)

        await asyncio.gather(*[_one(a) for a, _ in ranked])
        log.info("leaderboard_scored", buckets={k: len(v) for k, v in scored_by_cat.items()})

        # Track per-category summary for end-of-run logging.
        summary: dict[str, dict] = {}

        async with session_scope() as session:
            now = datetime.now(tz=timezone.utc)
            top_set: set[str] = set()
            for cat, items in scored_by_cat.items():
                cat_cfg = enabled_cats.get(cat) or {}
                top_n = int(cat_cfg.get("top_n", 30))
                min_wr = float(cat_cfg.get("min_win_rate", 0.0) or 0.0)
                # Apply per-category min_win_rate floor before truncating to top_n.
                eligible = [
                    it for it in items
                    if (it.get("win_rate") is not None and float(it["win_rate"]) >= min_wr)
                ]
                eligible.sort(key=lambda x: x["_score"], reverse=True)
                kept = eligible[:top_n]

                kept_positions_sum = 0
                for i in kept:
                    addr = i["address"]
                    top_set.add(addr)
                    kept_positions_sum += int(i.get("n_total_positions", 0) or 0)
                    await session.execute(
                        pg_insert(Wallet)
                        .values(address=addr, category=cat, is_active=True, last_seen=now)
                        .on_conflict_do_update(
                            index_elements=["address"],
                            set_={"category": cat, "is_active": True, "last_seen": now},
                        )
                    )
                    # UPSERT per migration 0007 — see stats_loop for full context.
                    ws_values = dict(
                        address=addr,
                        window="30d",
                        pnl_usdc=i["pnl_usdc"],
                        realized_pnl_usdc=i["realized_pnl_usdc"],
                        roi=i["roi"],
                        win_rate=i["win_rate"],
                        sharpe=i["sharpe"],
                        trade_count=i["trade_count"],
                        avg_trade_size=i["avg_trade_size"],
                        n_decisions=i["n_decisions"],
                        n_open_positions=i["n_open_positions"],
                        n_total_positions=i["n_total_positions"],
                        n_trade_days=i["n_trade_days"],
                        computed_at=now,
                    )
                    ws_stmt = pg_insert(WalletStats).values(**ws_values)
                    await session.execute(ws_stmt.on_conflict_do_update(
                        index_elements=["address", "window"],
                        set_={k: v for k, v in ws_values.items()
                              if k not in ("address", "window")},
                    ))
                summary[cat] = {
                    "kept": len(kept),
                    "scored": len(items),
                    "eligible_after_wr": len(eligible),
                    "top_n": top_n,
                    "min_win_rate": min_wr,
                    "total_positions_sum": kept_positions_sum,
                }

            # IMPORTANT: only deactivate wallets we explicitly *re-scored* this
            # run that didn't make the top-set. Wallets the new run failed to
            # score (transient API failure, candidate not in scan window, etc.)
            # keep their prior is_active flag — otherwise a single bad run
            # would wipe the entire active set.
            scored_addrs: set[str] = {
                i["address"]
                for items in scored_by_cat.values()
                for i in items
            }
            to_deactivate = scored_addrs - top_set
            if to_deactivate:
                await session.execute(
                    update(Wallet)
                    .where(Wallet.address.in_(to_deactivate))
                    .values(is_active=False)
                )

            log.info(
                "leaderboard_done",
                kept=len(top_set),
                deactivated=len(to_deactivate),
                categories=list(scored_by_cat.keys()),
            )
            log.info(
                "leaderboard_summary",
                per_category=summary,
                total_kept=len(top_set),
                total_positions_sum=sum(c["total_positions_sum"] for c in summary.values()),
            )
    finally:
        await d.close()
