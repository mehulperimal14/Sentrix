"""MJPEG live stream."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, StreamingResponse

from sentrix.config import settings
from sentrix.pipeline.engine import get_pipeline
from sentrix.utils.logging import get_logger
from sentrix.web import auth

log = get_logger("streaming")
router = APIRouter()

try:
    import cv2
except ImportError:                                    # pragma: no cover
    cv2 = None                                         # type: ignore[assignment]

JPEG_QUALITY = 70


async def _frames():
    """Yield each new preview frame once.

    The pipeline encodes a preview once however many viewers there are; this
    only polls its sequence number, so an unchanged frame is never re-sent and
    a new one goes out within half a capture period.
    """
    pipeline = get_pipeline()
    poll = 0.5 / max(settings.pipeline.capture_fps, 1.0)
    sent = None
    while True:
        try:
            if pipeline.preview_seq != sent:
                seq, data = await asyncio.to_thread(pipeline.latest_jpeg, JPEG_QUALITY)
                if data is not None and seq != sent:
                    sent = seq
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                           + data + b"\r\n")
                    continue
        except Exception as exc:
            log.debug("stream encode failed: %s", exc)
        await asyncio.sleep(poll)


@router.get("/video")
async def video(request: Request):
    """The HUD stream for an LC session. Portal viewers never reach this
    route (the hardening refuses tunnel identities here): the link relays
    frames to them instead (docs/7 §5.7)."""
    if auth.principal_of(request) is None:
        return RedirectResponse(url="/login")
    return StreamingResponse(
        _frames(), media_type="multipart/x-mixed-replace; boundary=frame")


__all__ = ["router"]
