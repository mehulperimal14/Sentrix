"""Device-side storage for the Portal link: outbox, profile cache, remote audit.

Minimal CRUD from the foundation; track T-LINK owns this module from here on.
All calls go through ``database.session_scope`` at call time (tests point it at
a temporary file).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy.exc import IntegrityError

from sentrix.storage import database
from sentrix.storage.models import (LinkOutboxRecord, ProfileCacheRecord,
                                    RemoteAuditRecord)

#: Outbox priorities: SOS events jump the queue, seq numbers never change.
PRIORITY_SOS = 0
PRIORITY_NORMAL = 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


def pending(limit: int = 64) -> list[dict[str, Any]]:
    """Unacked events, SOS first, then by seq."""
    with database.session_scope() as session:
        rows = (session.query(LinkOutboxRecord)
                .filter(LinkOutboxRecord.acked_at.is_(None))
                .order_by(LinkOutboxRecord.priority, LinkOutboxRecord.seq)
                .limit(limit).all())
        return [{"seq": r.seq, "kind": r.kind, "priority": r.priority,
                 "payload": json.loads(r.payload_json or "{}"),
                 "created_at": r.created_at.isoformat() if r.created_at else None,
                 "attempts": r.attempts} for r in rows]


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
                  request_id: str | None = None) -> None:
    with database.session_scope() as session:
        session.add(RemoteAuditRecord(
            at=_now(), user_id=user_id, user_name=(user_name or "")[:100] or None,
            role=role, kind=kind, target=target[:255], allowed=allowed,
            status=status, reason=(reason or "")[:255] or None, request_id=request_id))


def recent_remote(limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    with database.session_scope() as session:
        rows = (session.query(RemoteAuditRecord)
                .order_by(RemoteAuditRecord.id.desc()).limit(limit).all())
        return [{"at": r.at.isoformat() if r.at else None, "user_id": r.user_id,
                 "user_name": r.user_name, "role": r.role, "kind": r.kind,
                 "target": r.target, "allowed": r.allowed, "status": r.status,
                 "reason": r.reason, "request_id": r.request_id} for r in rows]


__all__ = ["enqueue", "pending", "mark_sent", "ack", "depth", "save_profile",
           "active_profile", "wipe_profiles", "record_remote", "recent_remote",
           "PRIORITY_SOS", "PRIORITY_NORMAL"]
