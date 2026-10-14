"""/api/sos/* - the LC JSON API for assistance requests (docs/7 §9.2). Every
route requires the LC session or a verified tunnel identity; POST routes need
the CSRF header.

Owner: track T-SOS. Included by ``sentrix.web.extensions``; empty in the
foundation.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()

__all__ = ["router"]
