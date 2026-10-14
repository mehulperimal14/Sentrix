"""Contact consent pages: ``GET/POST /consent/{token}`` (confirm or opt out of
being an emergency contact; token purpose ``contact_consent``). SOS contact
pages are ``/c/{token}`` (T-LINK, ``routes/contacts.py``); e-mail
verification is ``/account/verify-email/{token}`` (``routes/auth.py``).

Owner: track T-ACCT. Included by ``sentrix_portal.app.create_app``; empty in
the foundation.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()

__all__ = ["router"]
