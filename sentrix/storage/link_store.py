"""Device-side storage for the Portal link: outbox, profile cache, remote audit.

All calls go through ``database.session_scope`` at call time (tests point it at
a temporary file). Owner: track T-LINK.

Sequence numbers. ``link_outbox.seq`` is the SQLite rowid (the table is not
declared ``AUTOINCREMENT``), so SQLite hands out ``max(seq) + 1``: deleting
the newest row would let the next event reuse an acknowledged number, which
the Portal would drop as a duplicate. :func:`prune` therefore never deletes
the newest row, and never the oldest one either: its ``created_at`` is the
outbox *epoch* the hello reports, so the Portal notices a factory reset (a
new database numbering from 1 again) and forgets the old numbers.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from sentrix.storage import database
from sentrix.storage.models import (LinkOutboxRecord, ProfileCacheRecord,
                                    RemoteAuditRecord)

#: Outbox priorities: SOS events jump the queue, seq numbers never change.
PRIORITY_SOS = 0
PRIORITY_NORMAL = 1
#: Acknowledged events are kept this long, then pruned (docs/7 §5.5).
ACKED_RETENTION_DAYS = 7


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# ── outbox ────────────────────────────────────────────────────────────────────
def enqueue(kind: str, payload: dict, *, priority: int = PRIORITY_NORMAL,
            dedupe_key: str | None = None) -> int | None:
    """Add an event; returns its seq, or None when ``dedupe_key`` exists."""
    try:
        with database.session_scope() as session:
            row = LinkOutboxRecord(kind=kind, priority=priority, dedupe_key=dedupe_key,
                                   payload_json=json.dumps(payload, default=str),
                                   created_at=_now())
            session.add(row)
            session.flush()
            return int(row.seq)
    except IntegrityError:
        return None


def _event_row(r: LinkOutboxRecord) -> dict[str, Any]:
    return {"seq": r.seq, "kind": r.kind, "priority": r.priority,
            "payload": json.loads(r.payload_json or "{}"),
            "created_at": _aware(r.created_at).isoformat() if r.created_at else None,
            "attempts": r.attempts}


def pending(limit: int = 64, exclude: Iterable[int] = ()) -> list[dict[str, Any]]:
    """Unacked events, SOS first, then by seq. ``exclude``: seqs already in
    flight on the current connection."""
    exclude = list(exclude)
    with database.session_scope() as session:
        query = (session.query(LinkOutboxRecord)
                 .filter(LinkOutboxRecord.acked_at.is_(None)))
        if exclude:
            query = query.filter(LinkOutboxRecord.seq.notin_(exclude))
        rows = (query.order_by(LinkOutboxRecord.priority, LinkOutboxRecord.seq)
                .limit(max(1, int(limit))).all())
        return [_event_row(r) for r in rows]


def mark_sent(seqs: Iterable[int]) -> None:
    seqs = list(seqs)
    if not seqs:
        return
    with database.session_scope() as session:
        for row in session.query(LinkOutboxRecord).filter(LinkOutboxRecord.seq.in_(seqs)):
            row.sent_at = _now()
            row.attempts = (row.attempts or 0) + 1


def ack(seqs: Iterable[int] = (), upto: int | None = None) -> int:
    """Mark events acked by the Portal (explicit seqs and/or every seq <= upto)."""
    seqs = list(seqs)
    with database.session_scope() as session:
        query = session.query(LinkOutboxRecord).filter(LinkOutboxRecord.acked_at.is_(None))
        rows = []
        if seqs:
            rows += query.filter(LinkOutboxRecord.seq.in_(seqs)).all()
        if upto is not None:
            rows += query.filter(LinkOutboxRecord.seq <= upto).all()
        now = _now()
        for row in {r.seq: r for r in rows}.values():
            row.acked_at = now
        return len({r.seq for r in rows})


def depth() -> int:
    with database.session_scope() as session:
        return (session.query(LinkOutboxRecord)
                .filter(LinkOutboxRecord.acked_at.is_(None)).count())


def summary() -> dict[str, Any]:
    """The hello's ``outbox`` block: ``head_seq`` (oldest unacked, or None),
    ``tail_seq`` (newest seq ever handed out, 0 when empty), ``depth`` and
    ``epoch`` (``created_at`` of the oldest row, which :func:`prune` keeps:
    a new value means a new database, i.e. a factory reset)."""
    with database.session_scope() as session:
        unacked = session.query(LinkOutboxRecord).filter(LinkOutboxRecord.acked_at.is_(None))
        head = unacked.with_entities(func.min(LinkOutboxRecord.seq)).scalar()
        count = unacked.count()
        tail = session.query(func.max(LinkOutboxRecord.seq)).scalar()
        first = (session.query(LinkOutboxRecord)
                 .order_by(LinkOutboxRecord.seq).first())
        epoch = (_aware(first.created_at).isoformat()
                 if first is not None and first.created_at else None)
        return {"head_seq": head, "tail_seq": int(tail or 0), "depth": count,
                "epoch": epoch}


def prune(days: float = ACKED_RETENTION_DAYS, now: datetime | None = None) -> int:
    """Delete events acknowledged more than ``days`` ago, except the oldest
    and the newest row (sequence and epoch anchors; module docstring)."""
    cutoff = (now or _now()) - timedelta(days=days)
    with database.session_scope() as session:
        lo = session.query(func.min(LinkOutboxRecord.seq)).scalar()
        hi = session.query(func.max(LinkOutboxRecord.seq)).scalar()
        if lo is None:
            return 0
        return (session.query(LinkOutboxRecord)
                .filter(LinkOutboxRecord.acked_at.is_not(None),
                        LinkOutboxRecord.acked_at < cutoff.replace(tzinfo=None),
                        LinkOutboxRecord.seq.notin_([lo, hi]))
                .delete(synchronize_session=False))


def last_payload(kind: str) -> dict[str, Any] | None:
    """The payload of the newest event of ``kind`` (acked or not), or None."""
    with database.session_scope() as session:
        row = (session.query(LinkOutboxRecord).filter_by(kind=kind)
               .order_by(LinkOutboxRecord.seq.desc()).first())
        return json.loads(row.payload_json or "{}") if row is not None else None


# ── profile cache ─────────────────────────────────────────────────────────────
def save_profile(version: int, blob: bytes, sha256: str) -> None:
    """Store an (already encrypted) profile and make it the active one."""
    with database.session_scope() as session:
        session.query(ProfileCacheRecord).update({"active": False})
        session.merge(ProfileCacheRecord(version=version, blob=blob, sha256=sha256,
                                         received_at=_now(), active=True))


def active_profile() -> dict[str, Any] | None:
    with database.session_scope() as session:
        row = (session.query(ProfileCacheRecord).filter_by(active=True)
               .order_by(ProfileCacheRecord.version.desc()).first())
        if row is None:
            return None
        return {"version": row.version, "blob": row.blob, "sha256": row.sha256,
                "received_at": row.received_at.isoformat() if row.received_at else None}


def wipe_profiles() -> int:
    """Unpair / factory reset: drop every cached profile."""
    with database.session_scope() as session:
        return session.query(ProfileCacheRecord).delete()


# ── remote audit ──────────────────────────────────────────────────────────────
def record_remote(*, kind: str, target: str, allowed: bool, user_id: str | None = None,
                  user_name: str | None = None, role: str | None = None,
                  status: int | None = None, reason: str | None = None,
                  request_id: str | None = None) -> int:
    """One row per tunnelled request, link command or subscription the device
    served or refused (``kind``: tunnel | command | video | state). Returns
    the row id."""
    with database.session_scope() as session:
        row = RemoteAuditRecord(
            at=_now(), user_id=(user_id or None) and str(user_id)[:64],
            user_name=(user_name or "")[:100] or None,
            role=role, kind=kind, target=target[:255], allowed=allowed,
            status=status, reason=(reason or "")[:255] or None,
            request_id=(request_id or None) and str(request_id)[:64])
        session.add(row)
        session.flush()
        return int(row.id)


def _remote_row(r: RemoteAuditRecord) -> dict[str, Any]:
    return {"id": r.id, "at": _aware(r.at).isoformat() if r.at else None,
            "user_id": r.user_id, "user_name": r.user_name, "role": r.role,
            "kind": r.kind, "target": r.target, "allowed": r.allowed,
            "status": r.status, "reason": r.reason, "request_id": r.request_id}


def recent_remote(limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    with database.session_scope() as session:
        rows = (session.query(RemoteAuditRecord)
                .order_by(RemoteAuditRecord.id.desc()).limit(limit).all())
        return [_remote_row(r) for r in rows]


def remote_since(after_id: int, limit: int = 200) -> list[dict[str, Any]]:
    """Rows with ``id > after_id``, oldest first (the ``remote.audit`` batches)."""
    with database.session_scope() as session:
        rows = (session.query(RemoteAuditRecord)
                .filter(RemoteAuditRecord.id > int(after_id))
                .order_by(RemoteAuditRecord.id).limit(max(1, int(limit))).all())
        return [_remote_row(r) for r in rows]


def last_remote_id() -> int:
    with database.session_scope() as session:
        return int(session.query(func.max(RemoteAuditRecord.id)).scalar() or 0)


def count_remote(*, kind: str, target: str, since: datetime,
                 allowed: bool | None = True) -> int:
    """Rows of one kind and target since ``since`` (the ``sms.send`` daily cap
    survives a restart because it counts these rows)."""
    with database.session_scope() as session:
        query = (session.query(RemoteAuditRecord)
                 .filter(RemoteAuditRecord.kind == kind,
                         RemoteAuditRecord.target == target,
                         RemoteAuditRecord.at >= since.astimezone(timezone.utc)
                         .replace(tzinfo=None)))
        if allowed is not None:
            query = query.filter(RemoteAuditRecord.allowed.is_(allowed))
        return query.count()


__all__ = ["enqueue", "pending", "mark_sent", "ack", "depth", "summary", "prune",
           "last_payload", "save_profile", "active_profile", "wipe_profiles",
           "record_remote", "recent_remote", "remote_since", "last_remote_id",
           "count_remote", "PRIORITY_SOS", "PRIORITY_NORMAL", "ACKED_RETENTION_DAYS"]
