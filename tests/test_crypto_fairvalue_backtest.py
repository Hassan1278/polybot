"""Tests for the pure core of scripts/crypto_fairvalue_backtest.py.

The model math (lognormal N(d2) fair value, realized vol, the divergence binning,
and the realized P&L of betting toward fair value) is pure and exact; the spot /
price-history I/O is integration-level and run on the VPS.
"""

from __future__ import annotations

from scripts.crypto_fairvalue_backtest import (
    bs_prob_above,
    divergence_table,
    fair_value,
    follow_model_edge,
    realized_vol,
)

# ── bs_prob_above / fair_value ───────────────────────────────────────────────

def test_bs_prob_atm_slightly_below_half():
    # ATM with vol: d2 = -½σ²T/(σ√T) = -¼ -> N(-0.25) ≈ 0.401 (lognormal median < mean)
    p = bs_prob_above(100, 100, 1.0, 0.5)
    assert 0.39 < p < 0.41


def test_bs_prob_deep_itm_and_otm():
    assert bs_prob_above(200, 100, 0.1, 0.5) > 0.97     # deep in-the-money -> ~1
    assert bs_prob_above(50, 100, 0.1, 0.5) < 0.03      # deep out-of-the-money -> ~0


def test_bs_prob_guards_degenerate_inputs():
    assert bs_prob_above(0, 100, 1, 0.5) is None
    assert bs_prob_above(100, 0, 1, 0.5) is None
    assert bs_prob_above(100, 100, 0, 0.5) is None
    assert bs_prob_above(100, 100, 1, 0) is None


def test_fair_value_above_and_below_are_complementary():
    pa = fair_value(120, 100, 0.2, 0.6, is_above=True)
    pb = fair_value(120, 100, 0.2, 0.6, is_above=False)
    assert abs((pa + pb) - 1.0) < 1e-9
    assert pa > 0.5                                      # spot above strike -> above-bet likely


# ── realized_vol ─────────────────────────────────────────────────────────────

def test_realized_vol_constant_log_returns_is_zero():
    assert realized_vol([100, 110, 121, 133.1]) < 1e-9  # +10% each step -> no variance
    assert realized_vol([100]) is None
    assert realized_vol([100, 100]) is None             # <3 points


def test_realized_vol_positive_for_choppy_series():
    v = realized_vol([100, 105, 100, 106, 99], periods_per_year=1.0)
    assert v and v > 0


# ── follow_model_edge (the headline P&L) ─────────────────────────────────────

def test_follow_model_edge_rewards_a_correct_model():
    # model fair (0.55) >> poly (0.30), and they all resolve YES -> buying YES at
    # 0.30 wins +0.70 each.
    fe = follow_model_edge([(0.30, 0.55, 1)] * 10, min_div=0.05)
    assert fe["n"] == 10
    assert abs(fe["edge"] - 0.70) < 1e-9


def test_follow_model_edge_punishes_a_wrong_model():
    # model says buy YES (fair>poly) but it resolves NO -> -0.30 each.
    fe = follow_model_edge([(0.30, 0.55, 0)] * 10, min_div=0.05)
    assert abs(fe["edge"] - (-0.30)) < 1e-9


def test_follow_model_edge_no_side_bet():
    # fair<poly -> bet NO at (1-poly); resolves NO -> edge = poly - 0 = poly.
    fe = follow_model_edge([(0.70, 0.40, 0)] * 5, min_div=0.05)
    assert abs(fe["edge"] - 0.70) < 1e-9


def test_follow_model_edge_ignores_small_divergence():
    assert follow_model_edge([(0.50, 0.52, 1), (0.50, 0.48, 0)], min_div=0.05)["n"] == 0


# ── divergence_table ─────────────────────────────────────────────────────────

def test_divergence_table_outcome_sides_with_model():
    # D=+0.25 (fair 0.55 >> poly 0.30), all resolve YES: outcome 1.0 is closer to
    # fair (0.55) than poly (0.30) -> model_err < poly_err (the edge signature).
    row = next(r for r in divergence_table([(0.30, 0.55, 1)] * 20) if r["n"])
    assert row["n"] == 20
    assert abs(row["mean_poly"] - 0.30) < 1e-9
    assert abs(row["mean_fair"] - 0.55) < 1e-9
    assert row["yes_rate"] == 1.0
    assert row["model_err"] < row["poly_err"]


def test_divergence_table_drops_malformed():
    table = divergence_table([(0.5, 0.5, 1), (None, 0.5, 1), (0.5, None, 0), (0.5, 0.5, 9)])
    assert sum(r["n"] for r in table) == 1
