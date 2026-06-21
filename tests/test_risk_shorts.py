"""Tests for `services/executor/risk.preflight` corner-cases:

  * zero / long-only positions (the cap arithmetic must use `abs()` so net-flat
    positions don't contribute negative exposure)
  * per-category cap rejects when the cumulative exposure across the category
    would exceed `max_per_category_usdc`

We mock `session_scope`, `kill_status`, and `risk_cfg.get()` so the test runs
without Postgres / Redis.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.executor import risk as risk_mod


# ── fakes ────────────────────────────────────────────────────────────────────

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
    """Replays a pre-supplied queue of _Result objects, one per execute()."""

    def __init__(self, results):
        self._queue = list(results)
        self.calls = 0

    async def execute(self, *_args, **_kwargs):
        self.calls += 1
        if not self._queue:
            return _Result(scalar=0)
        return self._queue.pop(0)


def _patch_session(monkeypatch, results):
    sess = _Session(results)

    @asynccontextmanager
    async def _scope():
        yield sess

    monkeypatch.setattr(risk_mod, "session_scope", _scope)
    return sess


def _patch_cfg(monkeypatch, *, max_position_usdc=25.0, max_per_market=50.0,
               max_per_category=75.0, max_open=5, max_orders_min=6,
               max_daily_loss=50.0, low_odds_threshold=0.20, low_odds_max=5.0,
               mode="paper"):
    cfg = {
        "position": {
            "max_position_usdc": max_position_usdc,
            "max_per_market_usdc": max_per_market,
            "max_per_category_usdc": max_per_category,
            "max_open_positions": max_open,
            "low_odds_price_threshold": low_odds_threshold,
            "low_odds_max_per_bet_usdc": low_odds_max,
        },
        "drawdown": {"max_daily_loss_usdc": max_daily_loss},
        "execution": {"max_orders_per_minute": max_orders_min},
    }
    # Old: risk.py imported risk_cfg directly from polybot.yaml_config and
    # patched its .get(). The runtime_config refactor switched to
    # merged_risk() which reads YAML + Redis overrides. Mock that path
    # instead. Same shape (dict with position / drawdown / execution).
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


# ── happy path: long-only / zero exposure passes ────────────────────────────

def test_preflight_passes_with_long_only_positions(monkeypatch):
    _patch_cfg(monkeypatch)
    _patch_kill(monkeypatch, status=None)

    # Result queue order matches the executor's query order:
    #   1) per-market sum            → 5.0 (existing long)
    #   2) per-category sum          → 5.0
    #   3) max-open-positions count  → 1
    #   4) realised pnl today        → 0.0
    #   5) recent order rate         → 0
    _patch_session(monkeypatch, [
        _Result(scalar=5.0),
        _Result(scalar=5.0),
        _Result(scalar=1),
        _Result(scalar=0.0),
        _Result(scalar=0),
    ])

    out = asyncio.run(risk_mod.preflight(
        mode="paper", market_id="M1", category="politics",
        side="BUY", size_usdc=10.0, score=0.8,
    ))
    assert out["ok"] is True


def test_preflight_zero_position_is_treated_as_no_exposure(monkeypatch):
    # A wallet with a fully closed (size=0) position should contribute 0 to
    # the per-market cap, so a fresh $20 order on a $25-cap market succeeds.
    _patch_cfg(monkeypatch, max_per_market=25.0)
    _patch_kill(monkeypatch, status=None)
    _patch_session(monkeypatch, [
        _Result(scalar=0.0),       # per-market sum (closed positions = 0)
        _Result(scalar=0.0),       # per-category sum
        _Result(scalar=0),         # open positions
        _Result(scalar=0.0),       # realised today
        _Result(scalar=0),         # rate
    ])
    out = asyncio.run(risk_mod.preflight(
        mode="paper", market_id="M1", category="politics",
        side="BUY", size_usdc=20.0, score=0.5,
    ))
    assert out["ok"] is True


# ── per-category cap rejection ───────────────────────────────────────────────

def test_preflight_rejects_per_category_cap_breach(monkeypatch):
    # Category already has $70 of exposure. A new $10 order pushes the total
    # to $80, exceeding the $75 cap → should raise RiskRejection.
    _patch_cfg(monkeypatch, max_per_market=100.0, max_per_category=75.0)
    _patch_kill(monkeypatch, status=None)
    _patch_session(monkeypatch, [
        _Result(scalar=0.0),       # per-market sum (under per-market cap)
        _Result(scalar=70.0),      # per-category sum (the breach)
        _Result(scalar=1),         # open positions
        _Result(scalar=0.0),       # realised today
        _Result(scalar=0),         # rate
    ])
    with pytest.raises(risk_mod.RiskRejection) as excinfo:
        asyncio.run(risk_mod.preflight(
            mode="paper", market_id="M1", category="politics",
            side="BUY", size_usdc=10.0, score=0.7,
        ))
    msg = str(excinfo.value)
    assert "per_category_cap" in msg
    assert "politics" in msg


def test_preflight_passes_when_category_cap_not_configured(monkeypatch):
    # If `max_per_category_usdc` is unset, the per-category cap is a no-op
    # regardless of how concentrated the category is.
    _patch_cfg(monkeypatch, max_per_category=None)
    _patch_kill(monkeypatch, status=None)
    _patch_session(monkeypatch, [
        _Result(scalar=0.0),       # per-market sum
        _Result(scalar=0),         # open positions (cap query skipped)
        _Result(scalar=0.0),       # realised today
        _Result(scalar=0),         # rate
    ])
    out = asyncio.run(risk_mod.preflight(
        mode="paper", market_id="M1", category="politics",
        side="BUY", size_usdc=10.0, score=0.6,
    ))
    assert out["ok"] is True


def test_preflight_rejects_when_size_above_max(monkeypatch):
    # The per-order size cap fires before any DB lookups happen.
    _patch_cfg(monkeypatch, max_position_usdc=25.0)
    _patch_kill(monkeypatch, status=None)
    _patch_session(monkeypatch, [])  # never reached

    with pytest.raises(risk_mod.RiskRejection) as excinfo:
        asyncio.run(risk_mod.preflight(
            mode="paper", market_id="M1", category="politics",
            side="BUY", size_usdc=999.0, score=0.9,
        ))
    assert "size>" in str(excinfo.value)


def test_preflight_kill_switch_blocks_everything(monkeypatch):
    _patch_cfg(monkeypatch)
    _patch_kill(monkeypatch, status="manual_halt")
    _patch_session(monkeypatch, [])  # never reached

    with pytest.raises(risk_mod.RiskRejection) as excinfo:
        asyncio.run(risk_mod.preflight(
            mode="paper", market_id="M1", category="politics",
            side="BUY", size_usdc=5.0, score=0.9,
        ))
    assert "kill_switch_active" in str(excinfo.value)


# ── per-bet cumulative cap (counts resting orders, mode-aware) ───────────────

def test_per_bet_cap_counts_resting_orders_in_live(monkeypatch):
    # LIVE: $45 of filled+resting BUY notional already on the market. A new $20
    # order pushes cumulative to $65 > $50 cap → reject. (The old Position-only
    # query summed to ~0 in live and let this stack through — the actual bug.)
    _patch_cfg(monkeypatch, max_per_market=50.0, mode="live")
    _patch_kill(monkeypatch, status=None)
    _patch_session(monkeypatch, [_Result(scalar=45.0)])  # _bet_notional (Fill sum)
    with pytest.raises(risk_mod.RiskRejection) as excinfo:
        asyncio.run(risk_mod.preflight(
            mode="live", market_id="M1", category="politics",
            side="BUY", size_usdc=20.0, score=0.7,
        ))
    assert "per_bet_cap" in str(excinfo.value)


def test_per_bet_cap_passes_under_limit(monkeypatch):
    _patch_cfg(monkeypatch, max_per_market=50.0)
    _patch_kill(monkeypatch, status=None)
    _patch_session(monkeypatch, [_Result(scalar=30.0)])  # rest fall back to 0
    out = asyncio.run(risk_mod.preflight(
        mode="paper", market_id="M1", category="crypto",
        side="BUY", size_usdc=15.0, score=0.7, price=0.50,
    ))
    assert out["ok"] is True


# ── low-odds (sub-threshold price) tight cap ─────────────────────────────────

def test_low_odds_cap_blocks_above_5usd(monkeypatch):
    # Entry price 0.15 (< 0.20) → $5 cap. Existing $3 + new $4 = $7 > $5 → reject.
    _patch_cfg(monkeypatch)
    _patch_kill(monkeypatch, status=None)
    _patch_session(monkeypatch, [_Result(scalar=3.0)])
    with pytest.raises(risk_mod.RiskRejection) as excinfo:
        asyncio.run(risk_mod.preflight(
            mode="paper", market_id="M1", category="crypto",
            side="BUY", size_usdc=4.0, score=0.7, price=0.15,
        ))
    msg = str(excinfo.value)
    assert "per_bet_cap" in msg and "low_odds" in msg


def test_low_odds_cap_allows_under_5usd(monkeypatch):
    _patch_cfg(monkeypatch)
    _patch_kill(monkeypatch, status=None)
    _patch_session(monkeypatch, [_Result(scalar=3.0)])
    out = asyncio.run(risk_mod.preflight(
        mode="paper", market_id="M1", category="crypto",
        side="BUY", size_usdc=1.0, score=0.7, price=0.15,
    ))
    assert out["ok"] is True


def test_low_odds_at_cap_blocks_further_orders(monkeypatch):
    # A sub-20c bet already at the $5 cap: ANY further order is refused, so the
    # bot stops piling into long shots once it hits the limit.
    _patch_cfg(monkeypatch)
    _patch_kill(monkeypatch, status=None)
    _patch_session(monkeypatch, [_Result(scalar=5.0)])
    with pytest.raises(risk_mod.RiskRejection) as excinfo:
        asyncio.run(risk_mod.preflight(
            mode="paper", market_id="M1", category="crypto",
            side="BUY", size_usdc=1.0, score=0.7, price=0.15,
        ))
    assert "per_bet_cap" in str(excinfo.value)


def test_high_odds_uses_full_50_cap(monkeypatch):
    # Price 0.50 (>= 0.20) → full $50 cap, not the long-shot cap. $40 + $15 = $55
    # > $50 → reject, and the reason is NOT tagged low_odds.
    _patch_cfg(monkeypatch, max_per_market=50.0)
    _patch_kill(monkeypatch, status=None)
    _patch_session(monkeypatch, [_Result(scalar=40.0)])
    with pytest.raises(risk_mod.RiskRejection) as excinfo:
        asyncio.run(risk_mod.preflight(
            mode="paper", market_id="M1", category="crypto",
            side="BUY", size_usdc=15.0, score=0.7, price=0.50,
        ))
    msg = str(excinfo.value)
    assert "per_bet_cap" in msg and "low_odds" not in msg
