"""Tunnel parity: /d/{device_id}/{path} forwarded to the device's LC;
/d/{device_id}/video and /d/{device_id}/ws/threat relayed from frame/state
messages; offline mirror pages.

Owner: track T-LINK. Included by ``sentrix_portal.app.create_app``; empty in
the foundation.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()

__all__ = ["router"]
