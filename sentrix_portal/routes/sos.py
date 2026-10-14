"""Portal SOS: /sos and /sos/{request_id} (prompt, countdown, approve, decline
with PIN, I'm checking, Request help now), /a/{token} confirmation pages for
e-mail/SMS action links (POST + CSRF), cloud authority timers when the device
is offline.

* ``GET /sos``: the household's open and recent requests (owner view: a
  duress approval reads "Marked as false alarm").
* ``GET /sos/{id}``: one request with its countdown, snapshot (consented, the
  only one the cloud keeps), the buttons the state allows and the timeline
  (restricted rows hidden); opening it marks the prompt seen.
* ``POST /sos/{id}/{action}``: approve (with Police / Fire / Medical), decline
  or "I'm safe" (SOS PIN), I'm checking, Request help now. Online the device
  decides (``sos.respond``); offline the cloud does (``services.sos.respond``).
* ``GET /sos/api/open`` and ``GET /sos/api/requests/{id}``: JSON for the
  page's polling and the global "Do you need assistance?" banner
  (``/static/sos/sos.js``, which any Portal page may include).
* ``GET/POST /a/{token}``: an e-mail action link opens a confirmation page;
  only the POST (CSRF double-submit) acts, once. Link scanners that fetch
  every URL therefore approve nothing. "False alarm" is never offered there:
  it needs the PIN on the signed-in page.

Owner: track T-LINK.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from sentrix.actor import Actor
from sentrix.sos import states as S
from sentrix_portal import security
from sentrix_portal.db import DB
from sentrix_portal.models import AssistanceRequest, SosSnapshot, User, utcnow
from sentrix_portal.routes.device_proxy import actor_of
from sentrix_portal.services import sos as sos_service
from sentrix_portal.services.hub import UUID_RE

router = APIRouter()
_members = security.require_role("owner", "member")
#: Actions a signed-in owner or member may post here.
PAGE_ACTIONS = ("approve", "checking", "decline", "safe", "help_now")
#: Actions an e-mail action link may take (never a decline: that needs the PIN).
LINK_ACTIONS = ("approve", "checking")
AUTHORITY_LABELS = {"POLICE": "Police", "FIRE": "Fire", "MEDICAL": "Medical"}
MESSAGES = {
    "ok": "Done.", "pin_invalid": "That PIN is not right.",
    "pin_locked": "Too many wrong PINs. Try again in 10 minutes; asking for help still works.",
    "conflict": "This request has moved on; the page shows where it is now.",
    "device_offline": "The device is offline; the SENTRIX Portal handled it.",
    "timeout": "The device did not answer in time. Try again.",
    "forbidden_role": "You cannot do that here.",
}


def _cfg():
    from sentrix.config import settings
    return settings


def call_112(settings) -> dict:
    """The "Call 112 now" target: never a tel:112 link in test mode (D13)."""
    if settings.test_mode:
        number = settings.desk_number
        return {"href": f"tel:{number}" if number else None,
                "label": f"Call the SENTRIX Desk ({number})" if number
                else "Call 112 now (test mode: no number set)", "test_mode": True}
    return {"href": "tel:112", "label": "Call 112 now", "test_mode": False}


def _request_for(db: Session, principal: security.Principal,
                 request_id: str) -> AssistanceRequest:
    if not UUID_RE.match(request_id or ""):
        raise HTTPException(404, detail="not_found")
    row = db.get(AssistanceRequest, request_id)
    if row is None or not principal.household_id or row.household_id != principal.household_id:
        raise HTTPException(404, detail="not_found")
    return row


def _wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return bool(request.headers.get("x-requested-with")) or (
        "application/json" in accept and "text/html" not in accept)


# ── owner pages ───────────────────────────────────────────────────────────────
@router.get("/sos")
async def sos_index(request: Request, db: Session = DB,
                    principal: security.Principal = Depends(_members)):
    rows = (db.query(AssistanceRequest)
            .filter(AssistanceRequest.household_id == principal.household_id)
            .order_by(AssistanceRequest.created_at.desc()).limit(30).all())
    views = [sos_service.request_view(r, "owner") for r in rows]
    return request.app.state.templates.TemplateResponse(
        request=request, name="sos/index.html",
        context={"principal": principal, "active_page": "sos",
                 "open": [v for v in views if v["state"] in S.OPEN_STATES],
                 "recent": [v for v in views if v["state"] not in S.OPEN_STATES]})


@router.get("/sos/api/open")
async def sos_open(db: Session = DB,
                   principal: security.Principal = Depends(_members)) -> JSONResponse:
    if not principal.household_id:
        return JSONResponse({"requests": []})
    return JSONResponse({"requests": sos_service.open_requests(db, principal.household_id)},
                        headers={"Cache-Control": "no-store"})


@router.get("/sos/api/requests/{request_id}")
async def sos_request_json(request_id: str, db: Session = DB,
                           principal: security.Principal = Depends(_members)) -> JSONResponse:
    row = _request_for(db, principal, request_id)
    return JSONResponse({"request": sos_service.request_view(row, "owner"),
                         "events": sos_service.events_view(db, row.id, "owner")},
                        headers={"Cache-Control": "no-store"})


@router.get("/sos/{request_id}/snapshot.jpg")
async def sos_snapshot(request_id: str, db: Session = DB,
                       principal: security.Principal = Depends(_members)) -> Response:
    row = _request_for(db, principal, request_id)
    snapshot = db.query(SosSnapshot).filter_by(request_id=row.id).first()
    if snapshot is None or not snapshot.data:
        raise HTTPException(404, detail="not_found")
    return Response(snapshot.data, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@router.get("/sos/{request_id}")
async def sos_request_page(request_id: str, request: Request, db: Session = DB,
                           principal: security.Principal = Depends(_members)):
    row = _request_for(db, principal, request_id)
    await sos_service.mark_seen(db, row.id, actor_of(principal))
    view = sos_service.request_view(row, "owner")
    settings = request.app.state.settings
    has_snapshot = db.query(SosSnapshot.id).filter_by(request_id=row.id).first() is not None
    return request.app.state.templates.TemplateResponse(
        request=request, name="sos/request.html",
        context={"principal": principal, "active_page": "sos", "req": view,
                 "events": sos_service.events_view(db, row.id, "owner"),
                 "has_snapshot": has_snapshot, "call": call_112(settings),
                 "authorities": AUTHORITY_LABELS,
                 "default_authority": "FIRE" if row.incident_type == "fire" else "POLICE",
                 "message": MESSAGES.get(request.query_params.get("m", ""))})


@router.post("/sos/{request_id}/{action}")
async def sos_action(request_id: str, action: str, request: Request,
                     authority: str | None = Form(None), pin: str | None = Form(None),
                     db: Session = DB,
                     principal: security.Principal = Depends(_members)):
    if action not in PAGE_ACTIONS:
        raise HTTPException(404, detail="not_found")
    row = _request_for(db, principal, request_id)
    outcome = await sos_service.respond(db, row.id, action, actor=actor_of(principal),
                                        channel="portal", authority=authority,
                                        pin=pin or None)
    target = (outcome.get("request") or {}).get("id") if action == "help_now" else row.id
    if _wants_json(request):
        return JSONResponse(outcome, status_code=outcome["status"])
    return RedirectResponse(f"/sos/{target or row.id}?m={outcome['code']}", status_code=303)


# ── action links (/a/) ────────────────────────────────────────────────────────
def _link(db: Session, token: str):
    found = sos_service.resolve_token(db, token, ("action_link",))
    if found is None or found.used_at is not None:
        return None, None, None
    row = db.get(AssistanceRequest, found.request_id) if found.request_id else None
    user = db.get(User, found.subject_id) if found.subject_id else None
    if row is None or user is None or user.status != "active":
        return None, None, None
    return found, row, user


@router.get("/a/{token}")
async def action_link_page(token: str, request: Request, db: Session = DB):
    found, row, user = _link(db, token)
    templates = request.app.state.templates
    if found is None:
        return templates.TemplateResponse(
            request=request, name="sos/link_expired.html", status_code=404,
            context={"principal": None})
    found.opened_count = (found.opened_count or 0) + 1
    security.audit(db, "action_link.opened", actor_type="user", actor_id=user.id,
                   household_id=row.household_id, target_type="request", target_id=row.id,
                   ip=security.client_ip(request))
    view = sos_service.request_view(row, "owner")
    holder = Response()
    csrf = security.issue_login_csrf(holder)           # double-submit: no session here
    response = templates.TemplateResponse(
        request=request, name="sos/action.html",
        context={"principal": None, "req": view, "token": token, "name": user.display_name,
                 "csrf": csrf, "call": call_112(request.app.state.settings),
                 "authorities": AUTHORITY_LABELS,
                 "default_authority": "FIRE" if row.incident_type == "fire" else "POLICE",
                 "can_act": view["state"] in S.PROMPTING_STATES})
    for cookie in holder.headers.getlist("set-cookie"):
        response.headers.append("set-cookie", cookie)
    return response


@router.post("/a/{token}")
async def action_link_post(token: str, request: Request, action: str = Form(...),
                           authority: str | None = Form(None),
                           csrf_token: str | None = Form(None),
                           db: Session = DB):
    if not security.verify_login_csrf(request, csrf_token):
        raise HTTPException(403, detail="csrf")
    found, row, user = _link(db, token)
    templates = request.app.state.templates
    if found is None:
        return templates.TemplateResponse(request=request, name="sos/link_expired.html",
                                          status_code=404, context={"principal": None})
    if action not in LINK_ACTIONS:
        raise HTTPException(400, detail="invalid_args")
    found.used_at = utcnow()
    actor = Actor(type=user.role if user.role in ("owner", "member") else "owner",
                  id=user.id, name=user.display_name or user.username).to_dict()
    outcome = await sos_service.respond(db, row.id, action, actor=actor, channel="email",
                                        authority=authority)
    security.audit(db, "action_link.used", actor_type="user", actor_id=user.id,
                   household_id=row.household_id, target_type="request", target_id=row.id,
                   ip=security.client_ip(request),
                   detail={"action": action, "code": outcome["code"]})
    return templates.TemplateResponse(
        request=request, name="sos/done.html", status_code=200 if outcome["ok"] else 409,
        context={"principal": None, "outcome": outcome,
                 "message": MESSAGES.get(outcome["code"], outcome.get("message") or "")})


__all__ = ["router", "call_112", "PAGE_ACTIONS", "LINK_ACTIONS"]
