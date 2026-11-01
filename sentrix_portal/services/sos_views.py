"""What the Portal's SOS pages show and do, shared by the HTML routes
(``routes/sos.py``, ``desk.py``, ``contacts.py``) and the JSON API
(``routes/api_v2``): the "Call 112" target (never ``tel:112`` in test mode,
D13), the owner's request lookup, e-mail action links, the contact's SOS
page, and the Desk's queue, card and answers. Every answer goes through
``services.sos.respond`` (online the device decides, offline the cloud).

Views follow docs/7 §7.8: an owner or member always gets the ``owner``
projection (a duress approval reads "Marked as false alarm"), the Desk and
contacts get the duress banner.

Owner: track T-LINK.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, Callable

from sqlalchemy.orm import Session

from sentrix.actor import Actor
from sentrix.sos import states as S
from sentrix_portal import security
from sentrix_portal.models import (AssistanceRequest, Camera, Device, EmergencyContact,
                                   Household, SosSnapshot, User, utcnow)
from sentrix_portal.services import sos as sos_service
from sentrix_portal.services.hub import UUID_RE

#: Actions a signed-in owner or member may take on a request.
PAGE_ACTIONS = ("approve", "checking", "decline", "safe", "help_now")
#: Actions an e-mail action link may take (never a decline: that needs the PIN).
LINK_ACTIONS = ("approve", "checking")
#: Actions on a contact's SOS page.
CONTACT_ACTIONS = ("acknowledge", "approve")
AUTHORITY_LABELS = {"POLICE": "Police", "FIRE": "Fire", "MEDICAL": "Medical"}
#: The owner's page (``/sos/<id>?m=<code>``).
OWNER_MESSAGES = {
    "ok": "Done.", "pin_invalid": "That PIN is not right.",
    "pin_locked": "Too many wrong PINs. Try again in 10 minutes; asking for help still works.",
    "conflict": "This request has moved on; the page shows where it is now.",
    "device_offline": "The device is offline; the SENTRIX Portal handled it.",
    "timeout": "The device did not answer in time. Try again.",
    "forbidden_role": "You cannot do that here.",
}
#: The contact's page (``/c/<token>?m=<code>``).
CONTACT_MESSAGES = {"ok": "Thank you. The resident and the SENTRIX desk can see it.",
                    "forbidden_role": "This request does not let contacts ask for help.",
                    "conflict": "The request has moved on.",
                    "timeout": "The device did not answer in time. Try again."}
#: The Desk's request page (``/desk/requests/<id>?m=<code>``).
DESK_MESSAGES = {"ok": "Done.", "conflict": "The request has moved on.",
                 "invalid_args": "Choose an outcome.", "timeout": "The device did not answer."}
DESK_OUTCOMES = {"help_arrived": "Help arrived (simulated)", "real_threat": "Real threat",
                 "false_alarm_confirmed": "False alarm confirmed",
                 "no_action_needed": "No action needed"}


def _cfg():
    from sentrix.config import settings
    return settings


def call_112(settings) -> dict:
    """The "Call 112 now" target: never a tel:112 link in test mode (D13)."""
    if settings.test_mode:
        from sentrix.alerting.guard import is_emergency_number
        number = re.sub(r"[^\d+]", "", str(settings.desk_number or ""))
        if number and is_emergency_number(number):
            number = ""                           # never tel:112 in test mode
        return {"href": f"tel:{number}" if number else None,
                "label": f"Call the SENTRIX Desk ({number})" if number
                else "Calling is off in this demo: no SENTRIX Desk number is set",
                "test_mode": True}
    return {"href": "tel:112", "label": "Call 112 now", "test_mode": False}


def actor_of(principal: security.Principal) -> dict:
    step = principal.step_up_at.timestamp() if principal.step_up_at else None
    return Actor(type=principal.role, id=principal.user_id, name=principal.display_name,
                 step_up_at=step).to_dict()


def default_authority(row: AssistanceRequest) -> str:
    return "FIRE" if row.incident_type == "fire" else "POLICE"


def snapshot_of(db: Session, row: AssistanceRequest) -> SosSnapshot | None:
    """The request's consented snapshot, when it holds a picture."""
    snapshot = db.query(SosSnapshot).filter_by(request_id=row.id).first()
    return snapshot if snapshot is not None and snapshot.data else None


def has_snapshot(db: Session, row: AssistanceRequest) -> bool:
    return db.query(SosSnapshot.id).filter_by(request_id=row.id).first() is not None


def _address(household: Household | None, *, district: bool) -> str | None:
    if household is None:
        return None
    parts = (household.house, household.building, household.street, household.locality,
             household.landmark, household.city,
             *((household.district,) if district else ()), household.state, household.pincode)
    return ", ".join(p for p in parts if p)


def _map_url(household: Household | None) -> str | None:
    if household is None or household.lat is None or household.lon is None:
        return None
    return f"https://maps.google.com/?q={household.lat},{household.lon}"


def _medical(db: Session, household: Household | None) -> str | None:
    if household is not None and household.include_medical and \
            sos_service.consent_active(db, household.id, "medical_notes"):
        return security.unseal(household.medical_enc)
    return None


def _owner(db: Session, household_id: str | None) -> User | None:
    if not household_id:
        return None
    return db.query(User).filter_by(household_id=household_id, role="owner").first()


# ── the household's own requests ──────────────────────────────────────────────
def household_request(db: Session, principal: security.Principal,
                      request_id: str) -> AssistanceRequest | None:
    """The request, if it is this user's household's (tenant isolation)."""
    if not UUID_RE.match(request_id or ""):
        return None
    row = db.get(AssistanceRequest, request_id)
    if row is None or not principal.household_id or row.household_id != principal.household_id:
        return None
    return row


def household_requests(db: Session, household_id: str, *, limit: int = 30,
                       offset: int = 0) -> list[AssistanceRequest]:
    return (db.query(AssistanceRequest)
            .filter(AssistanceRequest.household_id == household_id)
            .order_by(AssistanceRequest.created_at.desc()).offset(offset).limit(limit).all())


async def answer(db: Session, principal: security.Principal, row: AssistanceRequest,
                 action: str, *, authority: str | None, pin: str | None) -> dict[str, Any]:
    """An owner's or member's answer (``sos.respond``, channel ``portal``)."""
    return await sos_service.respond(db, row.id, action, actor=actor_of(principal),
                                     channel="portal", authority=authority, pin=pin or None)


# ── action links (/a/<token>) ─────────────────────────────────────────────────
def action_link(db: Session, token: str):
    """(token row, request, user) of a live, unused action link, else Nones."""
    found = sos_service.resolve_token(db, token, ("action_link",))
    if found is None or found.used_at is not None:
        return None, None, None
    row = db.get(AssistanceRequest, found.request_id) if found.request_id else None
    user = db.get(User, found.subject_id) if found.subject_id else None
    if row is None or user is None or user.status != "active":
        return None, None, None
    return found, row, user


def action_link_opened(db: Session, found, row: AssistanceRequest, user: User, *,
                       ip: str | None) -> None:
    found.opened_count = (found.opened_count or 0) + 1
    security.audit(db, "action_link.opened", actor_type="user", actor_id=user.id,
                   household_id=row.household_id, target_type="request", target_id=row.id,
                   ip=ip)


async def use_action_link(db: Session, found, row: AssistanceRequest, user: User,
                          action: str, *, authority: str | None,
                          ip: str | None) -> dict[str, Any]:
    """The link's one answer (the caller checked ``action`` is in
    :data:`LINK_ACTIONS`). As the Telegram buttons: only a help request that
    went through uses the link up; a timeout ("try again") or "I'm checking"
    leaves it usable."""
    actor = Actor(type=user.role if user.role in ("owner", "member") else "owner",
                  id=user.id, name=user.display_name or user.username).to_dict()
    outcome = await sos_service.respond(db, row.id, action, actor=actor, channel="email",
                                        authority=authority)
    if outcome["ok"] and action == "approve":
        found.used_at = utcnow()
    security.audit(db, "action_link.used", actor_type="user", actor_id=user.id,
                   household_id=row.household_id, target_type="request", target_id=row.id,
                   ip=ip, detail={"action": action, "code": outcome["code"]})
    return outcome


# ── the contact's SOS page (/c/<token>) ───────────────────────────────────────
def contact_page(db: Session, token: str):
    """(token row, request, CONFIRMED contact) of a live contact page."""
    found = sos_service.resolve_token(db, token, ("contact_page",))
    if found is None:
        return None, None, None
    row = db.get(AssistanceRequest, found.request_id) if found.request_id else None
    contact = db.get(EmergencyContact, found.subject_id) if found.subject_id else None
    if row is None or contact is None or contact.consent_status != "CONFIRMED":
        return None, None, None
    return found, row, contact


def contact_page_opened(db: Session, found, row: AssistanceRequest,
                        contact: EmergencyContact, *, ip: str | None) -> None:
    found.opened_count = (found.opened_count or 0) + 1
    security.audit(db, "contact_page.opened", actor_type="contact", actor_id=contact.id,
                   household_id=row.household_id, target_type="request", target_id=row.id,
                   ip=ip, detail={"opened_count": found.opened_count})


def contact_view(db: Session, row: AssistanceRequest, contact: EmergencyContact,
                 settings) -> dict[str, Any]:
    """What the contact's page shows: the request (contact view, with the
    duress banner), where, who, medical notes only with consent, and the
    actions this request allows a contact."""
    view = sos_service.request_view(row, "contact")
    household = db.get(Household, row.household_id) if row.household_id else None
    owner = _owner(db, row.household_id)
    can_approve = (view["state"] in S.PROMPTING_STATES
                   and sos_service._contact_allowed(db, row, Actor("contact", id=contact.id)))
    return {"req": view, "contact": contact,
            "resident": owner.display_name if owner else "the resident",
            "resident_phone": (security.unseal(owner.phone_enc)
                               if owner is not None and owner.phone_enc else None),
            "address": _address(household, district=False),
            "landmark": household.landmark if household else None,
            "entry_note": household.entry_note if household else None,
            "map_url": _map_url(household), "medical": _medical(db, household),
            "has_snapshot": has_snapshot(db, row),
            "can_acknowledge": view["state"] in (S.APPROVED, S.DISPATCHED, S.FAILED),
            "can_approve": can_approve, "call": call_112(settings)}


async def contact_act(db: Session, row: AssistanceRequest, contact: EmergencyContact,
                      action: str, *, ip: str | None) -> dict[str, Any]:
    """"I'm handling it" or "Alert the SENTRIX desk" (the caller checked
    ``action`` is in :data:`CONTACT_ACTIONS`)."""
    actor = Actor("contact", id=contact.id, name=contact.name).to_dict()
    outcome = await sos_service.respond(
        db, row.id, action, actor=actor, channel="contact_page",
        detail={"by": f"{contact.name} (emergency contact)"} if action == "acknowledge" else None)
    security.audit(db, f"contact_page.{action}", actor_type="contact", actor_id=contact.id,
                   household_id=row.household_id, target_type="request", target_id=row.id,
                   ip=ip, detail={"code": outcome["code"]})
    return outcome


# ── a device's offline mirror ─────────────────────────────────────────────────
def device_mirror(db: Session, principal: security.Principal, device: Device) -> dict[str, Any]:
    """What the Portal keeps of a device while it is offline: its incidents
    and dispatch packages (household only: the Desk sees no history) and the
    open requests in the viewer's projection. ``request_rows``: the rows."""
    from sentrix_portal.models import DispatchMirror, IncidentMirror
    household = principal.role in ("owner", "member")
    incidents = (db.query(IncidentMirror).filter_by(device_id=device.id)
                 .order_by(IncidentMirror.started_at.desc()).limit(50).all()) if household else []
    packages = (db.query(DispatchMirror).filter_by(device_id=device.id)
                .order_by(DispatchMirror.created_at.desc()).limit(20).all()) if household else []
    rows = (db.query(AssistanceRequest)
            .filter(AssistanceRequest.device_id == device.id,
                    AssistanceRequest.state.in_(sorted(S.OPEN_STATES))).all())
    view = "desk" if principal.role == "operator" else "owner"
    return {"incidents": incidents, "packages": packages, "request_rows": rows,
            "requests": [sos_service.request_view(r, view) for r in rows],
            "can_label": household}


# ── the Desk ──────────────────────────────────────────────────────────────────
def desk_visible(row: AssistanceRequest) -> bool:
    """On the help path, or closed within the link grace (2 h)."""
    if row.state in sos_service.DESK_STATES:
        return True
    if row.state == S.CLOSED and row.updated_at is not None:
        updated = sos_service._aware(row.updated_at)
        return utcnow() - updated <= timedelta(seconds=_cfg().sos.link_grace_seconds)
    return False


def desk_request(db: Session, request_id: str) -> AssistanceRequest | None:
    if not UUID_RE.match(request_id or ""):
        return None
    row = db.get(AssistanceRequest, request_id)
    return row if row is not None and desk_visible(row) else None


def desk_card(db: Session, row: AssistanceRequest) -> dict:
    """The queue row: the request in desk view plus where and who."""
    view = sos_service.request_view(row, "desk")
    household = db.get(Household, row.household_id) if row.household_id else None
    owner = _owner(db, row.household_id)
    view.update(city=household.city if household else None,
                locality=household.locality if household else None,
                resident=owner.display_name if owner else None,
                acknowledged=row.state in (S.ACKNOWLEDGED, S.CLOSED))
    return view


def desk_queue(db: Session) -> dict[str, Any]:
    cards = [desk_card(db, r) for r in sos_service.desk_queue(db)]
    return {"requests": cards,
            "unacknowledged": sum(1 for c in cards if not c["acknowledged"])}


def desk_detail(db: Session, row: AssistanceRequest, *, online: Callable[[str], bool],
                settings) -> dict[str, Any]:
    """The SOS card: text, address and map, household notes, the resident's
    callback number, the snapshot, the live view when the household allowed
    ``desk_live_view``, and the full timeline."""
    household = db.get(Household, row.household_id) if row.household_id else None
    owner = _owner(db, row.household_id)
    device = db.get(Device, row.device_id) if row.device_id else None
    camera = db.query(Camera).filter_by(device_id=row.device_id).first() if device else None
    live = bool(device is not None and row.state in sos_service.DESK_STATES
                and sos_service.consent_active(db, row.household_id, "desk_live_view")
                and online(device.id))
    return {"req": desk_card(db, row), "events": sos_service.events_view(db, row.id, "desk"),
            "household": household, "address": _address(household, district=True),
            "medical": _medical(db, household),
            "owner_name": owner.display_name if owner else None,
            "owner_phone": security.unseal(owner.phone_enc) if owner and owner.phone_enc
            else None,
            "room": camera.room_label if camera else None, "map_url": _map_url(household),
            "has_snapshot": has_snapshot(db, row),
            "live_url": f"/d/{device.id}/live" if live else None,
            "outcomes": DESK_OUTCOMES, "call": call_112(settings)}


async def desk_acknowledge(db: Session, principal: security.Principal,
                           row: AssistanceRequest) -> dict[str, Any]:
    return await sos_service.respond(db, row.id, "acknowledge", actor=actor_of(principal),
                                     channel="desk",
                                     detail={"by": f"SENTRIX desk ({principal.display_name})"})


async def desk_called_112(db: Session, principal: security.Principal,
                          row: AssistanceRequest) -> dict[str, Any]:
    return await sos_service.record_note(
        db, row.id, actor=actor_of(principal),
        detail={"kind": "desk_called_112", "simulated": True,
                "note": "Called 112 (simulated: SENTRIX does not contact 112)"})


async def desk_resolve(db: Session, principal: security.Principal, row: AssistanceRequest,
                       outcome: Any, note: Any) -> dict[str, Any]:
    if outcome not in DESK_OUTCOMES:
        return sos_service.result("invalid_args", "unknown outcome")
    return await sos_service.respond(db, row.id, "resolve", actor=actor_of(principal),
                                     channel="desk",
                                     detail={"outcome": outcome,
                                             "note": (note or "")[:500]})


__all__ = ["PAGE_ACTIONS", "LINK_ACTIONS", "CONTACT_ACTIONS", "AUTHORITY_LABELS",
           "OWNER_MESSAGES", "CONTACT_MESSAGES", "DESK_MESSAGES", "DESK_OUTCOMES",
           "call_112", "actor_of", "default_authority", "snapshot_of", "has_snapshot",
           "household_request", "household_requests", "answer", "action_link",
           "action_link_opened", "use_action_link", "contact_page", "contact_page_opened",
           "contact_view", "contact_act", "device_mirror", "desk_visible", "desk_request", "desk_card",
           "desk_queue", "desk_detail", "desk_acknowledge", "desk_called_112",
           "desk_resolve"]
