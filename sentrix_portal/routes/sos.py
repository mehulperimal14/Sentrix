"""Portal SOS: /sos and /sos/{request_id} (prompt, countdown, approve, decline
with PIN, I'm checking, Request help now), /a/{token} confirmation pages for
e-mail/SMS action links (POST + CSRF), cloud authority timers when the device
is offline.

Owner: track T-LINK. Included by ``sentrix_portal.app.create_app``; empty in
the foundation.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()

__all__ = ["router"]
