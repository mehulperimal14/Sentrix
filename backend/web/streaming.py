# web/streaming.py
#
# ARCHITECTURE: MJPEG video streaming endpoint.
# Reads the latest annotated frame from SystemEngine and JPEG-encodes it
# for HTTP streaming. The /video route produces a continuous multipart/x-mixed-replace
# MJPEG stream compatible with standard <img src="/video"> HTML elements.
# Auth-guarded: unauthenticated requests are redirected to /login.

import asyncio
import os

import cv2
from fastapi import APIRouter, Cookie
from fastapi.responses import StreamingResponse, RedirectResponse

from core.engine_instance import get_system_engine
from core.security        import verify_token

router = APIRouter()

JPEG_QUALITY = 70
STREAM_FPS   = 25  # Smooth 25 FPS stream


async def _frame_generator():
    """Async generator that yields MJPEG frames with zero stutter."""
    engine = get_system_engine()
    frame_interval = 1.0 / STREAM_FPS

    while True:
        try:
            frame = engine.get_latest_frame()
            if frame is not None and frame.size > 0:
                ret, buffer = cv2.imencode(
                    ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
                )
                if ret:
                    jpg_bytes = buffer.tobytes()
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n\r\n"
                        + jpg_bytes
                        + b"\r\n"
                    )
        except Exception as e:
            print(f"[Streaming] Frame encode error: {e}")

        await asyncio.sleep(frame_interval)


@router.get("/video")
async def video_feed(session_token: str = Cookie(None)):
    """MJPEG stream of the live annotated camera feed. Requires valid session."""
    expected_pwd = os.getenv("SENTRIX_PASSWORD")
    if not expected_pwd or not verify_token(session_token, expected_pwd):
        return RedirectResponse(url="/login")

    return StreamingResponse(
        _frame_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )