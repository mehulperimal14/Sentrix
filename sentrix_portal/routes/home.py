"""GET / and /home: one card per device (online, HOME/ARMED, last incident,
AI rate, open SOS request), links to the device's own dashboard through the
tunnel, the onboarding banner and (base template) the test-mode banner.

Owner: track T-ACCT. Online state comes from the link hub
(``services.hub.is_online``); open requests from ``services.sos.open_requests``
(owner view: a duress approval reads "Marked as false alarm"); the last
incident from the metadata mirror. Nothing here reaches the device.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from sentrix_portal import security
from sentrix_portal.db import DB
from sentrix_portal.models import Device, IncidentMirror
from sentrix_portal.routes.auth import (HOUSEHOLD_ROLES, household_of, landing_for, render,
                                        see_other)
from sentrix_portal.services import accounts

router = APIRouter()


@router.get("/")
def root(request: Request, db: Session = DB):
    principal = security.principal_of(request, db)
    if principal is None:
        return see_other("/login")
    return see_other(landing_for(principal.role, household_of(db, principal)))


def _open_requests(db: Session, household_id: str) -> list[dict[str, Any]]:
    from sentrix_portal.services import sos
    try:
        return list(sos.open_requests(db, household_id) or [])
    except Exception:                                 # the page must still render
        return []


def device_card(db: Session, device: Device, requests: list[dict]) -> dict[str, Any]:
    from sentrix_portal.services import hub
    heartbeat = device.last_heartbeat or {}
    last = (db.query(IncidentMirror)
            .filter_by(device_id=device.id, household_id=device.household_id)
            .order_by(IncidentMirror.started_at.desc()).first())
    cameras = accounts.device_cameras(db, device.id)
    return {
        "id": device.id, "serial": device.serial, "name": device.name or device.serial,
        "state": device.state, "claimed": device.state == "CLAIMED",
        "online": device.state == "CLAIMED" and bool(hub.is_online(device.id)),
        "armed": bool(heartbeat.get("armed", device.armed)),
        "ai_hz": heartbeat.get("ai_hz"), "last_seen": accounts.iso(device.last_seen_at),
        "room": cameras[0].room_label if cameras else None,
        "last_incident": None if last is None else {
            "at": accounts.iso(last.started_at), "type": last.incident_type,
            "level": last.peak_level, "verdict": last.verdict},
        "requests": [r for r in requests if r.get("device_id") in (device.id, None)],
    }


@router.get("/home", response_class=HTMLResponse)
def home(request: Request, db: Session = DB,
         principal=Depends(security.require_role())):
    if principal.role not in HOUSEHOLD_ROLES:
        return see_other(landing_for(principal.role))
    household = household_of(db, principal)
    if household is None:
        return see_other("/account")
    requests = _open_requests(db, household.id)
    devices = accounts.household_devices(db, household.id)
    owner = accounts.household_owner(db, household.id)
    done = accounts.onboarding_done(db, household, owner)
    return render(request, "home/home.html", {
        "household": household, "cards": [device_card(db, d, requests) for d in devices],
        "requests": requests, "onboarding_done": household.onboarding_completed_at is not None,
        "next_step": household.onboarding_step or 1, "steps_done": done,
        "erasure_due": accounts.iso(accounts.erasure_due_at(household)),
    }, active_page="home")


__all__ = ["router", "device_card"]
