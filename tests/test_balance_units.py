"""Tests for the clob-rs balance units fix (Part 0):
`ClobClient._micro_usdc_to_dollars` + `ClobClient.balance()`.

The clob-rs sidecar returns the USDC collateral in raw 6-decimal base units
(microUSDC), e.g. "32845218.43" for ~$32.85. ClobClient.balance() is the single
chokepoint that converts to dollars. Without this the equity-drawdown breaker
read a $33 account as $32.8M and never tripped.
"""

from __future__ import annotations

import asyncio

import polybot.clients.clob as clob_mod
from polybot.clients.clob import ClobClient


# ── pure conversion ──────────────────────────────────────────────────────────

def test_micro_usdc_to_dollars():
    f = ClobClient._micro_usdc_to_dollars
    # The exact value observed in the live log: 32,845,218.43 microUSDC = $32.845.
    assert abs(float(f("32845218.43")) - 32.84521843) < 1e-6
    assert abs(float(f("33000000")) - 33.0) < 1e-9
    assert abs(float(f("500000")) - 0.50) < 1e-9
    assert abs(float(f("0")) - 0.0) < 1e-9


def test_micro_usdc_to_dollars_unparseable_passthrough():
    # Never raises; returns the raw value unchanged so balance() stays best-effort.
    assert ClobClient._micro_usdc_to_dollars("not_a_number") == "not_a_number"


# ── balance() wiring (fake sidecar) ──────────────────────────────────────────

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.content = b"x" if payload is not None else b""
        self.status_code = 200

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def get(self, *_a, **_k):
        return _FakeResp(self._payload)


def _patch(monkeypatch, payload):
    monkeypatch.setattr(clob_mod.httpx, "AsyncClient",
                        lambda *a, **k: _FakeAsyncClient(payload))


def test_balance_converts_base_units(monkeypatch):
    _patch(monkeypatch, {"ok": True, "balance": "32845218.43", "funder": "0xabc"})
    d = asyncio.run(ClobClient().balance())
    assert d["ok"] is True
    assert abs(float(d["balance"]) - 32.84521843) < 1e-6   # ~$32.85, not $32.8M
    assert d["funder"] == "0xabc"


def test_balance_error_not_converted(monkeypatch):
    _patch(monkeypatch, {"ok": False, "error": "sidecar down"})
    d = asyncio.run(ClobClient().balance())
    assert d["ok"] is False
    assert "balance" not in d            # no conversion attempted on an error reply
