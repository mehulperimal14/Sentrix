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

Owner: track T-LINK. What the pages show and do lives in
``services.sos_views`` (shared with ``/api/v2``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from sentrix.sos import states as S
from sentrix_portal import security
from sentrix_portal.db import DB
from sentrix_portal.models import AssistanceRequest
from sentrix_portal.services import sos as sos_service
from sentrix_portal.services import sos_views as views

router = APIRouter()
_members = security.require_role("owner", "member")
#: Actions a signed-in owner or member may post here.
PAGE_ACTIONS = views.PAGE_ACTIONS
#: Actions an e-mail action link may take (never a decline: that needs the PIN).
LINK_ACTIONS = views.LINK_ACTIONS
AUTHORITY_LABELS = views.AUTHORITY_LABELS
MESSAGES = views.OWNER_MESSAGES
call_112 = views.call_112


def _request_for(db: Session, principal: security.Principal,
                 request_id: str) -> AssistanceRequest:
    row = views.household_request(db, principal, request_id)
    if row is None:
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
    rows = views.household_requests(db, principal.household_id)
    page = [sos_service.request_view(r, "owner") for r in rows]
    return request.app.state.templates.TemplateResponse(
        request=request, name="sos/index.html",
        context={"principal": principal, "active_page": "sos",
                 "open": [v for v in page if v["state"] in S.OPEN_STATES],
                 "recent": [v for v in page if v["state"] not in S.OPEN_STATES]})


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
    snapshot = views.snapshot_of(db, row)
    if snapshot is None:
        raise HTTPException(404, detail="not_found")
    return Response(snapshot.data, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@router.get("/sos/{request_id}")
async def sos_request_page(request_id: str, request: Request, db: Session = DB,
                           principal: security.Principal = Depends(_members)):
    row = _request_for(db, principal, request_id)
    await sos_service.mark_seen(db, row.id, views.actor_of(principal))
    view = sos_service.request_view(row, "owner")
    settings = request.app.state.settings
    return request.app.state.templates.TemplateResponse(
        request=request, name="sos/request.html",
        context={"principal": principal, "active_page": "sos", "req": view,
                 "events": sos_service.events_view(db, row.id, "owner"),
                 "has_snapshot": views.has_snapshot(db, row), "call": call_112(settings),
                 "authorities": AUTHORITY_LABELS,
                 "default_authority": views.default_authority(row),
                 "message": MESSAGES.get(request.query_params.get("m", ""))})


@router.post("/sos/{request_id}/{action}")
async def sos_action(request_id: str, action: str, request: Request,
                     authority: str | None = Form(None), pin: str | None = Form(None),
                     db: Session = DB,
                     principal: security.Principal = Depends(_members)):
    if action not in PAGE_ACTIONS:
        raise HTTPException(404, detail="not_found")
    row = _request_for(db, principal, request_id)
    outcome = await views.answer(db, principal, row, action, authority=authority, pin=pin)
    target = (outcome.get("request") or {}).get("id") if action == "help_now" else row.id
    if _wants_json(request):
        return JSONResponse(outcome, status_code=outcome["status"])
    return RedirectResponse(f"/sos/{target or row.id}?m={outcome['code']}", status_code=303)


# ── action links (/a/) ────────────────────────────────────────────────────────
@router.get("/a/{token}")
async def action_link_page(token: str, request: Request, db: Session = DB):
    found, row, user = views.action_link(db, token)
    templates = request.app.state.templates
    if found is None:
        return templates.TemplateResponse(
            request=request, name="sos/link_expired.html", status_code=404,
            context={"principal": None})
    views.action_link_opened(db, found, row, user, ip=security.client_ip(request))
    view = sos_service.request_view(row, "owner")
    holder = Response()
    csrf = security.issue_login_csrf(holder)           # double-submit: no session here
    response = templates.TemplateResponse(
        request=request, name="sos/action.html",
        context={"principal": None, "req": view, "token": token, "name": user.display_name,
                 "csrf": csrf, "call": call_112(request.app.state.settings),
                 "authorities": AUTHORITY_LABELS,
                 "default_authority": views.default_authority(row),
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
    found, row, user = views.action_link(db, token)
    templates = request.app.state.templates
    if found is None:
        return templates.TemplateResponse(request=request, name="sos/link_expired.html",
                                          status_code=404, context={"principal": None})
    if action not in LINK_ACTIONS:
        raise HTTPException(400, detail="invalid_args")
    outcome = await views.use_action_link(db, found, row, user, action, authority=authority,
                                          ip=security.client_ip(request))
    return templates.TemplateResponse(
        request=request, name="sos/done.html", status_code=200 if outcome["ok"] else 409,
        context={"principal": None, "outcome": outcome,
                 "message": MESSAGES.get(outcome["code"], outcome.get("message") or "")})


__all__ = ["router", "call_112", "PAGE_ACTIONS", "LINK_ACTIONS"]
