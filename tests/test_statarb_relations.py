"""Tests for the intra-Polymarket no-arb core (services/statarb/relations.py).

The core is pure (ladders in, ArbOpportunity|None out), so these are exact:
edge math, depth-walking across levels, the marginal cutoff that stops adding
baskets once they stop being profitable, the min-depth/min-edge/fee gates, and
the K-outcome "buy the field" generalization.
"""

from __future__ import annotations

from services.statarb.relations import (
    asks_from_book,
    best_ask,
    bids_from_book,
    binary_complement_arb,
    field_buy_arb,
)


def _lv(price, size):
    # CLOB levels are string-valued — the normalizer must cope with that.
    return {"price": str(price), "size": str(size)}


# ── book extraction ──────────────────────────────────────────────────────────

def test_book_helpers_extract_sides_and_tolerate_empty():
    book = {"asks": [_lv(0.4, 10)], "bids": [_lv(0.3, 5)]}
    assert asks_from_book(book) == [{"price": "0.4", "size": "10"}]
    assert bids_from_book(book) == [{"price": "0.3", "size": "5"}]
    assert asks_from_book({}) == [] and asks_from_book(None) == []
    assert bids_from_book({}) == [] and bids_from_book(None) == []


def test_best_ask_is_lowest_valid_level():
    assert best_ask([_lv(0.45, 10), _lv(0.40, 5), _lv(0.50, 100)]) == 0.40   # cheapest
    assert best_ask([]) is None
    assert best_ask(None) is None
    assert best_ask([{"size": "10"}]) is None                                # malformed -> none


# ── binary YES/NO complementarity ────────────────────────────────────────────

def test_binary_arb_basic_edge_and_size():
    # 0.40 + 0.45 = 0.85 < 1, depth 100 each -> 100 pairs, $15 locked.
    opp = binary_complement_arb([_lv(0.40, 100)], [_lv(0.45, 100)])
    assert opp is not None
    assert opp.kind == "binary_complement"
    assert opp.shares == 100.0
    assert opp.cost_usdc == 85.0
    assert opp.payout_usdc == 100.0
    assert abs(opp.net_usdc - 15.0) < 1e-9
    # ROI = 15/85 = 1764.7 bps
    assert abs(opp.edge_bps - (15.0 / 85.0) * 10_000.0) < 1e-6
    assert [leg.label for leg in opp.legs] == ["YES", "NO"]


def test_binary_no_arb_when_sum_at_or_above_one():
    assert binary_complement_arb([_lv(0.55, 100)], [_lv(0.50, 100)]) is None   # 1.05
    assert binary_complement_arb([_lv(0.60, 100)], [_lv(0.40, 100)]) is None   # exactly 1.00


def test_binary_size_capped_by_thinner_leg():
    # YES depth 100, NO depth 60 -> only 60 pairs possible (need equal shares).
    opp = binary_complement_arb([_lv(0.40, 100)], [_lv(0.45, 60)])
    assert opp is not None
    assert opp.shares == 60.0
    assert abs(opp.cost_usdc - 0.85 * 60) < 1e-9
    assert abs(opp.net_usdc - (60 - 0.85 * 60)) < 1e-9


def test_binary_walks_multiple_levels():
    # YES: 50@0.40 then 100@0.48 ; NO: 80@0.45.
    #  pairs 1-50  : 0.40+0.45 = 0.85  -> cost 42.5
    #  pairs 51-80 : 0.48+0.45 = 0.93  -> cost 27.9  (NO depth caps at 80)
    opp = binary_complement_arb([_lv(0.40, 50), _lv(0.48, 100)], [_lv(0.45, 80)])
    assert opp is not None
    assert opp.shares == 80.0
    assert abs(opp.cost_usdc - (42.5 + 27.9)) < 1e-9
    assert abs(opp.net_usdc - (80.0 - 70.4)) < 1e-9


def test_binary_marginal_cutoff_stops_before_unprofitable_depth():
    # First level pair 0.40+0.45=0.85 (profit). Next 0.62+0.45=1.07 (>=1): STOP.
    # Even though depth exists, the unprofitable pairs are NOT taken.
    opp = binary_complement_arb([_lv(0.40, 50), _lv(0.62, 100)], [_lv(0.45, 200)])
    assert opp is not None
    assert opp.shares == 50.0
    assert abs(opp.net_usdc - 7.5) < 1e-9


def test_binary_fee_and_gas_reduce_edge():
    # 0.45 + 0.50 = 0.95, depth 100. 2% fee on $95 spend = $1.90; gas $0.05.
    opp = binary_complement_arb(
        [_lv(0.45, 100)], [_lv(0.50, 100)], fee_bps=200, gas_usdc=0.05
    )
    assert opp is not None
    assert abs(opp.fees_usdc - (95.0 * 0.02 + 0.05)) < 1e-9
    assert abs(opp.net_usdc - (100.0 - 95.0 - 1.90 - 0.05)) < 1e-9


def test_binary_fee_can_erase_a_thin_edge():
    # 0.49 + 0.50 = 0.99 gross edge $1 / 100 shares; a 2% fee ($1.98) erases it.
    assert binary_complement_arb([_lv(0.49, 100)], [_lv(0.50, 100)], fee_bps=200) is None


def test_binary_min_edge_bps_gate():
    # 0.498 + 0.50 = 0.998 -> 20 bps ROI; gate at 50 bps rejects, 10 bps passes.
    asks_y, asks_n = [_lv(0.498, 100)], [_lv(0.50, 100)]
    assert binary_complement_arb(asks_y, asks_n, min_edge_bps=50) is None
    assert binary_complement_arb(asks_y, asks_n, min_edge_bps=10) is not None


def test_binary_min_edge_usdc_gate():
    # $0.20 locked on 100 shares; require $1 -> rejected.
    assert binary_complement_arb([_lv(0.499, 100)], [_lv(0.50, 100)], min_edge_usdc=1.0) is None


def test_binary_tokens_propagate_to_legs():
    opp = binary_complement_arb(
        [_lv(0.40, 10)], [_lv(0.45, 10)], yes_token="TY", no_token="TN"
    )
    assert opp is not None
    assert opp.legs[0].token_id == "TY" and opp.legs[1].token_id == "TN"
    assert abs(opp.legs[0].avg_price - 0.40) < 1e-9
    assert abs(opp.legs[1].avg_price - 0.45) < 1e-9


def test_binary_handles_empty_or_malformed_books():
    assert binary_complement_arb([], [_lv(0.45, 10)]) is None
    assert binary_complement_arb([{"size": "10"}], [_lv(0.45, 10)]) is None   # no price key
    assert binary_complement_arb([_lv(-0.4, 10)], [_lv(0.45, 10)]) is None    # non-positive


# ── multi-outcome "buy the field" ────────────────────────────────────────────

def test_field_arb_three_outcomes():
    # 0.30 × 3 = 0.90 < 1, depth 100 each -> 100 baskets, $10 locked.
    opp = field_buy_arb([[_lv(0.30, 100)], [_lv(0.30, 100)], [_lv(0.30, 100)]])
    assert opp is not None
    assert opp.kind == "field_buy"
    assert opp.n_legs == 3
    assert opp.shares == 100.0
    assert abs(opp.cost_usdc - 90.0) < 1e-9
    assert abs(opp.net_usdc - 10.0) < 1e-9


def test_field_no_arb_when_field_sums_above_one():
    assert field_buy_arb([[_lv(0.40, 100)], [_lv(0.40, 100)], [_lv(0.40, 100)]]) is None  # 1.20


def test_field_size_capped_by_thinnest_outcome():
    # Thinnest leg has depth 25 -> at most 25 baskets.
    opp = field_buy_arb([[_lv(0.30, 100)], [_lv(0.30, 25)], [_lv(0.30, 100)]])
    assert opp is not None
    assert opp.shares == 25.0


def test_field_labels_and_tokens_propagate():
    opp = field_buy_arb(
        [[_lv(0.30, 10)], [_lv(0.30, 10)]],
        labels=["Alice", "Bob"],
        token_ids=["TA", "TB"],
    )
    assert opp is not None
    assert [leg.label for leg in opp.legs] == ["Alice", "Bob"]
    assert [leg.token_id for leg in opp.legs] == ["TA", "TB"]


def test_field_requires_at_least_two_outcomes():
    assert field_buy_arb([[_lv(0.30, 10)]]) is None
    assert field_buy_arb([]) is None


def test_field_rejects_when_any_outcome_has_no_book():
    # A missing leg means no guaranteed basket -> no arb.
    assert field_buy_arb([[_lv(0.30, 100)], [], [_lv(0.30, 100)]]) is None
