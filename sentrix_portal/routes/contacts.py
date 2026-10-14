"""Contact token pages /c/{token}: consent confirm/opt-out, SOS view with I'm
handling it / Call resident / Call 112 (Desk number in test mode). Per contact
and request; expire at close + 2 h; every open logged.

Owner: track T-LINK. Included by ``sentrix_portal.app.create_app``; empty in
the foundation.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()

__all__ = ["router"]
