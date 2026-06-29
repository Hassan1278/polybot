"""Tests for the pure core of scripts/weather_paper.py. CLOB/gamma/DB I/O runs on the VPS."""

from __future__ import annotations

from scripts.weather_paper import (
    agg,
    best_bid_ask,
    evaluate_ladder,
    mid_of,
    no_pnl,
    rank_by_mid,
    yes_pnl,
)


def test_best_bid_ask():
    book = {"bids": [{"price": "0.40", "size": "1"}, {"price": "0.42", "size": "1"}],
            "asks": [{"price": "0.46", "size": "1"}, {"price": "0.45", "size": "1"}]}
    bid, ask = best_bid_ask(book)
    assert bid == 0.42 and ask == 0.45          # best bid = highest, best ask = lowest
    assert best_bid_ask({}) == (None, None)
    assert best_bid_ask({"bids": [], "asks": [{"price": "0.5"}]}) == (None, 0.5)


def test_mid_of():
    assert mid_of(0.42, 0.46) == 0.44
    assert mid_of(None, 0.5) == 0.5 and mid_of(0.5, None) == 0.5
    assert mid_of(None, None) is None


def test_yes_no_pnl():
    assert abs(yes_pnl(0.40, True) - 0.60) < 1e-9     # bought 0.40, won -> +0.60
    assert abs(yes_pnl(0.40, False) + 0.40) < 1e-9    # lost -> -0.40
    assert yes_pnl(None, True) is None
    # buy NO by hitting bid 0.05 (cost 0.95); YES lost -> +0.05; YES won -> -0.95
    assert abs(no_pnl(0.05, False) - 0.05) < 1e-9
    assert abs(no_pnl(0.05, True) + 0.95) < 1e-9


def test_rank_by_mid_drops_unpriced():
    bs = [{"label": "a", "mid": 0.2}, {"label": "b", "mid": None}, {"label": "c", "mid": 0.5}]
    r = rank_by_mid(bs)
    assert [x["label"] for x in r] == ["c", "a"]


def test_evaluate_ladder():
    # favorite "30" priced ask 0.45 and it WON -> fav_yes = +0.55; rank2 "29" lost -> fade NO +0.30
    buckets = [
        {"label": "30", "bid": 0.43, "ask": 0.45, "mid": 0.44},   # favorite, wins
        {"label": "29", "bid": 0.28, "ask": 0.32, "mid": 0.30},   # rank2, loses
        {"label": "35", "bid": 0.02, "ask": 0.05, "mid": 0.035},  # longshot, loses
    ]
    ev = evaluate_ladder(buckets, "30")
    assert abs(ev["fav_yes"] - 0.55) < 1e-9
    assert "fav_yes_85c" not in ev                       # favorite mid 0.44 < 0.85
    assert abs(ev["fade_rank2_no"] - 0.28) < 1e-9        # bid 0.28, rank2 lost -> +0.28
    assert abs(ev["fade_longshots_no"] - 0.02) < 1e-9    # bid 0.02, longshot lost -> +0.02


def test_evaluate_ladder_85c_subset():
    buckets = [{"label": "hi", "bid": 0.84, "ask": 0.86, "mid": 0.85},
               {"label": "lo", "bid": 0.10, "ask": 0.14, "mid": 0.12}]
    ev = evaluate_ladder(buckets, "hi")
    assert "fav_yes_85c" in ev and abs(ev["fav_yes_85c"] - 0.14) < 1e-9   # ask 0.86, won -> +0.14


def test_agg():
    s = agg([0.1, -0.1, 0.1, -0.1])
    assert s["n"] == 4 and abs(s["mean"]) < 1e-9 and s["se"] > 0
    assert agg([]) == {"n": 0}
    assert agg([0.5])["se"] is None      # n=1, no se
