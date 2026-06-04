"""Pure-logic tests for the synchronous parts of the gate chain.

Async gates that touch DB/CLOB are covered by integration tests outside CI.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.signals.conditions.correlation_score import CorrelationScore
from services.signals.conditions.risk_reward import RiskReward


def _ctx(cand, extra=None):
    c = MagicMock()
    c.candidate = cand
    c.session = AsyncMock()
    c.redis = AsyncMock()
    c.now_ts = 0.0
    c.extra = extra or {}
    return c


def test_correlation_score_min_wallets():
    g = CorrelationScore(enabled=True, params={"min_score": 0.5, "min_wallets": 3})
    res = asyncio.run(g.evaluate(_ctx({"wallets": ["a", "b"], "correlation_score": 0.9})))
    assert not res.passed and "n=2" in res.reason


def test_correlation_score_min_score():
    g = CorrelationScore(enabled=True, params={"min_score": 0.7, "min_wallets": 1})
    res = asyncio.run(g.evaluate(_ctx({"wallets": ["a"], "correlation_score": 0.4})))
    assert not res.passed


def test_correlation_score_pass():
    g = CorrelationScore(enabled=True, params={"min_score": 0.5, "min_wallets": 1})
    res = asyncio.run(g.evaluate(_ctx({"wallets": ["a"], "correlation_score": 0.9})))
    assert res.passed


def test_risk_reward_buy_attractive():
    g = RiskReward(enabled=True, params={"min_rr": 1.5, "max_entry_price": 0.9, "min_entry_price": 0.05})
    ctx = _ctx({"side": "BUY", "avg_price": 0.3}, extra={"expected_avg_price": 0.3})
    res = asyncio.run(g.evaluate(ctx))
    assert res.passed
    # (1-0.3)/0.3 ≈ 2.33 >= 1.5
    assert ctx.extra["rr"] > 1.5


def test_risk_reward_above_cap():
    g = RiskReward(enabled=True, params={"min_rr": 1.0, "max_entry_price": 0.8, "min_entry_price": 0.05})
    res = asyncio.run(g.evaluate(_ctx({"side": "BUY", "avg_price": 0.95}, extra={"expected_avg_price": 0.95})))
    assert not res.passed


def test_risk_reward_disabled_always_pass():
    g = RiskReward(enabled=False, params={})
    res = asyncio.run(g.evaluate(_ctx({"side": "BUY", "avg_price": 0.99}, extra={"expected_avg_price": 0.99})))
    assert res.passed
