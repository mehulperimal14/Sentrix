"""/api/link/* - link status, remote viewers and the remote-access audit (docs/7
§9.2).

* ``GET /api/link``: the ``state.link`` block, ``remote_viewers`` and the link
  service's health (``enabled: false`` while ``SENTRIX_LINK_ENABLED`` is off);
* ``GET /api/link/remote-audit?limit=``: the newest ``remote_audit`` rows, every
  tunnelled request and link command the device served or refused (who, what,
  allowed, why), so the household sees on the device what the cloud did;
* ``POST /api/link/reconnect``: drop the link and dial again (device admin on
  the LAN only; never through the tunnel, ``sentrix/link/policy.py``).

Owner: track T-LINK. Included by ``sentrix.web.extensions``.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from sentrix import services
from sentrix.pipeline.state import state
from sentrix.web.auth import LcError, require_lc

router = APIRouter()


@router.get("/api/link")
async def api_link(_principal=Depends(require_lc())) -> JSONResponse:
    live = state.get()
    service = services.registry.get("link")
    return JSONResponse({**(live.get("link") or {}),
                         "remote_viewers": int(live.get("remote_viewers") or 0),
                         "service": service.health() if service is not None else None})


@router.get("/api/link/remote-audit")
async def api_remote_audit(limit: int = Query(100, ge=1, le=500),
                           _principal=Depends(require_lc())) -> JSONResponse:
    from sentrix.storage import link_store
    rows = await asyncio.to_thread(link_store.recent_remote, limit)
    return JSONResponse({"rows": rows})


@router.post("/api/link/reconnect")
async def api_reconnect(_principal=Depends(require_lc("device_admin"))) -> JSONResponse:
    service = services.registry.get("link")
    if service is None or not hasattr(service, "reconnect_now"):
        raise LcError("unsupported", "the link is off (SENTRIX_LINK_ENABLED=0)")
    if not service.reconnect_now():
        raise LcError("conflict", "the link is not running")
    return JSONResponse({"status": "reconnecting"})


__all__ = ["router"]
