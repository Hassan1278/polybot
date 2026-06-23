"""Tests for the crypto win-region model in packages/polybot/asset_direction:
`win_region()` and `regions_conflict()`.

These back the generalized same-day bucket guard that catches threshold-vs-range
conflicts (e.g. holding "BTC above 62k NO" alongside "BTC between 60-62k NO").
"""

from __future__ import annotations

import math

from polybot.asset_direction import regions_conflict, win_region

INF = math.inf


# ── win_region: thresholds ───────────────────────────────────────────────────

def test_win_region_threshold_above_yes_no():
    q = "Will Bitcoin be above $62,000 on June 23?"
    assert win_region(q, None, "YES", "BUY") == [(62000.0, INF)]   # bull: wins > 62k
    assert win_region(q, None, "NO", "BUY") == [(-INF, 62000.0)]   # bear: wins < 62k


def test_win_region_threshold_below():
    q = "Will Bitcoin fall below $60,000 on June 23?"
    assert win_region(q, None, "YES", "BUY") == [(-INF, 60000.0)]  # bear
    assert win_region(q, None, "NO", "BUY") == [(60000.0, INF)]    # bull


def test_win_region_threshold_k_suffix():
    assert win_region("Bitcoin above 62k today?", None, "NO", "BUY") == [(-INF, 62000.0)]


def test_win_region_sell_flips_direction():
    q = "Will Bitcoin be above $62,000 on June 23?"
    # SELL YES is the same exposure as BUY NO.
    assert win_region(q, None, "YES", "SELL") == [(-INF, 62000.0)]


def test_win_region_question_and_slug_agree_on_level():
    # Question spells "$62,000", slug spells "62k" — same level, not ambiguous.
    assert win_region("Will Bitcoin be above $62,000?", "btc-above-62k-jun23",
                      "NO", "BUY") == [(-INF, 62000.0)]


# ── win_region: ranges ───────────────────────────────────────────────────────

def test_win_region_range_in_and_out():
    q = "Will Bitcoin be between $60,000 and $62,000 on June 23?"
    assert win_region(q, None, "YES", "BUY") == [(60000.0, 62000.0)]              # in
    assert win_region(q, None, "NO", "BUY") == [(-INF, 60000.0), (62000.0, INF)]  # out


# ── win_region: ambiguity → None (fail open) ─────────────────────────────────

def test_win_region_none_without_price_level():
    assert win_region("Bitcoin Up or Down on June 23?", "btc-updown", "UP", "BUY") is None


def test_win_region_none_on_compound_threshold():
    # Two genuinely different dollar levels → ambiguous → None.
    q = "Will Bitcoin be above $62,000, up from $60,000?"
    assert win_region(q, None, "YES", "BUY") is None


def test_win_region_none_on_unknown_outcome():
    assert win_region("Will Bitcoin be above $62,000?", None, "MAYBE", "BUY") is None


# ── regions_conflict ─────────────────────────────────────────────────────────

def test_conflict_threshold_vs_range_the_reported_bug():
    above_62k_no = [(-INF, 62000.0)]                       # wins <= 62k
    between_60_62_no = [(-INF, 60000.0), (62000.0, INF)]   # wins < 60k OR > 62k
    assert regions_conflict(above_62k_no, between_60_62_no) is True


def test_conflict_adjacent_yes_buckets():
    assert regions_conflict([(62000.0, 64000.0)], [(64000.0, 66000.0)]) is True


def test_conflict_disjoint_opposite_threshold():
    # "above 62k YES" vs "above 62k NO" — pure opposite bets.
    assert regions_conflict([(62000.0, INF)], [(-INF, 62000.0)]) is True


def test_no_conflict_nested_refinement():
    # "between 60-62k YES" is a subset of "above 58k YES": both win at 60-62k.
    assert regions_conflict([(60000.0, 62000.0)], [(58000.0, INF)]) is False


def test_no_conflict_identical_region():
    region = [(-INF, 60000.0), (62000.0, INF)]
    assert regions_conflict(region, list(region)) is False


def test_no_conflict_reissue_jitter_within_tolerance():
    # Same out-of-band bet reissued ~0.3% off → not a new conflicting band.
    a = [(-INF, 62000.0), (64000.0, INF)]
    b = [(-INF, 62150.0), (64200.0, INF)]
    assert regions_conflict(a, b) is False
