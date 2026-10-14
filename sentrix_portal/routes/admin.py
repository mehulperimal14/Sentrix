"""Company admin console (company_admin + TOTP): /admin/customers (create,
welcome letter print view, re-send activation), /admin/devices (provision,
label with QR, assign, transfer), /admin/operators, /admin/audit. No access to
customers' video, faces or evidence.

Owner: track T-ACCT. Included by ``sentrix_portal.app.create_app``; empty in
the foundation.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()

__all__ = ["router"]
