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

from sentrix.paths import DB_PATH, ensure_runtime_dirs
from sentrix.storage.models import Base, DispatchRecord, IncidentRecord
from sentrix.utils.logging import get_logger

log = get_logger("database")

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


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
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
def save_incident(incident, session_id: str) -> None:
    """Persist a closed incident. One row per episode, not per frame.

    Every field comes from the incident itself, which accumulated them as the
    episode ran. The previous signature took the *current* TCIResult, so the
    row recorded whichever frame happened to close the incident — usually the
    quiet one that ended it, not the one that mattered.
    """
    try:
        with session_scope() as session:
            session.merge(IncidentRecord(
                id=incident.id,
                session_id=session_id,
                started_at=datetime.fromtimestamp(incident.started_wall,
                                                  tz=timezone.utc),
                ended_at=datetime.now(timezone.utc),
                duration_seconds=round(incident.duration, 2),
                peak_level=incident.peak_level,
                peak_tci=round(incident.peak_tci, 4),
                incident_type=incident.incident_type,
                reason=incident.peak_reason,
                frames=incident.frames,
                evidence_count=incident.evidence_count,
                snapshot_path=incident.snapshot_path,
                evidence_bundle=incident.evidence_bundle,
                evidence_hash=incident.evidence_hash,
                person_present=bool(incident.peak_features.get("vision", 0) > 0),
                authorized=False,
                peak_features_json=json.dumps(incident.peak_features),
                actions_json=json.dumps(sorted(incident.actions)),
            ))
    except Exception as exc:
        log.error("save_incident failed: %s", exc)


def recent_incidents(limit: int = 100) -> list[dict[str, Any]]:
    try:
        with session_scope() as session:
            rows = (session.query(IncidentRecord)
                    .order_by(IncidentRecord.started_at.desc())
                    .limit(limit).all())
            return [{
                "id": r.id,
                "started_at": str(r.started_at),
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


def set_verdict(incident_id: str, verdict: str, note: str | None = None) -> bool:
    """Record operator feedback: was this a real threat?

    This is the single highest-value piece of data the deployment produces —
    it is what a retrained fusion model learns from.
    """
    verdict = verdict.upper()
    if verdict not in ("TRUE", "FALSE"):
        return False
    try:
        with session_scope() as session:
            row = session.query(IncidentRecord).filter_by(id=incident_id).first()
            if row is None:
                return False
            row.verdict, row.verdict_note = verdict, note
            return True
    except Exception as exc:
        log.error("set_verdict failed: %s", exc)
        return False


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
            ))
    except Exception as exc:
        log.error("save_dispatch failed: %s", exc)


def _dispatch_dict(row) -> dict[str, Any]:
    return {
        "id": row.id, "created_at": str(row.created_at),
        "incident_id": row.incident_id, "incident_type": row.incident_type,
        "level": row.level, "tci": row.tci, "user_name": row.user_name,
        "user_address": row.user_address, "user_phone": row.user_phone,
        "camera_location": row.camera_location,
        "evidence_bundle": row.evidence_bundle,
        "evidence_hash": row.evidence_hash, "authority": row.authority,
        "status": row.status,
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


def update_dispatch_status(package_id: str, status: str) -> None:
    try:
        with session_scope() as session:
            row = session.query(DispatchRecord).filter_by(id=package_id).first()
            if row is not None:
                row.status = status
    except Exception as exc:
        log.error("update_dispatch_status failed: %s", exc)


# ── retention ─────────────────────────────────────────────────────────────────
def prune(retention_days: int = 30) -> int:
    """Delete reviewed low-severity incidents older than the retention window.

    Level 4 and 5 incidents are never pruned, and neither is anything an
    operator has not reviewed — deleting unreviewed events would destroy the
    labelled data the fusion retraining depends on.
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


__all__ = ["init_db", "session_scope", "save_incident", "recent_incidents",
           "set_verdict", "incident_stats", "save_dispatch", "get_dispatch",
           "latest_pending_dispatch", "all_dispatches",
           "update_dispatch_status", "prune", "engine"]
