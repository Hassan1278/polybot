"""WebSocket fan-out. Subscribe once, receive every interesting event.

Auth model: clients must present EITHER a valid SIWE session token via the
`?session=` query param OR a legacy admin token via `?token=`. Without
auth the connection is rejected with 4401 BEFORE accept(), so an
unauthenticated peer never sees a single fill / signal / kill event.

Browsers can't set custom headers on WebSocket handshakes (no fetch-style
auth header), so query-param auth is the standard pattern. The session
token IS the bearer credential — same one stored in sessionStorage and
sent via X-Session-Token to /admin/* — so this isn't a new attack
surface, just a different transport.
"""

from __future__ import annotations

import asyncio
import json
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from polybot.config import settings
from polybot.logging import get_logger
from polybot.redis_bus import subscribe

log = get_logger(__name__)

router = APIRouter()


async def _ws_authorise(ws: WebSocket) -> str | None:
    """Resolve the websocket peer to a user identifier or None if unauth'd.

    SIWE path: ?session=<token> → returns the wallet address.
    Legacy path: ?token=<admin_token> → returns 'admin'.
    """
    session = ws.query_params.get("session")
    if session:
        from services.api.routes.auth import session_is_valid
        addr = await session_is_valid(session)
        if addr:
            return addr
    token = ws.query_params.get("token")
    if token:
        from services.api.deps import _verify_hardened, _verify_legacy
        secret = settings.admin_token.get_secret_value()
        if token.count(":") == 2 and _verify_hardened(token, secret):
            return "admin"
        legacy = os.environ.get("LEGACY_ADMIN_AUTH", "true").strip().lower() != "false"
        if legacy and _verify_legacy(token, secret):
            return "admin"
    return None


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    actor = await _ws_authorise(ws)
    if actor is None:
        # Close BEFORE accepting so a curious port-scanner never sees the
        # event channels. 1008 (policy violation) is the closest standard
        # close code; some clients map it to "permission denied".
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        log.warning("ws_unauth_close")
        return
    await ws.accept()
    log.info("ws_client_connected", actor=actor)

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
        # Drain ALL client frames until disconnect — the previous code
        # returned after the FIRST receive_text, tearing down the
        # forward tasks even though the client was still subscribed.
        # The dashboard's live trade/fill stream would silently die after
        # the first ping/heartbeat message.
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        for t in tasks:
            t.cancel()
        # Await cancellation so we don't leak Redis pubsub subscriptions
        # past the WebSocket disconnect (each forward holds a pubsub).
        await asyncio.gather(*tasks, return_exceptions=True)
        log.info("ws_client_disconnected")
