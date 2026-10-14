"""/privacy: notice, consents (one toggle per purpose), export (JSON), erasure,
grievance contact.

Owner: track T-ACCT. Included by ``sentrix_portal.app.create_app``; empty in
the foundation.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()

__all__ = ["router"]
