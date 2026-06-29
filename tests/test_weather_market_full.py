"""Tests for the pure probability/calibration core of scripts/weather_market_full.py.
The CLOB/gamma/Open-Meteo I/O is integration-level and runs on the VPS."""

from __future__ import annotations

from scripts.weather_market_full import (
    argmax_accuracy,
    brier,
    gauss_bucket_prob,
    ladder_groups,
    topk_coverage,
)
from scripts.weather_truth import parse_bucket


def test_gauss_point_bucket_atm():
    p = gauss_bucket_prob(28.0, 1.4, parse_bucket("28°C"))   # 1° band centered on the mean
    assert 0.20 < p < 0.35                                    # ≈ 0.278


def test_gauss_far_bucket_near_zero():
    assert gauss_bucket_prob(28.0, 1.4, parse_bucket("35°C")) < 0.001


def test_gauss_open_below_and_higher():
    pb = gauss_bucket_prob(28.0, 1.4, parse_bucket("30°C or below"))   # CDF(2/1.4)
    assert 0.88 < pb < 0.95
    ph = gauss_bucket_prob(28.0, 1.4, parse_bucket("26°C or higher"))  # 1−CDF(−2/1.4)
    assert 0.88 < ph < 0.95


def test_gauss_guards():
    assert gauss_bucket_prob(None, 1.4, parse_bucket("28°C")) is None
    assert gauss_bucket_prob(28.0, 0.0, parse_bucket("28°C")) is None


def test_brier():
    rows = [{"p_fc": 0.8, "won": 1.0}, {"p_fc": 0.2, "won": 0.0}, {"p_fc": 0.5, "won": 1.0}]
    # (0.2² + 0.2² + 0.5²)/3 = 0.11
    assert abs(brier(rows, "p_fc") - 0.11) < 1e-9
    assert brier([{"won": 1.0}], "p_fc") is None


def test_argmax_accuracy():
    rows = [
        # ladder A: forecast's top bucket (28) won; market's top bucket (27) lost
        {"lad": "A", "p_fc": 0.5, "p_mkt": 0.3, "won": 1.0},
        {"lad": "A", "p_fc": 0.3, "p_mkt": 0.5, "won": 0.0},
        # ladder B: both top buckets agree on the winner (30)
        {"lad": "B", "p_fc": 0.6, "p_mkt": 0.6, "won": 1.0},
        {"lad": "B", "p_fc": 0.2, "p_mkt": 0.2, "won": 0.0},
        # single-bucket ladder C: no real choice -> ignored
        {"lad": "C", "p_fc": 0.9, "p_mkt": 0.9, "won": 1.0},
    ]
    fc, mk, n = argmax_accuracy(rows)
    assert n == 2 and fc == 1.0 and mk == 0.5
    assert argmax_accuracy([]) == (None, None, 0)


def test_ladder_groups_filters_singletons():
    rows = [
        {"lad": "A", "won": 1.0}, {"lad": "A", "won": 0.0},
        {"lad": "B", "won": 1.0},  # singleton -> dropped (no real choice)
    ]
    g = ladder_groups(rows)
    assert len(g) == 1 and len(g[0]) == 2


def test_topk_coverage():
    # one ladder; winner (28) is the 2nd-highest market prob -> not in top1, in top2/3
    rows = [
        {"lad": "A", "p_mkt": 0.5, "won": 0.0},
        {"lad": "A", "p_mkt": 0.4, "won": 1.0},
        {"lad": "A", "p_mkt": 0.1, "won": 0.0},
    ]
    g = ladder_groups(rows)
    assert topk_coverage(g, lambda r: r["p_mkt"], 1) == 0.0
    assert topk_coverage(g, lambda r: r["p_mkt"], 2) == 1.0
    assert topk_coverage(g, lambda r: r["p_mkt"], 3) == 1.0
    assert topk_coverage([], lambda r: r["p_mkt"], 1) is None
