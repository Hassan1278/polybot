"""Tests for the pure core of scripts/weather_pnl.py — question parsing and realized
P&L. The gamma resolution + DB I/O is integration-level and run on the VPS.
"""

from __future__ import annotations

from scripts.weather_pnl import market_pnl, parse_q


def test_parse_q_variants():
    assert parse_q("Will the highest temperature in London be 28°C on June 21?") == \
        ("highest", "London", "28°C", "June 21")
    assert parse_q("Will the highest temperature in New York City be between 82-83°F on June 24?") == \
        ("highest", "New York City", "between 82-83°F", "June 24")
    assert parse_q("Will the lowest temperature in Hong Kong be 28°C on June 24?") == \
        ("lowest", "Hong Kong", "28°C", "June 24")
    assert parse_q("Will the highest temperature in Paris be 37°C or below on June 27?") == \
        ("highest", "Paris", "37°C or below", "June 27")
    assert parse_q("Bitcoin up or down?") == (None, None, None, None)


def test_market_pnl_held_to_resolution_no_won():
    # bought 10 NO @ 0.60 ($6), held; NO won -> 10 shares settle $10. P&L = 10 - 6 = +4.
    p = market_pnl([("BUY", 10.0, 6.0, 0.0)], no_won=True)
    assert p["net_shares"] == 10.0
    assert abs(p["realized"] - 4.0) < 1e-9


def test_market_pnl_held_to_resolution_no_lost():
    # same but the high hit the bucket (NO lost) -> settle $0. P&L = -6.
    p = market_pnl([("BUY", 10.0, 6.0, 0.0)], no_won=False)
    assert abs(p["realized"] - (-6.0)) < 1e-9


def test_market_pnl_flattened_before_resolution_is_roundtrip():
    # bought 10 @ 0.60, sold 10 @ 0.70 -> flat; outcome irrelevant, P&L = 7 - 6 = +1.
    p = market_pnl([("BUY", 10.0, 6.0, 0.0), ("SELL", 10.0, 7.0, 0.0)], no_won=True)
    assert p["net_shares"] == 0.0
    assert abs(p["realized"] - 1.0) < 1e-9


def test_market_pnl_fees_subtracted_and_unresolved():
    assert market_pnl([("BUY", 5.0, 3.0, 0.05)], no_won=None)["realized"] is None
    p = market_pnl([("BUY", 5.0, 3.0, 0.05)], no_won=True)   # settle 5, cost 3, fee .05
    assert abs(p["realized"] - (5.0 - 3.0 - 0.05)) < 1e-9
