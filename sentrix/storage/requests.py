"""Assistance requests and their hash-chained audit trail (device SQLite).

Minimal CRUD the foundation ships so every track writes the same rows; the SOS
engine (track T-SOS) owns this module from here on. All functions go through
``database.session_scope`` at call time, so tests that point
``database.SessionLocal`` at a temporary file never touch ``var/sentrix.db``.

The audit chain: ``hash = sha256(prev_hash || canonical(row))`` per request,
where ``canonical`` is the JSON of (request_id, seq, at, from_state, to_state,
event, actor_type, actor_id, channel, detail, restricted, origin) with sorted
keys. :func:`verify_chain` recomputes it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sentrix.sos import states as S
from sentrix.storage import database
from sentrix.storage.models import AssistanceRequestRecord, RequestEventRecord

_GENESIS = "0" * 64


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _utc(value).isoformat()


def canonical(fields: dict[str, Any]) -> bytes:
    return json.dumps(fields, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")


def _event_fields(row: RequestEventRecord) -> dict[str, Any]:
    return {"request_id": row.request_id, "seq": row.seq, "at": _iso(row.at),
            "from_state": row.from_state, "to_state": row.to_state,
            "event": row.event, "actor_type": row.actor_type,
            "actor_id": row.actor_id, "channel": row.channel,
            "detail": json.loads(row.detail_json or "{}"),
            "restricted": bool(row.restricted), "origin": row.origin}


def append_event(session, request_id: str, *, event: str, from_state: str | None,
                 to_state: str | None, actor_type: str = "system",
                 actor_id: str | None = None, channel: str | None = None,
                 detail: dict | None = None, restricted: bool = False,
                 origin: str = "device", at: datetime | None = None) -> RequestEventRecord:
    """Append one audit row inside an open session (same transaction as the
    state change it records)."""
    last = (session.query(RequestEventRecord)
            .filter_by(request_id=request_id)
            .order_by(RequestEventRecord.seq.desc()).first())
    row = RequestEventRecord(
        request_id=request_id, seq=(last.seq + 1) if last else 1, at=_utc(at),
        from_state=from_state, to_state=to_state, event=event,
        actor_type=actor_type, actor_id=actor_id, channel=channel,
        detail_json=json.dumps(detail or {}, sort_keys=True, default=str),
        restricted=restricted, origin=origin,
        prev_hash=last.hash if last else _GENESIS)
    row.hash = hashlib.sha256(row.prev_hash.encode() + canonical(_event_fields(row))).hexdigest()
    session.add(row)
    session.flush()
    return row


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
        "session_id": row.session_id,
    }


def event_dict(row: RequestEventRecord) -> dict[str, Any]:
    fields = _event_fields(row)
    fields.update(prev_hash=row.prev_hash, hash=row.hash)
    return fields


def create_request(request_id: str, *, incident_id: str, incident_type: str,
                   level: int, evidence_state: str, policy: dict,
                   expires_at: datetime | None, test_mode: bool,
                   session_id: str | None = None, camera_id: str = "CAM_0",
                   parent_request_id: str | None = None, flags: dict | None = None,
                   actor_type: str = "system", actor_id: str | None = None,
                   channel: str | None = "system",
                   detail: dict | None = None) -> dict[str, Any] | None:
    """Insert a DRAFTED request plus its ``created`` event. Idempotent: an
    existing id is returned unchanged."""
    with database.session_scope() as session:
        existing = session.get(AssistanceRequestRecord, request_id)
        if existing is not None:
            return request_dict(existing)
        now = _utc()
        row = AssistanceRequestRecord(
            id=request_id, incident_id=incident_id,
            incident_ids_json=json.dumps([incident_id]), state=S.DRAFTED,
            urgency="NORMAL", flags_json=json.dumps(flags or {}, sort_keys=True),
            trigger_level=level, peak_level=level, incident_type=incident_type,
            evidence_state=evidence_state, created_at=now, updated_at=now,
            expires_at=_utc(expires_at) if expires_at else None,
            policy_json=json.dumps(policy, sort_keys=True, default=str),
            camera_id=camera_id, test_mode=test_mode, version=1,
            authority_side="device", cloud_synced=False,
            parent_request_id=parent_request_id, session_id=session_id)
        session.add(row)
        append_event(session, request_id, event=S.CREATED, from_state=None,
                     to_state=S.DRAFTED, actor_type=actor_type, actor_id=actor_id,
                     channel=channel, detail=detail or {"incident_id": incident_id})
        session.flush()
        return request_dict(row)


def transition(request_id: str, event: str, *, actor_type: str = "system",
               actor_id: str | None = None, channel: str | None = None,
               detail: dict | None = None, restricted: bool = False,
               origin: str = "device", **fields: Any) -> dict[str, Any] | None:
    """Apply one event through ``states.next_state``; returns the new request
    dict, or None when the request is unknown or the event is not allowed in
    its current state (nothing is written then). ``fields`` are column values
    to set in the same transaction (e.g. ``authority="POLICE"``)."""
    with database.session_scope() as session:
        row = session.get(AssistanceRequestRecord, request_id)
        if row is None:
            return None
        to_state = S.next_state(row.state, event)
        if to_state is None:
            return None
        from_state = row.state
        for key, value in fields.items():
            setattr(row, key, value)
        row.state, row.version = to_state, (row.version or 0) + 1
        row.updated_at = _utc()
        append_event(session, request_id, event=event, from_state=from_state,
                     to_state=to_state, actor_type=actor_type, actor_id=actor_id,
                     channel=channel, detail=detail, restricted=restricted,
                     origin=origin)
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


def list_requests(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    with database.session_scope() as session:
        rows = (session.query(AssistanceRequestRecord)
                .order_by(AssistanceRequestRecord.created_at.desc())
                .offset(max(0, int(offset))).limit(limit).all())
        return [request_dict(r) for r in rows]


def events(request_id: str, include_restricted: bool = False) -> list[dict[str, Any]]:
    with database.session_scope() as session:
        query = session.query(RequestEventRecord).filter_by(request_id=request_id)
        if not include_restricted:
            query = query.filter(RequestEventRecord.restricted.is_(False))
        return [event_dict(r) for r in query.order_by(RequestEventRecord.seq).all()]


def verify_chain(request_id: str) -> dict[str, Any]:
    """Recompute the request's audit chain: {ok, entries, problems}."""
    problems: list[str] = []
    with database.session_scope() as session:
        rows = (session.query(RequestEventRecord).filter_by(request_id=request_id)
                .order_by(RequestEventRecord.seq).all())
        prev = _GENESIS
        for row in rows:
            expected = hashlib.sha256(prev.encode() + canonical(_event_fields(row))).hexdigest()
            if row.prev_hash != prev:
                problems.append(f"seq {row.seq}: broken link")
            if row.hash != expected:
                problems.append(f"seq {row.seq}: hash mismatch")
            prev = row.hash
        return {"ok": not problems, "entries": len(rows), "problems": problems}


__all__ = ["append_event", "create_request", "transition", "get_request",
           "open_request", "list_requests", "events", "verify_chain",
           "request_dict", "event_dict", "canonical"]
