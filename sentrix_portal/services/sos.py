"""The Portal's SOS: mirror, cloud authority and every SOS message (docs/7 §7.6, §8).

What this module owns on the Portal:

* **The mirror** of device requests and events, fed by the ``sos.*`` outbox
  events (:func:`ingest_device_event`). Each request's device chain is applied
  in its own seq order; an event that arrives before its predecessor stays in
  ``device_events`` (already stored and acked) and is applied once the gap
  fills. Every device event's hash and link are checked before it is stored
  (the device's ``storage/requests.py`` chain rule, recomputed here).
* **The cloud authority** while a device is offline (heartbeat lost 45 s,
  :func:`on_device_offline`): the request switches to ``authority_side =
  "cloud"`` and the cloud runs its timers (:func:`run_timers`: reminders, the
  lapse warning, expiry, re-pages, the 24 h stale close) and answers
  (:func:`respond`). A lost heartbeat during an open L3+ alarm-class
  incident without a request opens one in the cloud (flag
  ``device_offline``): the tamper signal. An L2 (unrecognised person, audio
  only, experimental) is audited and told to the owner only.
  Cloud events form their own chain (origin cloud) and go to the device in
  ``sos.sync`` after the next welcome (:func:`sync_messages`); the mirror
  merges the two sides with ``states.reconcile``.
* **Who gets which message at which step** (:func:`_after_event`): owner
  prompts, reminders, rises and lapse notices; contacts' SOS with their
  ``/c/`` page; the duress banner; "resolved" notices. Telegram gets a short
  text and a Portal link with buttons (``channels.base.callback_data``; the
  nonce lives in ``action_tokens``); e-mail gets the full text and links that
  open a confirmation page (``/a/``, ``/c/``). Messages go to
  ``services.notify.enqueue`` (track T-CH: transports, outbox, guard).
* :func:`respond`: the one entry point for SOS answers from Portal pages,
  action links, contact pages, the Desk and Telegram callbacks. Online it
  sends the ``sos.respond`` / ``sos.update`` command and returns the device's
  result; offline the cloud applies it (PIN checked against the household's
  hashes, the duress PIN included) and syncs later.

``actor`` is ``sentrix.actor.Actor.to_dict()``. Results have the shape of
``sentrix.sos.SosResult``; ``request`` is in the actor's view (owner view for
owners and members: a duress approval reads "Marked as false alarm").
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any

from sentrix.link import protocol
from sentrix.sos import states as S
from sentrix_portal.models import (ActionToken, AssistanceRequest, Camera, Consent,
                                   Device, DeviceEvent, EmergencyContact, Household,
                                   IncidentMirror, RequestEvent, SosSnapshot,
                                   TelegramLink, User, utcnow)

log = logging.getLogger("sentrix_portal.sos")

#: Portal-side actions: the owner's (sentrix.sos.RESPOND_ACTIONS) plus the
#: Desk's and contacts'.
ACTIONS = ("approve", "decline", "checking", "safe", "help_now", "acknowledge",
           "resolve")
OWNER_ACTIONS = frozenset({"approve", "decline", "checking", "safe", "help_now"})
#: What the Desk queue shows: requests on the help path that are not closed.
DESK_STATES = frozenset(S.HELP_PATH - S.TERMINAL_STATES)
_GENESIS = "0" * 64
_REQUEST_KINDS = ("sos.created", "sos.transition", "sos.event")
FOOTER = "SENTRIX does not contact 112 itself."
DURESS_BANNER = "Do not go to the home. Call 112."
WHAT = {"weapon": "a possible weapon (handgun or knife)", "fire": "visible fire",
        "assault": "a fight (seen and heard)",
        "intrusion": "an unrecognised person in the home",
        "duress": "an alert", "unknown": "an alert"}
OUTCOME_LABELS = {"help_arrived": "help arrived", "real_threat": "real threat",
                  "false_alarm_confirmed": "false alarm confirmed",
                  "no_action_needed": "no action needed"}
#: PIN back-off on the cloud path (the device has its own, sos/pins.py).
PIN_ATTEMPTS, PIN_WINDOW = 5, 600.0
_pin_failures: dict[str, deque] = defaultdict(deque)
#: PIN checks allowed while locked (the duress PIN only), per window.
LOCKED_CHECKS = 3
_locked_checks: dict[str, deque] = defaultdict(deque)
_pin_lock = threading.Lock()


def _cfg():
    from sentrix.config import settings
    return settings


def _test_mode() -> bool:
    """The Portal's test mode (``PORTAL_TEST_MODE``), the one its send guard
    enforces; never the device's ``SENTRIX_TEST_MODE``."""
    from sentrix_portal import settings as portal_settings
    return bool(portal_settings.current().test_mode)


def _call_text() -> str:
    """What a Portal text tells the reader to call: 112, or in test mode the
    Desk number (never 112 in test mode, D13)."""
    from sentrix.sos import templates
    from sentrix_portal import settings as portal_settings
    test_mode = _test_mode()
    desk = getattr(portal_settings.current(), "desk_number", None) if test_mode else None
    return templates.call_number({"desk": {"phone": desk or ""}}, test_mode)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    value = _aware(value)
    return value.isoformat() if value else None


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def result(code: str = "ok", message: str = "", request: dict | None = None) -> dict:
    """A ``SosResult``-shaped dict."""
    status = 200 if code == "ok" else (503 if code == "device_offline"
                                       else protocol.ERROR_CODES.get(code, 400))
    return {"ok": code == "ok", "code": code, "status": status, "message": message,
            "request": request}


def _uuid(value: Any) -> str | None:
    from sentrix_portal.services.hub import UUID_RE
    text = str(value or "").lower()
    return text if UUID_RE.match(text) else None


# ── chains ────────────────────────────────────────────────────────────────────
_FIELDS = ("request_id", "seq", "at", "from_state", "to_state", "event", "actor_type",
           "actor_id", "channel", "detail", "restricted", "origin")


def chain_hash(prev_hash: str, fields: dict[str, Any]) -> str:
    """The device's rule (``storage/requests.py``): sha256(prev || canonical(row))."""
    return hashlib.sha256(prev_hash.encode() + protocol.canonical_json(fields)).hexdigest()


def event_dict(row: RequestEvent) -> dict[str, Any]:
    """``requests.event_dict`` shape (``at`` verbatim as hashed)."""
    return {"request_id": row.request_id, "seq": row.seq,
            "at": row.at_text or _iso(row.at), "from_state": row.from_state,
            "to_state": row.to_state, "event": row.event, "actor_type": row.actor_type,
            "actor_id": row.actor_id, "channel": row.channel, "detail": row.detail or {},
            "restricted": bool(row.restricted), "origin": row.origin,
            "prev_hash": row.prev_hash, "hash": row.hash}


def _last_event(db, request_id: str, origin: str) -> RequestEvent | None:
    return (db.query(RequestEvent).filter_by(request_id=request_id, origin=origin)
            .order_by(RequestEvent.seq.desc()).first())


def has_cloud_events(db, request_id: str) -> bool:
    return db.query(RequestEvent).filter_by(request_id=request_id,
                                            origin="cloud").first() is not None


def verify_chains(db, request_id: str) -> dict[str, Any]:
    """Every chain of a mirrored request: ``{ok, entries, problems, origins}``."""
    problems: list[str] = []
    counts: dict[str, int] = {}
    rows = (db.query(RequestEvent).filter_by(request_id=request_id)
            .order_by(RequestEvent.origin, RequestEvent.seq).all())
    prev: dict[str, str] = {}
    for row in rows:
        counts[row.origin] = counts.get(row.origin, 0) + 1
        fields = {k: v for k, v in event_dict(row).items() if k in _FIELDS}
        if row.prev_hash != prev.get(row.origin, _GENESIS):
            problems.append(f"{row.origin} seq {row.seq}: broken link")
        if row.hash != chain_hash(row.prev_hash or _GENESIS, fields):
            problems.append(f"{row.origin} seq {row.seq}: hash mismatch")
        prev[row.origin] = row.hash
    return {"ok": not problems, "entries": len(rows), "problems": problems,
            "origins": counts}


# ── views ─────────────────────────────────────────────────────────────────────
def _flags(row: AssistanceRequest) -> dict:
    return dict(row.flags or {})


def request_view(row: AssistanceRequest, view: str = "owner") -> dict[str, Any]:
    """The request projected for ``view`` (owner | desk | contact | full)."""
    flags = _flags(row)
    if view == "owner":
        shown = S.owner_view(row.state, flags)
    else:
        shown = {"state": row.state, "label": S.STATE_LABELS.get(row.state, row.state),
                 "flags": flags}
    expires = _aware(row.expires_at)
    remaining = None
    if expires is not None and shown["state"] in S.PROMPTING_STATES:
        remaining = max(0, int((expires - utcnow()).total_seconds()))
    out = {
        "id": row.id, "short_id": f"SX-{row.id[:6].upper()}", "device_id": row.device_id,
        "state": shown["state"], "label": shown["label"], "flags": shown["flags"],
        "urgency": row.urgency, "incident_type": row.incident_type,
        "incident_ids": list(row.incident_ids or []), "evidence_state": row.evidence_state,
        "trigger_level": row.trigger_level, "peak_level": row.peak_level,
        "created_at": _iso(row.created_at), "updated_at": _iso(row.updated_at),
        "expires_at": _iso(row.expires_at), "remaining_s": remaining,
        "seen_at": _iso(row.seen_at), "authority": row.authority,
        "sos_text": row.sos_text_full, "parent_request_id": row.parent_request_id,
        "outcome": row.outcome, "authority_side": row.authority_side,
        "test_mode": bool(row.test_mode), "version": row.version,
        "desk_acknowledged_at": _iso(row.desk_acknowledged_at),
        "actions": owner_actions(shown["state"]) if view == "owner" else [],
    }
    if view in ("desk", "contact", "full"):
        out["duress"] = bool(flags.get("duress"))
        out["banner"] = DURESS_BANNER if flags.get("duress") else None
    if view == "full":
        out["policy"] = row.policy or {}
    return out


def owner_actions(state: str) -> list[str]:
    """Buttons the owner may press in ``state`` (``sentrix.sos.SOS_ACTIONS``)."""
    if state in S.PROMPTING_STATES:
        return ["approve", "call_112", "checking", "decline"]
    if state in (S.APPROVED, S.DISPATCHED, S.ACKNOWLEDGED, S.FAILED):
        return ["call_112", "safe"]
    if state in (S.EXPIRED, S.DECLINED, S.CANCELLED):
        return ["help_now"]
    return []


def events_view(db, request_id: str, view: str = "owner") -> list[dict[str, Any]]:
    query = db.query(RequestEvent).filter_by(request_id=request_id)
    rows = query.order_by(RequestEvent.at, RequestEvent.origin, RequestEvent.seq).all()
    out = []
    for row in rows:
        item = event_dict(row)
        if view == "owner" and row.restricted:
            # A duress approval reads as the decline it appears to be (the
            # device's owner_events does the same); other restricted rows
            # do not exist for the household.
            if item["event"] == S.APPROVE and (item["detail"] or {}).get("duress"):
                item.update(event=S.DECLINE, to_state=S.DECLINED, restricted=False,
                            detail={})
                out.append(item)
            continue
        if view == "owner":
            # A duress approval is a restricted row; the owner never sees it,
            # nor the flag in a later detail.
            item["detail"] = {k: v for k, v in (item["detail"] or {}).items()
                              if k != "duress"}
        out.append(item)
    if view == "owner":
        # Nor a sequence gap or a hash that would hint at a hidden row.
        for n, item in enumerate(out, 1):
            item.update(seq=n, hash=None, prev_hash=None)
    return out


def open_requests(db, household_id: str) -> list[dict[str, Any]]:
    """The household's open requests, owner view, newest first (a duress
    approval counts as closed: it reads "Marked as false alarm")."""
    rows = (db.query(AssistanceRequest)
            .filter(AssistanceRequest.household_id == household_id,
                    AssistanceRequest.state.in_(sorted(S.OPEN_STATES)))
            .order_by(AssistanceRequest.created_at.desc()).all())
    views = [request_view(row, "owner") for row in rows]
    return [v for v in views if v["state"] in S.OPEN_STATES]


def desk_queue(db) -> list[AssistanceRequest]:
    return (db.query(AssistanceRequest)
            .filter(AssistanceRequest.state.in_(sorted(DESK_STATES)))
            .order_by(AssistanceRequest.created_at.desc()).all())


# ── ingest of device events (the mirror) ──────────────────────────────────────
def _payload_request_id(payload: dict) -> str | None:
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    return _uuid(payload.get("request_id") or request.get("id"))


def ingest_device_event(db, device: Device, kind: str,
                        payload: dict) -> list[tuple[str, dict]]:
    """Apply one ``sos.*`` event (already stored in ``device_events``) and any
    buffered successors. Returns device commands for the hub to send."""
    request_id = _payload_request_id(payload)
    if request_id is None:
        raise ValueError(f"{kind} without a request id")
    return _drain(db, device, request_id)


def _pending_for(db, device: Device, request_id: str) -> tuple[dict[int, dict], list[dict]]:
    """Stored-but-unapplied device events of one request: chain events by
    seq, and snapshots."""
    since = utcnow() - timedelta(days=30)
    rows = (db.query(DeviceEvent)
            .filter(DeviceEvent.device_id == device.id,
                    DeviceEvent.kind.in_(protocol.SOS_EVENT_KINDS),
                    DeviceEvent.received_at >= since).all())
    events: dict[int, dict] = {}
    snapshots: list[dict] = []
    for row in rows:
        payload = row.payload or {}
        if _payload_request_id(payload) != request_id:
            continue
        if row.kind == "sos.snapshot":
            snapshots.append(payload)
            continue
        event = payload.get("event") if isinstance(payload.get("event"), dict) else None
        if event and event.get("origin", "device") == "device" and isinstance(event.get("seq"), int):
            events.setdefault(event["seq"], payload)
    return events, snapshots


def _drain(db, device: Device, request_id: str) -> list[tuple[str, dict]]:
    followups: list[tuple[str, dict]] = []
    events, snapshots = _pending_for(db, device, request_id)
    last = _last_event(db, request_id, "device")
    applied = last.seq if last else 0
    while applied + 1 in events:
        produced = _apply_device(db, device, events[applied + 1])
        if produced is None:
            break                                   # invalid: stop the chain here
        followups += produced
        applied += 1
    row = db.get(AssistanceRequest, request_id)
    if row is not None:
        for snapshot in snapshots:
            _store_snapshot(db, row, snapshot)
    return followups


def _apply_device(db, device: Device, payload: dict) -> list[tuple[str, dict]] | None:
    from sentrix_portal import security
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    event = payload["event"]
    request_id = _payload_request_id(payload)
    fields = {k: event.get(k) for k in _FIELDS}
    fields["detail"] = fields["detail"] or {}
    fields["restricted"] = bool(fields["restricted"])
    fields["origin"] = "device"
    last = _last_event(db, request_id, "device")
    prev = last.hash if last else _GENESIS
    if (fields["request_id"] != request_id or event.get("prev_hash") != prev
            or chain_hash(prev, fields) != event.get("hash")
            or (fields["to_state"] is not None and fields["to_state"] not in S.STATES)):
        log.error("device %s request %s: event %s does not verify; chain stopped",
                  device.id, request_id, fields.get("seq"))
        security.audit(db, "sos.chain_invalid", actor_type="device", actor_id=device.id,
                       household_id=device.household_id, target_type="request",
                       target_id=request_id, detail={"seq": fields.get("seq")})
        return None
    row = db.get(AssistanceRequest, request_id)
    created = row is None
    if created:
        row = AssistanceRequest(id=request_id, device_id=device.id,
                                household_id=device.household_id, version=0,
                                authority_side="device", flags={}, incident_ids=[],
                                policy={})
        db.add(row)
    _merge_request(db, row, request, has_cloud_events(db, request_id))
    db.add(RequestEvent(
        request_id=request_id, seq=fields["seq"], origin="device", at=_parse(fields["at"]),
        at_text=fields["at"], received_at=utcnow(), from_state=fields["from_state"],
        to_state=fields["to_state"], event=fields["event"],
        actor_type=str(fields["actor_type"] or "system")[:16],
        actor_id=(str(fields["actor_id"])[:64] if fields["actor_id"] else None),
        channel=(str(fields["channel"])[:16] if fields["channel"] else None),
        detail=fields["detail"], restricted=fields["restricted"],
        prev_hash=prev, hash=event["hash"]))
    db.flush()
    security.audit(db, f"sos.{str(fields['event'])[:40]}", actor_type="device",
                   actor_id=device.id, household_id=row.household_id,
                   target_type="request", target_id=request_id,
                   detail={"origin": "device", "seq": fields["seq"],
                           "to_state": fields["to_state"],
                           "actor": f"{fields['actor_type']}:{fields['actor_id'] or ''}"})
    return _after_event(db, row, event_dict_from(fields, event["hash"], prev), "device")


def event_dict_from(fields: dict, digest: str, prev: str) -> dict:
    return {**fields, "prev_hash": prev, "hash": digest}


#: Flags the cloud sets itself; the device's snapshot cannot clear them.
_CLOUD_SET_FLAGS = frozenset({"duress", "contacts_notified", "resident_safe", "stale",
                              "unacknowledged"})
_MERGED = ("urgency", "trigger_level", "peak_level", "evidence_state", "authority",
           "responded_by", "response_channel", "sos_text_full", "sos_text_sms",
           "profile_version", "parent_request_id", "package_id", "outcome")


def _merge_request(db, row: AssistanceRequest, request: dict, cloud_acted: bool) -> None:
    """Copy the device's request snapshot into the mirror. Once the cloud has
    acted on a request, the state is ``states.reconcile(device, cloud)``."""
    version = request.get("version")
    if isinstance(version, int) and version < (row.version or 0) and not cloud_acted:
        return                                       # an older snapshot: keep the newer
    state = request.get("state")
    if state in S.STATES:
        row.state = S.reconcile(state, row.state) if (cloud_acted and row.state) else state
    for key in _MERGED:
        if key in request:
            value = request[key]
            setattr(row, key, str(value)[:4000] if isinstance(value, str) else value)
    from sentrix_portal.services.hub import _incident_type
    if "incident_type" in request:
        row.incident_type = _incident_type(request.get("incident_type"))
    ids = [i for i in (request.get("incident_ids") or [request.get("incident_id")])
           if _uuid(i)]
    row.incident_ids = sorted(set(list(row.incident_ids or []) + ids))
    flags = dict(row.flags or {})
    for key, value in (request.get("flags") or {}).items():
        if key in S.FLAGS:
            # A flag the cloud's own answers set (duress, contacts notified,
            # resident safe) is never cleared by the device; the device's own
            # flags (device_offline, undelivered, ...) follow the device.
            flags[key] = (bool(value) or bool(flags.get(key))) if key in _CLOUD_SET_FLAGS \
                else bool(value)
    row.flags = flags
    if isinstance(request.get("repage_count"), int):
        row.repage_count = max(int(row.repage_count or 0), request["repage_count"])
    if isinstance(request.get("policy"), dict):
        row.policy = request["policy"]
    for key in ("created_at", "expires_at", "seen_at", "responded_at"):
        parsed = _parse(request.get(key))
        if parsed is not None:
            setattr(row, key, parsed)
    row.test_mode = bool(request.get("test_mode", True))
    row.version = max(int(version) if isinstance(version, int) else 0, row.version or 0)
    row.updated_at = utcnow()
    for incident_id in ids:
        mirror = db.get(IncidentMirror, incident_id)
        if mirror is not None and not mirror.request_id:
            mirror.request_id = row.id


def _store_snapshot(db, row: AssistanceRequest, payload: dict) -> bool:
    """One consented SOS snapshot per request (D12): JPEG <= 256 KiB, sha256
    checked, 30 days. Dropped unless the household consents now."""
    import base64
    if db.query(SosSnapshot).filter_by(request_id=row.id).first() is not None:
        return False
    if not consent_active(db, row.household_id, "sos_snapshot"):
        return False
    try:
        data = base64.b64decode(payload.get("data_b64") or "", validate=True)
    except Exception:
        return False
    if (not data or len(data) > protocol.INLINE_BODY_BYTES or not data.startswith(b"\xff\xd8")
            or hashlib.sha256(data).hexdigest() != payload.get("sha256")):
        return False
    days = _cfg().retention.sos_snapshot_days
    db.add(SosSnapshot(request_id=row.id, device_id=row.device_id,
                       content_type="image/jpeg", sha256=payload.get("sha256"), data=data,
                       created_at=utcnow(), expires_at=utcnow() + timedelta(days=days)))
    return True


def consent_active(db, household_id: str | None, purpose: str) -> bool:
    if not household_id:
        return False
    return (db.query(Consent).filter_by(household_id=household_id, purpose=purpose)
            .filter(Consent.withdrawn_at.is_(None)).first()) is not None


# ── cloud events ──────────────────────────────────────────────────────────────
def _cloud_event(db, row: AssistanceRequest, event: str, *, actor_type: str = "cloud",
                 actor_id: str | None = "cloud", channel: str | None = "system",
                 detail: dict | None = None, restricted: bool = False,
                 flags: dict | None = None, **fields: Any) -> dict | None:
    """Append one event to the cloud chain and apply it to the mirror; None
    when ``states.next_state`` refuses it (nothing written)."""
    from sentrix_portal import security
    to_state = S.next_state(row.state, event)
    if to_state is None:
        return None
    last = _last_event(db, row.id, "cloud")
    seq = (last.seq + 1) if last else 1
    prev = last.hash if last else _GENESIS
    at = utcnow()
    clean_detail = json.loads(json.dumps(detail or {}, sort_keys=True, default=str))
    record = {"request_id": row.id, "seq": seq, "at": at.isoformat(),
              "from_state": row.state, "to_state": to_state, "event": event,
              "actor_type": actor_type, "actor_id": actor_id, "channel": channel,
              "detail": clean_detail, "restricted": bool(restricted), "origin": "cloud"}
    digest = chain_hash(prev, record)
    db.add(RequestEvent(request_id=row.id, seq=seq, origin="cloud", at=at,
                        at_text=record["at"], received_at=at, from_state=row.state,
                        to_state=to_state, event=event, actor_type=actor_type,
                        actor_id=actor_id, channel=channel, detail=clean_detail,
                        restricted=bool(restricted), prev_hash=prev, hash=digest))
    row.state = to_state
    row.version = (row.version or 0) + 1
    row.updated_at = at
    if flags:
        merged = dict(row.flags or {})
        merged.update(flags)
        row.flags = merged
    for key, value in fields.items():
        setattr(row, key, value)
    db.flush()
    security.audit(db, f"sos.{event}", actor_type=actor_type, actor_id=actor_id,
                   household_id=row.household_id, target_type="request", target_id=row.id,
                   detail={"origin": "cloud", "seq": seq, "to_state": to_state})
    ev = {**record, "prev_hash": prev, "hash": digest}
    _after_event(db, row, ev, "cloud")
    return ev


def _policy_for(db, household: Household | None) -> dict[str, Any]:
    """The policy the cloud freezes on a request it opens (D9)."""
    from sentrix_portal.services import accounts
    if household is None:
        return {"window_seconds": _cfg().sos.window_seconds, "safety_defaults": True,
                "contacts": []}
    contacts = accounts.confirmed_contacts(db, household.id)
    return {"window_seconds": _window(household), "safety_defaults": bool(
        household.safety_defaults if household.safety_defaults is not None else True),
            "contacts_for_single_source_l3": _cfg().sos.contacts_for_single_source_l3,
            "contacts": [{"id": c.id, "name": c.name, "consent_status": c.consent_status}
                         for c in contacts],
            "channels": dict(household.channels or {}),
            "profile_version": household.profile_version, "origin": "cloud"}


def _window(household: Household | None) -> int:
    cfg = _cfg().sos
    window = int((household.sos_window_s if household else None) or cfg.window_seconds)
    if window not in cfg.window_choices:
        window = min(cfg.window_choices, key=lambda w: abs(w - window))
    if _test_mode() and cfg.demo_window_seconds:
        window = max(60, int(cfg.demo_window_seconds))
    return window


def create_cloud_request(db, device: Device | None, household_id: str, *,
                         incident_type: str, level: int, incident_ids: list[str],
                         flags: dict | None = None, parent: AssistanceRequest | None = None,
                         text: str | None = None) -> AssistanceRequest:
    """A request the cloud opens itself (heartbeat lost during an open L3+
    alarm, or "Request help now" while the device is offline): own UUID,
    ``authority_side="cloud"``, the cloud's chain starting at ``created``."""
    household = db.get(Household, household_id)
    policy = _policy_for(db, household)
    now = utcnow()
    row = AssistanceRequest(
        id=str(uuid.uuid4()), device_id=device.id if device else None,
        household_id=household_id, incident_ids=sorted({i for i in incident_ids if _uuid(i)}),
        state=None, urgency="HIGH" if level >= 4 else "NORMAL", flags=dict(flags or {}),
        trigger_level=level, peak_level=level, incident_type=incident_type,
        evidence_state="unconfirmed", created_at=now, updated_at=now,
        expires_at=now + timedelta(seconds=policy["window_seconds"]), policy=policy,
        profile_version=household.profile_version if household else None,
        test_mode=_test_mode(), version=0, authority_side="cloud",
        parent_request_id=parent.id if parent else None)
    row.sos_text_full = text or cloud_sos_text(db, row, household)
    db.add(row)
    db.flush()
    _cloud_event(db, row, S.CREATED,
                 detail={"why": "device_offline" if not parent else "help_requested_later",
                         "incident_ids": row.incident_ids,
                         "incident_id": row.incident_ids[0] if row.incident_ids else None,
                         "incident_type": incident_type, "level": int(level),
                         "evidence_state": row.evidence_state,
                         "expires_at": _iso(row.expires_at),
                         "policy": {"window_seconds": policy["window_seconds"],
                                    "safety_defaults": policy["safety_defaults"],
                                    "contacts": [{"id": c["id"]} for c in
                                                 policy.get("contacts") or []]}})
    return row


def cloud_sos_text(db, row: AssistanceRequest, household: Household | None) -> str:
    """The SOS text when the cloud has to write it (the device normally
    renders it from the profile with ``sentrix/sos/templates.py``)."""
    owner = _owner(db, row.household_id)
    lines = [f"SENTRIX SOS {short_id(row)}: {WHAT.get(row.incident_type or 'unknown', 'an alert')}"
             f" at {owner.display_name if owner else 'the home'}."]
    if (row.flags or {}).get("device_offline"):
        lines.append("The SENTRIX device went offline during an open alert.")
    if household is not None:
        address = ", ".join(p for p in (household.house, household.building, household.street,
                                        household.locality, household.landmark, household.city,
                                        household.state, household.pincode) if p)
        if address:
            lines.append(f"ADDRESS: {address}")
        if household.lat is not None and household.lon is not None:
            lines.append(f"MAP: https://maps.google.com/?q={household.lat},{household.lon}")
    lines.append(FOOTER)
    return "\n".join(lines)


# ── device offline, tamper, sync ──────────────────────────────────────────────
def open_request_for(db, device_id: str) -> AssistanceRequest | None:
    return (db.query(AssistanceRequest)
            .filter(AssistanceRequest.device_id == device_id,
                    AssistanceRequest.state.in_(sorted(S.OPEN_STATES)))
            .order_by(AssistanceRequest.created_at.desc()).first())


def offline_opens_request(incident_type: str, level: int) -> bool:
    """Whether a lost heartbeat during an open incident (with no request yet)
    opens a cloud request: only what would have opened one on the device - an
    alarm class (weapon, fire, a fight; a fight reaches L3 only corroborated
    by video and aligned audio) at ``sos.min_level``+. An unrecognised person,
    an audio-only or an experimental L2 never does (D8 as amended by D15)."""
    sos = _cfg().sos
    return int(level) >= int(sos.min_level) and incident_type in tuple(sos.alarm_classes)


def on_device_offline(db, device: Device, now: datetime | None = None) -> AssistanceRequest | None:
    """The heartbeat was lost for 45 s. An open request becomes the cloud's
    (flag ``device_offline``; confirmed contacts at once under safety
    defaults). With no request but an open incident in the last heartbeat:
    an alarm class at L3+ opens one in the cloud (the tamper signal); any other
    L2+ incident (an unrecognised person, audio only, experimental) is a
    ``device.tamper`` audit and an owner notice only - no request, no contacts."""
    from sentrix_portal import security
    from sentrix_portal.services.hub import _incident_type
    heartbeat = device.last_heartbeat or {}
    alarm = heartbeat.get("alarm") if isinstance(heartbeat.get("alarm"), dict) else {}
    level = int(alarm.get("level") or 1) if str(alarm.get("level") or "1").isdigit() else 1
    incident_type = _incident_type(alarm.get("incident_type"))
    row = open_request_for(db, device.id)
    if row is None and level < 2:
        return None
    opens = row is None and offline_opens_request(incident_type, level)
    security.audit(db, "device.tamper", actor_type="system", household_id=device.household_id,
                   target_type="device", target_id=device.id,
                   detail={"level": level, "incident_id": alarm.get("incident_id"),
                           "incident_type": incident_type,
                           "request_id": row.id if row else None,
                           "opens_request": bool(row is not None or opens)})
    if row is None:
        if not device.household_id:
            return None
        if not opens:
            _notify_owner_offline(db, device, incident_type, level, alarm.get("incident_id"))
            return None
        row = create_cloud_request(
            db, device, device.household_id,
            incident_type=incident_type, level=level,
            incident_ids=[alarm.get("incident_id")] if alarm.get("incident_id") else [],
            flags={"device_offline": True})
        _cloud_event(db, row, S.PROMPTED, detail={"via": "cloud"})
    else:
        row.authority_side = "cloud"
        _cloud_event(db, row, S.DEVICE_OFFLINE, flags={"device_offline": True},
                     detail={"last_seen_at": _iso(device.last_seen_at)})
    policy = row.policy or {}
    if policy.get("safety_defaults", True) and not (row.flags or {}).get("contacts_notified"):
        _cloud_event(db, row, S.CONTACTS_NOTIFIED, flags={"contacts_notified": True},
                     detail={"why": "device_offline"})
    return row


def _notify_owner_offline(db, device: Device, incident_type: str, level: int,
                          incident_id: str | None) -> None:
    """The owner, and nobody else, hears that the device went silent during an
    open L2 (no request: D8 as amended by D15)."""
    from sentrix.alerting.channels.base import Message
    owner, recipients = _owner_recipients_for(db, device.household_id)
    if owner is None:
        return
    text = (f"SENTRIX: the device stopped reporting during an open level-{level} alert "
            f"({WHAT.get(incident_type, 'an alert')}). No assistance request was opened "
            f"and nobody else was alerted. Check the home or the device.\n{FOOTER}")
    step = f"offline_l2:{incident_id or device.id}"
    for recipient in recipients:
        _enqueue(db, recipient, Message(template="device.offline", step=step, text=text,
                                        subject="SENTRIX: device offline during an alert"))


def sync_messages(db, device_id: str) -> list[dict]:
    """``sos.sync`` for every request of the device the cloud acted on in the
    last 7 days (the device imports the cloud chain idempotently). The device
    is the authority again from here on."""
    since = utcnow() - timedelta(days=7)
    ids = {r[0] for r in db.query(RequestEvent.request_id)
           .join(AssistanceRequest, AssistanceRequest.id == RequestEvent.request_id)
           .filter(AssistanceRequest.device_id == device_id, RequestEvent.origin == "cloud",
                   AssistanceRequest.updated_at >= since).distinct()}
    # A takeover that wrote no event yet is handed back too.
    ids |= {r[0] for r in db.query(AssistanceRequest.id)
            .filter(AssistanceRequest.device_id == device_id,
                    AssistanceRequest.authority_side == "cloud")}
    messages = []
    for request_id in sorted(ids):
        row = db.get(AssistanceRequest, request_id)
        if row.authority_side == "cloud" and row.state in S.OPEN_STATES:
            _cloud_event(db, row, S.DEVICE_ONLINE, detail={"via": "reconnect"},
                         flags={"device_offline": False})
        side = row.authority_side or "device"
        row.authority_side = "device"
        events = [event_dict(e) for e in db.query(RequestEvent)
                  .filter_by(request_id=request_id, origin="cloud")
                  .order_by(RequestEvent.seq)]
        messages.append(protocol.envelope(
            protocol.SOS_SYNC, request_id=request_id, cloud_state=row.state,
            cloud_version=row.version or 0, authority_side=side, events=events))
    return messages


# ── answers ───────────────────────────────────────────────────────────────────
async def respond(db, request_id: str, action: str, *, actor: dict, channel: str,
                  authority: str | None = None, pin: str | None = None,
                  detail: dict | None = None) -> dict[str, Any]:
    """The one entry point for SOS answers (module docstring)."""
    from sentrix.actor import Actor
    from sentrix_portal.services import hub
    if db is None:
        return result("unsupported", "no database session")
    if action not in ACTIONS:
        return result("invalid_args", f"action must be one of {', '.join(ACTIONS)}")
    try:
        who = Actor.from_dict(actor)
    except ValueError as exc:
        return result("invalid_args", str(exc))
    row = db.get(AssistanceRequest, str(request_id))
    if row is None:
        return result("not_found", "unknown request")
    view = _view_for(who)
    refusal = _role_rule(who, action)
    if refusal is not None:
        return refusal
    if authority is not None:
        authority = str(authority).upper()
        if authority not in S.AUTHORITIES:
            return result("invalid_args", f"authority must be one of {', '.join(S.AUTHORITIES)}")
    if channel not in S.CHANNELS:
        channel = "portal"

    device_side = (row.authority_side or "device") == "device"
    online = row.device_id and hub.is_online(row.device_id) and device_side
    if not online and device_side and _heartbeat_alive(db, row):
        # Not connected right now, but the heartbeat is not lost yet (docs/7
        # §7.6): the device still runs this request's timers, so the cloud
        # must not take over on a reconnect blip.
        return result("timeout", "the device did not answer in time; try again",
                      request_view(row, view))
    if online:
        # Hold no write lock while waiting for the device: its answer arrives
        # with outbox events the hub stores on another connection.
        db.commit()
        if action in OWNER_ACTIONS:
            name, args = "sos.respond", {"request_id": row.id, "action": action,
                                         "authority": authority, "pin": pin,
                                         "channel": channel}
        else:
            name, args = "sos.update", {"request_id": row.id, "event": action,
                                        "detail": dict(detail or {}, channel=channel)}
        reply = await hub.send_command(row.device_id, name, args, who.to_dict(),
                                       timeout=10, queue_if_offline=False)
        if reply["code"] != "device_offline":
            if reply["ok"]:
                _portal_bookkeeping(db, row, action, who, detail)
            body = reply.get("result") if isinstance(reply.get("result"), dict) else {}
            return result(reply["code"], reply.get("message", ""),
                          body.get("request") or request_view(row, view))
        db.refresh(row)                           # the device left meanwhile
        if _heartbeat_alive(db, row):
            return result("timeout", "the device did not answer in time; try again",
                          request_view(row, view))
    outcome = _cloud_respond(db, row, action, who, channel, authority, pin, detail)
    if outcome["ok"]:
        _portal_bookkeeping(db, row, action, who, detail)
    return outcome


def _heartbeat_alive(db, row: AssistanceRequest) -> bool:
    """The device is not connected but has not lost its heartbeat yet
    (``Device.online`` is cleared by the hub's watcher after 45 s)."""
    if not row.device_id:
        return False
    device = db.get(Device, row.device_id)
    return bool(device is not None and device.online)


def _view_for(who) -> str:
    return {"operator": "desk", "contact": "contact"}.get(who.type, "owner")


def _role_rule(who, action: str) -> dict | None:
    if who.type in ("owner", "member"):
        if action in OWNER_ACTIONS:
            return None
        return result("forbidden_role", "only the Desk or a contact can do that")
    if who.type == "operator":
        if action in ("acknowledge", "resolve"):
            return None
        return result("forbidden_role", "the Desk can acknowledge and resolve only")
    if who.type == "contact":
        if action in ("approve", "acknowledge"):
            return None
        return result("forbidden_role", "a contact can ask for help or take it on only")
    if who.type == "cloud" and action in ("acknowledge", "resolve"):
        return None
    return result("forbidden_role", f"{who.type} cannot answer a request")


def _portal_bookkeeping(db, row: AssistanceRequest, action: str, who, detail) -> None:
    from sentrix_portal import security
    if who.type == "operator" and action == "acknowledge" and row.desk_acknowledged_at is None:
        row.desk_operator_id = who.id
        row.desk_acknowledged_at = utcnow()
        security.audit(db, "desk.acknowledged", actor_type="user", actor_id=who.id,
                       household_id=row.household_id, target_type="request", target_id=row.id)
    if who.type == "operator" and action == "resolve":
        outcome = (detail or {}).get("outcome")
        row.outcome = outcome if outcome in S.OUTCOMES else row.outcome
        security.audit(db, "desk.resolved", actor_type="user", actor_id=who.id,
                       household_id=row.household_id, target_type="request", target_id=row.id,
                       detail={"outcome": row.outcome,
                               "note": str((detail or {}).get("note") or "")[:500]})
        verdict = S.OUTCOMES.get(row.outcome or "")
        for incident_id in row.incident_ids or []:
            mirror = db.get(IncidentMirror, incident_id)
            if mirror is not None and verdict:
                mirror.verdict, mirror.verdict_by = verdict, who.ref
                mirror.verdict_at = utcnow()
    db.flush()


def _pin_ok(household_id: str) -> bool:
    with _pin_lock:
        failures = _pin_failures[household_id]
        now = time.monotonic()
        while failures and now - failures[0] > PIN_WINDOW:
            failures.popleft()
        return len(failures) < PIN_ATTEMPTS


def _pin_failed(household_id: str) -> None:
    with _pin_lock:
        _pin_failures[household_id].append(time.monotonic())


def _locked_check_ok(household_id: str) -> bool:
    """While locked: may one more PIN be tried as the duress PIN?"""
    with _pin_lock:
        hits = _locked_checks[household_id]
        now = time.monotonic()
        while hits and now - hits[0] > PIN_WINDOW:
            hits.popleft()
        if len(hits) >= LOCKED_CHECKS:
            return False
        hits.append(now)
        return True


def reset_pin_limiter() -> None:
    """Tests only."""
    with _pin_lock:
        _pin_failures.clear()
        _locked_checks.clear()


def _check_pin(db, row: AssistanceRequest, pin: str | None) -> str:
    """"ok", "duress", "invalid", "locked" or "none" (no PIN configured)."""
    from sentrix_portal import security
    household = db.get(Household, row.household_id) if row.household_id else None
    if household is None or not household.sos_pin_hash:
        return "none"
    if not _pin_ok(household.id):
        # Help is never blocked (D9): the duress PIN still works while locked.
        if (pin and household.duress_pin_hash and _locked_check_ok(household.id)
                and security.verify_pin(household.duress_pin_hash, pin)):
            return "duress"
        return "locked"
    if pin and security.verify_pin(household.sos_pin_hash, pin):
        return "ok"
    if pin and household.duress_pin_hash and security.verify_pin(household.duress_pin_hash, pin):
        return "duress"
    _pin_failed(household.id)
    return "invalid"


def _contact_allowed(db, row: AssistanceRequest, who) -> bool:
    """A contact may ask for help only under the request's frozen safety
    defaults, and only if the policy lists them as CONFIRMED."""
    policy = row.policy or {}
    if not policy.get("safety_defaults"):
        return False
    return who.id in _policy_contact_ids(db, row)


def _policy_contact_ids(db, row: AssistanceRequest) -> set[str]:
    policy = row.policy or {}
    listed = policy.get("contacts") or policy.get("confirmed_contacts")
    ids: set[str] = set()
    if isinstance(listed, list):
        for item in listed:
            if isinstance(item, dict) and item.get("consent_status", "CONFIRMED") == "CONFIRMED":
                ids.add(str(item.get("id")))
            elif isinstance(item, str):
                ids.add(item)
        return ids
    from sentrix_portal.services import accounts
    return {c.id for c in accounts.confirmed_contacts(db, row.household_id)}


def _cloud_respond(db, row: AssistanceRequest, action: str, who, channel: str,
                   authority: str | None, pin: str | None, detail: dict | None) -> dict:
    previous_side, before = row.authority_side, row.version or 0
    outcome = _cloud_answer(db, row, action, who, channel, authority, pin, detail)
    if (row.version or 0) != before or row.authority_side == "cloud":
        row.authority_side = "cloud"           # the cloud wrote the answer
    else:
        row.authority_side = previous_side     # nothing written: no takeover
    return outcome


def _cloud_answer(db, row: AssistanceRequest, action: str, who, channel: str,
                  authority: str | None, pin: str | None, detail: dict | None) -> dict:
    view = _view_for(who)
    actor_type, actor_id = who.event_actor_type, who.ref
    if action == "approve":
        if who.type == "contact" and not _contact_allowed(db, row, who):
            return result("forbidden_role", "this request does not let contacts ask for help",
                          request_view(row, view))
        if row.state in S.HELP_PATH:
            return result("ok", "help is already on its way", request_view(row, view))
        if _cloud_event(db, row, S.APPROVE, actor_type=actor_type, actor_id=actor_id,
                        channel=channel, detail={"authority": authority},
                        authority=authority or _default_authority(row),
                        responded_at=utcnow(), responded_by=actor_id,
                        response_channel=channel) is None:
            return result("conflict", f"cannot ask for help in {row.state}",
                          request_view(row, view))
        _cloud_event(db, row, S.DELIVERED, detail={"target": "desk"})
        return result("ok", "", request_view(row, view))
    if action == "checking":
        if _cloud_event(db, row, S.CHECKING, actor_type=actor_type, actor_id=actor_id,
                        channel=channel, seen_at=row.seen_at or utcnow()) is None:
            return result("conflict", f"not waiting for an answer ({row.state})",
                          request_view(row, view))
        return result("ok", "", request_view(row, view))
    if action in ("decline", "safe"):
        verdict = _check_pin(db, row, pin)
        if verdict == "locked":
            return result("pin_locked", "too many wrong PINs; try again in 10 minutes",
                          request_view(row, view))
        if verdict == "none":
            return result("pin_invalid", "no SOS PIN is set for this home",
                          request_view(row, view))
        if verdict == "invalid":
            _cloud_event(db, row, S.PIN_FAILED, actor_type=actor_type, actor_id=actor_id,
                         channel=channel)
            return result("pin_invalid", "wrong PIN", request_view(row, view))
        if verdict == "duress":
            if row.state in S.PROMPTING_STATES:
                _cloud_event(db, row, S.APPROVE, actor_type=actor_type, actor_id=actor_id,
                             channel=channel, detail={"duress": True}, restricted=True,
                             flags={"duress": True}, authority=_default_authority(row),
                             responded_at=utcnow(), responded_by=actor_id,
                             response_channel=channel)
                _cloud_event(db, row, S.DELIVERED, detail={"target": "desk"},
                             restricted=True)
            elif row.state in S.OPEN_STATES and not (row.flags or {}).get("duress"):
                # Help path: the duress banner to the Desk and the contacts.
                _cloud_event(db, row, S.CONTACTS_NOTIFIED, actor_type=actor_type,
                             actor_id=actor_id, channel=channel, restricted=True,
                             flags={"duress": True, "contacts_notified": True},
                             detail={"step": S.STEP_DURESS, "duress": True, "via": action})
            elif row.state in S.TERMINAL_STATES:
                # Closed already: a silent linked request, approved at once.
                device = db.get(Device, row.device_id) if row.device_id else None
                new = create_cloud_request(db, device, row.household_id,
                                           incident_type=row.incident_type or "unknown",
                                           level=int(row.peak_level or 3),
                                           incident_ids=list(row.incident_ids or []),
                                           parent=row, flags={"duress": True},
                                           text=row.sos_text_full)
                _cloud_event(db, new, S.APPROVE, actor_type=actor_type, actor_id=actor_id,
                             channel=channel, detail={"duress": True}, restricted=True,
                             flags={"duress": True}, authority=_default_authority(new),
                             responded_at=utcnow(), responded_by=actor_id,
                             response_channel=channel)
                _cloud_event(db, new, S.DELIVERED, detail={"target": "desk"},
                             restricted=True)
            # The person forcing the resident sees the same answer as a decline.
            return result("ok", "", request_view(row, view))
        event = S.DECLINE if action == "decline" else S.RESIDENT_SAFE
        flags = {"resident_safe": True} if action == "safe" else None
        if _cloud_event(db, row, event, actor_type=actor_type, actor_id=actor_id,
                        channel=channel, flags=flags, responded_at=utcnow(),
                        responded_by=actor_id, response_channel=channel) is None:
            return result("conflict", f"cannot {action} in {row.state}",
                          request_view(row, view))
        return result("ok", "", request_view(row, view))
    if action == "help_now":
        if (row.flags or {}).get("duress"):
            # Looks declined to the household: answer as a declined request
            # would, and page the Desk and contacts again with the banner.
            _cloud_event(db, row, S.NOTIFICATION, actor_type=actor_type, actor_id=actor_id,
                         channel=channel, restricted=True,
                         detail={"step": f"help_now{int(row.repage_count or 0) + 1}",
                                 "duress": True})
            return result("ok", "Help requested", request_view(row, view))
        if row.state not in (S.EXPIRED, S.DECLINED, S.CANCELLED):
            return result("conflict", "this request is still open", request_view(row, view))
        _cloud_event(db, row, S.HELP_REQUESTED_LATER, actor_type=actor_type,
                     actor_id=actor_id, channel=channel)
        device = db.get(Device, row.device_id) if row.device_id else None
        new = create_cloud_request(db, device, row.household_id,
                                   incident_type=row.incident_type or "unknown",
                                   level=int(row.peak_level or 3),
                                   incident_ids=list(row.incident_ids or []), parent=row,
                                   text=row.sos_text_full)
        _cloud_event(db, new, S.APPROVE, actor_type=actor_type, actor_id=actor_id,
                     channel=channel, authority=authority or _default_authority(new),
                     responded_at=utcnow(), responded_by=actor_id, response_channel=channel)
        _cloud_event(db, new, S.DELIVERED, detail={"target": "desk"})
        return result("ok", "", request_view(new, view))
    if action == "acknowledge":
        if row.state == S.ACKNOWLEDGED:
            return result("ok", "already acknowledged", request_view(row, view))
        if _cloud_event(db, row, S.ACKNOWLEDGE, actor_type=actor_type, actor_id=actor_id,
                        channel=channel, detail={"by": who.name}) is None:
            return result("conflict", f"cannot acknowledge in {row.state}",
                          request_view(row, view))
        return result("ok", "", request_view(row, view))
    if action == "resolve":
        outcome = (detail or {}).get("outcome")
        if outcome not in S.OUTCOMES:
            return result("invalid_args", f"outcome must be one of {', '.join(S.OUTCOMES)}")
        if _cloud_event(db, row, S.RESOLVE, actor_type=actor_type, actor_id=actor_id,
                        channel=channel, outcome=outcome,
                        detail={"outcome": outcome,
                                "note": str((detail or {}).get("note") or "")[:500]}) is None:
            return result("conflict", f"cannot resolve in {row.state}", request_view(row, view))
        return result("ok", "", request_view(row, view))
    return result("unsupported", action)


def _default_authority(row: AssistanceRequest) -> str:
    return "FIRE" if row.incident_type == "fire" else "POLICE"


async def mark_seen(db, request_id: str, actor: dict) -> None:
    """The owner opened the request: ``seen`` (pauses the unseen-SMS rule)."""
    from sentrix_portal.services import hub
    row = db.get(AssistanceRequest, request_id)
    if row is None or row.seen_at is not None or row.state not in S.OPEN_STATES:
        return
    row.seen_at = utcnow()
    if row.device_id and hub.is_online(row.device_id) and row.authority_side != "cloud":
        db.commit()
        await hub.send_command(row.device_id, "sos.update",
                               {"request_id": row.id, "event": "seen",
                                "detail": {"channel": "portal"}}, actor, timeout=5,
                               queue_if_offline=True)
        return
    _cloud_event(db, row, S.SEEN, actor_type="owner", actor_id=None, channel="portal")


#: Portal outbox statuses the device hears about (it derives ``delivered`` /
#: ``undelivered`` itself; SUPPRESSED, a dry run, never counts as delivered).
NOTIFIED_STATUSES = frozenset({"SENT", "DELIVERED", "FAILED", "SUPPRESSED"})


def notification_status(db, *, request_id: str | None, channel: str, recipient_ref: str,
                        step: str, status: str, error: str | None = None) -> str | None:
    """Delivery feedback from the Portal's channels (T-CH's outbox worker
    calls this after a status change of an SOS message). Online, the device
    gets ``sos.update notification`` (queued if it drops meanwhile); offline,
    the cloud records the ``notification`` event in its own chain. Returns
    "device", "cloud" or None (not an SOS message, or not a final status)."""
    from sentrix_portal.services import hub
    if not request_id or status not in NOTIFIED_STATUSES:
        return None
    row = db.get(AssistanceRequest, request_id)
    if row is None:
        return None
    detail = {"channel": channel, "recipient_ref": recipient_ref, "step": step,
              "status": status, "error": (error or "")[:200] or None}
    current = hub.current_hub()
    if (row.device_id and current is not None and current.online(row.device_id)
            and row.authority_side != "cloud"):
        current.schedule(current.send_command(
            row.device_id, "sos.update", {"request_id": row.id, "event": "notification",
                                          "detail": detail}, hub._cloud_actor(), timeout=10,
            queue_if_offline=True))
        return "device"
    _cloud_event(db, row, S.NOTIFICATION, detail=detail)
    return "cloud"


async def record_note(db, request_id: str, *, actor: dict, detail: dict) -> dict:
    """A Desk note that changes no state ("Called 112 (simulated)"): a
    ``notification`` event on the device (online) or in the cloud chain."""
    from sentrix.actor import Actor
    from sentrix_portal import security
    from sentrix_portal.services import hub
    row = db.get(AssistanceRequest, request_id)
    if row is None:
        return result("not_found", "unknown request")
    who = Actor.from_dict(actor)
    security.audit(db, "desk.note", actor_type="user", actor_id=who.id,
                   household_id=row.household_id, target_type="request", target_id=row.id,
                   detail=detail)
    if row.device_id and hub.is_online(row.device_id) and row.authority_side != "cloud":
        db.commit()
        reply = await hub.send_command(row.device_id, "sos.update",
                                       {"request_id": row.id, "event": "notification",
                                        "detail": detail}, who.to_dict(), timeout=10,
                                       queue_if_offline=True)
        if reply["ok"] or reply.get("queued"):
            return result("ok", "", request_view(row, "desk"))
        return result(reply["code"], reply["message"], request_view(row, "desk"))
    _cloud_event(db, row, S.NOTIFICATION, actor_type=who.event_actor_type, actor_id=who.ref,
                 channel="desk", detail=detail)
    return result("ok", "", request_view(row, "desk"))


# ── who gets which message ────────────────────────────────────────────────────
def short_id(row: AssistanceRequest) -> str:
    return f"SX-{row.id[:6].upper()}"


def _owner(db, household_id: str | None) -> User | None:
    if not household_id:
        return None
    return (db.query(User).filter_by(household_id=household_id, role="owner",
                                     status="active").order_by(User.created_at).first())


def _room(db, row: AssistanceRequest) -> str:
    camera = (db.query(Camera).filter_by(device_id=row.device_id).first()
              if row.device_id else None)
    return camera.room_label if camera and camera.room_label else "the camera's room"


def _evidence(row: AssistanceRequest) -> str:
    if row.evidence_state == "corroborated":
        return "confirmed by two different models"
    if row.evidence_state == "confirmed":
        return "possible (one camera check)"
    return "not confirmed by a camera check"


def _base_url() -> str:
    from sentrix_portal import settings as portal_settings
    return portal_settings.current().base_url.rstrip("/")


def mint_token(db, *, purpose: str, row: AssistanceRequest, subject_type: str,
               subject_id: str | None, nonce: bool = False) -> tuple[str, ActionToken]:
    """A single-use link token bound to request + recipient (only its digest
    is stored). It expires at the latest 24 h + 2 h from now; closing the
    request shortens it to close + 2 h, a decline revokes contact pages."""
    from sentrix.alerting.channels.base import new_callback_nonce
    from sentrix_portal import security
    raw = security.new_token(24)
    cfg = _cfg().sos
    token = ActionToken(token_hash=security.token_digest(raw), purpose=purpose,
                        household_id=row.household_id, request_id=row.id,
                        subject_type=subject_type, subject_id=subject_id,
                        nonce=new_callback_nonce() if nonce else None, created_at=utcnow(),
                        expires_at=utcnow() + timedelta(seconds=cfg.stale_close_seconds
                                                        + cfg.link_grace_seconds))
    db.add(token)
    db.flush()
    return raw, token


def resolve_token(db, raw: str | None, purposes: tuple[str, ...]) -> ActionToken | None:
    """The live token row for ``raw`` (any of ``purposes``), else None."""
    from sentrix_portal import security
    if not raw or len(raw) > 200:
        return None
    token = db.query(ActionToken).filter_by(token_hash=security.token_digest(raw)).first()
    if token is None or token.purpose not in purposes or token.revoked_at is not None:
        return None
    if _aware(token.expires_at) <= utcnow():
        return None
    return token


def _close_tokens(db, row: AssistanceRequest, *, revoke_contacts: bool) -> None:
    grace = timedelta(seconds=_cfg().sos.link_grace_seconds)
    limit = utcnow() + grace
    for token in db.query(ActionToken).filter_by(request_id=row.id, revoked_at=None):
        if revoke_contacts and token.purpose == "contact_page":
            token.revoked_at = utcnow()
        elif _aware(token.expires_at) > limit:
            token.expires_at = limit


def _owner_recipients(db, row: AssistanceRequest):
    return _owner_recipients_for(db, row.household_id)


def _owner_recipients_for(db, household_id: str | None):
    from sentrix.alerting.channels.base import Recipient
    owner = _owner(db, household_id)
    household = db.get(Household, household_id) if household_id else None
    if owner is None:
        return None, []
    channels = dict((household.channels if household else None) or {})
    out = []
    if channels.get("telegram", True):
        link = (db.query(TelegramLink).filter_by(subject_type="user", subject_id=owner.id,
                                                 status="active").first())
        if link is not None and link.chat_id:
            out.append(Recipient("owner", owner.display_name or owner.username, "telegram",
                                 f"tg:{link.chat_id}", "owner"))
    if channels.get("email", True) and owner.email:
        out.append(Recipient("owner", owner.display_name or owner.username, "email",
                             owner.email, "owner"))
    return owner, out


def _contact_recipients(db, row: AssistanceRequest):
    from sentrix.alerting.channels.base import Recipient
    ids = _policy_contact_ids(db, row)
    contacts = (db.query(EmergencyContact)
                .filter(EmergencyContact.household_id == row.household_id,
                        EmergencyContact.consent_status == "CONFIRMED")
                .order_by(EmergencyContact.priority).all())
    out = []
    for contact in contacts:
        if contact.id not in ids:
            continue
        recipients = []
        chat = contact.telegram_chat_id
        if not chat:
            link = (db.query(TelegramLink).filter_by(subject_type="contact",
                                                     subject_id=contact.id,
                                                     status="active").first())
            chat = link.chat_id if link else None
        if chat:
            recipients.append(Recipient(f"contact:{contact.id}", contact.name, "telegram",
                                        f"tg:{chat}", "contact"))
        if contact.email:
            recipients.append(Recipient(f"contact:{contact.id}", contact.name, "email",
                                        contact.email, "contact"))
        out.append((contact, recipients))
    return out


def _enqueue(db, recipient, message) -> None:
    from sentrix_portal.services import notify
    try:
        notify.enqueue(db, recipient, message)
    except NotImplementedError:
        pass
    except Exception as exc:                  # a channel problem never breaks the mirror
        log.error("message %s to %s not queued: %s", message.step, recipient.ref, exc)


def _send_owner(db, row: AssistanceRequest, *, step: str, template: str, text: str,
                subject: str, with_buttons: bool = False) -> None:
    from sentrix.alerting.channels.base import Button, Message, callback_data
    owner, recipients = _owner_recipients(db, row)
    if owner is None:
        return
    page = f"{_base_url()}/sos/{row.id}"
    urgency = row.urgency or "NORMAL"
    for recipient in recipients:
        buttons: tuple = ()
        body = text
        html_body = None
        if with_buttons and recipient.channel == "telegram":
            _, token = mint_token(db, purpose="action_link", row=row, subject_type="user",
                                  subject_id=owner.id, nonce=True)
            buttons = (Button("Alert my contacts and the SENTRIX desk", "approve",
                              callback_data=callback_data(token.nonce, "approve")),
                       Button("I'm checking", "checking",
                              callback_data=callback_data(token.nonce, "checking")),
                       Button("False alarm (PIN)", "decline_page", url=page))
        elif with_buttons and recipient.channel == "email":
            raw, _ = mint_token(db, purpose="action_link", row=row, subject_type="user",
                                subject_id=owner.id)
            action = f"{_base_url()}/a/{raw}"
            body = f"{text}\n\nAnswer without signing in: {action}\nOpen the request: {page}"
            html_body = _html(text, [("Answer now", action), ("Open the request", page)])
        elif recipient.channel == "email":
            html_body = _html(text, [("Open the request", page)])
        _enqueue(db, recipient, Message(template=template, step=step, text=body,
                                        request_id=row.id, subject=subject, html=html_body,
                                        buttons=buttons, urgency=urgency))


def _send_contacts(db, row: AssistanceRequest, *, step: str, template: str,
                   text_for, subject: str, with_page: bool = True) -> None:
    from sentrix.alerting.channels.base import Button, Message, callback_data
    for contact, recipients in _contact_recipients(db, row):
        if not recipients:
            continue
        raw = token = None
        if with_page:
            raw, token = mint_token(db, purpose="contact_page", row=row,
                                    subject_type="contact", subject_id=contact.id, nonce=True)
        page = f"{_base_url()}/c/{raw}" if raw else None
        for recipient in recipients:
            short, full = text_for(contact, page)
            buttons: tuple = ()
            html_body = None
            if recipient.channel == "telegram":
                text = short
                if token is not None and row.state in DESK_STATES | S.PROMPTING_STATES:
                    buttons = (Button("I'm handling it", "acknowledge",
                                      callback_data=callback_data(token.nonce, "acknowledge")),
                               Button("Open", "open", url=page))
            else:
                text = full
                html_body = _html(full, [("Open the SOS page", page)] if page else [])
            _enqueue(db, recipient, Message(template=template, step=step, text=text,
                                            request_id=row.id, subject=subject,
                                            html=html_body, buttons=buttons,
                                            urgency="HIGH"))


def _html(text: str, links: list[tuple[str, str]]) -> str:
    body = "".join(f"<p>{html.escape(line)}</p>" for line in text.splitlines() if line)
    body += "".join(f'<p><a href="{html.escape(url)}">{html.escape(label)}</a></p>'
                    for label, url in links if url)
    return f"<div>{body}</div>"


def _after_event(db, row: AssistanceRequest, ev: dict, origin: str) -> list[tuple[str, dict]]:
    """Messages for one applied event, and device commands for the hub."""
    event = ev["event"]
    followups: list[tuple[str, dict]] = []
    flags = row.flags or {}
    duress = bool(flags.get("duress"))
    what = WHAT.get(row.incident_type or "unknown", "an alert")
    short = short_id(row)
    page = f"{_base_url()}/sos/{row.id}"
    owner = _owner(db, row.household_id)
    owner_name = owner.display_name if owner else "the resident"
    room = _room(db, row)

    def sos_texts(contact, contact_page):
        link = f" Open: {contact_page}." if contact_page else ""
        if duress:
            short_text = f"SENTRIX SOS {short} - DURESS: {DURESS_BANNER}{link} {FOOTER}"
            return short_text, (row.sos_text_full or short_text) + \
                f"\n\nDURESS: {DURESS_BANNER}\n{FOOTER}"
        short_text = (f"SENTRIX SOS {short}: {owner_name} may need help ({what}, "
                      f"{_evidence(row)}).{link} {FOOTER}")
        full = row.sos_text_full or cloud_sos_text(db, row, db.get(Household, row.household_id))
        return short_text, f"{full}\n\n{contact_page or ''}\n{FOOTER}".strip()

    if event == S.PROMPTED:
        minutes = max(1, int(((_aware(row.expires_at) or utcnow()) - utcnow())
                             .total_seconds() // 60))
        _send_owner(db, row, step=S.STEP_T0, template="sos.prompt", with_buttons=True,
                    subject=f"SENTRIX alert {short}: {what}",
                    text=(f"SENTRIX alert {short}: {what} in {room}, {_evidence(row)}, "
                          f"level {row.peak_level or row.trigger_level}. Do you need "
                          f"assistance? Answer within {minutes} min: {page}. {FOOTER}"))
    elif event == S.REMINDER:
        step = (ev.get("detail") or {}).get("step") or _reminder_step(row)
        _send_owner(db, row, step=str(step), template="sos.reminder", with_buttons=True,
                    subject=f"SENTRIX reminder {short}",
                    text=(f"SENTRIX reminder {short}: {what} in {room} is still waiting "
                          f"for your answer: {page}. {FOOTER}"))
    elif event == S.LAPSE_WARNING:
        _send_owner(db, row, step=S.STEP_LAPSE, template="sos.lapse_warning",
                    with_buttons=True, subject=f"SENTRIX {short} lapses in 5 minutes",
                    text=(f"SENTRIX {short}: the request lapses in 5 minutes unanswered: "
                          f"{page}. {FOOTER}"))
    elif event == S.LEVEL_RISE:
        level = int((ev.get("detail") or {}).get("level") or row.peak_level or 4)
        step = S.RISE_STEPS.get(level, f"rise{level}")
        _send_owner(db, row, step=step, template="sos.reminder", with_buttons=True,
                    subject=f"SENTRIX {short}: level {level}",
                    text=(f"SENTRIX {short}: the alert rose to level {level} ({what}, "
                          f"{_evidence(row)}): {page}. {FOOTER}"))
    elif event == S.CONTACTS_NOTIFIED:
        # A duress PIN on the help path: the duress banner, as a new step.
        _send_contacts(db, row, step=S.STEP_DURESS if duress else S.STEP_CONTACTS_T0,
                       template="sos.duress" if duress else "sos.full",
                       text_for=sos_texts, subject=f"SENTRIX SOS {short}")
    elif event in (S.APPROVE, S.RETRY):
        _send_contacts(db, row, step=S.STEP_DURESS if duress else S.STEP_CONTACTS_T0,
                       template="sos.duress" if duress else "sos.full",
                       text_for=sos_texts, subject=f"SENTRIX SOS {short}")
        if origin == "device" and row.authority_side != "cloud":
            # The request is in the Desk queue: that counts as delivered,
            # even in a dry run (docs/7 §7.11).
            followups.append(("sos.update", {"request_id": row.id, "event": "delivered",
                                             "detail": {"target": "desk"}}))
    elif event == S.ACKNOWLEDGE and not duress:
        who = (ev.get("detail") or {}).get("by") or "the SENTRIX desk"
        _send_owner(db, row, step="acknowledged", template="sos.resolved",
                    subject=f"SENTRIX {short} acknowledged",
                    text=f"SENTRIX {short}: acknowledged by {who}. {FOOTER}")
    elif event == S.EXPIRE:
        _send_owner(db, row, step=S.STEP_LAPSED, template="sos.lapsed",
                    subject=f"SENTRIX {short} lapsed unanswered",
                    text=(f"SENTRIX {short}: the request lapsed unanswered. Nobody was told "
                          f"the home is safe; please review it: {page}. {FOOTER}"))
        if flags.get("contacts_notified"):
            _send_contacts(db, row, step=S.STEP_LAPSED, template="sos.lapsed",
                           with_page=False, subject=f"SENTRIX {short} lapsed",
                           text_for=lambda c, p: ((f"SENTRIX: the alert at {owner_name}'s home "
                                                   f"({short}) lapsed unanswered. {FOOTER}"),) * 2)
        _close_tokens(db, row, revoke_contacts=False)
    elif event in (S.DECLINE, S.CANCEL):
        if flags.get("contacts_notified"):
            _send_contacts(db, row, step=S.STEP_RESOLVED, template="sos.resolved",
                           with_page=False, subject=f"SENTRIX {short} resolved",
                           text_for=lambda c, p: ((f"SENTRIX: the alert at {owner_name}'s home "
                                                   f"({short}) was resolved by the resident. "
                                                   f"{FOOTER}"),) * 2)
        _close_tokens(db, row, revoke_contacts=True)
    elif event in (S.RESOLVE, S.STALE_CLOSE):
        label = OUTCOME_LABELS.get(row.outcome or "", "closed")
        if not duress:
            _send_owner(db, row, step=S.STEP_RESOLVED, template="sos.resolved",
                        subject=f"SENTRIX {short} closed",
                        text=f"SENTRIX {short}: closed by the SENTRIX desk ({label}). {FOOTER}")
        _send_contacts(db, row, step=S.STEP_RESOLVED, template="sos.resolved",
                       with_page=False, subject=f"SENTRIX {short} closed",
                       text_for=lambda c, p: ((f"SENTRIX: the alert at {owner_name}'s home "
                                               f"({short}) was closed ({label}). {FOOTER}"),) * 2)
        _close_tokens(db, row, revoke_contacts=False)
    elif event == S.DEVICE_OFFLINE:
        _send_owner(db, row, step="offline", template="sos.prompt", with_buttons=True,
                    subject=f"SENTRIX {short}: device offline",
                    text=(f"SENTRIX {short}: your device went offline during an open alert. "
                          f"The SENTRIX Portal keeps the request open: {page}. {FOOTER}"))
    elif event == S.REPAGE:
        n = int((ev.get("detail") or {}).get("n") or (row.repage_count or 1))
        _send_contacts(db, row, step=S.repage_step(n),
                       template="sos.duress" if duress else "sos.full",
                       text_for=sos_texts, subject=f"SENTRIX SOS {short} (reminder {n})")
    elif event == S.CREATED and origin == "cloud" and not row.parent_request_id:
        pass                                   # the prompt follows with "prompted"
    if row.state in S.TERMINAL_STATES and event not in (S.DECLINE, S.CANCEL, S.EXPIRE,
                                                        S.RESOLVE, S.STALE_CLOSE):
        _close_tokens(db, row, revoke_contacts=False)
    return followups


def _reminder_step(row: AssistanceRequest) -> str:
    created = _aware(row.created_at) or utcnow()
    elapsed = (utcnow() - created).total_seconds()
    offsets = [o for o in _cfg().sos.reminder_offsets_seconds if o <= elapsed + 1]
    return S.reminder_step(offsets[-1] if offsets else _cfg().sos.reminder_offsets_seconds[0])


# ── cloud timers ──────────────────────────────────────────────────────────────
def _done_steps(db, row: AssistanceRequest) -> set[tuple[str, str | None]]:
    return {(e.event, (e.detail or {}).get("step"))
            for e in db.query(RequestEvent).filter_by(request_id=row.id)}


def run_timers(db, now: datetime | None = None) -> int:
    """One pass of the cloud's timers over the requests it is the authority
    for. Returns the number of events written."""
    cfg = _cfg().sos
    now = now or utcnow()
    written = 0
    rows = (db.query(AssistanceRequest)
            .filter(AssistanceRequest.authority_side == "cloud",
                    AssistanceRequest.state.in_(sorted(S.OPEN_STATES))).all())
    for row in rows:
        created = _aware(row.created_at) or now
        expires = _aware(row.expires_at)
        done = _done_steps(db, row)
        if row.state in S.PROMPTING_STATES:
            if row.state == S.DRAFTED:
                written += _cloud_event(db, row, S.PROMPTED, detail={"via": "timer"}) is not None
            if expires is not None and now >= expires:
                written += _cloud_event(db, row, S.EXPIRE, detail={"via": "timer"}) is not None
                continue
            if (expires is not None and now >= expires - timedelta(seconds=cfg.lapse_warning_seconds)
                    and (S.LAPSE_WARNING, S.STEP_LAPSE) not in done):
                written += _cloud_event(db, row, S.LAPSE_WARNING,
                                        detail={"step": S.STEP_LAPSE}) is not None
            checking = _last_checking(db, row)
            snoozed = checking is not None and \
                now < checking + timedelta(seconds=cfg.seen_snooze_seconds)
            for offset in cfg.reminder_offsets_seconds:
                step = S.reminder_step(offset)
                due = created + timedelta(seconds=offset)
                if snoozed:
                    break                         # "I'm checking" pauses reminders only
                if now >= due and (S.REMINDER, step) not in done:
                    if expires is None or due < expires - timedelta(seconds=cfg.lapse_warning_seconds):
                        written += _cloud_event(db, row, S.REMINDER,
                                                detail={"step": step}) is not None
                    done.add((S.REMINDER, step))
            continue
        if row.state == S.APPROVED:
            written += _cloud_event(db, row, S.DELIVERED, detail={"target": "desk"}) is not None
            continue
        started = _aware(row.responded_at) or created
        if row.state in (S.DISPATCHED, S.ACKNOWLEDGED) and \
                now - started >= timedelta(seconds=cfg.stale_close_seconds):
            written += _cloud_event(db, row, S.STALE_CLOSE, flags={"stale": True}) is not None
            continue
        if row.state in (S.DISPATCHED, S.FAILED):
            cap = cfg.repage_cap_test_mode if _test_mode() else cfg.repage_cap
            count = int(row.repage_count or 0)
            if count >= cap:
                if not (row.flags or {}).get("unacknowledged"):
                    flags = dict(row.flags or {})
                    flags["unacknowledged"] = True
                    row.flags = flags
                    _send_owner(db, row, step="unacknowledged", template="sos.reminder",
                                subject=f"SENTRIX {short_id(row)} not acknowledged",
                                text=(f"SENTRIX {short_id(row)}: nobody has acknowledged the "
                                      f"request yet. Call {_call_text()} if you need help. "
                                      f"{FOOTER}"))
                continue
            if now >= started + timedelta(seconds=_repage_offset(count + 1)):
                # One re-page for the latest one due (a late takeover does not
                # burst every missed re-page, one per sweep).
                n = count + 1
                while n < cap and now >= started + timedelta(seconds=_repage_offset(n + 1)):
                    n += 1
                row.repage_count = n
                written += _cloud_event(db, row, S.REPAGE,
                                        detail={"n": n, "collapsed": n - count - 1}) is not None
    return written


def _last_checking(db, row: AssistanceRequest) -> datetime | None:
    last = (db.query(RequestEvent).filter_by(request_id=row.id, event=S.CHECKING)
            .order_by(RequestEvent.at.desc()).first())
    return _aware(last.at) if last is not None else None


def _repage_offset(n: int) -> float:
    cfg = _cfg().sos
    offsets = list(cfg.repage_offsets_seconds)
    if n <= len(offsets):
        return float(offsets[n - 1])
    return float(offsets[-1] + (n - len(offsets)) * cfg.repage_interval_seconds)


def start(app):
    """Background: cloud timers for requests whose device is offline."""
    period = max(1.0, float(_cfg().sos.sweep_interval_seconds))

    async def loop() -> None:
        while True:
            try:
                await asyncio.to_thread(_timers_once, app)
            except Exception as exc:
                log.error("cloud SOS timers: %s", exc, exc_info=True)
            await asyncio.sleep(period)

    task = asyncio.ensure_future(loop())
    return task.cancel


def _timers_once(app) -> int:
    with app.state.db.session() as db:
        return run_timers(db)


__all__ = ["ACTIONS", "respond", "open_requests", "start", "ingest_device_event",
           "on_device_offline", "sync_messages", "run_timers", "request_view",
           "events_view", "desk_queue", "mint_token", "resolve_token", "mark_seen",
           "record_note", "create_cloud_request", "verify_chains", "chain_hash",
           "event_dict", "consent_active", "owner_actions", "DESK_STATES",
           "DURESS_BANNER", "FOOTER", "reset_pin_limiter"]
