"""Emergency contacts' token pages (API v2, no account): the consent page
``/consent/<token>`` (confirm or opt out; a confirmation returns the
personal opt-out link and the Telegram deep link) and the per-request SOS
page ``/contact-page/<token>`` (what happened, where, the snapshot, "I'm
handling it" and "Alert the SENTRIX desk"). Opening a page changes nothing
but its counter and the audit; answers carry the double-submit token the
page returned (``X-CSRF-Token``).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from sentrix_portal import security
from sentrix_portal.db import DB
from sentrix_portal.routes.api_v2.common import (ApiError, check_login_csrf, ip_of,
                                                 raise_for, raise_for_sos)
from sentrix_portal.routes.api_v2.onboarding import Purpose, purposes_out
from sentrix_portal.routes.api_v2.schemas import CallTarget, StaffRequest, staff_request
from sentrix_portal.routes.api_v2.sos import JPEG
from sentrix_portal.services import accounts
from sentrix_portal.services import household_flows as flows
from sentrix_portal.services import sos_views as views

router = APIRouter(tags=["contacts"])

PAGE_CLOSED = ("SOS pages for emergency contacts work only while the request is open, plus two "
               "hours, and close when the resident marks the alarm as false. If you are "
               "worried, call the resident, or 112 in an emergency.")


# ── consent (/consent/<token>) ────────────────────────────────────────────────
class ConsentPageOut(BaseModel):
    contact_name: str
    relation: str | None
    #: PENDING (asked to confirm) or CONFIRMED (the link is the opt-out link).
    status: str
    owner_name: str
    purposes: list[Purpose]
    notice_version: str
    declined_days: int
    csrf_token: str


class ConsentIn(BaseModel):
    decision: Literal["confirm", "decline"] | None = None


class ConsentDoneOut(BaseModel):
    decision: Literal["confirm", "decline"]
    #: One use, 24 h: get SOS alerts on Telegram (null without a bot).
    telegram_link: str | None
    #: The contact's personal opt-out link (a year), shown once.
    opt_out_link: str | None


def _consent(db: Session, token: str):
    found = accounts.consent_page_target(db, token)
    if found is None:
        raise ApiError("not_found", flows.CONSENT_LINK_NOT_VALID)
    return found


@router.get("/consent/{token}", response_model=ConsentPageOut)
def consent_page(token: str, request: Request, response: Response,
                 db: Session = DB) -> ConsentPageOut:
    row, contact = _consent(db, token)
    flows.consent_opened(row)
    view = flows.consent_view(db, contact)
    csrf = security.login_csrf_token(request)
    security.set_login_csrf(response, csrf)
    return ConsentPageOut(contact_name=contact.name, relation=contact.relation,
                          status=contact.consent_status, owner_name=view["owner_name"],
                          purposes=purposes_out(), notice_version=view["notice_version"],
                          declined_days=view["declined_days"], csrf_token=csrf)


@router.post("/consent/{token}", response_model=ConsentDoneOut)
async def consent_answer(token: str, body: ConsentIn, request: Request,
                         db: Session = DB) -> ConsentDoneOut:
    """Only a CONFIRMED contact ever receives an SOS; opting out erases the
    contact's number and e-mail at once."""
    check_login_csrf(request, "Open the link again and answer.")
    row, contact = _consent(db, token)
    outcome = await flows.decide_consent(db, row, contact, body.decision, ip=ip_of(request))
    raise_for(outcome, db)
    return ConsentDoneOut(**outcome.data)


# ── the contact's SOS page (/contact-page/<token>) ────────────────────────────
class ContactPageOut(BaseModel):
    contact_name: str
    resident: str
    #: The resident's callback number; null under duress ("Do not call the
    #: resident first").
    resident_phone: str | None
    request: StaffRequest
    address: str | None
    landmark: str | None
    entry_note: str | None
    map_url: str | None
    #: Only with the household's consent.
    medical: str | None
    has_snapshot: bool
    snapshot_url: str | None
    can_acknowledge: bool
    can_approve: bool
    call: CallTarget
    footer: str
    csrf_token: str


class ContactActionIn(BaseModel):
    action: Literal["acknowledge", "approve"]


class ContactActionOut(BaseModel):
    ok: bool = True
    message: str
    request: StaffRequest


def _page(db: Session, request: Request, token: str):
    found, row, contact = views.contact_page(db, token)
    if found is None:
        raise ApiError("not_found", PAGE_CLOSED)
    return found, row, contact


@router.get("/contact-page/{token}", response_model=ContactPageOut)
def contact_page(token: str, request: Request, response: Response,
                 db: Session = DB) -> ContactPageOut:
    found, row, contact = _page(db, request, token)
    views.contact_page_opened(db, found, row, contact, ip=ip_of(request))
    view = views.contact_view(db, row, contact, request.app.state.settings)
    csrf = security.login_csrf_token(request)
    security.set_login_csrf(response, csrf)
    response.headers["Referrer-Policy"] = "no-referrer"
    duress = bool((row.flags or {}).get("duress"))
    return ContactPageOut(
        contact_name=contact.name, resident=view["resident"],
        resident_phone=None if duress else view["resident_phone"],
        request=staff_request(row, "contact"), address=view["address"],
        landmark=view["landmark"], entry_note=view["entry_note"], map_url=view["map_url"],
        medical=view["medical"], has_snapshot=view["has_snapshot"],
        snapshot_url=(f"/api/v2/contact-page/{token}/snapshot.jpg" if view["has_snapshot"]
                      else None),
        can_acknowledge=view["can_acknowledge"], can_approve=view["can_approve"],
        call=CallTarget(**view["call"]),
        footer=("This page works only while the request is open (plus two hours). "
                "SENTRIX does not contact 112 itself."),
        csrf_token=csrf)


@router.get("/contact-page/{token}/snapshot.jpg", response_class=Response, responses=JPEG)
def contact_snapshot(token: str, request: Request, db: Session = DB) -> Response:
    _, row, _ = _page(db, request, token)
    snapshot = views.snapshot_of(db, row)
    if snapshot is None:
        raise ApiError("not_found", "There is no snapshot for this request.")
    return Response(snapshot.data, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.post("/contact-page/{token}", response_model=ContactActionOut)
async def contact_action(token: str, body: ContactActionIn, request: Request,
                         db: Session = DB) -> ContactActionOut:
    """"I'm handling it" while help is on its way; "Alert the SENTRIX desk"
    while the resident has not answered (only when the request's policy lets
    contacts ask for help)."""
    check_login_csrf(request, "The page expired. Open the link again.")
    _, row, contact = _page(db, request, token)
    result = await views.contact_act(db, row, contact, body.action, ip=ip_of(request))
    raise_for_sos(result, views.CONTACT_MESSAGES, db)
    db.refresh(row)
    return ContactActionOut(message=views.CONTACT_MESSAGES["ok"],
                            request=staff_request(row, "contact"))


__all__ = ["router"]
