from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from polybot.stats import cluster_active_wallets, jaccard_matrix, wallet_stats_from_trades


def _t(off: int, **kw):
    return {"ts": datetime.now(tz=timezone.utc) - timedelta(minutes=off),
            "market_id": kw.get("m", "M1"),
            "wallet": kw.get("w", "0xa"),
            "outcome": kw.get("o", "YES"),
            "side": kw.get("s", "BUY"),
            "size_shares": kw.get("sz", 10.0),
            "price": kw.get("p", 0.4),
            "notional_usdc": kw.get("n", 4.0),
            "fee_usdc": kw.get("f", 0.0)}


def test_wallet_stats_empty():
    s = wallet_stats_from_trades(pd.DataFrame())
    assert s["trade_count"] == 0


def test_wallet_stats_profitable():
    df = pd.DataFrame([
        _t(60, m="M1", s="BUY",  n=10, p=0.3, sz=33),
        _t(50, m="M1", s="SELL", n=20, p=0.6, sz=33),
        _t(40, m="M2", s="BUY",  n=10, p=0.5, sz=20),
        _t(30, m="M2", s="SELL", n=15, p=0.75, sz=20),
    ])
    s = wallet_stats_from_trades(df, window_days=None)
    assert s["pnl_usdc"] > 0
    assert s["win_rate"] == 1.0
    assert s["trade_count"] == 4


def test_cluster_min_wallets():
    df = pd.DataFrame([
        _t(2, w="0xa", m="M1"),
        _t(2, w="0xb", m="M1"),
    ])
    out = cluster_active_wallets(df, window_minutes=10, min_wallets=3)
    assert out == []
    out = cluster_active_wallets(df, window_minutes=10, min_wallets=2)
    assert len(out) == 1
    assert set(out[0]["wallets"]) == {"0xa", "0xb"}


def test_jaccard_self_one():
    labels, m = jaccard_matrix({"a": {"x", "y"}, "b": {"y", "z"}, "c": set()})
    i = labels.index("a")
    assert m[i, i] == 1.0
    j = labels.index("b")
    assert 0 < m[i, j] < 1
    k = labels.index("c")
    assert m[i, k] == 0.0
