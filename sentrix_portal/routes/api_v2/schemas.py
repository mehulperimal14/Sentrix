"""Response models shared by several API v2 areas: SOS requests in their
three projections (owner, Desk, contact), timeline events, the call target
and device cards. Every time is ISO 8601 UTC with ``Z``; no model carries a
hash, a token digest or a secret.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from sqlalchemy.orm import Session

from sentrix.sos import states as S
from sentrix_portal.models import AssistanceRequest
from sentrix_portal.routes.api_v2.common import clean_detail, z
from sentrix_portal.services import sos as sos_service


class CallTarget(BaseModel):
    """The "Call 112 now" button: never ``tel:112`` in test mode (D13)."""

    href: str | None
    label: str
    test_mode: bool


class AuthorityOption(BaseModel):
    value: str
    label: str


class OwnerRequest(BaseModel):
    """A request as an owner or member sees it (docs/7 §7.8): a duress
    approval reads exactly like "Marked as false alarm"."""

    id: str
    short_id: str
    device_id: str | None
    state: str
    label: str
    flags: dict[str, Any]
    urgency: str | None
    incident_type: str | None
    incident_ids: list[str]
    evidence_state: str | None
    trigger_level: int | None
    peak_level: int | None
    created_at: str | None
    updated_at: str | None
    expires_at: str | None
    remaining_s: int | None
    seen_at: str | None
    authority: str | None
    sos_text: str | None
    parent_request_id: str | None
    outcome: str | None
    test_mode: bool
    desk_acknowledged_at: str | None
    actions: list[str]


class StaffRequest(OwnerRequest):
    """The Desk's and a contact's view: the duress flag and its banner."""

    duress: bool
    banner: str | None


class DeskCard(StaffRequest):
    city: str | None
    locality: str | None
    resident: str | None
    acknowledged: bool


class TimelineEvent(BaseModel):
    seq: int
    at: str | None
    event: str
    from_state: str | None
    to_state: str | None
    actor_type: str | None
    channel: str | None
    origin: str | None
    detail: dict[str, Any]
    restricted: bool = False


_TIMES = ("created_at", "updated_at", "expires_at", "seen_at", "desk_acknowledged_at")
_PROMPTING = {S.DRAFTED, S.PENDING, None}


def _times(view: dict[str, Any]) -> dict[str, Any]:
    out = dict(view)
    for key in _TIMES:
        out[key] = z(out.get(key))
    return out


def owner_request(row: AssistanceRequest) -> OwnerRequest:
    """``request_view(row, "owner")`` with nothing that would tell a duress
    approval from a decline: no authority, outcome or Desk acknowledgement,
    and the time of the answer as its last change."""
    view = _times(sos_service.request_view(row, "owner"))
    if (row.flags or {}).get("duress"):
        view.update(authority=None, outcome=None, desk_acknowledged_at=None,
                    updated_at=z(row.responded_at) or view["updated_at"])
    return OwnerRequest(**view)


def staff_request(row: AssistanceRequest, view: str) -> StaffRequest:
    return StaffRequest(**_times(sos_service.request_view(row, view)))


def desk_card(card: dict[str, Any]) -> DeskCard:
    return DeskCard(**_times(card))


def _event(item: dict[str, Any], *, restricted: bool) -> TimelineEvent:
    return TimelineEvent(seq=item["seq"], at=z(item.get("at")), event=item["event"],
                         from_state=item.get("from_state"), to_state=item.get("to_state"),
                         actor_type=item.get("actor_type"), channel=item.get("channel"),
                         origin=item.get("origin"), detail=clean_detail(item.get("detail")),
                         restricted=bool(item.get("restricted")) if restricted else False)


def owner_events(db: Session, row: AssistanceRequest) -> list[TimelineEvent]:
    """The owner's timeline. For a duress request only what a decline would
    show: the events up to the answer (the export's rule, docs/7 §7.8)."""
    # "Request help now" is shown by the linked request it opens, not by an
    # event on this one (a duress request cannot record that event).
    items = [item for item in sos_service.events_view(db, row.id, "owner")
             if item["event"] != S.HELP_REQUESTED_LATER]
    if (row.flags or {}).get("duress"):
        items = [item for item in items
                 if item["event"] == S.DECLINE
                 or {item.get("from_state"), item.get("to_state")} <= _PROMPTING]
    for n, item in enumerate(items, 1):
        item["seq"] = n
    return [_event(item, restricted=False) for item in items]


def staff_events(db: Session, row: AssistanceRequest, view: str) -> list[TimelineEvent]:
    return [_event(item, restricted=True)
            for item in sos_service.events_view(db, row.id, view)]


class DeviceCard(BaseModel):
    """A device on the home page."""

    id: str
    serial: str
    name: str
    state: str
    claimed: bool
    online: bool
    armed: bool
    ai_hz: float | None
    last_seen: str | None
    room: str | None
    last_incident: dict[str, Any] | None
    requests: list[OwnerRequest]


__all__ = ["CallTarget", "AuthorityOption", "OwnerRequest", "StaffRequest", "DeskCard",
           "TimelineEvent", "owner_request", "staff_request", "desk_card", "owner_events",
           "staff_events", "DeviceCard"]
