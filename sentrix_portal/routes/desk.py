"""SENTRIX Desk console (operator + TOTP): /desk queue with alarm sound, SOS
card, acknowledge, resolve with outcome, re-page timer. Sees a household only
while its request is on the help path.

Owner: track T-LINK. Included by ``sentrix_portal.app.create_app``; empty in
the foundation.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()

__all__ = ["router"]
