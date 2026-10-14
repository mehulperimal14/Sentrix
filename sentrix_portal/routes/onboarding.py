"""The 7-step onboarding wizard /onboarding/{1..7} and the device claim by code
or QR (/claim?sn=...).

Owner: track T-ACCT. Included by ``sentrix_portal.app.create_app``; empty in
the foundation.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()

__all__ = ["router"]
