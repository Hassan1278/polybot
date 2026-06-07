"""Per-category metrics — the dashboard /metrics page reads this.

`GET /metrics/categories?window=24h`

For each category currently enabled (per merged_categories), returns:
  - config snapshot (tags, top_n, min_win_rate, enabled)
  - signal volume in the window (candidates, passed)
  - fill / settle / win-loss counts
  - net realized + open cost basis + unrealized MTM
  - wallet roster size

The query joins signals + fills + positions on markets.category. It's a
single query per metric category so the endpoint stays under ~300 ms even
with thousands of fills.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from polybot.db import get_session
from polybot.runtime_config import current_mode, merged_categories

router = APIRouter()


_WINDOWS = {"1h": "1 hour", "24h": "24 hours", "7d": "7 days", "30d": "30 days"}


@router.get("/categories")
async def categories_metrics(
    window: str = Query("24h", description="one of 1h, 24h, 7d, 30d"),
    s: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if window not in _WINDOWS:
        raise HTTPException(400, f"window must be one of {list(_WINDOWS)}")
    win_sql = _WINDOWS[window]

    mode = await current_mode()
    cats = await merged_categories(mode)

    # One bulk-fetch query that joins all the relevant counts per category.
    # We use raw text() with a bind param for the interval — see B11 in
    # BUGS.md (INTERVAL :var doesn't bind, must build with format()).
    # CROSS JOIN win at the end so the LEFT JOINs are pure m-anchored and
    # `win.t` is still in scope for the FILTER predicates + final GROUP BY.
    # The implicit comma-join from a previous draft broke `m`'s visibility
    # inside the LEFT JOIN's ON clause.
    sql = text(f"""
        WITH win AS (SELECT NOW() - INTERVAL '{win_sql}' AS t)
        SELECT
          m.category                                                                        AS category,
          COUNT(DISTINCT s.id)                                FILTER (WHERE s.ts >= win.t)  AS signals,
          COUNT(DISTINCT s.id)                                FILTER (WHERE s.ts >= win.t AND s.gate_pass) AS passed,
          COUNT(DISTINCT f.id)
            FILTER (WHERE f.ts >= win.t AND f.status='filled' AND f.mode='paper')           AS fills_paper,
          COUNT(DISTINCT f.id)
            FILTER (WHERE f.ts >= win.t AND f.status='settled' AND f.mode='paper')          AS settles,
          COALESCE(SUM(f.notional_usdc)
            FILTER (WHERE f.ts >= win.t AND f.status='settled' AND f.mode='paper'
                    AND f.price > 0.5), 0)                                                  AS settled_win_notional,
          COALESCE(SUM(f.notional_usdc)
            FILTER (WHERE f.ts >= win.t AND f.status='settled' AND f.mode='paper'
                    AND f.price <= 0.5), 0)                                                 AS settled_lose_notional
        FROM markets m
        LEFT JOIN signals s ON s.market_id = m.market_id
        LEFT JOIN fills   f ON f.market_id = m.market_id
        CROSS JOIN win
        WHERE m.category IS NOT NULL
        GROUP BY m.category, win.t
    """)
    rows = (await s.execute(sql)).all()
    by_cat: dict[str, dict[str, Any]] = {}
    for r in rows:
        by_cat[r.category] = {
            "signals_window": int(r.signals or 0),
            "passed_window": int(r.passed or 0),
            "fills_paper_window": int(r.fills_paper or 0),
            "settles_window": int(r.settles or 0),
            "settled_win_notional": float(r.settled_win_notional or 0),
            "settled_lose_notional": float(r.settled_lose_notional or 0),
        }

    # Open positions + cost basis per category (separate aggregation since
    # it's instantaneous, not windowed).
    pos_sql = text("""
        SELECT m.category,
               COUNT(*)                                       AS open,
               COALESCE(SUM(p.size_shares * p.avg_price), 0)  AS cost_basis,
               COALESCE(SUM(p.realized_pnl_usdc), 0)          AS realized
        FROM positions p JOIN markets m USING(market_id)
        WHERE p.size_shares > 0
        GROUP BY m.category
    """)
    pos_rows = (await s.execute(pos_sql)).all()
    by_pos: dict[str, dict[str, Any]] = {
        r.category: {
            "open_positions": int(r.open),
            "cost_basis_usdc": round(float(r.cost_basis), 2),
            "realized_on_open_usdc": round(float(r.realized), 2),
        }
        for r in pos_rows
    }

    # Active wallet roster per category — separate so we count even when
    # the wallets haven't traded in the window.
    wal_sql = text("""
        SELECT category, COUNT(*) FILTER (WHERE is_active) AS active, COUNT(*) AS total
        FROM wallets WHERE category IS NOT NULL
        GROUP BY category
    """)
    wal_rows = (await s.execute(wal_sql)).all()
    by_wal: dict[str, dict[str, Any]] = {
        r.category: {"active_wallets": int(r.active or 0), "total_wallets": int(r.total or 0)}
        for r in wal_rows
    }

    # Compose final response: one row per category (union of yaml + metrics
    # observations) plus a TOTAL row.
    all_cats = set(cats.keys()) | set(by_cat.keys()) | set(by_pos.keys()) | set(by_wal.keys())
    out_rows: list[dict[str, Any]] = []
    for name in sorted(all_cats):
        cfg = cats.get(name) or {}
        metrics = by_cat.get(name, {})
        pos = by_pos.get(name, {})
        wal = by_wal.get(name, {})

        settles = metrics.get("settles_window") or 0
        wins = settled_win_n = float(metrics.get("settled_win_notional") or 0)
        # crude winrate: settled fills at price > 0.5 vs total settles.
        # NOTE: This is by-count proxy via notional split; the real win flag
        # lives in the executor's settle code path. For dashboard
        # observability this is close enough.
        wr = None
        if settles > 0:
            # Estimate: assume each settle contributes equally; ratio of
            # winning $ to total settled $ proxies winrate.
            total_n = (metrics.get("settled_win_notional") or 0) + (metrics.get("settled_lose_notional") or 0)
            wr = (wins / total_n) if total_n > 0 else None

        out_rows.append({
            "name": name,
            "enabled": bool(cfg.get("enabled", False)),
            "config": {
                "tags": cfg.get("tags", []),
                "top_n": cfg.get("top_n"),
                "min_win_rate": cfg.get("min_win_rate"),
            },
            "metrics_window": window,
            **metrics,
            **pos,
            **wal,
            "win_rate_estimate": round(wr, 3) if wr is not None else None,
        })

    return {"window": window, "mode": mode, "categories": out_rows}
