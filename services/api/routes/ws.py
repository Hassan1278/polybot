"""WebSocket fan-out. Subscribe once, receive every interesting event."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from polybot.logging import get_logger
from polybot.redis_bus import subscribe

log = get_logger(__name__)

router = APIRouter()


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    log.info("ws_client_connected")

    async def forward(channel: str) -> None:
        async for msg in subscribe(channel):
            await ws.send_text(json.dumps({"channel": channel, "data": msg}))

    tasks = [
        asyncio.create_task(forward("trade:new")),
        asyncio.create_task(forward("signal:new")),
        asyncio.create_task(forward("fill:new")),
        asyncio.create_task(forward("kill:set")),
        asyncio.create_task(forward("kill:clear")),
    ]
    try:
        await ws.receive_text()        # block until client disconnects
    except WebSocketDisconnect:
        pass
    finally:
        for t in tasks:
            t.cancel()
        log.info("ws_client_disconnected")
