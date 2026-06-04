from __future__ import annotations

from fastapi import APIRouter

from polybot.config import settings
from polybot.redis_bus import kill_status

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "mode": settings.trading_mode,
        "can_sign": settings.can_sign,
        "kill_switch": await kill_status(),
    }
