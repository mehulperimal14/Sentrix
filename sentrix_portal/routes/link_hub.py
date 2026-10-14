"""The device socket WS /device/v1/link (protocol v1, sentrix.link.protocol):
auth, hello/heartbeat, outbox events with acks, state/frame fan-out, commands,
profile push.

Owner: track T-LINK. Included by ``sentrix_portal.app.create_app``; empty in
the foundation.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()

__all__ = ["router"]
