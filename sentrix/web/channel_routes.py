"""/api/channels/* - per-channel readiness and a guarded test message (docs/7
§9.2).

Owner: track T-CH. Included by ``sentrix.web.extensions``; empty in the
foundation.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()

__all__ = ["router"]
