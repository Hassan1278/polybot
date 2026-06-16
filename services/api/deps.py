from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import time
from typing import Final

from fastapi import Header, HTTPException, status

from polybot.config import settings
from polybot.logging import get_logger
from polybot.redis_bus import client as _redis_client

log = get_logger(__name__)

# Replay protection window: a hardened token's timestamp must be within
# +/- TIMESTAMP_SKEW_SECONDS of the server's current time.
TIMESTAMP_SKEW_SECONDS: Final[int] = 60

# Triple-verify HIGH-2: the in-memory LRU replay cache was lost on every
# API restart, letting an attacker re-replay the same token after a
# restart inside the 60-second skew window. Moved to Redis with TTL =
# 2 × TIMESTAMP_SKEW_SECONDS so seen tokens survive restarts; the TTL
# is bounded so the key space stays small (≤ 1 token per second of
# real admin traffic, times 120 s = ~120 keys at peak).
_REPLAY_KEY_PREFIX: Final[str] = "polybot:admin_replay:"
_REPLAY_TTL_S: Final[int] = TIMESTAMP_SKEW_SECONDS * 2


def _legacy_auth_enabled() -> bool:
    # Production deployments should set LEGACY_ADMIN_AUTH=false to disable the
    # plain-token path (mode a). Default is True for now to preserve dashboard
    # convenience during rollout.
    return os.environ.get("LEGACY_ADMIN_AUTH", "true").strip().lower() != "false"


def _compute_signature(secret: str, ts: str) -> str:
    return hmac.new(secret.encode("utf-8"), ts.encode("utf-8"), hashlib.sha256).hexdigest()


async def _remember_token_async(token: str) -> bool:
    """Record a hardened token in Redis with TTL; return False if it
    was already seen (replay).

    Uses Redis SET NX EX so:
      - First insert succeeds → returns True (token unseen).
      - Repeat insert fails (NX) → returns False (replay).
    On Redis failure we FAIL CLOSED (return False = treat as replay) so
    a downed Redis can't be used to bypass replay protection. This is the
    opposite of the rate-limit fail-open choice — for SECURITY decisions,
    "service degraded → admin requests blocked" is the safer default.
    """
    key = _REPLAY_KEY_PREFIX + hashlib.sha256(token.encode("utf-8")).hexdigest()
    try:
        # set returns True if key was set (= new token), None/False if it
        # already existed (= replay).
        result = await _redis_client().set(key, "1", nx=True, ex=_REPLAY_TTL_S)
        return bool(result)
    except Exception:  # noqa: BLE001
        log.exception("admin_replay_redis_failed_failing_closed")
        return False


def _remember_token(token: str) -> bool:
    """Sync wrapper for use inside _verify_hardened (which is called from
    a sync FastAPI dependency). FastAPI runs sync deps in a threadpool
    via anyio, so we can drive the Redis call via asyncio.run on a fresh
    event loop without colliding with the request's own loop."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Already in an async context (rare for a sync dep, but
            # possible if someone calls require_admin directly from
            # async code): run a new coroutine via asyncio.run_coroutine_threadsafe
            # would deadlock — fall back to a fresh loop in a new thread.
            import threading
            result_holder: list[bool] = [False]
            err_holder: list[BaseException | None] = [None]

            def _run() -> None:
                try:
                    result_holder[0] = asyncio.run(_remember_token_async(token))
                except BaseException as e:  # noqa: BLE001
                    err_holder[0] = e

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            t.join(timeout=5)
            if err_holder[0] is not None:
                raise err_holder[0]
            return result_holder[0]
    except RuntimeError:
        pass
    return asyncio.run(_remember_token_async(token))


def _verify_hardened(token: str, secret: str) -> bool:
    parts = token.split(":")
    if len(parts) != 3:
        return False
    provided_secret, ts_str, provided_sig = parts

    # Constant-time compare on the secret portion first.
    if not hmac.compare_digest(provided_secret, secret):
        return False

    try:
        ts = int(ts_str)
    except ValueError:
        return False

    now = int(time.time())
    if abs(now - ts) > TIMESTAMP_SKEW_SECONDS:
        return False

    expected_sig = _compute_signature(secret, ts_str)
    if not hmac.compare_digest(provided_sig, expected_sig):
        return False

    # Reject replays of an otherwise-valid token.
    if not _remember_token(token):
        return False

    return True


def _verify_legacy(token: str, secret: str) -> bool:
    return hmac.compare_digest(token, secret)


async def require_admin(
    x_admin_token: str | None = Header(default=None),
    x_session_token: str | None = Header(default=None),
) -> None:
    """Authorise an admin request via EITHER:

      - X-Admin-Token (legacy or hardened HMAC), or
      - X-Session-Token (SIWE — see services/api/routes/auth.py).

    Both are checked because Web3 wallet sign-in is the preferred UX
    going forward, but the admin token stays for scripts (kill_switch.py
    etc.) and as a break-glass when no wallet is set up yet.
    """
    # SIWE session path — preferred for browser users.
    if x_session_token:
        from services.api.routes.auth import session_is_valid
        addr = await session_is_valid(x_session_token)
        if addr is not None:
            return  # authenticated as `addr`

    # Legacy / scripted X-Admin-Token path.
    if x_admin_token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad admin token")

    secret = settings.admin_token.get_secret_value()

    # Prefer hardened mode (b): if the token has the "secret:ts:sig" shape we
    # require it to pass full HMAC + timestamp + replay checks.
    if x_admin_token.count(":") == 2:
        if _verify_hardened(x_admin_token, secret):
            return
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad admin token")

    # Fallback to legacy mode (a) only when explicitly enabled.
    if _legacy_auth_enabled() and _verify_legacy(x_admin_token, secret):
        return

    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad admin token")


def make_admin_token(secret: str) -> str:
    """Produce a fresh hardened admin token of the form ``secret:ts:hmac``.

    Intended for client scripts (e.g. ``scripts/kill_switch.py``) so they can
    authenticate against endpoints guarded by :func:`require_admin` without
    relying on the legacy plain-token path.
    """
    ts = str(int(time.time()))
    sig = _compute_signature(secret, ts)
    return f"{secret}:{ts}:{sig}"
