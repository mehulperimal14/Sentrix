"""/settings: SOS window, safety defaults, SOS and duress PINs (step-up),
channels, Telegram linking. Changes apply to future requests only.

Owner: track T-ACCT. Included by ``sentrix_portal.app.create_app``; empty in
the foundation.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()

__all__ = ["router"]
