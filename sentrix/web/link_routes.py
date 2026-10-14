"""/api/link/* - link status, remote viewers and the remote-access audit (docs/7
§9.2).

Owner: track T-LINK. Included by ``sentrix.web.extensions``; empty in the
foundation.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()

__all__ = ["router"]
