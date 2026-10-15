"""/api/node/* - device telemetry and the confirmed 1 s test siren (docs/7 §9.2).

Owner: track T-HW. Included by ``sentrix.web.extensions``.

* ``GET /api/node`` — the ``state.node`` block (the node and gpio services
  write it; ``NODE_IDLE`` while they are off) with ``gpio = {siren, leds:
  {green, amber, red}, buzzer, pir, door, mains}``, plus the siren as the
  pipeline sees it (backend, real, armed, on), the test limiter, the camera
  stream's status (Picamera2) and which hardware services run.
* ``POST /api/node/siren-test`` — 1 s of siren through
  ``actuators.siren_test`` (the limiter is shared with the link's
  ``siren.test`` command: one test per minute for the whole process). 409
  ``conflict`` while the siren line is disarmed, 429 ``rate_limited`` with
  ``Retry-After``.

Who may test is the tunnel path table's rule (owner + step-up ≤ 300 s,
``sentrix/link/policy.py``) and the device admin on the LAN; like every
console route this one only requires an authenticated principal and never
restates the path table (``sentrix.web.auth``). The client sends the CSRF
header like any state-changing fetch (T-WEB's hardening checks it).
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from sentrix.web.auth import LcError, LcPrincipal, require_lc

router = APIRouter()


def _pipeline() -> Any:
    from sentrix.pipeline.engine import get_pipeline
    return get_pipeline()


def _siren_status(siren: Any) -> dict[str, Any]:
    status = getattr(siren, "status", None)
    if callable(status):
        return status()
    from sentrix.io.actuators import siren_armed
    return {"backend": siren.name, "real": siren.is_real, "armed": siren_armed(), "on": False}


def node_payload() -> dict[str, Any]:
    """The ``/api/node`` body."""
    from sentrix import services
    from sentrix.config import settings
    from sentrix.io.actuators import siren_arming, siren_test_retry_after
    from sentrix.pipeline.state import NODE_IDLE, state

    payload = dict(state.get().get("node") or NODE_IDLE)
    pipeline = _pipeline()
    siren = _siren_status(pipeline.siren)
    payload["siren"] = siren
    payload["siren_armed"] = siren.get("armed")
    payload["siren_arming"] = siren_arming()
    payload["siren_test"] = {"seconds": settings.link.siren_test_seconds,
                             "min_interval_seconds": settings.link.siren_test_min_interval_seconds,
                             "retry_after": siren_test_retry_after()}
    camera_status = getattr(pipeline.source, "status", None)
    payload["camera"] = (camera_status() if callable(camera_status)
                         else {"name": pipeline.source.name, "online": pipeline.source.online})
    health = services.registry.health()
    payload["services"] = {name: (health.get(name) or {}).get("status", "off")
                           for name in ("node", "gpio")}
    from sentrix.io.speaker import get_speaker
    speaker = get_speaker().status()
    payload["speaker"] = {k: speaker[k] for k in ("enabled", "backend", "real", "ready",
                                                  "speaking", "last")}
    return payload


@router.get("/api/node")
async def api_node(_principal: LcPrincipal = Depends(require_lc())) -> JSONResponse:
    return JSONResponse(await asyncio.to_thread(node_payload))


@router.post("/api/node/siren-test")
async def api_siren_test(principal: LcPrincipal = Depends(require_lc())) -> JSONResponse:
    from sentrix.io.actuators import siren_test

    pipeline = await asyncio.to_thread(_pipeline)
    result = await asyncio.to_thread(siren_test, pipeline.siren, actor=principal.to_actor())
    if not result["ok"]:
        headers = None
        if result.get("retry_after"):
            headers = {"Retry-After": str(max(1, math.ceil(result["retry_after"])))}
        raise LcError(result["code"], result["message"], headers=headers)
    return JSONResponse(result)


__all__ = ["router", "node_payload"]
