"""Tests for `cluster_active_wallets` time-decay scoring.

The function now accepts `half_life_seconds`, `k_wallets`, and `k_notional`
kwargs in addition to the legacy `window_minutes`/`min_wallets` arguments. We
verify that:

  * fresh, dense wallet activity ranks higher than stale or sparse activity
  * the new kwargs actually move the resulting `correlation_score`
  * pathological inputs (empty frame, half-life <= 0, all trades expired by
    the cutoff) are handled safely
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from polybot.stats import cluster_active_wallets


def _trade(off_seconds: float, **kw):
    """Construct a single trade row offset `off_seconds` into the past."""
    return {
        "ts": datetime.now(tz=timezone.utc) - timedelta(seconds=off_seconds),
        "market_id": kw.get("m", "M1"),
        "wallet": kw.get("w", "0xa"),
        "outcome": kw.get("o", "YES"),
        "side": kw.get("s", "BUY"),
        "size_shares": kw.get("sz", 10.0),
        "price": kw.get("p", 0.4),
        "notional_usdc": kw.get("n", 4.0),
        "fee_usdc": kw.get("f", 0.0),
    }


def test_cluster_decay_happy_path_fresh_burst_scores_high():
    # 4 distinct wallets all trading within the last 30s on M1/BUY:
    # large fresh burst → score should be well above 0.5.
    df = pd.DataFrame([
        _trade(5,  w="0xa", n=500),
        _trade(10, w="0xb", n=500),
        _trade(15, w="0xc", n=500),
        _trade(20, w="0xd", n=500),
    ])
    out = cluster_active_wallets(
        df,
        window_minutes=10,
        min_wallets=3,
        half_life_seconds=300.0,
        k_wallets=2.5,
        k_notional=2_000.0,
    )
    assert len(out) == 1
    cluster = out[0]
    assert cluster["market_id"] == "M1"
    assert cluster["side"] == "BUY"
    assert set(cluster["wallets"]) == {"0xa", "0xb", "0xc", "0xd"}
    # Score must be in [0, 1] and high enough to fire a typical 0.5 gate.
    assert 0.5 < cluster["correlation_score"] <= 1.0


def test_cluster_decay_older_trades_score_lower_than_fresher():
    # Two equivalent groups except for age — the fresher group must outscore
    # the stale group thanks to the recency-decay factor.
    fresh = pd.DataFrame([
        _trade(1,  w="0xa", n=600, m="MF"),
        _trade(2,  w="0xb", n=600, m="MF"),
        _trade(3,  w="0xc", n=600, m="MF"),
    ])
    stale = pd.DataFrame([
        _trade(540, w="0xa", n=600, m="MS"),  # ~9 minutes old at HL=60s
        _trade(545, w="0xb", n=600, m="MS"),
        _trade(550, w="0xc", n=600, m="MS"),
    ])
    fresh_out = cluster_active_wallets(
        fresh, window_minutes=15, min_wallets=3,
        half_life_seconds=60.0,
    )
    stale_out = cluster_active_wallets(
        stale, window_minutes=15, min_wallets=3,
        half_life_seconds=60.0,
    )
    assert len(fresh_out) == 1 and len(stale_out) == 1
    assert fresh_out[0]["correlation_score"] > stale_out[0]["correlation_score"]
    # Recency decay is exponential — at ~9 half-lives the stale score must be
    # essentially zero after rounding.
    assert stale_out[0]["correlation_score"] < 0.1


def test_cluster_decay_empty_frame_returns_empty_list():
    out = cluster_active_wallets(
        pd.DataFrame(),
        window_minutes=10,
        min_wallets=2,
        half_life_seconds=300.0,
    )
    assert out == []


def test_cluster_decay_zero_half_life_is_safe():
    # half_life_seconds <= 0 must not raise (division-by-zero / log guard).
    df = pd.DataFrame([
        _trade(5, w="0xa", n=100),
        _trade(6, w="0xb", n=100),
        _trade(7, w="0xc", n=100),
    ])
    out = cluster_active_wallets(
        df, window_minutes=10, min_wallets=2,
        half_life_seconds=0.0,
    )
    assert isinstance(out, list)
    assert len(out) == 1
    # Score is still clamped to [0, 1].
    assert 0.0 <= out[0]["correlation_score"] <= 1.0


def test_cluster_decay_all_trades_outside_window_returns_empty():
    # Every trade is older than window_minutes → nothing should be emitted.
    df = pd.DataFrame([
        _trade(60 * 30, w="0xa"),
        _trade(60 * 31, w="0xb"),
        _trade(60 * 32, w="0xc"),
    ])
    out = cluster_active_wallets(df, window_minutes=10, min_wallets=2)
    assert out == []


def test_cluster_decay_larger_k_notional_lowers_score():
    # Increasing k_notional makes the same notional saturate less, so the
    # notional sub-factor — and therefore the composite — must drop.
    df = pd.DataFrame([
        _trade(5, w="0xa", n=300),
        _trade(6, w="0xb", n=300),
        _trade(7, w="0xc", n=300),
    ])
    small_k = cluster_active_wallets(
        df, window_minutes=10, min_wallets=3,
        k_notional=500.0, half_life_seconds=600.0,
    )
    big_k = cluster_active_wallets(
        df, window_minutes=10, min_wallets=3,
        k_notional=50_000.0, half_life_seconds=600.0,
    )
    assert small_k and big_k
    assert small_k[0]["correlation_score"] > big_k[0]["correlation_score"]
