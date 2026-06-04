"""Tests for `polybot.alerts.notify` — telegram-mocked, throttle-verified.

The alert module debounces identical `(level, title, sorted-tags)` combos
within a 60 s window. We mock `telegram_send` to count actual sends and
verify that:

  * the first call for a given key fires
  * an immediate duplicate is suppressed
  * a different key (different title or tag) is allowed through
  * resetting the internal _last_sent map re-enables the same key
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from polybot import alerts


def _reset_throttle():
    """Wipe the module-level dedupe map between tests for isolation."""
    alerts._last_sent.clear()


# ── happy path ──────────────────────────────────────────────────────────────

def test_notify_first_call_sends(monkeypatch):
    _reset_throttle()
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(alerts, "telegram_send", send)

    asyncio.run(alerts.notify("info", "Signal fired", "details"))
    assert send.await_count == 1


# ── edge cases ──────────────────────────────────────────────────────────────

def test_notify_duplicate_is_throttled(monkeypatch):
    _reset_throttle()
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(alerts, "telegram_send", send)

    asyncio.run(alerts.notify("warn", "Risk", "body"))
    asyncio.run(alerts.notify("warn", "Risk", "body"))
    asyncio.run(alerts.notify("warn", "Risk", "body"))
    assert send.await_count == 1


def test_notify_different_title_is_not_throttled(monkeypatch):
    _reset_throttle()
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(alerts, "telegram_send", send)

    asyncio.run(alerts.notify("info", "First", "x"))
    asyncio.run(alerts.notify("info", "Second", "x"))
    assert send.await_count == 2


def test_notify_different_tags_breaks_throttle(monkeypatch):
    # Same level+title but different tag values → distinct throttle keys.
    _reset_throttle()
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(alerts, "telegram_send", send)

    asyncio.run(alerts.notify("info", "Fill", "b", tags={"market": "A"}))
    asyncio.run(alerts.notify("info", "Fill", "b", tags={"market": "B"}))
    assert send.await_count == 2


def test_notify_after_throttle_reset_resends(monkeypatch):
    # Clearing _last_sent simulates the throttle window expiring.
    _reset_throttle()
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(alerts, "telegram_send", send)

    asyncio.run(alerts.notify("critical", "Kill", "body"))
    assert send.await_count == 1
    _reset_throttle()
    asyncio.run(alerts.notify("critical", "Kill", "body"))
    assert send.await_count == 2


def test_notify_unknown_level_is_treated_as_info(monkeypatch):
    _reset_throttle()
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(alerts, "telegram_send", send)

    asyncio.run(alerts.notify("bogus", "Hello", "world"))
    # Sent exactly once (treated as info; not silently dropped).
    assert send.await_count == 1
    # Sending it again with the SAME normalised key gets throttled.
    asyncio.run(alerts.notify("bogus", "Hello", "world"))
    assert send.await_count == 1


def test_telegram_send_without_config_returns_false(monkeypatch):
    # When telegram is not configured, the real telegram_send returns False
    # without raising — this guards the convenience wrappers.
    monkeypatch.setattr(alerts.settings, "telegram_bot_token", None, raising=False)
    monkeypatch.setattr(alerts.settings, "telegram_chat_id", None, raising=False)
    sent = asyncio.run(alerts.telegram_send("hi"))
    assert sent is False
