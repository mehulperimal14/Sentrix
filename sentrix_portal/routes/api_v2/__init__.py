"""The Portal's JSON API for the single-page app (docs/9 §4-§6), under
``/api/v2``: one module per area. Each calls the same service functions as
the HTML page it mirrors (``services.account_flows``, ``household_flows``,
``sos_views``, ``admin_flows``), so rules, messages, audit events, rate
limits and step-up stay identical.

``create_app`` includes :data:`router` and calls :func:`install` (the error
model for every ``/api/v2`` answer).
"""

from __future__ import annotations

from fastapi import APIRouter

from sentrix_portal.routes.api_v2 import (admin, auth, contacts, desk, home, household,
                                          onboarding, privacy, settings, sos)
from sentrix_portal.routes.api_v2.common import ERROR_RESPONSES, PREFIX
from sentrix_portal.routes.api_v2.common import install as _install_errors

AREAS = (auth, home, onboarding, household, settings, privacy, sos, contacts, desk, admin)

router = APIRouter(prefix=PREFIX, responses=ERROR_RESPONSES)
for _area in AREAS:
    router.include_router(_area.router)


def install(app) -> None:
    _install_errors(app)


__all__ = ["router", "install", "AREAS"]
