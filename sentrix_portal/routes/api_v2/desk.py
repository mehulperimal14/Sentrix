"""SENTRIX Desk (API v2, operator + TOTP): the queue (requests on the help
path), one request's card (the full SOS text with the DURESS banner,
address and map, household notes, the resident's callback number, the
snapshot, the live view when allowed, the full timeline), and the answers:
acknowledge, "Called 112" (simulated: SENTRIX never contacts 112) and
resolve with an outcome.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from sentrix_portal.db import DB
from sentrix_portal.models import AssistanceRequest
from sentrix_portal.routes.api_v2.common import OPERATOR, ApiError, raise_for_sos
from sentrix_portal.routes.api_v2.schemas import (CallTarget, DeskCard, TimelineEvent,
                                                  desk_card, staff_events)
from sentrix_portal.routes.api_v2.sos import JPEG
from sentrix_portal.services import sos_views as views
from sentrix_portal.services.hub import hub_for

router = APIRouter(tags=["desk"])
Outcome = Literal["help_arrived", "real_threat", "false_alarm_confirmed", "no_action_needed"]


class QueueOut(BaseModel):
    requests: list[DeskCard]
    unacknowledged: int


class OutcomeOption(BaseModel):
    value: str
    label: str


class DeskHome(BaseModel):
    adults: int | None = None
    children: int | None = None
    seniors: int | None = None
    pets: str | None = None


class DeskRequestOut(BaseModel):
    request: DeskCard
    events: list[TimelineEvent]
    address: str | None
    entry_note: str | None
    map_url: str | None
    home: DeskHome
    #: Consented medical notes only.
    medical: str | None
    resident: str | None
    resident_phone: str | None
    room: str | None
    has_snapshot: bool
    snapshot_url: str | None
    #: The tunnelled live view, when the household allowed the Desk's.
    live_url: str | None
    outcomes: list[OutcomeOption]
    call: CallTarget


class ResolveIn(BaseModel):
    outcome: Outcome | None = None
    note: str | None = None


class DeskDoneOut(BaseModel):
    ok: bool = True
    message: str
    request: DeskCard


def _row(db: Session, request_id: str) -> AssistanceRequest:
    row = views.desk_request(db, request_id)
    if row is None:
        raise ApiError("not_found", "This request is not in the Desk's queue.")
    return row


@router.get("/desk/queue", response_model=QueueOut)
def queue(db: Session = DB, principal=Depends(OPERATOR)) -> QueueOut:
    """Newest first; an alarm should sound while ``unacknowledged`` > 0."""
    data = views.desk_queue(db)
    return QueueOut(requests=[desk_card(c) for c in data["requests"]],
                    unacknowledged=data["unacknowledged"])


def _detail(db: Session, request: Request, row: AssistanceRequest) -> DeskRequestOut:
    detail = views.desk_detail(db, row, online=hub_for(request.app).online,
                               settings=request.app.state.settings)
    household = detail["household"]
    occupants = (household.occupants or {}) if household is not None else {}
    return DeskRequestOut(
        request=desk_card(detail["req"]),
        events=staff_events(db, row, "desk"), address=detail["address"],
        entry_note=household.entry_note if household is not None else None,
        map_url=detail["map_url"],
        home=DeskHome(adults=occupants.get("adults"), children=occupants.get("children"),
                      seniors=occupants.get("seniors"),
                      pets=household.pets if household is not None else None),
        medical=detail["medical"], resident=detail["owner_name"],
        resident_phone=detail["owner_phone"], room=detail["room"],
        has_snapshot=detail["has_snapshot"],
        snapshot_url=(f"/api/v2/desk/requests/{row.id}/snapshot.jpg" if detail["has_snapshot"]
                      else None),
        live_url=detail["live_url"],
        outcomes=[OutcomeOption(value=k, label=v) for k, v in detail["outcomes"].items()],
        call=CallTarget(**detail["call"]))


@router.get("/desk/requests/{request_id}", response_model=DeskRequestOut)
def desk_request(request_id: str, request: Request, db: Session = DB,
                 principal=Depends(OPERATOR)) -> DeskRequestOut:
    return _detail(db, request, _row(db, request_id))


@router.get("/desk/requests/{request_id}/snapshot.jpg", response_class=Response,
            responses=JPEG)
def desk_snapshot(request_id: str, db: Session = DB, principal=Depends(OPERATOR)) -> Response:
    snapshot = views.snapshot_of(db, _row(db, request_id))
    if snapshot is None:
        raise ApiError("not_found", "There is no snapshot for this request.")
    return Response(snapshot.data, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


def _done(db: Session, row: AssistanceRequest, result: dict, *,
          field: str | None = None) -> DeskDoneOut:
    raise_for_sos(result, views.DESK_MESSAGES, db, field=field)
    db.expire_all()
    fresh = db.get(AssistanceRequest, row.id)
    return DeskDoneOut(message=views.DESK_MESSAGES["ok"],
                       request=desk_card(views.desk_card(db, fresh)))


@router.post("/desk/requests/{request_id}/acknowledge", response_model=DeskDoneOut)
async def acknowledge(request_id: str, db: Session = DB,
                      principal=Depends(OPERATOR)) -> DeskDoneOut:
    row = _row(db, request_id)
    return _done(db, row, await views.desk_acknowledge(db, principal, row))


@router.post("/desk/requests/{request_id}/called-112", response_model=DeskDoneOut)
async def called_112(request_id: str, db: Session = DB,
                     principal=Depends(OPERATOR)) -> DeskDoneOut:
    """A note in the timeline only: SENTRIX does not contact 112."""
    row = _row(db, request_id)
    return _done(db, row, await views.desk_called_112(db, principal, row))


@router.post("/desk/requests/{request_id}/resolve", response_model=DeskDoneOut)
async def resolve(request_id: str, body: ResolveIn, db: Session = DB,
                  principal=Depends(OPERATOR)) -> DeskDoneOut:
    row = _row(db, request_id)
    result = await views.desk_resolve(db, principal, row, body.outcome, body.note)
    return _done(db, row, result, field="outcome")


__all__ = ["router"]
