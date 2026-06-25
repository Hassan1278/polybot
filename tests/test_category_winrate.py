"""Tests for the pure per-category win-rate aggregation in
scripts/category_winrate.py (the DB join + fetch are thin I/O run on the VPS)."""

from __future__ import annotations

from scripts.category_winrate import (
    aggregate_by_category,
    bet_outcome,
    settled_market_nets,
)


def _ev(cond, typ, side, usdc):
    return {"conditionId": cond, "type": typ, "side": side, "usdcSize": usdc,
            "size": 0, "price": 0}


def test_settled_net_simple_roundtrip():
    nets = settled_market_nets([
        _ev("A", "TRADE", "BUY", 60.0),
        _ev("A", "TRADE", "SELL", 65.0),
    ], open_conds=set())
    assert round(nets["A"], 2) == 5.0


def test_settled_net_redeemed_winner_nets_cost_correctly():
    # The key fix: a held-to-resolution win logs BUY + REDEEM under the SAME
    # conditionId (even though token ids differ), so cashflow nets the cost.
    nets = settled_market_nets([
        _ev("A", "TRADE", "BUY", 45.0),
        _ev("A", "REDEEM", "", 100.0),
    ], open_conds=set())
    assert round(nets["A"], 2) == 55.0          # not +100, not "unknown"


def test_settled_net_expired_worthless_is_full_loss():
    # Bought, never sold/redeemed, not open -> lost the cost.
    nets = settled_market_nets([_ev("A", "TRADE", "BUY", 30.0)], open_conds=set())
    assert round(nets["A"], 2) == -30.0


def test_settled_net_excludes_open_markets():
    nets = settled_market_nets([
        _ev("A", "TRADE", "BUY", 60.0),
        _ev("A", "TRADE", "SELL", 65.0),
        _ev("B", "TRADE", "BUY", 10.0),
    ], open_conds={"B"})
    assert "A" in nets and "B" not in nets        # B still open -> excluded


def test_bet_outcome_thresholds():
    assert bet_outcome(5.0) == "win"
    assert bet_outcome(-5.0) == "loss"
    assert bet_outcome(0.0) == "flat"
    assert bet_outcome(0.005) == "flat"     # sub-cent noise ignored
    assert bet_outcome(-0.005) == "flat"


def test_aggregate_winrate_excludes_flats():
    items = [
        ("weather", "win", 1.0),
        ("weather", "win", 1.0),
        ("weather", "loss", -5.0),
        ("weather", "flat", 0.0),
    ]
    out = aggregate_by_category(items)
    w = out["weather"]
    assert w["win"] == 2 and w["loss"] == 1 and w["flat"] == 1 and w["n"] == 4
    assert round(w["winrate"], 4) == round(2 / 3, 4)   # flat excluded from rate
    assert round(w["net"], 2) == -3.0                  # +WR but NET NEGATIVE (fat tail)


def test_aggregate_separates_categories():
    items = [
        ("crypto", "win", 10.0),
        ("crypto", "loss", -2.0),
        ("sports", "loss", -4.0),
    ]
    out = aggregate_by_category(items)
    assert out["crypto"]["winrate"] == 0.5
    assert round(out["crypto"]["net"], 2) == 8.0
    assert out["sports"]["winrate"] == 0.0
    assert round(out["sports"]["net"], 2) == -4.0


def test_aggregate_winrate_none_when_all_flat():
    out = aggregate_by_category([("x", "flat", 0.0)])
    assert out["x"]["winrate"] is None


def test_high_winrate_can_be_unprofitable():
    # 9 small wins + 1 big loss = 90% win-rate, negative net. The headline point.
    items = [("weather", "win", 1.0)] * 9 + [("weather", "loss", -20.0)]
    out = aggregate_by_category(items)
    assert out["weather"]["winrate"] == 0.9
    assert out["weather"]["net"] < 0
