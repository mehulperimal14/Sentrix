"""/api/speaker/* - the talk-down speaker (decision D16).

* ``GET /api/speaker`` - status: enabled, backend, ready, speaking, the last
  message, limits and the presets.
* ``POST /api/speaker/say`` ``{"text": "..."}`` or ``{"preset_id": "leave"}`` -
  speak it in the room. 409 ``conflict`` while the speaker is off or still
  talking, 400 ``invalid_args`` for empty/over-long text or an unknown preset,
  429 ``rate_limited`` with ``Retry-After`` past 3 messages a minute.

Who may speak is the tunnel path table's rule (owner + step-up <= 300 s,
``sentrix/link/policy.py``) and the device admin on the LAN; like every
console route this one only requires an authenticated principal and never
restates the path table (``sentrix.web.auth``). The client sends the CSRF
header like any state-changing fetch (T-WEB's hardening checks it). Every
message is audited (``speaker_say`` in the event log, mirrored to the Portal).
"""

from __future__ import annotations

import asyncio
import math

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse

from sentrix.web.auth import LcError, LcPrincipal, require_lc

router = APIRouter()


@router.get("/api/speaker")
async def api_speaker(_principal: LcPrincipal = Depends(require_lc())) -> JSONResponse:
    from sentrix.io.speaker import get_speaker
    speaker = await asyncio.to_thread(get_speaker)
    return JSONResponse(speaker.status())


@router.post("/api/speaker/say")
async def api_speaker_say(payload: dict = Body(default_factory=dict),
                          principal: LcPrincipal = Depends(require_lc())) -> JSONResponse:
    from sentrix.io.speaker import get_speaker
    if not isinstance(payload, dict):
        raise LcError("invalid_args", "send {text} or {preset_id}")
    text, preset = payload.get("text"), payload.get("preset_id")
    if text is not None and not isinstance(text, str):
        raise LcError("invalid_args", "text must be a string")
    if preset is not None and not isinstance(preset, str):
        raise LcError("invalid_args", "preset_id must be a string")
    speaker = await asyncio.to_thread(get_speaker)
    result = await asyncio.to_thread(speaker.say, text, preset, actor=principal.to_actor())
    if not result.ok:
        headers = None
        if result.retry_after:
            headers = {"Retry-After": str(max(1, math.ceil(result.retry_after)))}
        raise LcError(result.code, result.message, headers=headers)
    return JSONResponse(result.to_dict())


__all__ = ["router"]
