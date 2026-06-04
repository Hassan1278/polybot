from __future__ import annotations

from fastapi import APIRouter, Depends

from polybot.redis_bus import kill_clear, kill_set, kill_status
from services.api.deps import require_admin

router = APIRouter()


@router.post("/kill", dependencies=[Depends(require_admin)])
async def kill(reason: str = "manual") -> dict:
    await kill_set(reason)
    return {"killed": True, "reason": reason}


@router.post("/kill/clear", dependencies=[Depends(require_admin)])
async def unkill(by: str = "manual") -> dict:
    await kill_clear(by)
    return {"killed": False, "by": by}


@router.get("/kill")
async def kill_get() -> dict:
    return {"active": (await kill_status()) is not None,
            "reason": await kill_status()}
