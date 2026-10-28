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

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from sentrix_portal import security
from sentrix_portal.db import DB
from sentrix_portal.models import AssistanceRequest
from sentrix_portal.services import sos_views as views
from sentrix_portal.services.hub import hub_for

router = APIRouter()
_operator = security.require_role("operator")
OUTCOMES = views.DESK_OUTCOMES
MESSAGES = views.DESK_MESSAGES


def _row(db: Session, request_id: str) -> AssistanceRequest:
    row = views.desk_request(db, request_id)
    if row is None:
        raise HTTPException(404, detail="not_found")
    return row


_card = views.desk_card


@router.get("/desk")
async def desk_index(request: Request, db: Session = DB,
                     principal: security.Principal = Depends(_operator)):
    return request.app.state.templates.TemplateResponse(
        request=request, name="desk/index.html",
        context={"principal": principal, "active_page": "desk",
                 "cards": views.desk_queue(db)["requests"]})


@router.get("/desk/api/queue")
async def desk_queue_json(db: Session = DB,
                          principal: security.Principal = Depends(_operator)) -> JSONResponse:
    return JSONResponse(views.desk_queue(db), headers={"Cache-Control": "no-store"})


@router.get("/desk/requests/{request_id}/snapshot.jpg")
async def desk_snapshot(request_id: str, db: Session = DB,
                        principal: security.Principal = Depends(_operator)) -> Response:
    row = _row(db, request_id)
    snapshot = views.snapshot_of(db, row)
    if snapshot is None:
        raise HTTPException(404, detail="not_found")
    return Response(snapshot.data, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@router.get("/desk/requests/{request_id}")
async def desk_request(request_id: str, request: Request, db: Session = DB,
                       principal: security.Principal = Depends(_operator)):
    row = _row(db, request_id)
    detail = views.desk_detail(db, row, online=hub_for(request.app).online,
                               settings=request.app.state.settings)
    return request.app.state.templates.TemplateResponse(
        request=request, name="desk/request.html",
        context={"principal": principal, "active_page": "desk", **detail,
                 "message": MESSAGES.get(request.query_params.get("m", ""))})


def _done(request: Request, row_id: str, outcome: dict):
    accept = request.headers.get("accept", "")
    if request.headers.get("x-requested-with") or (
            "application/json" in accept and "text/html" not in accept):
        return JSONResponse(outcome, status_code=outcome["status"])
    return RedirectResponse(f"/desk/requests/{row_id}?m={outcome['code']}", status_code=303)


@router.post("/desk/requests/{request_id}/acknowledge")
async def desk_acknowledge(request_id: str, request: Request, db: Session = DB,
                           principal: security.Principal = Depends(_operator)):
    row = _row(db, request_id)
    return _done(request, row.id, await views.desk_acknowledge(db, principal, row))


@router.post("/desk/requests/{request_id}/called-112")
async def desk_called_112(request_id: str, request: Request, db: Session = DB,
                          principal: security.Principal = Depends(_operator)):
    row = _row(db, request_id)
    return _done(request, row.id, await views.desk_called_112(db, principal, row))


@router.post("/desk/requests/{request_id}/resolve")
async def desk_resolve(request_id: str, request: Request, outcome: str = Form(...),
                       note: str | None = Form(None), db: Session = DB,
                       principal: security.Principal = Depends(_operator)):
    row = _row(db, request_id)
    return _done(request, row.id, await views.desk_resolve(db, principal, row, outcome, note))


__all__ = ["router", "OUTCOMES"]
