"""/api/node - device telemetry (docs/7 §9.2).

Owner: track T-HW. Included by ``sentrix.web.extensions``.

* ``GET /api/node`` — the ``state.node`` block (the node and gpio services
  write it; ``NODE_IDLE`` while they are off) with ``gpio = {leds: {green,
  amber, red}, pir, door, mains}``, plus the camera stream's status
  (Picamera2), the microphone (``AudioEngine.status()``, None with audio off)
  and which hardware services run.

The device has no siren since D25 (2026-11-20; ``POST /api/node/siren-test``
and the link's ``siren.test`` went with it), no buzzer and no speaker since
D19.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from sentrix.web.auth import LcPrincipal, require_lc

router = APIRouter()


def _pipeline() -> Any:
    from sentrix.pipeline.engine import get_pipeline
    return get_pipeline()


def node_payload() -> dict[str, Any]:
    """The ``/api/node`` body."""
    from sentrix import services
    from sentrix.pipeline.state import NODE_IDLE, state

    payload = dict(state.get().get("node") or NODE_IDLE)
    pipeline = _pipeline()
    camera_status = getattr(pipeline.source, "status", None)
    payload["camera"] = (camera_status() if callable(camera_status)
                         else {"name": pipeline.source.name, "online": pipeline.source.online})
    # Which microphone, at what native rate; None with audio off (SENTRIX_AUDIO=0).
    from sentrix.pipeline.engine import audio_status
    payload["microphone"] = audio_status(getattr(pipeline, "audio", None))
    health = services.registry.health()
    payload["services"] = {name: (health.get(name) or {}).get("status", "off")
                           for name in ("node", "gpio")}
    return payload


@router.get("/api/node")
async def api_node(_principal: LcPrincipal = Depends(require_lc())) -> JSONResponse:
    return JSONResponse(await asyncio.to_thread(node_payload))


__all__ = ["router", "node_payload"]
