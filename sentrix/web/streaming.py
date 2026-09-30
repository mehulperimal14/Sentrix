"""MJPEG live stream."""

from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, Cookie
from fastapi.responses import RedirectResponse, StreamingResponse

from sentrix.config import settings
from sentrix.pipeline.engine import get_pipeline
from sentrix.utils.logging import get_logger
from sentrix.web.security import verify_token

log = get_logger("streaming")
router = APIRouter()

try:
    import cv2
except ImportError:                                    # pragma: no cover
    cv2 = None                                         # type: ignore[assignment]

JPEG_QUALITY = 70


async def _frames():
    pipeline = get_pipeline()
    interval = 1.0 / max(settings.pipeline.capture_fps, 1.0)
    while True:
        try:
            frame = pipeline.latest_frame()
            if cv2 is not None and frame is not None and frame.size:
                ok, buffer = cv2.imencode(".jpg", frame,
                                          [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                if ok:
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                           + buffer.tobytes() + b"\r\n")
        except Exception as exc:
            log.debug("stream encode failed: %s", exc)
        await asyncio.sleep(interval)


@router.get("/video")
async def video(session_token: str | None = Cookie(None)):
    password = os.getenv("SENTRIX_PASSWORD", "").strip()
    if not password or not verify_token(session_token, password):
        return RedirectResponse(url="/login")
    return StreamingResponse(
        _frames(), media_type="multipart/x-mixed-replace; boundary=frame")


__all__ = ["router"]
