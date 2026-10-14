"""Contact token pages /c/{token}: consent confirm/opt-out, SOS view with I'm
handling it / Call resident / Call 112 (Desk number in test mode). Per contact
and request; expire at close + 2 h; every open logged.

These pages are **SOS only** (consent confirmation is T-ACCT's
``/consent/{token}``). A contact has no login: the token in the link (purpose
``contact_page``, bound to one request and one CONFIRMED contact, stored as a
digest) is the credential. It expires when the request closes + 2 h and is
revoked when the resident declines. Every open is counted and audited
(``contact_page.opened``). Actions happen only on a POST with the
double-submit CSRF token:

* "I'm handling it" (``acknowledge``) while help is on its way;
* "Alert the SENTRIX desk" (``approve``) while the resident has not answered,
  only when the request's frozen policy has safety defaults on and lists this
  contact as CONFIRMED (the device checks the same rule).

The snapshot is shown only here (``/c/{token}/snapshot.jpg``, consented, never
a standalone link); medical notes only with the household's consent. Under
duress the page leads with "Do not go to the home. Call 112." and hides
"Call resident".

Owner: track T-LINK.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from sentrix.actor import Actor
from sentrix.sos import states as S
from sentrix_portal import security
from sentrix_portal.db import get_db
from sentrix_portal.models import (AssistanceRequest, EmergencyContact, Household,
                                   SosSnapshot, User)
from sentrix_portal.routes.sos import call_112
from sentrix_portal.services import sos as sos_service

router = APIRouter()
CONTACT_ACTIONS = ("acknowledge", "approve")
MESSAGES = {"ok": "Thank you. The resident and the SENTRIX desk can see it.",
            "forbidden_role": "This request does not let contacts ask for help.",
            "conflict": "The request has moved on.",
            "timeout": "The device did not answer in time. Try again."}


def _page(db: Session, token: str):
    found = sos_service.resolve_token(db, token, ("contact_page",))
    if found is None:
        return None, None, None
    row = db.get(AssistanceRequest, found.request_id) if found.request_id else None
    contact = db.get(EmergencyContact, found.subject_id) if found.subject_id else None
    if row is None or contact is None or contact.consent_status != "CONFIRMED":
        return None, None, None
    return found, row, contact


def _expired(request: Request) -> Response:
    return request.app.state.templates.TemplateResponse(
        request=request, name="contacts/expired.html", status_code=404,
        context={"principal": None})


def _context(db: Session, request: Request, row: AssistanceRequest,
             contact: EmergencyContact, token: str) -> dict:
    view = sos_service.request_view(row, "contact")
    household = db.get(Household, row.household_id) if row.household_id else None
    owner = (db.query(User).filter_by(household_id=row.household_id, role="owner").first()
             if row.household_id else None)
    medical = None
    if household is not None and household.include_medical and \
            sos_service.consent_active(db, household.id, "medical_notes"):
        medical = security.unseal(household.medical_enc)
    address = None
    if household is not None:
        address = ", ".join(p for p in (household.house, household.building, household.street,
                                        household.locality, household.landmark,
                                        household.city, household.state, household.pincode)
                            if p)
    can_approve = (view["state"] in S.PROMPTING_STATES
                   and sos_service._contact_allowed(db, row, Actor("contact", id=contact.id)))
    return {"principal": None, "req": view, "token": token, "contact": contact,
            "resident": owner.display_name if owner else "the resident",
            "resident_phone": (security.unseal(owner.phone_enc)
                               if owner is not None and owner.phone_enc else None),
            "address": address, "landmark": household.landmark if household else None,
            "entry_note": household.entry_note if household else None,
            "map_url": (f"https://maps.google.com/?q={household.lat},{household.lon}"
                        if household is not None and household.lat is not None
                        and household.lon is not None else None),
            "medical": medical,
            "has_snapshot": db.query(SosSnapshot.id).filter_by(request_id=row.id).first()
            is not None,
            "can_acknowledge": view["state"] in (S.APPROVED, S.DISPATCHED, S.FAILED),
            "can_approve": can_approve, "call": call_112(request.app.state.settings)}


@router.get("/c/{token}/snapshot.jpg")
async def contact_snapshot(token: str, db: Session = Depends(get_db)) -> Response:
    found, row, _contact = _page(db, token)
    if found is None:
        raise HTTPException(404, detail="not_found")
    snapshot = db.query(SosSnapshot).filter_by(request_id=row.id).first()
    if snapshot is None or not snapshot.data:
        raise HTTPException(404, detail="not_found")
    return Response(snapshot.data, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.get("/c/{token}")
async def contact_page(token: str, request: Request, db: Session = Depends(get_db)):
    found, row, contact = _page(db, token)
    if found is None:
        return _expired(request)
    found.opened_count = (found.opened_count or 0) + 1
    security.audit(db, "contact_page.opened", actor_type="contact", actor_id=contact.id,
                   household_id=row.household_id, target_type="request", target_id=row.id,
                   ip=security.client_ip(request),
                   detail={"opened_count": found.opened_count})
    holder = Response()
    csrf = security.issue_login_csrf(holder)
    context = _context(db, request, row, contact, token)
    context.update(csrf=csrf, message=MESSAGES.get(request.query_params.get("m", "")))
    response = request.app.state.templates.TemplateResponse(
        request=request, name="contacts/page.html", context=context,
        headers={"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})
    for cookie in holder.headers.getlist("set-cookie"):
        response.headers.append("set-cookie", cookie)
    return response


@router.post("/c/{token}")
async def contact_action(token: str, request: Request, action: str = Form(...),
                         csrf_token: str | None = Form(None), db: Session = Depends(get_db)):
    from fastapi.responses import RedirectResponse
    if not security.verify_login_csrf(request, csrf_token):
        raise HTTPException(403, detail="csrf")
    found, row, contact = _page(db, token)
    if found is None:
        return _expired(request)
    if action not in CONTACT_ACTIONS:
        raise HTTPException(400, detail="invalid_args")
    actor = Actor("contact", id=contact.id, name=contact.name).to_dict()
    outcome = await sos_service.respond(
        db, row.id, action, actor=actor, channel="contact_page",
        detail={"by": f"{contact.name} (emergency contact)"} if action == "acknowledge" else None)
    security.audit(db, f"contact_page.{action}", actor_type="contact", actor_id=contact.id,
                   household_id=row.household_id, target_type="request", target_id=row.id,
                   ip=security.client_ip(request), detail={"code": outcome["code"]})
    return RedirectResponse(f"/c/{token}?m={outcome['code']}", status_code=303)


__all__ = ["router", "CONTACT_ACTIONS"]
