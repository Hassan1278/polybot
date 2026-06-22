"""Tests for the exit-mirror decision (Part 3): `exit_loop.weighted_support_remaining`
and the `_evaluate` dissolution/threshold logic.

The DB queries (cluster recovery, per-wallet net, quality weights) are
monkeypatched so the decision logic is tested in isolation (DB-less).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import services.executor.exit_loop as ex
from services.executor.exit_loop import weighted_support_remaining


# ── pure: weighted_support_remaining ─────────────────────────────────────────

def test_wsr_all_long_is_one():
    assert weighted_support_remaining([1, 1, 1], [False, False, False]) == 1.0


def test_wsr_all_sold_is_zero():
    assert weighted_support_remaining([1, 1, 1], [True, True, True]) == 0.0


def test_wsr_empty_is_one():
    assert weighted_support_remaining([], []) == 1.0


def test_wsr_zero_weights_is_one():
    # No usable quality signal → don't exit.
    assert weighted_support_remaining([0, 0], [True, True]) == 1.0


def test_wsr_quality_weighted():
    # One high-quality (0.9) holder outweighs two low-quality (0.1) sellers.
    r = weighted_support_remaining([0.9, 0.1, 0.1], [False, True, True])
    assert abs(r - (0.9 / 1.1)) < 1e-9


# ── _evaluate decision ───────────────────────────────────────────────────────

def _patch_eval(monkeypatch, *, cluster, is_fallback, nets, weights, cfg):
    calls = {"flag": 0, "close": None}

    async def _cfg():
        return cfg

    @asynccontextmanager
    async def _scope():
        yield object()

    async def _ec(_s, _m, _o):
        return (cluster, is_fallback)

    async def _nbw(_s, _m, _o, _w):
        return nets

    async def _w(_s, _wallets, _win):
        return weights

    async def _flag(_m, _o, _cfg):
        calls["flag"] += 1

    async def _close(_m, _o, _cluster, fb, _cfg):
        calls["close"] = {"fallback": fb}

    monkeypatch.setattr(ex, "_exit_cfg", _cfg)
    monkeypatch.setattr(ex, "session_scope", _scope)
    monkeypatch.setattr(ex, "_entry_cluster", _ec)
    monkeypatch.setattr(ex, "_net_by_wallet", _nbw)
    monkeypatch.setattr(ex, "_weights", _w)
    monkeypatch.setattr(ex, "_flag_dissolving", _flag)
    monkeypatch.setattr(ex, "_do_close", _close)
    return calls


def test_evaluate_closes_when_dissolved(monkeypatch):
    cluster = ["0xa", "0xb", "0xc"]
    calls = _patch_eval(
        monkeypatch, cluster=cluster, is_fallback=False,
        nets={"0xa": 5.0, "0xb": 0.0, "0xc": -2.0},     # 2 of 3 no longer long
        weights={"0xa": 0.5, "0xb": 0.5, "0xc": 0.5},   # remaining = 1/3 < 0.5
        cfg={"enabled": True, "support_dissolution_threshold": 0.5,
             "quality_window": "30d"})
    asyncio.run(ex._evaluate("M", "YES"))
    assert calls["flag"] == 1 and calls["close"] == {"fallback": False}


def test_evaluate_holds_when_supported(monkeypatch):
    cluster = ["0xa", "0xb", "0xc"]
    calls = _patch_eval(
        monkeypatch, cluster=cluster, is_fallback=False,
        nets={"0xa": 5.0, "0xb": 5.0, "0xc": 5.0},      # all still long → remaining 1.0
        weights={"0xa": 0.5, "0xb": 0.5, "0xc": 0.5},
        cfg={"enabled": True, "support_dissolution_threshold": 0.5,
             "quality_window": "30d"})
    asyncio.run(ex._evaluate("M", "YES"))
    assert calls["flag"] == 0 and calls["close"] is None


def test_evaluate_fallback_uses_stricter_threshold(monkeypatch):
    # remaining = 0.4: a normal cluster (thr 0.5) would EXIT, but an inferred
    # (fallback) cluster needs < 0.35 to act → holds.
    cluster = ["a", "b", "c", "d", "e"]
    calls = _patch_eval(
        monkeypatch, cluster=cluster, is_fallback=True,
        nets={"a": 5.0, "b": 5.0, "c": 0.0, "d": 0.0, "e": 0.0},   # 2/5 = 0.4 long
        weights={w: 0.5 for w in cluster},
        cfg={"enabled": True, "support_dissolution_threshold": 0.5,
             "fallback_support_dissolution_threshold": 0.35, "quality_window": "30d"})
    asyncio.run(ex._evaluate("M", "YES"))
    assert calls["close"] is None        # 0.4 >= 0.35 → still supported under fallback


def test_evaluate_disabled_is_noop(monkeypatch):
    calls = _patch_eval(
        monkeypatch, cluster=["0xa"], is_fallback=False,
        nets={"0xa": -1.0}, weights={"0xa": 0.5},
        cfg={"enabled": False})
    asyncio.run(ex._evaluate("M", "YES"))
    assert calls["flag"] == 0 and calls["close"] is None
