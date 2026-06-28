"""Tests for the pure relative-divergence core (scripts/cross_venue_divergence.py).

Relative divergence in odds space + the systematic-vs-noise verdict are pure and
exact; the Limitless/Polymarket I/O is integration-level and run on the VPS.
"""

from __future__ import annotations

import math

from scripts.cross_venue_divergence import divergence_summary, logit, relative_divergence


def test_logit_basic_and_clamped():
    assert abs(logit(0.5)) < 1e-12
    assert logit(0.75) > 0 and logit(0.25) < 0
    # clamped off the boundary -> finite, not inf
    assert math.isfinite(logit(0.0)) and math.isfinite(logit(1.0))


def test_relative_divergence_captures_tail_blowup():
    # 0.025 vs 0.006: tiny absolute gap (0.019) but ~4x ratio and a big log-odds move.
    rd = relative_divergence(0.025, 0.006)
    assert abs(rd["abs_gap"] - 0.019) < 1e-9
    assert abs(rd["ratio"] - (0.025 / 0.006)) < 1e-9
    assert rd["log_odds_diff"] > 1.0                    # large relative divergence


def test_relative_divergence_liquid_agreement_is_small():
    rd = relative_divergence(0.685, 0.655)              # near-even, venues agree
    assert abs(rd["log_odds_diff"]) < 0.2


def test_relative_divergence_sign_and_guards():
    assert relative_divergence(0.05, 0.02)["log_odds_diff"] > 0   # Limitless richer
    assert relative_divergence(0.02, 0.05)["log_odds_diff"] < 0   # Polymarket richer
    assert relative_divergence(None, 0.5) is None
    assert relative_divergence(0.0, 0.5) is None
    assert relative_divergence(0.5, 1.0) is None


# ── divergence_summary: the systematic-vs-noise verdict ──────────────────────

def test_summary_flags_systematic_bias():
    # every pair: Limitless richer by a consistent +0.8 log-odds -> systematic.
    s = divergence_summary([0.8, 0.75, 0.85, 0.78, 0.82])
    assert s["n"] == 5
    assert s["systematic"] is True
    assert s["lim_higher_frac"] == 1.0
    assert s["mean_log_odds"] > 0.5


def test_summary_flags_symmetric_noise():
    # big divergences but no consistent direction -> noise (mean ≈ 0).
    s = divergence_summary([1.4, -1.3, 1.1, -1.2, 0.9, -1.0])
    assert s["systematic"] is False
    assert abs(s["mean_log_odds"]) < 0.3
    assert s["median_abs_log_odds"] > 1.0               # diverges a lot, just not directionally


def test_summary_empty():
    assert divergence_summary([])["n"] == 0
