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

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from sentrix_portal import security
from sentrix_portal.db import DB
from sentrix_portal.services import sos_views as views

router = APIRouter()
CONTACT_ACTIONS = views.CONTACT_ACTIONS
MESSAGES = views.CONTACT_MESSAGES


def _expired(request: Request) -> Response:
    return request.app.state.templates.TemplateResponse(
        request=request, name="contacts/expired.html", status_code=404,
        context={"principal": None})


@router.get("/c/{token}/snapshot.jpg")
async def contact_snapshot(token: str, db: Session = DB) -> Response:
    found, row, _contact = views.contact_page(db, token)
    if found is None:
        raise HTTPException(404, detail="not_found")
    snapshot = views.snapshot_of(db, row)
    if snapshot is None:
        raise HTTPException(404, detail="not_found")
    return Response(snapshot.data, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.get("/c/{token}")
async def contact_page(token: str, request: Request, db: Session = DB):
    found, row, contact = views.contact_page(db, token)
    if found is None:
        return _expired(request)
    views.contact_page_opened(db, found, row, contact, ip=security.client_ip(request))
    holder = Response()
    csrf = security.issue_login_csrf(holder)
    context = {"principal": None, "token": token,
               **views.contact_view(db, row, contact, request.app.state.settings)}
    context.update(csrf=csrf, message=MESSAGES.get(request.query_params.get("m", "")))
    response = request.app.state.templates.TemplateResponse(
        request=request, name="contacts/page.html", context=context,
        headers={"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})
    for cookie in holder.headers.getlist("set-cookie"):
        response.headers.append("set-cookie", cookie)
    return response


@router.post("/c/{token}")
async def contact_action(token: str, request: Request, action: str = Form(...),
                         csrf_token: str | None = Form(None), db: Session = DB):
    from fastapi.responses import RedirectResponse
    if not security.verify_login_csrf(request, csrf_token):
        raise HTTPException(403, detail="csrf")
    found, row, contact = views.contact_page(db, token)
    if found is None:
        return _expired(request)
    if action not in CONTACT_ACTIONS:
        raise HTTPException(400, detail="invalid_args")
    outcome = await views.contact_act(db, row, contact, action,
                                      ip=security.client_ip(request))
    return RedirectResponse(f"/c/{token}?m={outcome['code']}", status_code=303)


__all__ = ["router", "CONTACT_ACTIONS"]
