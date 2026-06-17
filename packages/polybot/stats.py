"""Wallet & cluster statistics.

The honest source of truth for a wallet's PnL on Polymarket is its
`/positions` endpoint, which exposes per-market `cashPnl` (mark-to-market)
and `realizedPnl` (closed bets only). Win-rate computed from trade-prints
alone is systematically inflated because we can't see when a position was
ultimately redeemed at 0 or 1.

Two stats functions:
  - `wallet_stats_from_positions(positions, trades_df=None, ...)` — preferred.
  - `wallet_stats_from_trades(trades_df, ...)` — legacy fallback, kept for
    components that only have trade data.

Both return identical dicts. `win_rate` and `sharpe` are `None` (NULL in DB,
"—" in UI) when not enough data is available — never a misleading 0 or 1.

Cluster / sizing helpers:
  - `cluster_active_wallets(...)` — surfaces bursts of correlated wallet
    activity inside a sliding window, scored in [0, 1] with exponential
    time-decay so fresh trades dominate.
  - `position_size_from_score(...)` — translates a [0, 1] confidence score
    into a USDC stake using a clamped linear multiplier around an anchor.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

# Thresholds — a single decision is meaningless, so we hide the metric.
MIN_DECISIONS_FOR_WR = 5
MIN_DAYS_FOR_SHARPE = 5
EPS = 0.01                  # USDC rounding noise

# ── cluster scoring defaults ────────────────────────────────────────────────
# Half-life (seconds) for the recency weight in `cluster_active_wallets`.
# A trade `half_life_seconds` old contributes 0.5 to the recency average,
# `2*half_life_seconds` old contributes 0.25, etc.
#
# IMPORTANT: this must be aligned with the trade_ingest poll interval
# (currently 15 min). If half-life << poll-interval, every trade we observe
# is already "old" and the score collapses to near zero before any gate can
# evaluate it. 900 s = 15 min keeps fresh-cycle trades weighted ~0.50-1.00.
DEFAULT_HALF_LIFE_SECONDS: float = 900.0   # 15 minutes (matches trade_ingest)

# Saturation constants for the wallet-count and notional sub-scores.
# `1 - exp(-x / k)` saturates around 0.86 when x = 2k and ~0.95 when x = 3k.
# k_wallets = 2.0  → 4 distinct wallets ≈ 0.86 (production sweet spot)
# k_notional = 500 USDC → 1_000 USDC ≈ 0.86 (most clusters are small;
#                                            the OLD 2000 cap meant typical
#                                            cluster contributed <0.10 → too low)
DEFAULT_K_WALLETS: float = 2.0
DEFAULT_K_NOTIONAL: float = 500.0

# ── sizing defaults ─────────────────────────────────────────────────────────
# Multiplier clamp for `position_size_from_score`: never less than 0.25× the
# base stake (we still want exposure on weak-but-positive signals) and never
# more than 3× (cap conviction trades regardless of the absolute `max_usdc`).
_SIZE_MULT_MIN: float = 0.25
_SIZE_MULT_MAX: float = 3.0


def _empty_stats() -> dict[str, Any]:
    return {
        "pnl_usdc": 0.0,
        "realized_pnl_usdc": 0.0,
        "roi": 0.0,
        "win_rate": None,
        "sharpe": None,
        "trade_count": 0,
        "avg_trade_size": 0.0,
        "n_decisions": 0,
        "n_open_positions": 0,
        "n_total_positions": 0,
        "n_trade_days": 0,
    }


def _daily_sharpe(trades_df: pd.DataFrame) -> tuple[float | None, int]:
    """Returns (sharpe, n_trade_days). Sharpe is None if not computable.

    A single-day population yields `std == NaN` (pandas default ddof=1), so
    we guard against both NaN and non-positive variance below.
    """
    if trades_df is None or trades_df.empty:
        return None, 0

    def _day_pnl(x: pd.DataFrame) -> float:
        sell = float(x.loc[x["side"] == "SELL", "notional_usdc"].sum())
        buy = float(x.loc[x["side"] == "BUY", "notional_usdc"].sum())
        fee = float(x["fee_usdc"].sum())
        return sell - buy - fee

    daily = trades_df.assign(d=trades_df["ts"].dt.floor("D"))
    raw = daily.groupby("d").apply(_day_pnl)
    if isinstance(raw, pd.DataFrame):
        by_day = pd.Series([], dtype="float64") if raw.empty else pd.to_numeric(raw.iloc[:, 0], errors="coerce").dropna()
    else:
        by_day = pd.to_numeric(raw, errors="coerce").dropna()

    n_days = int(len(by_day))
    if n_days < MIN_DAYS_FOR_SHARPE:
        return None, n_days
    std = float(by_day.std())
    mean = float(by_day.mean())
    # Guard: single-day populations make std NaN; flat days make it 0.
    if not math.isfinite(std) or std <= 0:
        return None, n_days
    return mean / std, n_days


def wallet_stats_from_positions(
    positions: list[dict[str, Any]],
    *,
    trades_df: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Compute wallet stats from Polymarket's `/positions` endpoint payload.

    `positions` is the list returned by `DataClient.positions(addr)`.
    `trades_df` (optional) is used solely for the daily-bucket Sharpe.
    """
    if not positions:
        out = _empty_stats()
        if trades_df is not None and not trades_df.empty:
            out["trade_count"] = int(len(trades_df))
            out["avg_trade_size"] = float(trades_df["notional_usdc"].mean())
            out["sharpe"], out["n_trade_days"] = _daily_sharpe(trades_df)
        return out

    # ── decisions / win-rate (only fully closed bets count) ────────────────
    decisions = [p for p in positions if abs(float(p.get("realizedPnl", 0) or 0)) > EPS]
    wins = sum(1 for p in decisions if float(p.get("realizedPnl", 0) or 0) > 0)
    n_decisions = len(decisions)
    win_rate: float | None = wins / n_decisions if n_decisions >= MIN_DECISIONS_FOR_WR else None

    # ── PnL aggregates ─────────────────────────────────────────────────────
    total_pnl = float(sum(float(p.get("cashPnl", 0) or 0) for p in positions))
    realized_pnl = float(sum(float(p.get("realizedPnl", 0) or 0) for p in positions))
    initial = float(sum(float(p.get("initialValue", 0) or 0) for p in positions))
    roi = total_pnl / initial if initial > 0 else 0.0
    n_open = sum(1 for p in positions if abs(float(p.get("size", 0) or 0)) > EPS)

    sharpe, n_days = _daily_sharpe(trades_df) if trades_df is not None else (None, 0)
    trade_count = int(len(trades_df)) if trades_df is not None else 0
    avg_size = float(trades_df["notional_usdc"].mean()) if (trades_df is not None and not trades_df.empty) else 0.0

    return {
        "pnl_usdc": total_pnl,
        "realized_pnl_usdc": realized_pnl,
        "roi": roi,
        "win_rate": win_rate,
        "sharpe": sharpe,
        "trade_count": trade_count,
        "avg_trade_size": avg_size,
        "n_decisions": n_decisions,
        "n_open_positions": n_open,
        "n_total_positions": len(positions),
        "n_trade_days": n_days,
    }


def wallet_stats_from_trades(
    trades: pd.DataFrame, *, window_days: int | None = 30
) -> dict[str, Any]:
    """Legacy / fallback: trade-only stats. Returns None for win-rate/sharpe
    when not enough information is present, never a misleading 0 or 1.
    """
    if trades.empty:
        return _empty_stats()

    if window_days is not None:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=window_days)
        trades = trades[trades["ts"] >= cutoff]
    if trades.empty:
        return _empty_stats()

    def _market_pnl(d: pd.DataFrame) -> float | None:
        has_buy = bool((d["side"] == "BUY").any())
        has_sell = bool((d["side"] == "SELL").any())
        if not (has_buy and has_sell):
            return None
        sell = float(d.loc[d["side"] == "SELL", "notional_usdc"].sum())
        buy = float(d.loc[d["side"] == "BUY", "notional_usdc"].sum())
        fee = float(d["fee_usdc"].sum())
        return sell - buy - fee

    g = trades.groupby(["market_id", "outcome"])
    raw = g.apply(_market_pnl)
    if isinstance(raw, pd.DataFrame):
        pnl_per_market = pd.Series([], dtype="float64") if raw.empty else pd.to_numeric(raw.iloc[:, 0], errors="coerce").dropna()
    else:
        pnl_per_market = pd.to_numeric(raw, errors="coerce").dropna()

    decided = int(len(pnl_per_market))
    win_rate: float | None
    if decided >= MIN_DECISIONS_FOR_WR:
        wins = int((pnl_per_market > 0).sum())
        win_rate = wins / decided
    else:
        win_rate = None

    total_pnl = float(pnl_per_market.sum()) if decided else 0.0
    gross = float(trades.loc[trades["side"] == "BUY", "notional_usdc"].sum())
    roi = total_pnl / gross if gross > 0 else 0.0
    sharpe, n_days = _daily_sharpe(trades)

    return {
        "pnl_usdc": total_pnl,
        "realized_pnl_usdc": total_pnl,
        "roi": roi,
        "win_rate": win_rate,
        "sharpe": sharpe,
        "trade_count": int(len(trades)),
        "avg_trade_size": float(trades["notional_usdc"].mean()),
        "n_decisions": decided,
        "n_open_positions": 0,
        "n_total_positions": 0,
        "n_trade_days": n_days,
    }


# ── correlation helpers ─────────────────────────────────────────────────────

def jaccard_matrix(sets: dict[str, set]) -> tuple[list[str], np.ndarray]:
    labels = list(sets.keys())
    n = len(labels)
    m = np.zeros((n, n), dtype=float)
    for i, a in enumerate(labels):
        sa = sets[a]
        for j in range(i, n):
            sb = sets[labels[j]]
            union = sa | sb
            sim = len(sa & sb) / len(union) if union else 0.0
            m[i, j] = m[j, i] = sim
    return labels, m


def cluster_active_wallets(
    recent_trades: pd.DataFrame,
    *,
    window_minutes: int,
    min_wallets: int,
    k_wallets: float = DEFAULT_K_WALLETS,
    k_notional: float = DEFAULT_K_NOTIONAL,
    half_life_seconds: float = DEFAULT_HALF_LIFE_SECONDS,
) -> list[dict]:
    """Detect bursts of correlated wallet activity inside a sliding window.

    Groups trades by `(market_id, side)` and emits a cluster dict per group
    that has at least `min_wallets` distinct wallets. Each cluster gets a
    probabilistic `correlation_score` in [0, 1] composed of three factors:

      wallet_weight  = 1 - exp(-n_wallets / k_wallets)
            5 unique wallets at k=2.5 → ≈0.86
      notional_weight = 1 - exp(-notional_usdc / k_notional)
            4_000 USDC at k=2_000  → ≈0.86
      recency_weight  = mean( exp(-age_seconds * ln(2) / half_life_seconds) )
            half-life = 5 min: a 5-min-old trade contributes 0.5, fresh = 1.0

      score = wallet_weight * (0.5 + 0.5 * notional_weight) * recency_weight

    The `(0.5 + 0.5 * notional_weight)` factor means even a small-notional
    burst with many fresh wallets still scores meaningfully (≥0.5× of the
    wallet weight), unlike the previous formula which collapsed to ~0 for
    notional ≪ 1_000 USDC.

    The original kwargs (`window_minutes`, `min_wallets`) are preserved
    positionally-by-name for backward compatibility with existing callers.
    """
    if recent_trades.empty:
        return []
    now = datetime.now(tz=timezone.utc)
    cutoff = now - timedelta(minutes=window_minutes)
    rt = recent_trades[recent_trades["ts"] >= cutoff]
    if rt.empty:
        return []

    # Avoid division-by-zero if a caller passes a non-positive half-life.
    decay_lambda = math.log(2.0) / half_life_seconds if half_life_seconds > 0 else 0.0

    # Group by outcome too — without it, multi-outcome markets (e.g.
    # "Who wins X?" with 5+ candidates) conflate wallets betting on
    # different outcomes into the same cluster, producing nonsense signals
    # where the gate chain then fails or routes to the wrong token.
    # For binary markets this is a no-op (YES/NO already imply opposite sides).
    out: list[dict] = []
    has_outcome = "outcome" in rt.columns
    group_keys = ["market_id", "outcome", "side"] if has_outcome else ["market_id", "side"]
    for keys, g in rt.groupby(group_keys):
        if has_outcome:
            market_id, outcome, side = keys
        else:
            market_id, side = keys
            outcome = None
        unique_wallets = g["wallet"].unique().tolist()
        n_wallets = len(unique_wallets)
        if n_wallets < min_wallets:
            continue

        notional = float(g["notional_usdc"].sum())
        n_trades = int(len(g))

        # Age in seconds (clamped >=0 in case of clock skew on incoming ts).
        ages = (now - g["ts"]).dt.total_seconds().clip(lower=0.0)
        if decay_lambda > 0:
            decays = np.exp(-decay_lambda * ages.to_numpy(dtype=float))
        else:
            decays = np.ones(n_trades, dtype=float)
        recency_weight = float(decays.mean()) if n_trades else 0.0

        wallet_weight = 1.0 - math.exp(-n_wallets / k_wallets) if k_wallets > 0 else 0.0
        notional_weight = 1.0 - math.exp(-notional / k_notional) if k_notional > 0 else 0.0

        score = wallet_weight * (0.5 + 0.5 * notional_weight) * recency_weight
        # Numerical safety: clamp to [0, 1] in case of FP drift.
        score = max(0.0, min(1.0, score))

        size_total = float(g["size_shares"].sum())
        avg_price = float((g["price"] * g["size_shares"]).sum() / size_total) if size_total > 0 else 0.0

        out.append({
            "market_id": market_id,
            "side": side,
            "outcome": str(outcome) if outcome is not None else "YES",
            "wallets": unique_wallets,
            "avg_price": avg_price,
            "notional_usdc": notional,
            "correlation_score": round(score, 4),
        })
    return out


def position_size_from_score(
    score: float,
    base_usdc: float,
    max_usdc: float,
    *,
    anchor: float = 0.5,
    steepness: float = 2.0,
) -> float:
    """Scale a USDC stake by a [0, 1] signal-strength score.

    Math:
        multiplier = clamp(1 + steepness * (score - anchor), 0.25, 3.0)
        size       = clamp(base_usdc * multiplier, base_usdc * 0.25, max_usdc)

    Intuition with the defaults (`anchor=0.5`, `steepness=2.0`):
        score = 0.5  → multiplier = 1.0       → size = base_usdc
        score = 1.0  → multiplier = 2.0       → size ≈ 2× base   (capped at 3×)
        score = 0.0  → multiplier = 0.0 → 0.25 → size = 0.25 × base
        score = 0.75 → multiplier = 1.5       → size = 1.5 × base

    Note: the spec asks for "score=1.0 → ~3×". With steepness=2.0 the raw
    multiplier at score=1.0 is 2.0, which is reached normally; to hit the
    3.0 ceiling the caller may pass `steepness=5.0` (then score=1.0 → 3.0
    after the clamp). The 3× upper clamp is what makes "~3×" achievable.

    `score` outside [0, 1] is tolerated (no hard assert) — the multiplier
    clamp handles the rest, so noisy upstream signals can't blow up sizing.
    """
    if base_usdc <= 0 or max_usdc <= 0:
        return 0.0

    raw_mult = 1.0 + steepness * (float(score) - anchor)
    multiplier = max(_SIZE_MULT_MIN, min(_SIZE_MULT_MAX, raw_mult))

    floor = base_usdc * _SIZE_MULT_MIN
    size = base_usdc * multiplier
    return float(max(floor, min(max_usdc, size)))
