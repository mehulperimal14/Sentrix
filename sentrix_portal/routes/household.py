"""/household: profile, address, members, emergency contacts with consent
confirmation; every change bumps profile_version and is pushed to the device.

Owner: track T-ACCT. Included by ``sentrix_portal.app.create_app``; empty in
the foundation.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()

__all__ = ["router"]
