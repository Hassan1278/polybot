from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request

from polybot.redis_bus import kill_clear, kill_set, kill_status
from services.api.deps import require_admin

router = APIRouter()


def _actor(request: Request, x_session_token: str | None) -> str:
    """Best-effort actor identifier for the audit log."""
    if x_session_token:
        return f"session:{x_session_token[:8]}"
    return f"ip:{request.client.host}" if request.client else "unknown"


@router.post("/kill", dependencies=[Depends(require_admin)])
async def kill(
    request: Request,
    reason: str = "manual",
    x_session_token: str | None = Header(default=None),
) -> dict:
    await kill_set(reason, actor=_actor(request, x_session_token))
    return {"killed": True, "reason": reason}


@router.post("/kill/clear", dependencies=[Depends(require_admin)])
async def unkill(
    request: Request,
    by: str = "manual",
    x_session_token: str | None = Header(default=None),
) -> dict:
    actor = _actor(request, x_session_token)
    await kill_clear(by if by != "manual" else actor)
    return {"killed": False, "by": by}


@router.get("/kill", dependencies=[Depends(require_admin)])
async def kill_get() -> dict:
    # B15: GET also requires admin — kill-switch status is operational info,
    # not for public consumption (an attacker can correlate this with their
    # own trading to time exploits). Mirrors the POST endpoints above.
    return {"active": (await kill_status()) is not None,
            "reason": await kill_status()}
