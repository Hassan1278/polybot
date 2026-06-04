"""Tests for `leaderboard_scraper._ranking_score`.

Formula under test:
    pnl_signal    = tanh(realized_pnl / 5000)
    wr_signal     = (win_rate - 0.5) * 2     (0 if win_rate is None)
    sharpe_signal = tanh(sharpe / 1.5)        (0 if sharpe is None)
    depth_signal  = tanh(n_decisions / 50)
    score = 0.40*pnl + 0.30*wr + 0.15*sharpe + 0.15*depth

Rejection floors:
    - dominant_category is None              → 0.0
    - n_decisions < 5                        → 0.0
    - |realized_pnl_usdc| < 50               → 0.0
"""

from __future__ import annotations

import math

import pytest

from services.ingest.jobs.leaderboard_scraper import _ranking_score


def _stats(**kw):
    base = {
        "realized_pnl_usdc": 0.0,
        "win_rate": None,
        "sharpe": None,
        "n_decisions": 0,
    }
    base.update(kw)
    return base


# ── happy path: a "sharp" wallet scores meaningfully positive ────────────────

def test_ranking_sharp_wallet_scores_positive():
    s = _ranking_score(
        _stats(
            realized_pnl_usdc=2_500.0,
            win_rate=0.70,
            sharpe=1.2,
            n_decisions=40,
        ),
        dominant_category="politics",
    )
    # All four signals are positive, so the composite must be > 0.3.
    assert s > 0.3
    assert s <= 1.0


def test_ranking_exact_formula_matches_reference():
    # Pin down the math with known inputs so an accidental weight change
    # is caught loudly.
    pnl = 5_000.0   # tanh(1) ≈ 0.7616
    wr = 0.75       # (0.75-0.5)*2 = 0.5
    sharpe = 1.5    # tanh(1) ≈ 0.7616
    n = 50          # tanh(1) ≈ 0.7616

    expected = (
        math.tanh(pnl / 5000) * 0.40
        + (wr - 0.5) * 2 * 0.30
        + math.tanh(sharpe / 1.5) * 0.15
        + math.tanh(n / 50) * 0.15
    )
    s = _ranking_score(
        _stats(realized_pnl_usdc=pnl, win_rate=wr, sharpe=sharpe, n_decisions=n),
        dominant_category="politics",
    )
    assert s == pytest.approx(expected, abs=1e-9)


# ── edge cases / rejection floors ────────────────────────────────────────────

def test_ranking_none_category_returns_zero():
    s = _ranking_score(
        _stats(realized_pnl_usdc=10_000.0, win_rate=0.9, n_decisions=100),
        dominant_category=None,
    )
    assert s == 0.0


def test_ranking_too_few_decisions_returns_zero():
    s = _ranking_score(
        _stats(realized_pnl_usdc=10_000.0, win_rate=0.9, n_decisions=3),
        dominant_category="politics",
    )
    assert s == 0.0


def test_ranking_below_pnl_floor_returns_zero():
    # |realised PnL| under the $50 floor → zero regardless of WR / sharpe.
    s = _ranking_score(
        _stats(realized_pnl_usdc=10.0, win_rate=0.9, n_decisions=20,
               sharpe=2.0),
        dominant_category="politics",
    )
    assert s == 0.0


def test_ranking_losing_wallet_scores_negative():
    # Big negative PnL and sub-50% WR should pull the composite below 0.
    s = _ranking_score(
        _stats(realized_pnl_usdc=-3_000.0, win_rate=0.30,
               sharpe=-0.5, n_decisions=30),
        dominant_category="politics",
    )
    assert s < 0


def test_ranking_missing_winrate_and_sharpe_treated_as_neutral():
    # When win_rate / sharpe are None they contribute 0 (not NaN). PnL +
    # depth alone should still drive a small positive score.
    s = _ranking_score(
        _stats(realized_pnl_usdc=1_000.0, win_rate=None,
               sharpe=None, n_decisions=20),
        dominant_category="politics",
    )
    expected = (
        math.tanh(1_000.0 / 5000.0) * 0.40
        + 0.0
        + 0.0
        + math.tanh(20 / 50.0) * 0.15
    )
    assert s == pytest.approx(expected, abs=1e-9)
    assert s > 0
