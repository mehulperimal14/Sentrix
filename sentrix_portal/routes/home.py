"""GET / and /home: one card per device (online, HOME/ARMED, last incident,
AI rate, open SOS request), links to the device's own dashboard through the
tunnel, the onboarding banner and (base template) the test-mode banner.

Owner: track T-ACCT. Online state comes from the link hub
(``services.hub.is_online``); open requests from ``services.sos.open_requests``
(owner view: a duress approval reads "Marked as false alarm"); the last
incident from the metadata mirror. Nothing here reaches the device.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from sentrix_portal import security
from sentrix_portal.db import DB
from sentrix_portal.routes.auth import (HOUSEHOLD_ROLES, household_of, landing_for, render,
                                        see_other)
from sentrix_portal.services import household_flows as flows

router = APIRouter()

device_card = flows.device_card


@router.get("/")
def root(request: Request, db: Session = DB):
    principal = security.principal_of(request, db)
    if principal is None:
        return see_other("/login")
    return see_other(landing_for(principal.role, household_of(db, principal)))


@router.get("/home", response_class=HTMLResponse)
def home(request: Request, db: Session = DB,
         principal=Depends(security.require_role())):
    if principal.role not in HOUSEHOLD_ROLES:
        return see_other(landing_for(principal.role))
    household = household_of(db, principal)
    if household is None:
        return see_other("/account")
    return render(request, "home/home.html", flows.home_state(db, household),
                  active_page="home")


__all__ = ["router", "device_card"]
