"""Tests for the pure core of scripts/favorite_backtest.py. The CLOB/DB I/O is
integration-level and runs on the VPS."""

from __future__ import annotations

from scripts.favorite_backtest import edge_stats, pick_favorite, size_bucket, verdict


def test_size_bucket():
    assert size_bucket(2) == "2 (binary)"
    assert size_bucket(1) == "2 (binary)"
    assert size_bucket(3) == "3-4" and size_bucket(4) == "3-4"
    assert size_bucket(5) == "5-8" and size_bucket(8) == "5-8"
    assert size_bucket(9) == "9+" and size_bucket(20) == "9+"


def test_pick_favorite():
    sibs = [{"price": 0.2, "id": "a"}, {"price": 0.5, "id": "b"}, {"price": 0.3, "id": "c"}]
    assert pick_favorite(sibs)["id"] == "b"
    # ignores unpriced siblings
    assert pick_favorite([{"price": None}, {"price": 0.1, "id": "x"}])["id"] == "x"
    assert pick_favorite([]) is None
    assert pick_favorite([{"price": None}]) is None


def test_edge_stats():
    # favorite priced 0.40 but wins 0.50 -> +0.10 edge
    rows = [(1.0, 0.4), (0.0, 0.4), (1.0, 0.4), (0.0, 0.4)]  # win 50% @ 0.40
    s = edge_stats(rows)
    assert s["n"] == 4 and abs(s["hit"] - 0.5) < 1e-9 and abs(s["price"] - 0.4) < 1e-9
    assert abs(s["edge"] - 0.10) < 1e-9
    assert edge_stats([]) == {"n": 0}


def test_verdict():
    assert verdict({"n": 0}) == "n/a"
    assert verdict({"n": 50, "edge": 0.10, "se": 0.02}) == "+EV"        # 0.10-0.04>0
    assert verdict({"n": 50, "edge": -0.10, "se": 0.02}) == "NEGATIVE"  # -0.10+0.04<0
    assert verdict({"n": 50, "edge": 0.03, "se": 0.02}) == "breakeven/noise"  # CI spans 0
