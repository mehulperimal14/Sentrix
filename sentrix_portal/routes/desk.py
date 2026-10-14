"""SENTRIX Desk console (operator + TOTP): /desk queue with alarm sound, SOS
card, acknowledge, resolve with outcome, re-page timer. Sees a household only
while its request is on the help path.

The Desk is a *simulated* responder console for the capstone (D8): it is a
help-path target, so a request in its queue counts as delivered; it never
contacts 112. Every page and action requires the ``operator`` role with TOTP
verified this session (``security.require_role``).

* ``GET /desk``: the queue (APPROVED, DISPATCHED, ACKNOWLEDGED, FAILED), newest
  first; an alarm sounds (``/static/desk/desk.js``, Web Audio) while any
  request is not acknowledged. ``GET /desk/api/queue`` is its JSON.
* ``GET /desk/requests/{id}``: the SOS card: full text (with the DURESS banner
  "Do not go to the home. Call 112." when the resident was coerced), address
  and map, household notes, the resident's callback number, the consented SOS
  snapshot, the live view when the household allowed ``desk_live_view``, and
  the full timeline.
* ``POST /desk/requests/{id}/acknowledge`` | ``called-112`` (simulated; a note
  in the timeline) | ``resolve`` (outcome -> verdict).

Owner: track T-LINK.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from sentrix.sos import states as S
from sentrix_portal import security
from sentrix_portal.db import get_db
from sentrix_portal.models import (AssistanceRequest, Camera, Device, Household,
                                   SosSnapshot, User, utcnow)
from sentrix_portal.routes.device_proxy import actor_of
from sentrix_portal.routes.sos import call_112
from sentrix_portal.services import sos as sos_service
from sentrix_portal.services.hub import UUID_RE, hub_for

router = APIRouter()
_operator = security.require_role("operator")
OUTCOMES = {"help_arrived": "Help arrived (simulated)", "real_threat": "Real threat",
            "false_alarm_confirmed": "False alarm confirmed",
            "no_action_needed": "No action needed"}
MESSAGES = {"ok": "Done.", "conflict": "The request has moved on.",
            "invalid_args": "Choose an outcome.", "timeout": "The device did not answer."}


def _cfg():
    from sentrix.config import settings
    return settings


def _visible(row: AssistanceRequest) -> bool:
    """On the help path, or closed within the link grace (2 h)."""
    if row.state in sos_service.DESK_STATES:
        return True
    if row.state == S.CLOSED and row.updated_at is not None:
        updated = sos_service._aware(row.updated_at)
        return utcnow() - updated <= timedelta(seconds=_cfg().sos.link_grace_seconds)
    return False


def _row(db: Session, request_id: str) -> AssistanceRequest:
    if not UUID_RE.match(request_id or ""):
        raise HTTPException(404, detail="not_found")
    row = db.get(AssistanceRequest, request_id)
    if row is None or not _visible(row):
        raise HTTPException(404, detail="not_found")
    return row


def _card(db: Session, row: AssistanceRequest) -> dict:
    """The queue row: the request in desk view plus where and who."""
    view = sos_service.request_view(row, "desk")
    household = db.get(Household, row.household_id) if row.household_id else None
    owner = (db.query(User).filter_by(household_id=row.household_id, role="owner").first()
             if row.household_id else None)
    view.update(city=household.city if household else None,
                locality=household.locality if household else None,
                resident=owner.display_name if owner else None,
                acknowledged=row.state in (S.ACKNOWLEDGED, S.CLOSED))
    return view


@router.get("/desk")
async def desk_index(request: Request, db: Session = Depends(get_db),
                     principal: security.Principal = Depends(_operator)):
    cards = [_card(db, r) for r in sos_service.desk_queue(db)]
    return request.app.state.templates.TemplateResponse(
        request=request, name="desk/index.html",
        context={"principal": principal, "active_page": "desk", "cards": cards})


@router.get("/desk/api/queue")
async def desk_queue_json(db: Session = Depends(get_db),
                          principal: security.Principal = Depends(_operator)) -> JSONResponse:
    cards = [_card(db, r) for r in sos_service.desk_queue(db)]
    return JSONResponse({"requests": cards,
                         "unacknowledged": sum(1 for c in cards if not c["acknowledged"])},
                        headers={"Cache-Control": "no-store"})


@router.get("/desk/requests/{request_id}/snapshot.jpg")
async def desk_snapshot(request_id: str, db: Session = Depends(get_db),
                        principal: security.Principal = Depends(_operator)) -> Response:
    row = _row(db, request_id)
    snapshot = db.query(SosSnapshot).filter_by(request_id=row.id).first()
    if snapshot is None or not snapshot.data:
        raise HTTPException(404, detail="not_found")
    return Response(snapshot.data, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@router.get("/desk/requests/{request_id}")
async def desk_request(request_id: str, request: Request, db: Session = Depends(get_db),
                       principal: security.Principal = Depends(_operator)):
    row = _row(db, request_id)
    household = db.get(Household, row.household_id) if row.household_id else None
    owner = (db.query(User).filter_by(household_id=row.household_id, role="owner").first()
             if row.household_id else None)
    device = db.get(Device, row.device_id) if row.device_id else None
    camera = db.query(Camera).filter_by(device_id=row.device_id).first() if device else None
    live = bool(device is not None and row.state in sos_service.DESK_STATES
                and sos_service.consent_active(db, row.household_id, "desk_live_view")
                and hub_for(request.app).online(device.id))
    medical = None
    if household is not None and household.include_medical and \
            sos_service.consent_active(db, household.id, "medical_notes"):
        medical = security.unseal(household.medical_enc)
    address = None
    if household is not None:
        address = ", ".join(p for p in (household.house, household.building, household.street,
                                        household.locality, household.landmark,
                                        household.city, household.district, household.state,
                                        household.pincode) if p)
    return request.app.state.templates.TemplateResponse(
        request=request, name="desk/request.html",
        context={"principal": principal, "active_page": "desk", "req": _card(db, row),
                 "events": sos_service.events_view(db, row.id, "desk"),
                 "household": household, "address": address, "medical": medical,
                 "owner_name": owner.display_name if owner else None,
                 "owner_phone": security.unseal(owner.phone_enc) if owner and owner.phone_enc
                 else None,
                 "room": camera.room_label if camera else None,
                 "map_url": (f"https://maps.google.com/?q={household.lat},{household.lon}"
                             if household is not None and household.lat is not None
                             and household.lon is not None else None),
                 "has_snapshot": db.query(SosSnapshot.id).filter_by(
                     request_id=row.id).first() is not None,
                 "live_url": f"/d/{device.id}/live" if live else None,
                 "outcomes": OUTCOMES, "call": call_112(request.app.state.settings),
                 "message": MESSAGES.get(request.query_params.get("m", ""))})


def _done(request: Request, row_id: str, outcome: dict):
    accept = request.headers.get("accept", "")
    if request.headers.get("x-requested-with") or (
            "application/json" in accept and "text/html" not in accept):
        return JSONResponse(outcome, status_code=outcome["status"])
    return RedirectResponse(f"/desk/requests/{row_id}?m={outcome['code']}", status_code=303)


@router.post("/desk/requests/{request_id}/acknowledge")
async def desk_acknowledge(request_id: str, request: Request, db: Session = Depends(get_db),
                           principal: security.Principal = Depends(_operator)):
    row = _row(db, request_id)
    outcome = await sos_service.respond(db, row.id, "acknowledge", actor=actor_of(principal),
                                        channel="desk",
                                        detail={"by": f"SENTRIX desk ({principal.display_name})"})
    return _done(request, row.id, outcome)


@router.post("/desk/requests/{request_id}/called-112")
async def desk_called_112(request_id: str, request: Request, db: Session = Depends(get_db),
                          principal: security.Principal = Depends(_operator)):
    row = _row(db, request_id)
    outcome = await sos_service.record_note(
        db, row.id, actor=actor_of(principal),
        detail={"kind": "desk_called_112", "simulated": True,
                "note": "Called 112 (simulated: SENTRIX does not contact 112)"})
    return _done(request, row.id, outcome)


@router.post("/desk/requests/{request_id}/resolve")
async def desk_resolve(request_id: str, request: Request, outcome: str = Form(...),
                       note: str | None = Form(None), db: Session = Depends(get_db),
                       principal: security.Principal = Depends(_operator)):
    row = _row(db, request_id)
    if outcome not in OUTCOMES:
        return _done(request, row.id, sos_service.result("invalid_args", "unknown outcome"))
    result = await sos_service.respond(db, row.id, "resolve", actor=actor_of(principal),
                                       channel="desk",
                                       detail={"outcome": outcome, "note": (note or "")[:500]})
    return _done(request, row.id, result)


__all__ = ["router", "OUTCOMES"]
