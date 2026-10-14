"""Sign-in and account: GET/POST /login, POST /logout, GET/POST
/activate/{token}, /account (password, sessions), /account/2fa (TOTP; required
for company_admin and operator). Back-off per (username, IP), never a hard
lockout.

Owner: track T-ACCT. Included by ``sentrix_portal.app.create_app``; empty in
the foundation.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()

__all__ = ["router"]
