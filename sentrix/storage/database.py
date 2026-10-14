"""SQLite persistence layer.

WAL is enabled explicitly. The mid-semester report claims "SQLite3 with WAL
mode" but nothing in the previous code ever issued the pragma, so the database
ran in rollback-journal mode — writers blocked readers, which matters here
because the pipeline thread writes while the dashboard reads.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from sentrix import listeners as _listeners
from sentrix.paths import DB_PATH, ensure_runtime_dirs
from sentrix.storage.models import (AssistanceRequestRecord, Base, DispatchRecord,
                                    IncidentRecord)
from sentrix.utils.logging import get_logger

log = get_logger("database")

# ── change listeners (docs/7 §6.3) ────────────────────────────────────────────
# The registry lives in ``sentrix.listeners`` (stdlib only) so the vault, the
# event log and the runtime mode can announce writes without importing the
# database. These names stay for the callers that registered here first.
LISTENER_KINDS = _listeners.KINDS
add_listener = _listeners.add_listener
remove_listener = _listeners.remove_listener
_emit = _listeners.emit

#: Level names, duplicated from PolicyConfig so the storage layer stays free of
#: a config import. Kept in sync by tests/test_pipeline.py.
_STATUS_NAMES = {1: "SECURE", 2: "SUSPICIOUS", 3: "ELEVATED", 4: "HIGH",
                 5: "CRITICAL"}

ensure_runtime_dirs()
engine = create_engine(f"sqlite:///{DB_PATH}",
                       connect_args={"check_same_thread": False}, echo=False)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


@event.listens_for(engine, "connect")
def _configure_sqlite(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


#: Columns added after the first release, as (table, column, SQL type). An
#: existing database gains them on start-up; nothing is dropped or rewritten.
#: New tables need no entry: ``create_all`` adds a missing table.
_ADDED_COLUMNS = (
    ("incidents", "trigger_json", "TEXT"),
    # Portal and SOS (docs/7 §6.1)
    ("incidents", "verdict_by", "VARCHAR(64)"),
    ("incidents", "verdict_at", "DATETIME"),
    ("incidents", "request_id", "VARCHAR(36)"),
    ("dispatch_packages", "request_id", "VARCHAR(36)"),
    ("dispatch_packages", "chosen_authority", "VARCHAR(10)"),
    ("dispatch_packages", "sent_at", "DATETIME"),
    ("dispatch_packages", "sent_by", "VARCHAR(64)"),
    ("dispatch_packages", "sent_to_json", "TEXT"),
    ("dispatch_packages", "error", "TEXT"),
    ("dispatch_packages", "expires_at", "DATETIME"),
    ("dispatch_packages", "updated_at", "DATETIME"),
    ("enrolled_faces", "member_id", "VARCHAR(36)"),
    ("enrolled_faces", "consent_id", "VARCHAR(36)"),
    ("enrolled_faces", "enrolled_by", "VARCHAR(64)"),
    # notifications existed in foundation builds without it (docs/7 §8.2)
    ("notifications", "meta_json", "TEXT"),
)
#: Indexes created if missing: (name, table, columns, unique). A unique index
#: on rows that would violate it fails loudly at start-up rather than silently.
_ADDED_INDEXES = (
    ("ix_incidents_request_id", "incidents", ("request_id",), False),
    ("ix_dispatch_packages_request_id", "dispatch_packages", ("request_id",), False),
    # One chain per (request, origin): docs/7 §7.6.
    ("ux_request_events_origin_seq", "request_events", ("request_id", "origin", "seq"), True),
)


def _migrate() -> None:
    with engine.begin() as conn:
        for table, column, sql_type in _ADDED_COLUMNS:
            present = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if present and column not in present:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
                log.info("database: added %s.%s", table, column)
        for name, table, columns, unique in _ADDED_INDEXES:
            present = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if set(columns) <= present:
                kind = "UNIQUE INDEX" if unique else "INDEX"
                conn.exec_driver_sql(f"CREATE {kind} IF NOT EXISTS {name} ON {table} "
                                     f"({', '.join(columns)})")


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _migrate()
    log.info("database ready: %s (WAL)", DB_PATH)


@contextmanager
def session_scope():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ── incidents ─────────────────────────────────────────────────────────────────
def save_incident(incident, session_id: str, ongoing: bool = False) -> None:
    """Persist an incident. One row per episode, not per frame.

    Called when the incident opens (``ongoing=True``, no end time), again when
    its peak moves, and when it closes, so a crash or ``taskkill /F`` keeps the
    row. Type, reason, TCI, features, authorisation and trigger all come from
    one :class:`~sentrix.pipeline.incidents.PeakSnapshot`, read once, so a row
    never mixes ticks.
    """
    peak = incident.peak
    try:
        with session_scope() as session:
            session.merge(IncidentRecord(
                id=incident.id,
                session_id=session_id,
                started_at=datetime.fromtimestamp(incident.started_wall,
                                                  tz=timezone.utc),
                ended_at=None if ongoing else datetime.now(timezone.utc),
                duration_seconds=round(incident.duration, 2),
                peak_level=incident.peak_level,
                peak_tci=round(peak.tci, 4),
                incident_type=peak.incident_type,
                reason=(peak.reason or "")[:255],
                frames=incident.frames,
                evidence_count=incident.evidence_count,
                snapshot_path=incident.snapshot_path,
                evidence_bundle=incident.evidence_bundle,
                evidence_hash=incident.evidence_hash,
                person_present=peak.person_present,
                authorized=peak.authorized,
                peak_features_json=json.dumps(peak.features),
                trigger_json=json.dumps(peak.trigger, default=str),
                actions_json=json.dumps(sorted(incident.actions)),
            ))
    except Exception as exc:
        log.error("save_incident failed: %s", exc)
        return
    _emit("incident", lambda: {
        "id": incident.id, "session_id": session_id, "ongoing": ongoing,
        "started_at": datetime.fromtimestamp(incident.started_wall,
                                             tz=timezone.utc).isoformat(),
        "duration_seconds": round(incident.duration, 2),
        "peak_level": incident.peak_level, "peak_tci": round(peak.tci, 4),
        "incident_type": peak.incident_type, "reason": (peak.reason or "")[:255],
        "frames": incident.frames, "evidence_count": incident.evidence_count,
        "snapshot_path": incident.snapshot_path,
        "evidence_bundle": incident.evidence_bundle,
        "evidence_hash": incident.evidence_hash,
        "person_present": peak.person_present, "authorized": peak.authorized,
        "actions": sorted(incident.actions)})


def _incident_listener_dict(row: IncidentRecord, ongoing: bool = False) -> dict[str, Any]:
    """The "incident" listener payload built from a stored row (the same keys
    as ``save_incident``'s)."""
    return {
        "id": row.id, "session_id": row.session_id, "ongoing": ongoing,
        "started_at": _iso_utc(row.started_at),
        "duration_seconds": row.duration_seconds, "peak_level": row.peak_level,
        "peak_tci": row.peak_tci, "incident_type": row.incident_type,
        "reason": row.reason, "frames": row.frames,
        "evidence_count": row.evidence_count, "snapshot_path": row.snapshot_path,
        "evidence_bundle": row.evidence_bundle, "evidence_hash": row.evidence_hash,
        "person_present": row.person_present, "authorized": row.authorized,
        "actions": json.loads(row.actions_json) if row.actions_json else []}


def close_interrupted(current_session: str) -> int:
    """End rows left open by a session that crashed or was killed.

    Their end is the last time they were written (start + saved duration).
    Each closed row is announced to the "incident" listeners (``ongoing``
    False), so the Portal's mirror closes it too: the app lifespan calls this
    after the services started, while the link client listens.
    """
    closed: list[dict[str, Any]] = []
    try:
        with session_scope() as session:
            rows = (session.query(IncidentRecord)
                    .filter(IncidentRecord.ended_at.is_(None),
                            IncidentRecord.session_id != current_session).all())
            for row in rows:
                start = row.started_at
                if start is not None and start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                row.ended_at = (start + timedelta(seconds=row.duration_seconds or 0.0)
                                if start is not None else datetime.now(timezone.utc))
                closed.append(_incident_listener_dict(row))
            if rows:
                log.info("closed %d incident(s) interrupted by a previous shutdown", len(rows))
    except Exception as exc:
        log.error("close_interrupted failed: %s", exc)
        return 0
    for payload in closed:
        _emit("incident", payload)
    return len(closed)


def _iso_utc(value: datetime | None) -> str | None:
    """Stored naive UTC -> ISO 8601 with offset (None stays None)."""
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


def recent_incidents(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    """Newest first. ``limit`` is capped at 500; page with ``offset``."""
    limit = max(1, min(int(limit), 500))
    try:
        with session_scope() as session:
            rows = (session.query(IncidentRecord)
                    .order_by(IncidentRecord.started_at.desc())
                    .offset(max(0, int(offset))).limit(limit).all())
            return [{
                "id": r.id,
                "started_at": str(r.started_at),
                # Stored as naive UTC; the page renders it in local time.
                "started_iso": (r.started_at.replace(tzinfo=timezone.utc).isoformat()
                                if r.started_at else None),
                "duration_seconds": r.duration_seconds,
                "peak_level": r.peak_level,
                "peak_tci": r.peak_tci,
                "incident_type": r.incident_type,
                "reason": r.reason,
                "frames": r.frames,
                "evidence_count": r.evidence_count,
                "evidence_bundle": r.evidence_bundle,
                "authorized": r.authorized,
                "verdict": r.verdict,
                "verdict_note": r.verdict_note,
                "verdict_by": r.verdict_by,
                "verdict_at": _iso_utc(r.verdict_at),
                "request_id": r.request_id,
                "ended_at": str(r.ended_at) if r.ended_at else None,
                "session_id": r.session_id,
                "trigger": json.loads(r.trigger_json) if r.trigger_json else None,
                # Legacy aliases for the existing events.html template.
                "created_at": str(r.started_at),
                "level": r.peak_level,
                "tci": r.peak_tci,
                "camera_id": r.camera_id,
                "status": _STATUS_NAMES.get(r.peak_level, "UNKNOWN"),
                "snapshot_path": r.snapshot_path,
            } for r in rows]
    except Exception as exc:
        log.error("recent_incidents failed: %s", exc)
        return []


def set_verdict(incident_id: str, verdict: str, note: str | None = None, *,
                by: str | None = None, channel: str | None = None,
                actor_type: str | None = None) -> bool:
    """Record operator feedback: was this a real threat?

    This is the single highest-value piece of data the deployment produces —
    it is what a retrained fusion model learns from. ``by`` records who
    (``Actor.ref``: "lc:device_admin", "portal:<user id>", "desk:<operator
    id>"); ``actor_type`` is the request-event actor type
    (``Actor.event_actor_type``: device_admin, owner, member, desk, contact,
    cloud), guessed from ``by`` when omitted.

    A FALSE verdict's effect on help depends on the incident's assistance
    request (docs/7 §7.7), so a false-alarm click can never quietly cancel
    help that was asked for:

    * no request: cancel the incident's PENDING packages (pre-portal rule);
    * request DRAFTED or PENDING: the request becomes CANCELLED (reason
      verdict) and its PENDING package CANCELLED (matched by request id);
    * request on the help path: flag ``resident_safe``; the package and the
      help stay as they are (only the Desk or a contact closes it);
    * duress request: nothing changes (the caller still reports success);
    * request already terminal: only the label is recorded.

    With an open request the SOS PIN is required, so callers go through
    ``sos.false_verdict`` (which turns the duress PIN into approve + duress
    before this runs), never straight here. The write holds
    ``requests.REQUEST_LOCK``. The "verdict" listeners get ``request_id``,
    ``request_state`` and ``request_version``: the SOS engine re-reads the
    request, publishes its new events, refreshes ``state.sos``, cancels its
    timers and tells notified contacts.
    """
    from sentrix.actor import event_actor_type_for_ref
    from sentrix.storage.requests import REQUEST_LOCK

    verdict = verdict.upper()
    if verdict not in ("TRUE", "FALSE"):
        return False
    actor_type = actor_type or event_actor_type_for_ref(by)
    outcome = "label_only"
    request_info: dict[str, Any] = {"request_id": None, "request_state": None,
                                    "request_version": None}
    try:
        with REQUEST_LOCK, session_scope() as session:
            row = session.query(IncidentRecord).filter_by(id=incident_id).first()
            if row is None:
                return False
            row.verdict, row.verdict_note = verdict, note
            row.verdict_by, row.verdict_at = by, datetime.now(timezone.utc)
            if verdict == "FALSE":
                outcome = _false_verdict_effect(session, row, by, channel, actor_type)
            if row.request_id:
                request = session.get(AssistanceRequestRecord, row.request_id)
                if request is not None:
                    request_info = {"request_id": request.id,
                                    "request_state": request.state,
                                    "request_version": request.version}
    except Exception as exc:
        log.error("set_verdict failed: %s", exc)
        return False
    _emit("verdict", {"incident_id": incident_id, "verdict": verdict, "note": note,
                      "by": by, "actor_type": actor_type, "channel": channel,
                      "effect": outcome, **request_info,
                      "at": datetime.now(timezone.utc).isoformat()})
    return True


def _false_verdict_effect(session, incident: IncidentRecord, by: str | None,
                          channel: str | None, actor_type: str) -> str:
    """Apply a FALSE verdict to the incident's request/package; returns what it
    did. Runs inside ``set_verdict``'s transaction, under REQUEST_LOCK."""
    from sentrix.sos import states as S
    from sentrix.storage.requests import append_event

    request = (session.get(AssistanceRequestRecord, incident.request_id)
               if incident.request_id else None)
    if request is None:
        # A false alarm must not leave an emergency package waiting for
        # someone to press "send".
        cancelled = (session.query(DispatchRecord)
                     .filter_by(incident_id=incident.id, status="PENDING")
                     .update({"status": "CANCELLED"}))
        if cancelled:
            log.info("cancelled %d dispatch package(s) for false alarm %s",
                     cancelled, incident.id[:8])
        return "packages_cancelled" if cancelled else "label_only"

    flags = json.loads(request.flags_json or "{}")
    if flags.get("duress"):
        return "label_only"
    if request.state in S.PROMPTING_STATES:
        previous = request.state
        request.state = S.CANCELLED
        request.version = (request.version or 0) + 1
        request.updated_at = datetime.now(timezone.utc)
        (session.query(DispatchRecord)
         .filter_by(request_id=request.id, status="PENDING")
         .update({"status": "CANCELLED"}))
        append_event(session, request.id, event=S.CANCEL, from_state=previous,
                     to_state=S.CANCELLED, actor_type=actor_type, actor_id=by,
                     channel=channel, detail={"reason": "verdict_false",
                                              "incident_id": incident.id})
        return "request_cancelled"
    if request.state in S.HELP_PATH and request.state != S.CLOSED:
        flags["resident_safe"] = True
        request.flags_json = json.dumps(flags, sort_keys=True)
        request.version = (request.version or 0) + 1
        request.updated_at = datetime.now(timezone.utc)
        append_event(session, request.id, event=S.RESIDENT_SAFE,
                     from_state=request.state, to_state=request.state,
                     actor_type=actor_type, actor_id=by, channel=channel,
                     detail={"reason": "verdict_false", "incident_id": incident.id})
        return "resident_safe"
    return "label_only"


def incident_stats() -> dict[str, Any]:
    """Counts used by the dashboard and the false-alarm-rate calculation."""
    try:
        with session_scope() as session:
            total = session.query(IncidentRecord).count()
            confirmed = session.query(IncidentRecord).filter_by(verdict="TRUE").count()
            false_alarm = session.query(IncidentRecord).filter_by(verdict="FALSE").count()
            reviewed = confirmed + false_alarm
            return {
                "total": total,
                "confirmed": confirmed,
                "false_alarms": false_alarm,
                "unreviewed": total - reviewed,
                "false_alarm_rate": (round(false_alarm / reviewed, 4)
                                     if reviewed else None),
            }
    except Exception as exc:
        log.error("incident_stats failed: %s", exc)
        return {}


# ── dispatch ──────────────────────────────────────────────────────────────────
def save_dispatch(package) -> None:
    try:
        with session_scope() as session:
            session.merge(DispatchRecord(
                id=package.id,
                created_at=datetime.fromisoformat(package.created_at),
                incident_id=package.incident_id,
                incident_type=package.incident_type,
                level=package.level,
                tci=package.tci,
                user_name=package.user_name,
                user_address=package.user_address,
                user_phone=package.user_phone,
                camera_location=package.camera_location,
                evidence_bundle=package.evidence_bundle,
                evidence_hash=package.evidence_hash,
                authority=package.authority,
                status=package.status,
                payload_json=package.payload_json,
                # Only when the package carries one: merge() must not wipe a
                # request id linked separately.
                **({"request_id": package.request_id}
                   if getattr(package, "request_id", None) else {}),
            ))
    except Exception as exc:
        log.error("save_dispatch failed: %s", exc)
        return
    _emit("dispatch", lambda: get_dispatch(package.id) or {"id": package.id})


def _dispatch_dict(row) -> dict[str, Any]:
    return {
        "id": row.id, "created_at": str(row.created_at),
        "created_iso": _iso_utc(row.created_at),
        "incident_id": row.incident_id, "incident_type": row.incident_type,
        "level": row.level, "tci": row.tci, "user_name": row.user_name,
        "user_address": row.user_address, "user_phone": row.user_phone,
        "camera_location": row.camera_location,
        "evidence_bundle": row.evidence_bundle,
        "evidence_hash": row.evidence_hash, "authority": row.authority,
        "status": row.status,
        "request_id": row.request_id,
        "chosen_authority": row.chosen_authority,
        "sent_at": _iso_utc(row.sent_at), "sent_by": row.sent_by,
        "sent_to": json.loads(row.sent_to_json) if row.sent_to_json else [],
        "error": row.error, "expires_at": _iso_utc(row.expires_at),
        # Legacy alias for the existing dispatch.html template.
        "recommended_authority": row.authority,
    }


def get_dispatch(package_id: str) -> dict[str, Any] | None:
    try:
        with session_scope() as session:
            row = session.query(DispatchRecord).filter_by(id=package_id).first()
            return _dispatch_dict(row) if row else None
    except Exception as exc:
        log.error("get_dispatch failed: %s", exc)
        return None


def latest_pending_dispatch() -> dict[str, Any] | None:
    try:
        with session_scope() as session:
            row = (session.query(DispatchRecord).filter_by(status="PENDING")
                   .order_by(DispatchRecord.created_at.desc()).first())
            return _dispatch_dict(row) if row else None
    except Exception as exc:
        log.error("latest_pending_dispatch failed: %s", exc)
        return None


def all_dispatches(limit: int = 50) -> list[dict[str, Any]]:
    try:
        with session_scope() as session:
            rows = (session.query(DispatchRecord)
                    .order_by(DispatchRecord.created_at.desc())
                    .limit(limit).all())
            return [_dispatch_dict(r) for r in rows]
    except Exception as exc:
        log.error("all_dispatches failed: %s", exc)
        return []


#: Package statuses (docs/7 §6.1). Only PENDING and FAILED may still be sent.
DISPATCH_STATUSES = ("PENDING", "SENT", "CANCELLED", "EXPIRED", "FAILED")


def update_dispatch_status(package_id: str, status: str, **fields: Any) -> None:
    """Set a package's status, plus any of the M10 columns (``sent_at``,
    ``sent_by``, ``chosen_authority``, ``error``, ``request_id``,
    ``expires_at``; ``sent_to`` as a list)."""
    allowed = {"sent_at", "sent_by", "chosen_authority", "error", "request_id",
               "expires_at"}
    try:
        with session_scope() as session:
            row = session.query(DispatchRecord).filter_by(id=package_id).first()
            if row is None:
                return
            row.status = status
            row.updated_at = datetime.now(timezone.utc)
            for key, value in fields.items():
                if key == "sent_to":
                    row.sent_to_json = json.dumps(value, default=str)
                elif key in allowed:
                    setattr(row, key, value)
    except Exception as exc:
        log.error("update_dispatch_status failed: %s", exc)
        return
    _emit("dispatch", lambda: get_dispatch(package_id) or {"id": package_id})


# ── retention ─────────────────────────────────────────────────────────────────
def prune(retention_days: int = 30) -> int:
    """Delete reviewed low-severity incidents older than the retention window.

    Level 4 and 5 incidents are never pruned, and neither is anything an
    operator has not reviewed — deleting unreviewed events would destroy the
    labelled data the fusion retraining depends on.

    The decisions' hard caps (``settings.retention``: SOS snapshot 30 d,
    unreviewed 180 d, TRUE 1 y, audit 1 y) override these exemptions; they and
    the vault tombstones are the integration step's (docs/7 §6.2).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    try:
        with session_scope() as session:
            deleted = (session.query(IncidentRecord)
                       .filter(IncidentRecord.started_at < cutoff,
                               IncidentRecord.peak_level < 4,
                               IncidentRecord.verdict.isnot(None))
                       .delete(synchronize_session=False))
            if deleted:
                log.info("pruned %d incident(s) older than %d days",
                         deleted, retention_days)
            return int(deleted)
    except Exception as exc:
        log.error("prune failed: %s", exc)
        return 0


def link_incident_request(incident_id: str, request_id: str) -> bool:
    """Record which assistance request an incident opened or joined."""
    try:
        with session_scope() as session:
            row = session.query(IncidentRecord).filter_by(id=incident_id).first()
            if row is None:
                return False
            row.request_id = request_id
            return True
    except Exception as exc:
        log.error("link_incident_request failed: %s", exc)
        return False


__all__ = ["init_db", "session_scope", "save_incident", "recent_incidents",
           "set_verdict", "incident_stats", "save_dispatch", "get_dispatch",
           "latest_pending_dispatch", "all_dispatches",
           "update_dispatch_status", "prune", "engine", "add_listener",
           "remove_listener", "LISTENER_KINDS", "DISPATCH_STATUSES",
           "link_incident_request"]
