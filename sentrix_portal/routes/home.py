"""GET / and /home: one card per device (online, HOME/ARMED, last incident, AI
Hz), open-request banner, test-mode banner.

Owner: track T-ACCT. Included by ``sentrix_portal.app.create_app``; empty in
the foundation.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()

__all__ = ["router"]
