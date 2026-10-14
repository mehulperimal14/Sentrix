"""LC pages /sos and /node, GET /api/level-history, and /setup/wifi (only while
SENTRIX_SETUP_MODE=1, docs/7 §9.2).

Owner: track T-WEB. Included by ``sentrix.web.extensions``; empty in the
foundation.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()

__all__ = ["router"]
