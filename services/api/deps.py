from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections import OrderedDict
from typing import Final

from fastapi import Header, HTTPException, status

from polybot.config import settings

# Replay protection window: a hardened token's timestamp must be within
# +/- TIMESTAMP_SKEW_SECONDS of the server's current time.
TIMESTAMP_SKEW_SECONDS: Final[int] = 60

# Bounded in-memory LRU cache of previously-seen hardened tokens. Dropping
# entries on restart is acceptable because the timestamp window is small
# (any token older than TIMESTAMP_SKEW_SECONDS is rejected anyway).
_REPLAY_CACHE_MAX: Final[int] = 1000
_seen_tokens: "OrderedDict[str, None]" = OrderedDict()


def _legacy_auth_enabled() -> bool:
    # Production deployments should set LEGACY_ADMIN_AUTH=false to disable the
    # plain-token path (mode a). Default is True for now to preserve dashboard
    # convenience during rollout.
    return os.environ.get("LEGACY_ADMIN_AUTH", "true").strip().lower() != "false"


def _compute_signature(secret: str, ts: str) -> str:
    return hmac.new(secret.encode("utf-8"), ts.encode("utf-8"), hashlib.sha256).hexdigest()


def _remember_token(token: str) -> bool:
    """Record a hardened token; return False if it was already seen (replay)."""
    if token in _seen_tokens:
        # Move to end so genuine recent reuse stays "hot" until eviction.
        _seen_tokens.move_to_end(token)
        return False
    _seen_tokens[token] = None
    while len(_seen_tokens) > _REPLAY_CACHE_MAX:
        _seen_tokens.popitem(last=False)
    return True


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


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
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
