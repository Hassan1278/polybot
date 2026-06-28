"""Tests for the pure relative-divergence core (scripts/cross_venue_divergence.py).

Relative divergence in odds space + the systematic-vs-noise verdict are pure and
exact; the Limitless/Polymarket I/O is integration-level and run on the VPS.
"""

from __future__ import annotations

import math

from scripts.cross_venue_divergence import (
    _duration_seconds,
    best_bid_ask,
    cross_edge,
    divergence_summary,
    logit,
    relative_divergence,
)


def test_duration_parsing_for_matching():
    assert _duration_seconds("BTC Up or Down - 15 Min") == 900
    assert _duration_seconds("ETH Up or Down - 5 Min") == 300
    assert _duration_seconds("SOL Up or Down - Hourly") == 3600
    assert _duration_seconds("XRP Up or Down - Daily") == 86400
    # Polymarket time-range form -> computed window length
    assert _duration_seconds("Bitcoin Up or Down - June 28, 8:00AM-8:15AM ET") == 900
    assert _duration_seconds("Bitcoin Up or Down - June 28, 8:10AM-8:15AM ET") == 300
    # undeterminable -> None (skip, don't contaminate the match)
    assert _duration_seconds("Bitcoin Up or Down on June 28?") is None


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


# ── best_bid_ask: real book parsing (the fix for the seed-price artifact) ─────

def test_best_bid_ask_picks_extremes_and_coerces_str():
    # Limitless floats vs Polymarket CLOB string prices — both supported.
    bids = [{"price": 0.25, "size": 1}, {"price": 0.21, "size": 1}, {"price": 0.011, "size": 1}]
    asks = [{"price": "0.48", "size": 1}, {"price": "0.9", "size": 1}]
    assert best_bid_ask(bids, asks) == (0.25, 0.48)        # highest bid, lowest ask


def test_best_bid_ask_handles_empty_and_malformed():
    assert best_bid_ask([], []) == (None, None)            # untraded one-sided book -> skip
    assert best_bid_ask([{"price": 0.3, "size": 1}], None) == (0.3, None)
    assert best_bid_ask([{"size": 1}], [{"price": "x"}]) == (None, None)  # malformed dropped


# ── cross_edge: the executable lock metric ───────────────────────────────────

def test_cross_edge_no_lock_when_books_dont_cross():
    # the real BTC example: LIM 0.25/0.48 around POLY 0.30/0.32 -> neither side crosses.
    ce = cross_edge(lim_bid=0.25, lim_ask=0.48, poly_bid=0.30, poly_ask=0.32)
    assert ce["edge"] < 0                                  # no arb
    # best of (poly_bid−lim_ask=-0.18, lim_bid−poly_ask=-0.07) = -0.07
    assert abs(ce["edge"] - (-0.07)) < 1e-9


def test_cross_edge_locks_when_one_bid_tops_other_ask():
    # POLY will buy Up at 0.55 while LIM sells Up at 0.45 -> buy@LIM, hedge@POLY.
    ce = cross_edge(lim_bid=0.40, lim_ask=0.45, poly_bid=0.55, poly_ask=0.60)
    assert abs(ce["edge"] - 0.10) < 1e-9                   # 0.55 − 0.45
    assert ce["direction"] == "Up@LIM/Down@POLY"


def test_cross_edge_reverse_direction():
    ce = cross_edge(lim_bid=0.60, lim_ask=0.65, poly_bid=0.40, poly_ask=0.45)
    assert abs(ce["edge"] - 0.15) < 1e-9                   # lim_bid 0.60 − poly_ask 0.45
    assert ce["direction"] == "Up@POLY/Down@LIM"


def test_cross_edge_guards_missing_quote():
    assert cross_edge(None, 0.5, 0.5, 0.5) is None
    assert cross_edge(0.5, 0.5, 0.5, None) is None
