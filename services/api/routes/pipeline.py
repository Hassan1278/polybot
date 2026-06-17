"""Pipeline health + gates summary endpoints.

These are the operator's at-a-glance "is the bot alive?" dashboard.
Everything is computed from the DB / Redis at request time — no caching —
because the surface area is tiny and the data freshness matters more than
shaving a few ms.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from polybot.config import settings
from polybot.db import get_session
from polybot.logging import get_logger
from polybot.redis_bus import client as redis_client
from polybot.redis_bus import kill_status

log = get_logger(__name__)

router = APIRouter()

# Window over which we compute the rolling rates / pass-rate. Matches the
# UX expectation ("15m heartbeat"); change here if the dashboard label moves.
_WINDOW_MINUTES = 15
_GATES_WINDOW_MINUTES = 60

# Redis key the WS client *may* write its current subscription count into.
# We treat absence as "unknown" (None) rather than 0 so we don't false-alarm
# when the metric simply isn't being published.
_WS_SUBSCRIBED_KEY = "polybot:ws:subscribed_assets"


async def _ws_subscribed_assets() -> int | None:
    """Best-effort read of the WS client's current subscription count.

    Returns None if Redis is unreachable or the key isn't set — callers should
    render that as "unknown" rather than "0 (broken)".
    """
    try:
        r = redis_client()
        raw = await r.get(_WS_SUBSCRIBED_KEY)
        if raw is None:
            return None
        return int(raw)
    except Exception as exc:  # noqa: BLE001
        log.warning("pipeline.ws_subscribed_lookup_failed", error=str(exc))
        return None


@router.get("/health")
async def pipeline_health(
    s: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Single-shot heartbeat of every stage of the pipeline.

    Aggregates are computed in one round-trip per table over the last
    `_WINDOW_MINUTES`. All timestamps are returned ISO-8601 UTC.
    """
    window = f"{_WINDOW_MINUTES} minutes"

    # Trades: count + last ts + lag (NOW() - max(ts)) in one query.
    trades_row = (
        await s.execute(
            text(
                """
                SELECT
                    COUNT(*) FILTER (WHERE ts >= NOW() - (:win)::interval) AS cnt,
                    MAX(ts)                                              AS last_ts,
                    EXTRACT(EPOCH FROM (NOW() - MAX(ts)))                AS lag_s
                FROM trades
                """
            ),
            {"win": window},
        )
    ).one()

    # Signals: count + pass-count + last ts in one query — divide for pass-rate.
    signals_row = (
        await s.execute(
            text(
                """
                SELECT
                    COUNT(*) FILTER (WHERE ts >= NOW() - (:win)::interval)                       AS cnt,
                    COUNT(*) FILTER (WHERE ts >= NOW() - (:win)::interval AND gate_pass IS TRUE) AS pass_cnt,
                    MAX(ts)                                                                   AS last_ts
                FROM signals
                """
            ),
            {"win": window},
        )
    ).one()

    fills_row = (
        await s.execute(
            text(
                """
                SELECT
                    COUNT(*) FILTER (WHERE ts >= NOW() - (:win)::interval) AS cnt,
                    MAX(ts)                                              AS last_ts
                FROM fills
                """
            ),
            {"win": window},
        )
    ).one()

    # Wallet + market metadata counts. These are point-in-time, not windowed.
    active_wallets = (
        await s.execute(text("SELECT COUNT(*) FROM wallets WHERE is_active IS TRUE"))
    ).scalar_one()
    markets_total = (await s.execute(text("SELECT COUNT(*) FROM markets"))).scalar_one()
    markets_uncategorised = (
        await s.execute(text("SELECT COUNT(*) FROM markets WHERE category IS NULL"))
    ).scalar_one()

    kill_reason = await kill_status()
    ws_count = await _ws_subscribed_assets()

    win_minutes = float(_WINDOW_MINUTES)

    def _per_min(cnt: Any) -> float:
        return round(float(cnt or 0) / win_minutes, 4)

    signal_cnt = int(signals_row.cnt or 0)
    pass_cnt = int(signals_row.pass_cnt or 0)
    pass_rate = round(pass_cnt / signal_cnt, 4) if signal_cnt else 0.0

    # lag_seconds: NOW() - max(trades.ts). NULL when there are zero trades ever.
    lag_raw = trades_row.lag_s
    lag_seconds = int(lag_raw) if lag_raw is not None else None

    def _iso(ts: datetime | None) -> str | None:
        if ts is None:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.isoformat()

    # Per-stage latency p50/p95 over the 60 min window. Diagnoses "why so
    # few fills" without staring at logs: if decide_lag p95 is multi-minute
    # the correlation_loop is starving; if execute_lag p95 spikes, the
    # executor is the choke point. Trades→signals is matched on signal
    # window-start ≈ trade.ts (best-effort proxy since signals don't carry
    # the originating trade ts).
    lat_row = (
        await s.execute(text(
            """
            WITH win AS (
              SELECT NOW() - INTERVAL '60 minutes' AS lo
            ),
            sig AS (
              SELECT EXTRACT(EPOCH FROM (s.ts - (SELECT lo FROM win))) AS age_s
              FROM signals s, win
              WHERE s.ts >= win.lo
            ),
            fill AS (
              SELECT EXTRACT(EPOCH FROM (f.ts - s.ts)) AS dt_s
              FROM fills f JOIN signals s ON s.id = f.signal_id
              WHERE f.ts >= (SELECT lo FROM win) AND s.ts <= f.ts
            )
            SELECT
              (SELECT percentile_cont(0.5)  WITHIN GROUP (ORDER BY dt_s) FROM fill) AS exec_p50,
              (SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY dt_s) FROM fill) AS exec_p95,
              (SELECT COUNT(*) FROM fill)                                            AS exec_n
            """
        ))
    ).one()

    return {
        "ws_subscribed_assets": ws_count,
        "trades_per_min_15m": _per_min(trades_row.cnt),
        "signals_per_min_15m": _per_min(signal_cnt),
        "signals_pass_rate_15m": pass_rate,
        "fills_per_min_15m": _per_min(fills_row.cnt),
        "last_trade_ts": _iso(trades_row.last_ts),
        "last_signal_ts": _iso(signals_row.last_ts),
        "last_fill_ts": _iso(fills_row.last_ts),
        "lag_seconds": lag_seconds,
        "active_wallets": int(active_wallets or 0),
        "markets_total": int(markets_total or 0),
        "markets_uncategorised": int(markets_uncategorised or 0),
        "kill_switch_active": kill_reason is not None,
        "current_mode": settings.trading_mode,
        "latency_60m": {
            "execute_p50_s": float(lat_row.exec_p50) if lat_row.exec_p50 is not None else None,
            "execute_p95_s": float(lat_row.exec_p95) if lat_row.exec_p95 is not None else None,
            "execute_n": int(lat_row.exec_n or 0),
        },
    }


@router.get("/gates/summary")
async def gates_summary(
    s: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Per-gate pass/fail counts over the last 60 minutes.

    `gate_results` is JSON of the shape `{gate_name: {"pass": bool, ...}}`.
    We aggregate in Python because the keys are dynamic and the row volume
    over an hour is small — pulling the JSON blobs and counting locally is
    simpler and more portable than a JSON-path query.
    """
    window = f"{_GATES_WINDOW_MINUTES} minutes"
    rows = (
        await s.execute(
            text(
                """
                SELECT gate_results, gate_pass
                FROM signals
                WHERE ts >= NOW() - (:win)::interval
                """
            ),
            {"win": window},
        )
    ).all()

    # `per_gate` now tracks a `reasons` Counter per gate so the operator
    # gets "top 3 fail reasons" on the dashboard instead of just
    # `pass / fail` counts. Cuts diagnose-time for the "0% pass for hours"
    # scenario from "grep logs" to "glance at the row".
    from collections import Counter
    per_gate: dict[str, dict[str, object]] = {}
    total = 0
    total_pass = 0
    for gate_results, gate_pass in rows:
        total += 1
        if gate_pass:
            total_pass += 1
        if not isinstance(gate_results, dict):
            continue
        for name, result in gate_results.items():
            bucket = per_gate.setdefault(
                str(name),
                {"pass": 0, "fail": 0, "_reasons": Counter()},
            )
            passed = False
            reason = None
            if isinstance(result, dict):
                passed = bool(result.get("pass"))
                reason = result.get("reason")
            else:
                passed = bool(result)
            bucket["pass" if passed else "fail"] += 1  # type: ignore[operator]
            # Bucket reasons by the leftmost token before '=' or ':'.
            # 'depth=42<75.0' → 'depth', 'no_token_id' → 'no_token_id',
            # 'cooldown_active:42m' → 'cooldown_active'. Keeps the chart
            # tight while preserving 'why' at a glance.
            if not passed and isinstance(reason, str):
                bucket_reason = reason
                for sep in ("=", ":", " "):
                    if sep in bucket_reason:
                        bucket_reason = bucket_reason.split(sep, 1)[0]
                        break
                bucket["_reasons"][bucket_reason] += 1  # type: ignore[index]

    # Materialise top reasons + drop the working Counter from the payload.
    out_gates: dict[str, dict[str, object]] = {}
    for name, bucket in per_gate.items():
        counter: Counter = bucket.pop("_reasons")  # type: ignore[assignment]
        top = counter.most_common(3)
        out_gates[name] = {
            **bucket,
            "top_reasons": [
                {"reason": r, "count": c} for r, c in top
            ],
        }

    return {
        "window_minutes": _GATES_WINDOW_MINUTES,
        "total_signals": total,
        "total_pass": total_pass,
        "gates": out_gates,
    }
