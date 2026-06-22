"""Tests for the exit-mirror foundation (Part 1):
`risk.compute_net_shares_held` and the exit-aware `risk.preflight` fast-path.

A position-CLOSING SELL (`is_exit=True`) must: re-verify we actually hold the
outcome (else the flag is cleared and the full guard gauntlet applies — so it
can't be abused into a naked short), skip every entry/concentration/cap guard,
optionally bypass the kill switch, but still respect the order-rate budget.

DB-less: replays a queue of `_Result` per `execute()`, mirroring test_risk_shorts.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from services.executor import risk as risk_mod


# ── fakes (mirror test_risk_shorts) ──────────────────────────────────────────

class _Result:
    def __init__(self, scalar=None, first=None):
        self._scalar = scalar
        self._first = first

    def scalar_one(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._scalar

    def first(self):
        return self._first


class _Session:
    def __init__(self, results):
        self._queue = list(results)
        self.calls = 0

    async def execute(self, *_a, **_k):
        self.calls += 1
        if not self._queue:
            return _Result(scalar=0)
        return self._queue.pop(0)


class _RaisingSession:
    async def execute(self, *_a, **_k):
        raise RuntimeError("db down")


def _patch_session(monkeypatch, results):
    sess = _Session(results)

    @asynccontextmanager
    async def _scope():
        yield sess

    monkeypatch.setattr(risk_mod, "session_scope", _scope)
    return sess


def _patch_cfg(monkeypatch, *, mode="paper", max_orders_min=6, exit_mirror=None):
    cfg = {
        "position": {"max_position_usdc": 25.0, "max_per_market_usdc": 50.0,
                     "max_per_category_usdc": 75.0, "max_open_positions": 5},
        "drawdown": {"max_daily_loss_usdc": 50.0},
        "execution": {"max_orders_per_minute": max_orders_min},
    }
    if exit_mirror is not None:
        cfg["exit_mirror"] = exit_mirror

    async def _merged_risk(_m=None):
        return cfg
    monkeypatch.setattr(risk_mod, "merged_risk", _merged_risk)

    async def _current_mode():
        return mode
    monkeypatch.setattr(risk_mod, "current_mode", _current_mode)


def _patch_kill(monkeypatch, status=None):
    async def _kill_status():
        return status
    monkeypatch.setattr(risk_mod, "kill_status", _kill_status)


# ── compute_net_shares_held ──────────────────────────────────────────────────

def test_net_shares_live_buy_minus_sell():
    sess = _Session([_Result(scalar=10.0), _Result(scalar=3.0)])  # BUY=10, SELL=3
    net = asyncio.run(risk_mod.compute_net_shares_held(
        sess, mode="live", market_id="M1", outcome="YES"))
    assert net == 7.0


def test_net_shares_paper_uses_position_scalar():
    sess = _Session([_Result(scalar=5.0)])
    net = asyncio.run(risk_mod.compute_net_shares_held(
        sess, mode="paper", market_id="M1", outcome="NO"))
    assert net == 5.0


def test_net_shares_no_outcome_is_zero_without_query():
    sess = _Session([_Result(scalar=999.0)])
    net = asyncio.run(risk_mod.compute_net_shares_held(
        sess, mode="live", market_id="M1", outcome=""))
    assert net == 0.0
    assert sess.calls == 0          # short-circuits before touching the DB


def test_net_shares_db_error_fails_safe_to_zero():
    net = asyncio.run(risk_mod.compute_net_shares_held(
        _RaisingSession(), mode="live", market_id="M1", outcome="YES"))
    assert net == 0.0               # fail-safe: hold nothing


# ── exit fast-path ───────────────────────────────────────────────────────────

def test_exit_passes_and_skips_all_entry_guards(monkeypatch):
    # Verified close (paper, hold 8 shares). Must return is_exit and touch ONLY
    # the re-verify query + the rate query — i.e. NONE of the ~6 entry-guard
    # queries ran (proven by sess.calls == 2).
    _patch_cfg(monkeypatch, mode="paper")
    _patch_kill(monkeypatch, status=None)
    sess = _patch_session(monkeypatch, [
        _Result(scalar=8.0),        # re-verify net (paper: 1 query)
        _Result(scalar=0),          # rate count
    ])
    out = asyncio.run(risk_mod.preflight(
        mode="paper", market_id="M1", category="crypto",
        side="SELL", size_usdc=12.0, score=0.0, outcome="YES", is_exit=True))
    assert out["ok"] is True and out["is_exit"] is True
    assert sess.calls == 2


def test_exit_flag_cleared_when_not_held(monkeypatch):
    # is_exit=True but we hold 0 shares → flag cleared → full path → kill applies.
    _patch_cfg(monkeypatch, mode="paper")
    _patch_kill(monkeypatch, status="manual_halt")
    _patch_session(monkeypatch, [_Result(scalar=0.0)])   # re-verify net = 0
    with pytest.raises(risk_mod.RiskRejection) as ei:
        asyncio.run(risk_mod.preflight(
            mode="paper", market_id="M1", category="crypto",
            side="SELL", size_usdc=12.0, score=0.0, outcome="YES", is_exit=True))
    msg = str(ei.value)
    assert "kill_switch_active" in msg and "exit_blocked" not in msg


def test_exit_bypasses_kill_when_allowed(monkeypatch):
    # Verified close under an active kill, allow_close_when_killed default True.
    _patch_cfg(monkeypatch, mode="paper")
    _patch_kill(monkeypatch, status="equity_drawdown")
    _patch_session(monkeypatch, [_Result(scalar=8.0), _Result(scalar=0)])
    out = asyncio.run(risk_mod.preflight(
        mode="paper", market_id="M1", category="crypto",
        side="SELL", size_usdc=12.0, score=0.0, outcome="YES", is_exit=True))
    assert out["is_exit"] is True


def test_exit_blocked_when_close_when_killed_disabled(monkeypatch):
    _patch_cfg(monkeypatch, mode="paper", exit_mirror={"allow_close_when_killed": False})
    _patch_kill(monkeypatch, status="manual_halt")
    _patch_session(monkeypatch, [_Result(scalar=8.0)])   # re-verify net>0
    with pytest.raises(risk_mod.RiskRejection) as ei:
        asyncio.run(risk_mod.preflight(
            mode="paper", market_id="M1", category="crypto",
            side="SELL", size_usdc=12.0, score=0.0, outcome="YES", is_exit=True))
    assert "kill_switch_active_exit_blocked" in str(ei.value)


def test_exit_still_rate_limited(monkeypatch):
    # Even a verified close respects the order-rate budget.
    _patch_cfg(monkeypatch, mode="paper", max_orders_min=6)
    _patch_kill(monkeypatch, status=None)
    _patch_session(monkeypatch, [_Result(scalar=8.0), _Result(scalar=6)])  # rate at cap
    with pytest.raises(risk_mod.RiskRejection) as ei:
        asyncio.run(risk_mod.preflight(
            mode="paper", market_id="M1", category="crypto",
            side="SELL", size_usdc=12.0, score=0.0, outcome="YES", is_exit=True))
    assert "rate_limit_exit" in str(ei.value)
