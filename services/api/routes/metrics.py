"""Per-category metrics — the dashboard /metrics page reads this.

`GET /metrics/categories?window=24h`

For each category currently enabled (per merged_categories), returns:
  - config snapshot (tags, top_n, min_win_rate, enabled)
  - signal volume in the window (candidates, passed)
  - fill / settle counts in the window
  - LIFETIME realized PnL (across ALL positions in this category, open + closed)
  - open positions count + cost basis + unrealized MTM (sum of size*(mark-avg))
  - settled wins/losses + honest win-rate (closed positions where realized>0)
  - wallet roster size

Realized AND win-rate include CLOSED positions (size_shares = 0). Earlier
versions filtered on size>0 and reported broken numbers — sports_other
showed -$4 lifetime when actual realized was +$41 because the 37 settled
positions were excluded by the filter.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from polybot.clients import ClobClient
from polybot.db import get_session
from polybot.market_resolver import token_for_outcome
from polybot.runtime_config import current_mode, merged_categories

router = APIRouter()


_WINDOWS = {"1h": "1 hour", "24h": "24 hours", "7d": "7 days", "30d": "30 days"}


async def _live_unrealized_by_category(s: AsyncSession) -> dict[str, dict[str, float]]:
    """For every OPEN position, fetch mark via CLOB best_mark (midpoint →
    fallback last-trade-price), compute (mark-avg)*size, aggregate by category.

    Returns {category: {"unrealized_usdc": x, "mark_known_count": n,
                        "mark_unknown_count": n}}.

    The mark lookup is concurrent with a hard 5 s outer timeout: a single
    slow CLOB call doesn't tank the whole endpoint.
    """
    rows = (await s.execute(text("""
        SELECT m.category, p.outcome, p.size_shares, p.avg_price,
               m.yes_token_id, m.no_token_id, m.outcomes, p.market_id
        FROM positions p JOIN markets m USING(market_id)
        WHERE p.wallet='PAPER' AND p.size_shares > 0 AND m.category IS NOT NULL
    """))).all()

    if not rows:
        return {}

    c = ClobClient()
    try:
        async def _one(row: Any) -> tuple[str, float | None]:
            from types import SimpleNamespace
            shim = SimpleNamespace(
                yes_token_id=row.yes_token_id, no_token_id=row.no_token_id,
                outcomes=row.outcomes, market_id=row.market_id,
            )
            tok = token_for_outcome(shim, row.outcome)
            if not tok:
                return row.category, None
            try:
                async with asyncio.timeout(3.0):
                    mark = await c.best_mark(str(tok))
                return row.category, mark if mark > 0 else None
            except Exception:  # noqa: BLE001
                return row.category, None

        try:
            async with asyncio.timeout(8.0):
                results = await asyncio.gather(
                    *[_one(r) for r in rows], return_exceptions=True,
                )
        except (TimeoutError, asyncio.TimeoutError):
            results = [(r.category, None) for r in rows]
    finally:
        await c.close()

    out: dict[str, dict[str, float]] = {}
    for r, res in zip(rows, results, strict=True):
        if isinstance(res, BaseException):
            mark = None
            cat = r.category
        else:
            cat, mark = res
        bucket = out.setdefault(cat, {"unrealized_usdc": 0.0, "mark_known": 0, "mark_unknown": 0})
        if mark is None:
            bucket["mark_unknown"] += 1
        else:
            bucket["unrealized_usdc"] += (float(mark) - float(r.avg_price)) * float(r.size_shares)
            bucket["mark_known"] += 1
    # Convert counts to floats so the return type is uniform (JSON-friendly).
    return out


@router.get("/categories")
async def categories_metrics(
    window: str = Query("24h", description="one of 1h, 24h, 7d, 30d"),
    include_marks: bool = Query(True, description="if true, fetch live CLOB marks for unrealized PnL"),
    s: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if window not in _WINDOWS:
        raise HTTPException(400, f"window must be one of {list(_WINDOWS)}")
    win_sql = _WINDOWS[window]

    mode = await current_mode()
    cats = await merged_categories(mode)

    # 1. Windowed signal/fill counts per category.
    sig_sql = text(f"""
        WITH win AS (SELECT NOW() - INTERVAL '{win_sql}' AS t)
        SELECT
          m.category                                                                        AS category,
          COUNT(DISTINCT s.id)                                FILTER (WHERE s.ts >= win.t)  AS signals,
          COUNT(DISTINCT s.id)                                FILTER (WHERE s.ts >= win.t AND s.gate_pass) AS passed,
          COUNT(DISTINCT f.id)
            FILTER (WHERE f.ts >= win.t AND f.status='filled' AND f.mode='paper')           AS fills_paper,
          COUNT(DISTINCT f.id)
            FILTER (WHERE f.ts >= win.t AND f.status='settled' AND f.mode='paper')          AS settles
        FROM markets m
        LEFT JOIN signals s ON s.market_id = m.market_id
        LEFT JOIN fills   f ON f.market_id = m.market_id
        CROSS JOIN win
        WHERE m.category IS NOT NULL
        GROUP BY m.category, win.t
    """)
    by_cat: dict[str, dict[str, Any]] = {}
    for r in (await s.execute(sig_sql)).all():
        by_cat[r.category] = {
            "signals_window": int(r.signals or 0),
            "passed_window": int(r.passed or 0),
            "fills_paper_window": int(r.fills_paper or 0),
            "settles_window": int(r.settles or 0),
        }

    # 2. Position aggregates per category (LIFETIME, both open and closed).
    #    `realized_lifetime` is the source-of-truth for "how much money did
    #    this category make/lose" — sums every position in the bucket
    #    regardless of whether shares are still held.
    #    Closed positions (size=0) keep their final realized_pnl_usdc which
    #    includes the settle amount, so this is the honest PnL number.
    pos_sql = text("""
        SELECT m.category,
               COUNT(*) FILTER (WHERE p.size_shares > 0)                                  AS open_positions,
               COUNT(*) FILTER (WHERE p.size_shares = 0)                                  AS closed_positions,
               COUNT(*) FILTER (WHERE p.size_shares = 0 AND p.realized_pnl_usdc > 0.01)   AS wins_closed,
               COUNT(*) FILTER (WHERE p.size_shares = 0 AND p.realized_pnl_usdc < -0.01)  AS losses_closed,
               COALESCE(SUM(p.size_shares * p.avg_price)
                        FILTER (WHERE p.size_shares > 0), 0)                              AS cost_basis,
               COALESCE(SUM(p.realized_pnl_usdc), 0)                                      AS realized_lifetime
        FROM positions p JOIN markets m USING(market_id)
        WHERE p.wallet='PAPER' AND m.category IS NOT NULL
        GROUP BY m.category
    """)
    by_pos: dict[str, dict[str, Any]] = {}
    for r in (await s.execute(pos_sql)).all():
        wins = int(r.wins_closed or 0)
        losses = int(r.losses_closed or 0)
        wr = (wins / (wins + losses)) if (wins + losses) > 0 else None
        by_pos[r.category] = {
            "open_positions": int(r.open_positions or 0),
            "closed_positions": int(r.closed_positions or 0),
            "wins_closed": wins,
            "losses_closed": losses,
            "win_rate_closed": round(wr, 3) if wr is not None else None,
            "cost_basis_usdc": round(float(r.cost_basis), 2),
            "realized_lifetime_usdc": round(float(r.realized_lifetime), 2),
        }

    # 3. Active wallet roster per category.
    wal_sql = text("""
        SELECT category, COUNT(*) FILTER (WHERE is_active) AS active, COUNT(*) AS total
        FROM wallets WHERE category IS NOT NULL
        GROUP BY category
    """)
    by_wal: dict[str, dict[str, Any]] = {
        r.category: {"active_wallets": int(r.active or 0), "total_wallets": int(r.total or 0)}
        for r in (await s.execute(wal_sql)).all()
    }

    # 4. Live unrealized MTM (optional — slow when CLOB is laggy).
    by_unreal: dict[str, dict[str, float]] = {}
    if include_marks:
        try:
            by_unreal = await _live_unrealized_by_category(s)
        except Exception:  # noqa: BLE001
            by_unreal = {}

    # Compose final rows. Categories surface even when YAML has no entry
    # (e.g. legacy "sports" bucket) but are flagged enabled=false unless
    # the merged config says otherwise.
    all_cats = set(cats.keys()) | set(by_cat.keys()) | set(by_pos.keys()) | set(by_wal.keys()) | set(by_unreal.keys())
    out_rows: list[dict[str, Any]] = []
    for name in sorted(all_cats):
        cfg = cats.get(name) or {}
        metrics = by_cat.get(name, {})
        pos = by_pos.get(name, {})
        wal = by_wal.get(name, {})
        unreal = by_unreal.get(name, {})

        unrealized_usdc = round(unreal.get("unrealized_usdc", 0.0), 2) if unreal else None
        # Net PnL = realized lifetime + current unrealized.
        net_pnl = None
        if "realized_lifetime_usdc" in pos:
            base = pos["realized_lifetime_usdc"]
            net_pnl = round(base + (unrealized_usdc or 0.0), 2)

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
            "unrealized_mtm_usdc": unrealized_usdc,
            "mark_known_count": int(unreal.get("mark_known", 0)) if unreal else None,
            "mark_unknown_count": int(unreal.get("mark_unknown", 0)) if unreal else None,
            "net_pnl_usdc": net_pnl,
        })

    # Totals row for the dashboard footer.
    totals = {
        "signals_window": sum(r.get("signals_window", 0) or 0 for r in out_rows),
        "passed_window": sum(r.get("passed_window", 0) or 0 for r in out_rows),
        "fills_paper_window": sum(r.get("fills_paper_window", 0) or 0 for r in out_rows),
        "settles_window": sum(r.get("settles_window", 0) or 0 for r in out_rows),
        "open_positions": sum(r.get("open_positions", 0) or 0 for r in out_rows),
        "closed_positions": sum(r.get("closed_positions", 0) or 0 for r in out_rows),
        "wins_closed": sum(r.get("wins_closed", 0) or 0 for r in out_rows),
        "losses_closed": sum(r.get("losses_closed", 0) or 0 for r in out_rows),
        "cost_basis_usdc": round(sum(r.get("cost_basis_usdc", 0) or 0 for r in out_rows), 2),
        "realized_lifetime_usdc": round(sum(r.get("realized_lifetime_usdc", 0) or 0 for r in out_rows), 2),
        "unrealized_mtm_usdc": (
            round(sum(r.get("unrealized_mtm_usdc") or 0 for r in out_rows), 2)
            if include_marks else None
        ),
    }
    if totals["wins_closed"] + totals["losses_closed"] > 0:
        totals["win_rate_closed"] = round(
            totals["wins_closed"] / (totals["wins_closed"] + totals["losses_closed"]), 3
        )
    else:
        totals["win_rate_closed"] = None

    return {"window": window, "mode": mode, "categories": out_rows, "totals": totals}
