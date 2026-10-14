"""The device socket WS /device/v1/link (protocol v1, sentrix.link.protocol):
auth, hello/heartbeat, outbox events with acks, state/frame fan-out, commands,
profile push. The connection logic is ``services.hub.LinkHub.serve``; this
module accepts the socket and adapts Starlette's WebSocket to the hub's
two-method connection.

Unauthenticated at the HTTP layer (gate P2): the device proves itself with
its token or bootstrap secret in the ``Authorization`` header; a bad one is
closed with 4001 after the accept, so the device learns the reason.

Owner: track T-LINK.
"""

from __future__ import annotations

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from sentrix.link import protocol
from sentrix_portal.services.hub import hub_for

router = APIRouter()


class StarletteConnection:
    """Starlette's server-side WebSocket behind the hub's connection interface."""

    def __init__(self, websocket: WebSocket) -> None:
        self.ws = websocket
        self.close_code: int | None = None

    async def send_text(self, text: str) -> None:
        await self.ws.send_text(text)

    async def send_bytes(self, data: bytes) -> None:
        await self.ws.send_bytes(data)

    async def receive(self) -> str | bytes:
        message = await self.ws.receive()
        if message["type"] == "websocket.disconnect":
            self.close_code = message.get("code")
            raise WebSocketDisconnect(message.get("code") or 1000)
        if message.get("text") is not None:
            return message["text"]
        if message.get("bytes") is not None:
            return message["bytes"]
        return ""

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.ws.application_state == WebSocketState.DISCONNECTED:
            return
        try:
            await self.ws.close(code=code, reason=reason[:120])
        except Exception:
            pass


@router.websocket(protocol.PATH)
async def device_link(websocket: WebSocket) -> None:
    await websocket.accept()
    await hub_for(websocket.app).serve(StarletteConnection(websocket),
                                       websocket.headers.get("authorization"))


__all__ = ["router", "StarletteConnection"]
