"""/api/channels/* - per-channel readiness and a guarded test message (docs/7
§9.2). Owner: track T-CH. Included by ``sentrix.web.extensions``.

* ``GET /api/channels`` — the ``state.channels`` block (every channel's
  readiness, the outbox depth, ``modem`` = ``{present, registered,
  signal_dbm, sms_today, ...}``, the send guard's switches) plus the most
  recent notifications, masked. Without the router: the block the lifespan
  publishes for the legacy notifier.
* ``POST /api/channels/test`` — device admin only: one test SMS to the owner
  number through the router and the send guard (dry run by default: the
  answer then says SUPPRESSED and why). JSON body ``{"channel":
  "modem_sms" | "twilio_sms"}`` optional; at most 3 per 10 minutes.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from sentrix.web import auth
from sentrix.web.security import RateLimiter

router = APIRouter()

#: A test SMS costs money and wakes a phone: a few per 10 minutes.
test_limiter = RateLimiter(max_attempts=3, window_seconds=600.0)


def _service():
    from sentrix.alerting.channels.router import current_service
    return current_service()


@router.get("/api/channels", dependencies=[Depends(auth.require_lc())])
async def api_channels() -> JSONResponse:
    service = _service()
    if service is None:
        from sentrix.pipeline.state import state
        return JSONResponse({**state.get().get("channels", {}), "recent": []})
    block = await asyncio.to_thread(service.status)
    recent = await asyncio.to_thread(service.recent, 20)
    return JSONResponse({**block, "recent": recent})


@router.post("/api/channels/test")
async def api_channels_test(request: Request,
                            principal=Depends(auth.require_lc("device_admin"))) -> JSONResponse:
    service = _service()
    if service is None:
        raise auth.LcError("unsupported", "the channel router is off "
                                          "(SENTRIX_CHANNELS_ENABLED=0)")
    channel = None
    raw = await request.body()
    if raw:
        try:
            body = json.loads(raw)
        except ValueError:
            raise auth.LcError("invalid_args", "the body must be JSON") from None
        if not isinstance(body, dict):
            raise auth.LcError("invalid_args", "the body must be a JSON object")
        channel = body.get("channel") or None
        if channel is not None and not isinstance(channel, str):
            raise auth.LcError("invalid_args", "channel must be a string")
    if not test_limiter.allow(auth.rate_key(request)):
        raise auth.LcError("rate_limited", "at most 3 test messages per 10 minutes",
                           headers={"Retry-After": "600"})
    try:
        result = await asyncio.to_thread(service.send_test, channel, by=principal.actor)
    except ValueError as exc:
        raise auth.LcError("invalid_args", str(exc)) from None
    except LookupError as exc:
        raise auth.LcError("unprocessable", str(exc)) from None
    return JSONResponse(result)


__all__ = ["router", "test_limiter"]
