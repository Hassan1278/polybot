"""Redis-backed rate limiter for admin endpoints.

Pattern: per-IP-per-minute fixed-window counter. Each request:
  - INCR polybot:ratelimit:admin:{ip}:{minute_bucket}
  - EXPIRE on first hit (TTL=90s so the bucket survives until the
    next minute boundary plus margin)
  - reject with 429 if count > limit

Why fixed-window over sliding-window: simpler, single round-trip,
acceptable burst behavior at the minute boundary (someone can burst
60 in two seconds and get 60 more right after — but that's still capped
at 2× the limit, well within the surface we're protecting).

Used by `services/api/main.py` to wrap the admin router. Not applied
to /health or /metrics (those are operational/observability, not
mutational).
"""

from __future__ import annotations

import os
import time
from typing import Final

from fastapi import HTTPException, Request, status

from polybot.logging import get_logger
from polybot.redis_bus import client as _redis

log = get_logger(__name__)

# Default budget: 60 admin requests per minute per IP. Generous enough for
# a busy dashboard session (kill/un-kill twice, page refreshes, etc.) but
# tight enough to slow down brute-force/replay attacks.
_DEFAULT_LIMIT: Final[int] = 60
_BUCKET_TTL_S: Final[int] = 90


def _trusted_proxies() -> set[str]:
    """Read TRUSTED_PROXIES env (comma-separated IPs). Empty = no trust.

    Only requests whose direct TCP peer (request.client.host) is in this
    set are allowed to set X-Forwarded-For for rate-limit attribution.
    Without this, anyone reaching the API directly on port 8000 could
    spoof XFF to a victim IP and either evade their own bucket OR DoS
    the victim's bucket. In prod Caddy is the only legitimate XFF setter.
    """
    raw = os.environ.get("TRUSTED_PROXIES", "")
    return {p.strip() for p in raw.split(",") if p.strip()}


def _client_ip(request: Request) -> str:
    """Resolve client IP with anti-spoofing.

    XFF is honoured ONLY when the direct peer (Starlette's
    request.client.host) appears in TRUSTED_PROXIES. Otherwise we ignore
    XFF entirely — an unverified hop's claim about the original IP can
    be anything.
    """
    direct = request.client.host if request.client else "unknown"
    trusted = _trusted_proxies()
    if trusted and direct in trusted:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            # Left-most untrusted hop is the closest to the originator we
            # can trust; we don't try to walk multi-proxy chains here.
            return xff.split(",")[0].strip()
    return direct


def admin_rate_limit(limit_per_minute: int = _DEFAULT_LIMIT):
    """FastAPI dependency that rate-limits the current request by IP.

    Usage:
        @router.post("/sensitive", dependencies=[Depends(admin_rate_limit())])
        ...

    Or as a default for an entire router:
        router = APIRouter(dependencies=[Depends(admin_rate_limit())])
    """

    async def _check(request: Request) -> None:
        ip = _client_ip(request)
        # Fixed-window bucket — minute granularity.
        bucket = int(time.time()) // 60
        key = f"polybot:ratelimit:admin:{ip}:{bucket}"
        r = _redis()
        try:
            count = await r.incr(key)
            if count == 1:
                # First hit in this bucket — set TTL so it auto-expires.
                await r.expire(key, _BUCKET_TTL_S)
        except Exception:  # noqa: BLE001
            # Redis hiccup: fail open (don't block legitimate admin ops
            # on transient infra issues). Logged so ops can spot it.
            log.exception("rate_limit_redis_failed", ip=ip)
            return
        if count > limit_per_minute:
            log.warning(
                "admin_rate_limit_exceeded",
                ip=ip, count=count, limit=limit_per_minute,
            )
            # Standard 429 with Retry-After header (seconds until next bucket).
            retry_after = 60 - (int(time.time()) % 60)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"admin rate limit {limit_per_minute}/min exceeded",
                headers={"Retry-After": str(retry_after)},
            )

    return _check
