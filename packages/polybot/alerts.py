"""Alerts facade — single responsibility: notify operator on important events.

Other services should import this module rather than talking to Sentry /
Telegram directly. All functions are safe no-ops when their backend isn't
configured, and `telegram_send` never raises.

Public API:
    init_sentry() -> None
    async notify(level, title, body, *, tags=None) -> None
    async telegram_send(msg: str) -> bool
    async signal_fired_alert(sig: dict) -> None
    async fill_alert(fill: dict) -> None
    async risk_rejected_alert(reason: str) -> None
    async kill_switch_alert(state: str) -> None
"""

from __future__ import annotations

import time
from typing import Any

import httpx

# Import-safe optional dependencies — services that don't have these installed
# (or that simply don't ship them) must still be able to import this module.
try:  # pragma: no cover - import guard
    import sentry_sdk  # type: ignore
except Exception:  # noqa: BLE001
    sentry_sdk = None  # type: ignore[assignment]

try:  # pragma: no cover - import guard
    # We don't actually use python-telegram-bot's client — we hit the HTTP API
    # directly via httpx — but the brief asks us to be import-safe if it's
    # missing, so we probe for it without failing.
    import telegram  # type: ignore  # noqa: F401
except Exception:  # noqa: BLE001
    telegram = None  # type: ignore[assignment]

from .config import settings
from .logging import get_logger

log = get_logger(__name__)

# Throttle window — never send the same alert key more than once per N seconds.
_THROTTLE_SECONDS: float = 60.0
_last_sent: dict[str, float] = {}

# Telegram HTTP endpoint template.
_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# Track whether sentry has been initialized so we don't double-init.
_sentry_inited: bool = False


# --------------------------------------------------------------------------- #
# Backend setup
# --------------------------------------------------------------------------- #
def init_sentry() -> None:
    """Initialize Sentry if a DSN is configured. Safe no-op otherwise."""
    global _sentry_inited
    if _sentry_inited:
        return
    dsn = getattr(settings, "sentry_dsn", None)
    if not dsn:
        return
    if sentry_sdk is None:
        log.warning("alerts.sentry_unavailable", reason="sentry_sdk not installed")
        return
    try:
        sentry_sdk.init(dsn=dsn, traces_sample_rate=0.05)
        _sentry_inited = True
        log.info("alerts.sentry_initialized")
    except Exception as exc:  # noqa: BLE001
        log.warning("alerts.sentry_init_failed", error=str(exc))


# --------------------------------------------------------------------------- #
# Throttle helper
# --------------------------------------------------------------------------- #
def _should_send(key: str) -> bool:
    """Return True if `key` hasn't been emitted within the throttle window."""
    now = time.monotonic()
    last = _last_sent.get(key)
    if last is not None and (now - last) < _THROTTLE_SECONDS:
        return False
    _last_sent[key] = now
    return True


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
async def telegram_send(msg: str) -> bool:
    """Send `msg` to the configured Telegram chat. Never raises.

    Returns True on HTTP 2xx, False otherwise (including when not configured).
    """
    token_secret = getattr(settings, "telegram_bot_token", None)
    chat_id = getattr(settings, "telegram_chat_id", None)
    if token_secret is None or not chat_id:
        return False
    try:
        token = token_secret.get_secret_value() if hasattr(token_secret, "get_secret_value") else str(token_secret)
    except Exception:  # noqa: BLE001
        return False
    if not token:
        return False

    url = _TELEGRAM_API.format(token=token)
    payload = {
        "chat_id": chat_id,
        "text": msg,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json=payload)
        if 200 <= resp.status_code < 300:
            return True
        log.warning(
            "alerts.telegram_non_2xx",
            status=resp.status_code,
            body=resp.text[:200],
        )
        return False
    except Exception as exc:  # noqa: BLE001
        log.warning("alerts.telegram_failed", error=str(exc))
        return False


# --------------------------------------------------------------------------- #
# Sentry helper
# --------------------------------------------------------------------------- #
def _sentry_capture(level: str, title: str, body: str, tags: dict | None) -> None:
    if sentry_sdk is None or not _sentry_inited:
        return
    try:
        with sentry_sdk.push_scope() as scope:  # type: ignore[attr-defined]
            scope.level = "error" if level == "critical" else ("warning" if level == "warn" else "info")
            for k, v in (tags or {}).items():
                try:
                    scope.set_tag(str(k), str(v))
                except Exception:  # noqa: BLE001
                    pass
            sentry_sdk.capture_message(f"{title}\n\n{body}")  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        log.warning("alerts.sentry_capture_failed", error=str(exc))


# --------------------------------------------------------------------------- #
# Top-level notify — fan-out
# --------------------------------------------------------------------------- #
async def notify(
    level: str,
    title: str,
    body: str,
    *,
    tags: dict | None = None,
) -> None:
    """Fan-out an alert to whichever backends are configured.

    Levels: 'info' | 'warn' | 'critical'. Unknown levels are treated as 'info'.
    For 'critical', we always *try* every configured backend regardless of
    individual failures.
    """
    lvl = (level or "info").lower()
    if lvl not in ("info", "warn", "critical"):
        lvl = "info"

    # Build a throttle key from level + title + sorted tag values so two
    # different signals don't suppress each other.
    tag_part = ""
    if tags:
        try:
            tag_part = "|" + ",".join(f"{k}={tags[k]}" for k in sorted(tags))
        except Exception:  # noqa: BLE001
            tag_part = ""
    key = f"{lvl}:{title}{tag_part}"
    if not _should_send(key):
        return

    # Always log locally — this is the cheapest, most reliable backend.
    log_fn = log.error if lvl == "critical" else (log.warning if lvl == "warn" else log.info)
    try:
        log_fn("alerts.notify", level=lvl, title=title, body=body, tags=tags or {})
    except Exception:  # noqa: BLE001
        pass

    # Sentry — only for warn/critical by default (info is noisy).
    if lvl in ("warn", "critical"):
        _sentry_capture(lvl, title, body, tags)

    # Telegram — info/warn/critical all go out if configured. Critical *always*
    # tries every backend so we don't short-circuit on a Sentry failure.
    icon = {"info": "i", "warn": "!", "critical": "!!"}[lvl]
    msg = f"*[{icon} {lvl.upper()}]* {title}\n{body}"
    if tags:
        try:
            tag_str = " ".join(f"`{k}={v}`" for k, v in tags.items())
            msg = f"{msg}\n{tag_str}"
        except Exception:  # noqa: BLE001
            pass
    await telegram_send(msg)


# --------------------------------------------------------------------------- #
# Convenience wrappers — small, opinionated payload shaping for known events.
# --------------------------------------------------------------------------- #
def _g(d: dict, *keys: str, default: Any = "?") -> Any:
    """Return the first present key from `d`, else `default`."""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


async def signal_fired_alert(sig: dict) -> None:
    """A trading signal fired and was forwarded to the executor."""
    if not isinstance(sig, dict):
        sig = {}
    name = _g(sig, "name", "signal", "strategy")
    market = _g(sig, "market", "market_id", "condition_id")
    side = _g(sig, "side", "direction")
    size = _g(sig, "size", "size_usdc", "qty")
    score = _g(sig, "score", "confidence")
    body = f"market={market} side={side} size={size} score={score}"
    await notify(
        "info",
        f"Signal fired: {name}",
        body,
        tags={"event": "signal_fired", "market": str(market), "name": str(name)},
    )


async def fill_alert(fill: dict) -> None:
    """An order filled (fully or partially)."""
    if not isinstance(fill, dict):
        fill = {}
    market = _g(fill, "market", "market_id", "condition_id")
    side = _g(fill, "side", "direction")
    price = _g(fill, "price", "avg_price")
    size = _g(fill, "size", "filled_size", "qty")
    order_id = _g(fill, "order_id", "id")
    body = f"market={market} side={side} price={price} size={size} order={order_id}"
    await notify(
        "info",
        "Fill",
        body,
        tags={"event": "fill", "market": str(market), "order_id": str(order_id)},
    )


async def risk_rejected_alert(reason: str) -> None:
    """The risk engine rejected an order."""
    await notify(
        "warn",
        "Risk rejected order",
        str(reason),
        tags={"event": "risk_rejected"},
    )


async def kill_switch_alert(state: str) -> None:
    """The kill-switch was toggled — always critical, hits every backend."""
    await notify(
        "critical",
        "Kill-switch toggled",
        f"new_state={state}",
        tags={"event": "kill_switch", "state": str(state)},
    )


__all__ = [
    "init_sentry",
    "notify",
    "telegram_send",
    "signal_fired_alert",
    "fill_alert",
    "risk_rejected_alert",
    "kill_switch_alert",
]
