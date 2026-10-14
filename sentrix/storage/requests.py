"""Assistance requests and their hash-chained audit trail (device SQLite).

Minimal CRUD the foundation ships so every track writes the same rows; the SOS
engine (track T-SOS) owns this module from here on, except the frozen names
below. All functions go through ``database.session_scope`` at call time, so
tests that point ``database.SessionLocal`` at a temporary file never touch
``var/sentrix.db``.

Concurrency: pysqlite begins a transaction only at the first write, so a
read-then-write (``transition``) can lose an update when two threads race
(the owner approves while the sweeper expires: both read PENDING, and an
APPROVED request ends EXPIRED). Every write to a request therefore holds
:data:`REQUEST_LOCK` (process-wide; one process owns every writer, decision
"Pi process layout"), and callers that read, decide and then write hold it
across the whole sequence or pass ``expected_version``.

The audit chain, per (request, origin): ``hash = sha256(prev_hash ||
canonical_json(row))``, where the row is (request_id, seq, at, from_state,
to_state, event, actor_type, actor_id, channel, detail, restricted, origin)
and ``canonical_json`` is ``sentrix.link.protocol.canonical_json`` (the Portal
recomputes the same bytes). Device and cloud events form two chains, each
numbered from 1, so cloud events imported from ``sos.sync`` never use up
device sequence numbers. :func:`verify_chain` checks every chain.

Frozen (other modules import them): ``REQUEST_LOCK``, ``append_event``,
``create_request``, ``transition``, ``request_dict``, ``event_dict``,
``canonical``. Their existing parameters stay as they were; the SOS engine's
additions are optional keyword-only arguments (``at``: the engine's clock, so
time-travel tests and the stored times agree; the row's evidence paths) and
new functions (:func:`force_transition` for the reconnect merge,
:func:`open_requests`, :func:`requests_for_incident`).
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func

from sentrix.link.protocol import canonical_json
from sentrix.sos import states as S
from sentrix.storage import database
from sentrix.storage.models import AssistanceRequestRecord, RequestEventRecord

_GENESIS = "0" * 64
ORIGINS = ("device", "cloud")

#: Held by every write to ``assistance_requests`` / ``request_events``
#: (``create_request``, ``transition``, ``import_event``, ``set_fields`` and
#: ``database.set_verdict``). Re-entrant, so a caller may hold it across a
#: read-decide-write sequence.
REQUEST_LOCK = threading.RLock()


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _utc(value).isoformat()


def canonical(fields: dict[str, Any]) -> bytes:
    """The bytes hashed for one event (``protocol.canonical_json``)."""
    return canonical_json(fields)


def _event_fields(row: RequestEventRecord) -> dict[str, Any]:
    return {"request_id": row.request_id, "seq": row.seq, "at": _iso(row.at),
            "from_state": row.from_state, "to_state": row.to_state,
            "event": row.event, "actor_type": row.actor_type,
            "actor_id": row.actor_id, "channel": row.channel,
            "detail": json.loads(row.detail_json or "{}"),
            "restricted": bool(row.restricted), "origin": row.origin}


def _chain_hash(prev_hash: str, fields: dict[str, Any]) -> str:
    return hashlib.sha256(prev_hash.encode() + canonical(fields)).hexdigest()


def _json_safe(detail: dict | None) -> dict:
    """Detail as stored: JSON round-tripped, so the hashed value is the value
    read back (no ``repr`` of a datetime that the other side cannot rebuild)."""
    return json.loads(json.dumps(detail or {}, sort_keys=True, default=str))


def _last_event(session, request_id: str, origin: str) -> RequestEventRecord | None:
    return (session.query(RequestEventRecord)
            .filter_by(request_id=request_id, origin=origin)
            .order_by(RequestEventRecord.seq.desc()).first())


def append_event(session, request_id: str, *, event: str, from_state: str | None,
                 to_state: str | None, actor_type: str = "system",
                 actor_id: str | None = None, channel: str | None = None,
                 detail: dict | None = None, restricted: bool = False,
                 origin: str = "device", at: datetime | None = None) -> RequestEventRecord:
    """Append one audit row of *this* side's chain (``origin``) inside an open
    session (same transaction as the state change it records). Frozen
    signature: ``database.set_verdict`` imports it. Cloud events received
    from the Portal go through :func:`import_event` instead."""
    if origin not in ORIGINS:
        raise ValueError(f"unknown origin {origin!r}")
    last = _last_event(session, request_id, origin)
    row = RequestEventRecord(
        request_id=request_id, seq=(last.seq + 1) if last else 1, at=_utc(at),
        from_state=from_state, to_state=to_state, event=event,
        actor_type=actor_type, actor_id=actor_id, channel=channel,
        detail_json=json.dumps(_json_safe(detail), sort_keys=True),
        restricted=restricted, origin=origin,
        prev_hash=last.hash if last else _GENESIS)
    row.hash = _chain_hash(row.prev_hash, _event_fields(row))
    session.add(row)
    session.flush()
    return row


def import_event(event: dict[str, Any]) -> tuple[dict | None, str]:
    """Store one event of the *other* side's chain exactly as it was sent
    (``event_dict`` shape, e.g. from ``sos.sync.events``).

    Returns (event dict, code): ``ok``; ``duplicate`` (already stored, nothing
    written); ``gap`` (an earlier seq of that chain is missing: buffer and
    retry); ``invalid`` (hash or link does not verify; nothing written).
    """
    try:
        fields = {key: event[key] for key in (
            "request_id", "seq", "at", "from_state", "to_state", "event",
            "actor_type", "actor_id", "channel", "detail", "restricted", "origin")}
        seq, origin = int(fields["seq"]), str(fields["origin"])
        prev_hash, claimed = str(event["prev_hash"]), str(event["hash"])
    except (KeyError, TypeError, ValueError):
        return None, "invalid"
    if origin not in ORIGINS or seq < 1:
        return None, "invalid"
    fields["detail"] = fields["detail"] or {}
    fields["restricted"] = bool(fields["restricted"])
    if _chain_hash(prev_hash, fields) != claimed:
        return None, "invalid"
    with REQUEST_LOCK, database.session_scope() as session:
        existing = (session.query(RequestEventRecord)
                    .filter_by(request_id=fields["request_id"], origin=origin, seq=seq)
                    .first())
        if existing is not None:
            return event_dict(existing), "duplicate"
        last = _last_event(session, fields["request_id"], origin)
        expected_seq = (last.seq + 1) if last else 1
        if seq != expected_seq:
            return None, "gap" if seq > expected_seq else "invalid"
        if prev_hash != (last.hash if last else _GENESIS):
            return None, "invalid"
        row = RequestEventRecord(
            request_id=fields["request_id"], seq=seq,
            at=_utc(datetime.fromisoformat(fields["at"])),
            from_state=fields["from_state"], to_state=fields["to_state"],
            event=fields["event"], actor_type=fields["actor_type"],
            actor_id=fields["actor_id"], channel=fields["channel"],
            detail_json=json.dumps(fields["detail"], sort_keys=True),
            restricted=fields["restricted"], origin=origin,
            prev_hash=prev_hash, hash=claimed)
        if _chain_hash(prev_hash, _event_fields(row)) != claimed:
            return None, "invalid"           # "at" would not round-trip identically
        session.add(row)
        session.flush()
        return event_dict(row), "ok"


def request_dict(row: AssistanceRequestRecord) -> dict[str, Any]:
    """Full (device-internal) view. Owner-facing code must project it through
    ``sentrix.sos.states.owner_view`` first (duress)."""
    return {
        "id": row.id, "short_id": f"SX-{row.id[:6].upper()}",
        "incident_id": row.incident_id,
        "incident_ids": json.loads(row.incident_ids_json or "[]"),
        "state": row.state, "urgency": row.urgency,
        "flags": json.loads(row.flags_json or "{}"),
        "trigger_level": row.trigger_level, "peak_level": row.peak_level,
        "incident_type": row.incident_type, "evidence_state": row.evidence_state,
        "created_at": _iso(row.created_at), "updated_at": _iso(row.updated_at),
        "expires_at": _iso(row.expires_at), "seen_at": _iso(row.seen_at),
        "responded_at": _iso(row.responded_at), "responded_by": row.responded_by,
        "response_channel": row.response_channel, "authority": row.authority,
        "sos_text_full": row.sos_text_full, "sos_text_sms": row.sos_text_sms,
        "snapshot_path": row.snapshot_path, "evidence_bundle": row.evidence_bundle,
        "evidence_hash": row.evidence_hash, "package_id": row.package_id,
        "policy": json.loads(row.policy_json or "{}"),
        "profile_version": row.profile_version, "camera_id": row.camera_id,
        "test_mode": bool(row.test_mode), "version": row.version,
        "authority_side": row.authority_side, "cloud_synced": bool(row.cloud_synced),
        "parent_request_id": row.parent_request_id, "outcome": row.outcome,
        "session_id": row.session_id, "repage_count": row.repage_count or 0,
    }


def event_dict(row: RequestEventRecord) -> dict[str, Any]:
    fields = _event_fields(row)
    fields.update(prev_hash=row.prev_hash, hash=row.hash)
    return fields


def create_request(request_id: str, *, incident_id: str | None, incident_type: str,
                   level: int, evidence_state: str, policy: dict,
                   expires_at: datetime | None, test_mode: bool,
                   session_id: str | None = None, camera_id: str = "CAM_0",
                   parent_request_id: str | None = None, flags: dict | None = None,
                   actor_type: str = "system", actor_id: str | None = None,
                   channel: str | None = "system", detail: dict | None = None,
                   origin: str = "device",
                   authority_side: str | None = None,
                   at: datetime | None = None,
                   snapshot_path: str | None = None, evidence_bundle: str | None = None,
                   evidence_hash: str | None = None, urgency: str = "NORMAL",
                   peak_level: int | None = None,
                   restricted: bool = False) -> dict[str, Any] | None:
    """Insert a DRAFTED request. Idempotent: an existing id is returned
    unchanged.

    ``origin="device"`` (the engine at an L3 commit, or "Request help now")
    also appends the device's ``created`` event. ``origin="cloud"`` is a
    request the cloud opened while the device was offline (heartbeat lost
    during an open L2+ incident, D9), learnt from ``sos.sync``: the row gets
    ``authority_side="cloud"`` and no device event; the cloud's own
    ``created`` event (and the rest of its chain) arrives in the sync and is
    stored with :func:`import_event`.

    ``at`` is the creation time (default now); ``snapshot_path``,
    ``evidence_bundle``, ``evidence_hash``, ``urgency`` and ``peak_level``
    (default ``level``) are stored with the row; ``restricted`` marks the
    ``created`` event (a silent duress request owners never see).
    """
    if origin not in ORIGINS:
        raise ValueError(f"unknown origin {origin!r}")
    with REQUEST_LOCK, database.session_scope() as session:
        existing = session.get(AssistanceRequestRecord, request_id)
        if existing is not None:
            return request_dict(existing)
        now = _utc(at)
        row = AssistanceRequestRecord(
            id=request_id, incident_id=incident_id,
            incident_ids_json=json.dumps([incident_id] if incident_id else []),
            state=S.DRAFTED, urgency=urgency or "NORMAL",
            flags_json=json.dumps(flags or {}, sort_keys=True),
            trigger_level=level, peak_level=max(level, peak_level or level),
            incident_type=incident_type,
            evidence_state=evidence_state, created_at=now, updated_at=now,
            expires_at=_utc(expires_at) if expires_at else None,
            policy_json=json.dumps(policy, sort_keys=True, default=str),
            camera_id=camera_id, test_mode=test_mode, version=1,
            authority_side=authority_side or ("device" if origin == "device" else "cloud"),
            cloud_synced=origin == "cloud",
            parent_request_id=parent_request_id, session_id=session_id,
            snapshot_path=snapshot_path, evidence_bundle=evidence_bundle,
            evidence_hash=evidence_hash, reminder_step=0, repage_count=0)
        session.add(row)
        if origin == "device":
            append_event(session, request_id, event=S.CREATED, from_state=None,
                         to_state=S.DRAFTED, actor_type=actor_type, actor_id=actor_id,
                         channel=channel, detail=detail or {"incident_id": incident_id},
                         restricted=restricted, at=now)
        session.flush()
        return request_dict(row)


@dataclass(frozen=True)
class TransitionResult:
    """``code``: ok | not_found | conflict (event not allowed in the current
    state, or ``expected_version`` is stale; nothing was written)."""

    request: dict | None
    code: str
    message: str = ""
    event: dict | None = None

    @property
    def ok(self) -> bool:
        return self.code == "ok"


def try_transition(request_id: str, event: str, *, expected_version: int | None = None,
                   actor_type: str = "system", actor_id: str | None = None,
                   channel: str | None = None, detail: dict | None = None,
                   restricted: bool = False, origin: str = "device",
                   at: datetime | None = None, **fields: Any) -> TransitionResult:
    """Apply one event through ``states.next_state`` under :data:`REQUEST_LOCK`.

    ``expected_version``: refuse (``conflict``) unless the request is still at
    that version, so a decision made on a stale read is never written.
    ``fields`` are column values set in the same transaction (e.g.
    ``authority="POLICE"``). Self-events (from = to) are recorded too. ``at``
    is the event time (default now).
    """
    with REQUEST_LOCK, database.session_scope() as session:
        row = session.get(AssistanceRequestRecord, request_id)
        if row is None:
            return TransitionResult(None, "not_found", "unknown request")
        if expected_version is not None and row.version != expected_version:
            return TransitionResult(request_dict(row), "conflict",
                                    f"stale version {expected_version} (now {row.version})")
        to_state = S.next_state(row.state, event)
        if to_state is None:
            return TransitionResult(request_dict(row), "conflict",
                                    f"{event} is not allowed in {row.state}")
        from_state = row.state
        for key, value in fields.items():
            setattr(row, key, value)
        row.state, row.version = to_state, (row.version or 0) + 1
        row.updated_at = _utc(at)
        audit = append_event(session, request_id, event=event, from_state=from_state,
                             to_state=to_state, actor_type=actor_type, actor_id=actor_id,
                             channel=channel, detail=detail, restricted=restricted,
                             origin=origin, at=row.updated_at)
        return TransitionResult(request_dict(row), "ok", event=event_dict(audit))


def force_transition(request_id: str, to_state: str, *, event: str = S.CONFLICT,
                     expected_version: int | None = None, actor_type: str = "system",
                     actor_id: str | None = None, channel: str | None = None,
                     detail: dict | None = None, restricted: bool = False,
                     at: datetime | None = None, **fields: Any) -> TransitionResult:
    """Move a request to ``to_state`` outside the transition table, recording
    ``event`` (``conflict``) with the real from/to states in the device chain.

    Only for the reconnect merge (``states.reconcile``), which may jump, e.g.
    PENDING -> EXPIRED (the cloud lapsed it while the device was off) or
    EXPIRED -> APPROVED (an answer reached the cloud in time). Everything
    else goes through :func:`try_transition`.
    """
    if to_state not in S.STATES:
        return TransitionResult(None, "conflict", f"unknown state {to_state!r}")
    with REQUEST_LOCK, database.session_scope() as session:
        row = session.get(AssistanceRequestRecord, request_id)
        if row is None:
            return TransitionResult(None, "not_found", "unknown request")
        if expected_version is not None and row.version != expected_version:
            return TransitionResult(request_dict(row), "conflict",
                                    f"stale version {expected_version} (now {row.version})")
        from_state = row.state
        for key, value in fields.items():
            setattr(row, key, value)
        row.state, row.version = to_state, (row.version or 0) + 1
        row.updated_at = _utc(at)
        audit = append_event(session, request_id, event=event, from_state=from_state,
                             to_state=to_state, actor_type=actor_type, actor_id=actor_id,
                             channel=channel, detail=detail, restricted=restricted,
                             origin="device", at=row.updated_at)
        return TransitionResult(request_dict(row), "ok", event=event_dict(audit))


def transition(request_id: str, event: str, *, actor_type: str = "system",
               actor_id: str | None = None, channel: str | None = None,
               detail: dict | None = None, restricted: bool = False,
               origin: str = "device", expected_version: int | None = None,
               at: datetime | None = None, **fields: Any) -> dict[str, Any] | None:
    """:func:`try_transition` returning the new request dict, or None when
    the request is unknown, the event is not allowed in its current state or
    ``expected_version`` is stale (nothing is written then)."""
    result = try_transition(request_id, event, expected_version=expected_version,
                            actor_type=actor_type, actor_id=actor_id, channel=channel,
                            detail=detail, restricted=restricted, origin=origin, at=at,
                            **fields)
    return result.request if result.ok else None


def set_fields(request_id: str, *, expected_version: int | None = None,
               **fields: Any) -> dict[str, Any] | None:
    """Bookkeeping columns without an event or a version bump (timers:
    ``next_timer_at``, ``reminder_step``, ``repage_count``, ``seen_at``,
    ``cloud_synced``). Anything a person or the cloud should see goes through
    a transition instead."""
    with REQUEST_LOCK, database.session_scope() as session:
        row = session.get(AssistanceRequestRecord, request_id)
        if row is None or (expected_version is not None and row.version != expected_version):
            return None
        for key, value in fields.items():
            setattr(row, key, value)
        return request_dict(row)


def get_request(request_id: str) -> dict[str, Any] | None:
    with database.session_scope() as session:
        row = session.get(AssistanceRequestRecord, request_id)
        return request_dict(row) if row else None


def open_request() -> dict[str, Any] | None:
    """The device's one open request (newest), or None."""
    with database.session_scope() as session:
        row = (session.query(AssistanceRequestRecord)
               .filter(AssistanceRequestRecord.state.in_(sorted(S.OPEN_STATES)))
               .order_by(AssistanceRequestRecord.created_at.desc()).first())
        return request_dict(row) if row else None


def open_requests() -> list[dict[str, Any]]:
    """Every request not in a terminal state, newest first (normally one; a
    request the cloud opened while the device was offline can make two until
    the merge links them)."""
    with database.session_scope() as session:
        rows = (session.query(AssistanceRequestRecord)
                .filter(AssistanceRequestRecord.state.in_(sorted(S.OPEN_STATES)))
                .order_by(AssistanceRequestRecord.created_at.desc()).all())
        return [request_dict(r) for r in rows]


def requests_for_incident(incident_id: str) -> list[dict[str, Any]]:
    """Requests this incident opened or joined, newest first."""
    if not incident_id:
        return []
    pattern = f'%"{incident_id}"%'
    with database.session_scope() as session:
        rows = (session.query(AssistanceRequestRecord)
                .filter((AssistanceRequestRecord.incident_id == incident_id)
                        | AssistanceRequestRecord.incident_ids_json.like(pattern))
                .order_by(AssistanceRequestRecord.created_at.desc()).all())
        return [request_dict(r) for r in rows
                if r.incident_id == incident_id
                or incident_id in json.loads(r.incident_ids_json or "[]")]


def list_requests(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    with database.session_scope() as session:
        rows = (session.query(AssistanceRequestRecord)
                .order_by(AssistanceRequestRecord.created_at.desc())
                .offset(max(0, int(offset))).limit(limit).all())
        return [request_dict(r) for r in rows]


def events(request_id: str, include_restricted: bool = False,
           origin: str | None = None) -> list[dict[str, Any]]:
    """Events ordered by (origin, seq); ``origin`` limits to one chain."""
    with database.session_scope() as session:
        query = session.query(RequestEventRecord).filter_by(request_id=request_id)
        if not include_restricted:
            query = query.filter(RequestEventRecord.restricted.is_(False))
        if origin is not None:
            query = query.filter_by(origin=origin)
        rows = query.order_by(RequestEventRecord.origin, RequestEventRecord.seq).all()
        return [event_dict(r) for r in rows]


def last_seq(request_id: str, origin: str = "device") -> int:
    """Highest stored seq of one chain (0 when empty)."""
    with database.session_scope() as session:
        value = (session.query(func.max(RequestEventRecord.seq))
                 .filter_by(request_id=request_id, origin=origin).scalar())
        return int(value or 0)


def verify_chain(request_id: str) -> dict[str, Any]:
    """Recompute every chain of the request: {ok, entries, problems, origins}.

    ``origins`` maps each origin to its number of entries; a problem names its
    chain ("cloud seq 3: broken link")."""
    problems: list[str] = []
    counts: dict[str, int] = {}
    with database.session_scope() as session:
        rows = (session.query(RequestEventRecord).filter_by(request_id=request_id)
                .order_by(RequestEventRecord.origin, RequestEventRecord.seq).all())
        prev: dict[str, str] = {}
        expected_seq: dict[str, int] = {}
        for row in rows:
            origin = row.origin or "device"
            counts[origin] = counts.get(origin, 0) + 1
            link = prev.get(origin, _GENESIS)
            if row.seq != expected_seq.get(origin, 1):
                problems.append(f"{origin} seq {row.seq}: gap before it")
            if row.prev_hash != link:
                problems.append(f"{origin} seq {row.seq}: broken link")
            if row.hash != _chain_hash(row.prev_hash or _GENESIS, _event_fields(row)):
                problems.append(f"{origin} seq {row.seq}: hash mismatch")
            prev[origin] = row.hash
            expected_seq[origin] = row.seq + 1
        return {"ok": not problems, "entries": len(rows), "problems": problems,
                "origins": counts}


__all__ = ["REQUEST_LOCK", "ORIGINS", "append_event", "import_event", "create_request",
           "TransitionResult", "try_transition", "transition", "force_transition",
           "set_fields", "get_request", "open_request", "open_requests",
           "requests_for_incident", "list_requests", "events", "last_seq",
           "verify_chain", "request_dict", "event_dict", "canonical"]
