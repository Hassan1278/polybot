"""Health endpoints.

  /health       — process is alive. Fast, no I/O. Used by load balancers
                  and any monitor that needs a sub-millisecond response.
  /health/deep  — process is alive AND can talk to its dependencies (DB
                  + Redis). Returns 503 if any dependency is unhealthy
                  so docker/k8s healthchecks can restart the container.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from polybot.config import settings
from polybot.db import get_session
from polybot.logging import get_logger
from polybot.redis_bus import client as redis_client
from polybot.redis_bus import kill_status

log = get_logger(__name__)
router = APIRouter()

# Hard timeout per dependency check so a slow dependency doesn't hang the
# whole healthcheck endpoint. Docker healthchecks typically retry every
# 30 s — we want to fail-fast.
_DEP_TIMEOUT_S = 2.0


@router.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "mode": settings.trading_mode,
        "can_sign": settings.can_sign,
        "kill_switch": await kill_status(),
    }


async def _check_db(s: AsyncSession) -> tuple[bool, str]:
    try:
        async with asyncio.timeout(_DEP_TIMEOUT_S):
            await s.execute(text("SELECT 1"))
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, type(exc).__name__ + ":" + str(exc)[:120]


async def _check_redis() -> tuple[bool, str]:
    try:
        async with asyncio.timeout(_DEP_TIMEOUT_S):
            ok = await redis_client().ping()
        return bool(ok), "ok" if ok else "ping returned falsy"
    except Exception as exc:  # noqa: BLE001
        return False, type(exc).__name__ + ":" + str(exc)[:120]


@router.get("/health/deep")
async def health_deep(s: AsyncSession = Depends(get_session)) -> JSONResponse:
    db_ok, db_msg = await _check_db(s)
    redis_ok, redis_msg = await _check_redis()
    kill = await kill_status() if redis_ok else None

    payload = {
        "ok": db_ok and redis_ok,
        "checks": {
            "db": {"ok": db_ok, "detail": db_msg},
            "redis": {"ok": redis_ok, "detail": redis_msg},
        },
        "mode": settings.trading_mode,
        "can_sign": settings.can_sign,
        "kill_switch": kill,
    }
    status_code = 200 if (db_ok and redis_ok) else 503
    return JSONResponse(payload, status_code=status_code)
