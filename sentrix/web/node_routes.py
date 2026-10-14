"""/api/node/* - device telemetry and the confirmed 1 s test siren (docs/7 §9.2).

Owner: track T-HW. Included by ``sentrix.web.extensions``; empty in the
foundation.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()

__all__ = ["router"]
