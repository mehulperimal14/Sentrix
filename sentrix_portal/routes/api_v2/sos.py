"""SOS for owners and members (API v2): the household's requests, one
request with its countdown and timeline (opening it marks the prompt seen),
the answers (approve with Police/Fire/Medical, I'm checking, false alarm or
"I'm safe" with the SOS PIN, Request help now), the consented snapshot; and
the e-mail/SMS action links ``/action/<token>`` (approve or I'm checking,
once, no sign-in).

Duress parity (docs/7 §7.8): after a duress PIN the owner's JSON reads
exactly like a normal false alarm (``schemas.owner_request`` /
``owner_events``), and every answer says "Done.". In test mode the call
target is the simulated Desk, never ``tel:112`` (D13).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from sentrix.sos import states as S
from sentrix_portal import security
from sentrix_portal.db import DB
from sentrix_portal.models import AssistanceRequest
from sentrix_portal.routes.api_v2.common import (ERROR_RESPONSES, HOUSEHOLD, ApiError,
                                                 check_login_csrf, ip_of, raise_for_sos)
from sentrix_portal.routes.api_v2.schemas import (AuthorityOption, CallTarget, OwnerRequest,
                                                  TimelineEvent, _times, owner_events,
                                                  owner_request)
from sentrix_portal.services import sos as sos_service
from sentrix_portal.services import sos_views as views

router = APIRouter(tags=["sos"])
Authority = Literal["POLICE", "FIRE", "MEDICAL"]
JPEG = {200: {"content": {"image/jpeg": {}}, "description": "The SOS snapshot"},
        **ERROR_RESPONSES}


class SosListOut(BaseModel):
    open: list[OwnerRequest]
    recent: list[OwnerRequest]
    next_cursor: str | None


class OpenRequestsOut(BaseModel):
    requests: list[OwnerRequest]


class SosDetailOut(BaseModel):
    request: OwnerRequest
    events: list[TimelineEvent]
    has_snapshot: bool
    snapshot_url: str | None
    call: CallTarget
    authorities: list[AuthorityOption]
    default_authority: Authority
    footer: str


class AnswerIn(BaseModel):
    action: Literal["approve", "checking", "decline", "safe", "help_now"]
    #: Who is needed (approve, help_now).
    authority: Authority | None = None
    #: The SOS PIN (decline, safe).
    pin: str | None = None


class AnswerOut(BaseModel):
    ok: bool = True
    message: str
    #: The request to show now ("Request help now" opens a new one).
    request_id: str
    request: OwnerRequest | None
    events: list[TimelineEvent]


def authorities_out() -> list[AuthorityOption]:
    return [AuthorityOption(value=k, label=v) for k, v in views.AUTHORITY_LABELS.items()]


def _row(db: Session, principal, request_id: str) -> AssistanceRequest:
    row = views.household_request(db, principal, request_id)
    if row is None:
        raise ApiError("not_found", "This request was not found.")
    return row


@router.get("/sos", response_model=SosListOut)
def sos_list(limit: int = Query(30, ge=1, le=100), cursor: str | None = None,
             db: Session = DB, principal=Depends(HOUSEHOLD)) -> SosListOut:
    """The household's requests, newest first, split into open and recent."""
    if not principal.household_id:
        return SosListOut(open=[], recent=[], next_cursor=None)
    offset = int(cursor) if cursor and cursor.isdigit() else 0
    rows = views.household_requests(db, principal.household_id, limit=limit + 1,
                                    offset=offset)
    more = len(rows) > limit
    shown = [owner_request(r) for r in rows[:limit]]
    return SosListOut(open=[r for r in shown if r.state in S.OPEN_STATES],
                      recent=[r for r in shown if r.state not in S.OPEN_STATES],
                      next_cursor=str(offset + limit) if more else None)


@router.get("/sos/open", response_model=OpenRequestsOut)
def sos_open(db: Session = DB, principal=Depends(HOUSEHOLD)) -> OpenRequestsOut:
    """For the "Do you need assistance?" banner on every page."""
    if not principal.household_id:
        return OpenRequestsOut(requests=[])
    return OpenRequestsOut(requests=[OwnerRequest(**_times(v)) for v in
                                     sos_service.open_requests(db, principal.household_id)])


def _detail(db: Session, request: Request, row: AssistanceRequest) -> SosDetailOut:
    snapshot = views.has_snapshot(db, row)
    return SosDetailOut(request=owner_request(row), events=owner_events(db, row),
                        has_snapshot=snapshot,
                        snapshot_url=f"/api/v2/sos/{row.id}/snapshot.jpg" if snapshot else None,
                        call=CallTarget(**views.call_112(request.app.state.settings)),
                        authorities=authorities_out(),
                        default_authority=views.default_authority(row),
                        footer=sos_service.FOOTER)


@router.get("/sos/{request_id}", response_model=SosDetailOut)
async def sos_detail(request_id: str, request: Request, db: Session = DB,
                     principal=Depends(HOUSEHOLD)) -> SosDetailOut:
    """One request (opening it marks the prompt seen, as the page did)."""
    row = _row(db, principal, request_id)
    await sos_service.mark_seen(db, row.id, views.actor_of(principal))
    db.refresh(row)
    return _detail(db, request, row)


@router.get("/sos/{request_id}/snapshot.jpg", response_class=Response, responses=JPEG)
def sos_snapshot(request_id: str, db: Session = DB, principal=Depends(HOUSEHOLD)) -> Response:
    row = _row(db, principal, request_id)
    snapshot = views.snapshot_of(db, row)
    if snapshot is None:
        raise ApiError("not_found", "There is no snapshot for this request.")
    return Response(snapshot.data, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@router.post("/sos/{request_id}/answer", response_model=AnswerOut)
async def sos_answer(request_id: str, body: AnswerIn, db: Session = DB,
                     principal=Depends(HOUSEHOLD)) -> AnswerOut:
    """Online the device decides; offline the Portal does (and syncs later)."""
    row = _row(db, principal, request_id)
    result = await views.answer(db, principal, row, body.action, authority=body.authority,
                                pin=body.pin)
    raise_for_sos(result, views.OWNER_MESSAGES, db, field="authority")
    target = row.id
    if body.action == "help_now":
        target = (result.get("request") or {}).get("id") or row.id
    db.expire_all()
    shown = db.get(AssistanceRequest, target)
    if shown is None or shown.household_id != principal.household_id:
        shown, target = db.get(AssistanceRequest, row.id), row.id
    return AnswerOut(message=views.OWNER_MESSAGES["ok"], request_id=target,
                     request=owner_request(shown), events=owner_events(db, shown))


# ── action links (/a/<token>) ─────────────────────────────────────────────────
class ActionLinkOut(BaseModel):
    name: str | None
    request: OwnerRequest
    #: Only while the household is still being asked.
    can_act: bool
    call: CallTarget
    authorities: list[AuthorityOption]
    default_authority: Authority
    #: Send back as ``X-CSRF-Token`` (also set as the ``portal_csrf`` cookie).
    csrf_token: str


class LinkAnswerIn(BaseModel):
    action: Literal["approve", "checking"]
    authority: Authority | None = None


class LinkAnswerOut(BaseModel):
    ok: bool = True
    message: str
    request: OwnerRequest


LINK_EXPIRED = ("Links in SENTRIX messages work once, and only while their request is open "
                "(plus two hours). Sign in to the SENTRIX Portal to see the request.")


@router.get("/action/{token}", response_model=ActionLinkOut)
def action_link(token: str, request: Request, response: Response,
                db: Session = DB) -> ActionLinkOut:
    """An e-mail/SMS action link: opening it changes nothing (link scanners
    open every URL); it is counted and audited."""
    found, row, user = views.action_link(db, token)
    if found is None:
        raise ApiError("not_found", LINK_EXPIRED)
    views.action_link_opened(db, found, row, user, ip=ip_of(request))
    csrf = security.login_csrf_token(request)
    security.set_login_csrf(response, csrf)
    shown = owner_request(row)
    return ActionLinkOut(name=user.display_name, request=shown,
                         can_act=shown.state in S.PROMPTING_STATES,
                         call=CallTarget(**views.call_112(request.app.state.settings)),
                         authorities=authorities_out(),
                         default_authority=views.default_authority(row), csrf_token=csrf)


@router.post("/action/{token}", response_model=LinkAnswerOut)
async def action_link_answer(token: str, body: LinkAnswerIn, request: Request,
                             db: Session = DB) -> LinkAnswerOut:
    """The link's one answer: ask for help or "I'm checking" (never a false
    alarm: that needs the PIN after signing in)."""
    check_login_csrf(request, "The page expired. Open the link again.")
    found, row, user = views.action_link(db, token)
    if found is None:
        raise ApiError("not_found", LINK_EXPIRED)
    result = await views.use_action_link(db, found, row, user, body.action,
                                         authority=body.authority, ip=ip_of(request))
    raise_for_sos(result, views.OWNER_MESSAGES, db, field="authority")
    db.refresh(row)
    return LinkAnswerOut(message=views.OWNER_MESSAGES["ok"], request=owner_request(row))


__all__ = ["router", "authorities_out", "LINK_EXPIRED"]
