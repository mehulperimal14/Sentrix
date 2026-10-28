"""Home (API v2): the household's device cards (online, HOME/ARMED, AI rate,
last incident, open requests), the setup banner and the erasure notice; and
a device's offline mirror (the incidents, dispatch packages and open
requests the Portal keeps while the device cannot be reached). Live views of
a device (its picture, level, own incidents, verdicts) are the device's own
API through the tunnel ``/d/<device_id>/api/...``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from sentrix_portal.db import DB
from sentrix_portal.routes.api_v2.common import (HOUSEHOLD, HOUSEHOLD_OR_DESK, ApiError,
                                                 household_of, z)
from sentrix_portal.routes.api_v2.schemas import (DeviceCard, OwnerRequest, StaffRequest,
                                                  _times, owner_request, staff_request)
from sentrix_portal.services import household_flows as flows

router = APIRouter(tags=["home"])


class HomeOut(BaseModel):
    household_id: str
    name: str | None
    erasure_due: str | None
    setup_done: bool
    #: The step an unfinished setup resumes at.
    next_step: int
    steps_done: dict[str, bool]
    requests: list[OwnerRequest]
    devices: list[DeviceCard]


def _owner_view(view: dict[str, Any]) -> OwnerRequest:
    return OwnerRequest(**_times(view))


def _card(card: dict[str, Any]) -> DeviceCard:
    last = card["last_incident"]
    return DeviceCard(**{**card, "last_seen": z(card["last_seen"]),
                         "last_incident": None if last is None else {**last, "at": z(last["at"])},
                         "requests": [_owner_view(r) for r in card["requests"]]})


@router.get("/home", response_model=HomeOut)
def home(db: Session = DB, principal=Depends(HOUSEHOLD)) -> HomeOut:
    household = household_of(db, principal)
    state = flows.home_state(db, household)
    return HomeOut(household_id=household.id, name=household.display_name,
                   erasure_due=z(state["erasure_due"]), setup_done=state["onboarding_done"],
                   next_step=int(state["next_step"]),
                   steps_done={str(k): bool(v) for k, v in state["steps_done"].items()},
                   requests=[_owner_view(r) for r in state["requests"]],
                   devices=[_card(c) for c in state["cards"]])


# ── a device's offline mirror ─────────────────────────────────────────────────
class MirrorIncident(BaseModel):
    id: str
    started_at: str | None
    ended_at: str | None
    incident_type: str | None
    peak_level: int | None
    peak_tci: float | None
    reason: str | None
    verdict: str | None


class MirrorPackage(BaseModel):
    id: str
    created_at: str | None
    status: str | None
    authority: str | None


class MirrorOut(BaseModel):
    device_id: str
    name: str | None
    serial: str
    online: bool
    last_seen: str | None
    #: Verdicts set now are queued and sync when the device is back
    #: (``POST /d/<device_id>/api/incidents/<id>/verdict``).
    can_label: bool
    incidents: list[MirrorIncident]
    packages: list[MirrorPackage]
    requests: list[OwnerRequest | StaffRequest]


@router.get("/devices/{device_id}/mirror", response_model=MirrorOut)
def mirror(device_id: str, request: Request, db: Session = DB,
           principal=Depends(HOUSEHOLD_OR_DESK)) -> MirrorOut:
    """What the Portal keeps of a device (the page shown while it is
    offline). The Desk sees open requests only, and only on the help path
    of a household that allowed the Desk's live view."""
    from fastapi import HTTPException

    from sentrix_portal.routes.device_proxy import authorize_device
    from sentrix_portal.services.hub import hub_for
    try:
        device = authorize_device(db, principal, device_id)
    except HTTPException as exc:
        raise ApiError("not_found" if exc.status_code == 404 else "forbidden") from None
    from sentrix_portal.services.sos_views import device_mirror
    state = device_mirror(db, principal, device)
    return MirrorOut(
        device_id=device.id, name=device.name, serial=device.serial,
        online=bool(hub_for(request.app).online(device.id)), last_seen=z(device.last_seen_at),
        can_label=state["can_label"],
        incidents=[MirrorIncident(id=i.id, started_at=z(i.started_at), ended_at=z(i.ended_at),
                                  incident_type=i.incident_type, peak_level=i.peak_level,
                                  peak_tci=i.peak_tci, reason=i.reason, verdict=i.verdict)
                   for i in state["incidents"]],
        packages=[MirrorPackage(id=p.id, created_at=z(p.created_at), status=p.status,
                                authority=p.authority) for p in state["packages"]],
        requests=[owner_request(row) if principal.role in ("owner", "member")
                  else staff_request(row, "desk") for row in state["request_rows"]])


__all__ = ["router"]
