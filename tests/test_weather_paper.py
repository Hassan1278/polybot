"""Tests for the pure core of scripts/weather_paper.py. CLOB/gamma/DB I/O runs on the VPS."""

from __future__ import annotations

from scripts.weather_paper import (
    agg,
    best_bid_ask,
    bet_pnl,
    daily_series,
    evaluate_ladder,
    max_drawdown,
    mid_of,
    no_pnl,
    open_position,
    rank_by_mid,
    simulate_portfolio,
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


def test_evaluate_ladder_winner_none():
    # the high landed in a bucket we didn't capture -> winner=None -> favorite LOST
    buckets = [{"label": "30", "bid": 0.43, "ask": 0.45, "mid": 0.44},
               {"label": "29", "bid": 0.28, "ask": 0.32, "mid": 0.30}]
    ev = evaluate_ladder(buckets, None)
    assert abs(ev["fav_yes"] + 0.45) < 1e-9          # favorite lost -> −ask
    assert abs(ev["fade_rank2_no"] - 0.28) < 1e-9    # rank2 also lost -> NO wins +bid


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


def test_bet_pnl():
    assert abs(bet_pnl(0.5, True, 5) - 5.0) < 1e-9    # 10 shares -> $10, +$5
    assert bet_pnl(0.5, False, 5) == -5.0             # loss capped at -stake
    assert bet_pnl(0.25, True, 5) == 15.0             # 20 shares -> $20, +$15
    assert bet_pnl(0.0, True, 5) is None and bet_pnl(None, True, 5) is None


def test_daily_series_and_drawdown():
    t0 = 1_700_000_000                                 # fixed ts (deterministic date)
    bets = [(t0, 0.5, True), (t0 + 100, 0.5, False),   # same UTC day: +5, -5 -> 0
            (t0 + 86400, 0.5, False)]                  # next day: -5
    s = daily_series(bets, 5)
    assert len(s) == 2
    assert abs(s[0][1]) < 1e-9 and abs(s[1][1] + 5.0) < 1e-9
    assert abs(max_drawdown([p for _d, p in s]) + 5.0) < 1e-9   # cum 0,-5 -> dd -5
    assert max_drawdown([]) == 0.0
    assert max_drawdown([3.0, 2.0]) == 0.0             # never below the start peak


def test_open_position():
    entry = [{"label": "30", "bid": 0.43, "ask": 0.45, "mid": 0.44},
             {"label": "29", "bid": 0.20, "ask": 0.24, "mid": 0.22}]
    latest = [{"label": "30", "bid": 0.53, "ask": 0.57, "mid": 0.55},
              {"label": "29", "bid": 0.10, "ask": 0.14, "mid": 0.12}]
    p = open_position(entry, latest, 5.0)               # bought "30" @0.45, now mid 0.55
    assert p["held"] == "30" and p["entry_ask"] == 0.45
    assert abs(p["shares"] - 5 / 0.45) < 1e-9
    assert abs(p["unreal"] - (5 / 0.45 * 0.55 - 5)) < 1e-9   # +$1.11 unrealized
    # held bucket absent from the latest snap -> can't mark -> unreal None
    p2 = open_position(entry, [{"label": "29", "mid": 0.12}], 5.0)
    assert p2["cur_mid"] is None and p2["unreal"] is None
    # no ask at entry -> no position
    assert open_position([{"label": "x", "bid": 0.1, "ask": None, "mid": 0.1}], latest, 5.0) is None


def test_simulate_portfolio():
    # $10 @ 0.50 wins (20 shares -> $20, +$10); $10 @ 0.40 loses (-$10) -> net 0
    port = simulate_portfolio([(0.50, True), (0.40, False)], 2000, 10)
    assert port["n"] == 2 and abs(port["staked"] - 20) < 1e-9
    assert abs(port["pnl"]) < 1e-9 and abs(port["final_equity"] - 2000) < 1e-9
    # a clean winner at 0.40: 25 shares -> $25 on $10 staked, +$15, ROI +150%
    win = simulate_portfolio([(0.40, True)], 2000, 10)
    assert abs(win["pnl"] - 15) < 1e-9 and abs(win["roi"] - 1.5) < 1e-9
    assert simulate_portfolio([(None, True), (0.0, True)], 100, 5)["n"] == 0  # skips bad asks
