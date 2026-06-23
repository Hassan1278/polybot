"""Tests for the exit-mirror cluster-recovery fix (multi-signal union).

`_entry_cluster` must recover the entry cluster from the UNION of every in-window
BUY Signal on (market, outcome), not just the most recent one — otherwise a
position accumulated across several signals is closed the moment a single
sub-cluster reverses, even while an earlier cluster is still long.

DB-less: a fake session replays a queue of canned results per `execute()`
(mirroring test_exit_preflight / test_risk_shorts), and the per-wallet net /
quality-weight lookups are monkeypatched so the decision logic runs in isolation.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import services.executor.exit_loop as ex
import services.signals.engine as engine

# ── fakes ────────────────────────────────────────────────────────────────────

class _Res:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _Sess:
    """Replays a queue of results, one per execute() call (in query order)."""

    def __init__(self, results):
        self._q = list(results)

    async def execute(self, *_a, **_k):
        return self._q.pop(0)


def _patch_scope(monkeypatch, results):
    @asynccontextmanager
    async def _s():
        yield _Sess(list(results))

    monkeypatch.setattr(ex, "session_scope", _s)


# ── pure: _union_wallets ─────────────────────────────────────────────────────

def test_union_wallets_dedupes_lowercases_and_orders():
    # a1 appears in both lists (dedupe), casing folded, first-seen order kept.
    assert ex._union_wallets([["B1", "a1"], ["a1", "A2"]]) == ["b1", "a1", "a2"]


def test_union_wallets_empty_and_none_safe():
    assert ex._union_wallets([]) == []
    assert ex._union_wallets([None, [], [None, ""], ["x"]]) == ["x"]


# ── _entry_cluster (real query path over a fake session) ─────────────────────

def test_entry_cluster_unions_multiple_signals():
    # Two BUY signals (newest first) → unioned, deduped, lower-cased cluster.
    sess = _Sess([_Res([(["B1", "a1"],), (["A1", "a2"],)])])
    cluster, is_fallback = asyncio.run(ex._entry_cluster(sess, "M", "YES"))
    assert cluster == ["b1", "a1", "a2"]
    assert is_fallback is False


def test_entry_cluster_falls_back_to_net_long_when_no_signal():
    # No signals → inferred net-long set (only net > 0), is_fallback=True.
    sess = _Sess([
        _Res([]),                                              # signals: none
        _Res([("0xWAL", 5.0), ("0xQUIET", 0.0), ("0xSHORT", -3.0)]),  # trades
    ])
    cluster, is_fallback = asyncio.run(ex._entry_cluster(sess, "M", "YES"))
    assert cluster == ["0xwal"]
    assert is_fallback is True


# ── _evaluate decision over the unioned cluster ──────────────────────────────

def _patch_eval(monkeypatch, *, signals_rows, nets, weights, cfg):
    """Wire _evaluate with the REAL _entry_cluster (fed by a fake session) but
    injected net / weight lookups, recording flag + close calls."""
    calls = {"flag": 0, "close": None}

    async def _cfg():
        return cfg

    async def _nbw(_s, _m, _o, _w):
        return nets

    async def _w(_s, _wallets, _win):
        return weights

    async def _flag(_m, _o, _cfg):
        calls["flag"] += 1

    async def _close(_m, _o, _cluster, fb, _cfg):
        calls["close"] = {"fallback": fb, "cluster": list(_cluster)}

    _patch_scope(monkeypatch, [_Res(signals_rows)])
    monkeypatch.setattr(ex, "_exit_cfg", _cfg)
    monkeypatch.setattr(ex, "_net_by_wallet", _nbw)
    monkeypatch.setattr(ex, "_weights", _w)
    monkeypatch.setattr(ex, "_flag_dissolving", _flag)
    monkeypatch.setattr(ex, "_do_close", _close)
    return calls


def test_evaluate_holds_when_only_one_subcluster_sold(monkeypatch):
    # Position built from signal A=[a1,a2] and signal B=[b1]. B reverses, A still
    # long → weighted support 2/3 >= 0.5 → HOLD. (Pre-fix, cluster would be only
    # the latest signal [b1] → support 0 → premature close.)
    calls = _patch_eval(
        monkeypatch,
        signals_rows=[(["b1"],), (["a1", "a2"],)],
        nets={"b1": -1.0, "a1": 5.0, "a2": 5.0},
        weights={"b1": 0.5, "a1": 0.5, "a2": 0.5},
        cfg={"enabled": True, "support_dissolution_threshold": 0.5,
             "quality_window": "30d"},
    )
    asyncio.run(ex._evaluate("M", "YES"))
    assert calls["flag"] == 0 and calls["close"] is None


def test_evaluate_closes_when_whole_union_reversed(monkeypatch):
    # Every wallet across BOTH signals has reversed → support 0 < 0.5 → CLOSE,
    # and the close gets the full unioned cluster (not just the latest signal).
    calls = _patch_eval(
        monkeypatch,
        signals_rows=[(["b1"],), (["a1", "a2"],)],
        nets={"b1": -1.0, "a1": 0.0, "a2": -2.0},
        weights={"b1": 0.5, "a1": 0.5, "a2": 0.5},
        cfg={"enabled": True, "support_dissolution_threshold": 0.5,
             "quality_window": "30d"},
    )
    asyncio.run(ex._evaluate("M", "YES"))
    assert calls["flag"] == 1
    assert calls["close"] == {"fallback": False, "cluster": ["b1", "a1", "a2"]}


# ── stop-adding suppression link (engine ↔ exit flag) ────────────────────────

def test_entry_suppressed_when_thesis_dissolving(monkeypatch):
    # When the exit_loop has flagged the thesis as dissolving, a fresh BUY
    # candidate is suppressed before any market/gate work.
    class _FakeRedis:
        async def get(self, _key):
            return "1"

    monkeypatch.setattr(engine, "redis_client", lambda: _FakeRedis())
    res = asyncio.run(engine.process_candidate(
        {"side": "BUY", "market_id": "M", "outcome": "YES"}))
    assert res["pass"] is False
    assert res["suppressed"] == "thesis_dissolving"
