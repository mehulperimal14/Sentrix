# db/database.py
#
# ARCHITECTURE: Synchronous SQLite database layer using SQLAlchemy.
# Switched from async to sync to simplify integration with sync AI engines.
# Provides get_session() context manager and CRUD helper functions used by
# system_engine.py, dispatch_service.py, and web routes.

import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Base, EventLog, DispatchPackageModel

DATABASE_URL = "sqlite:///./sentrix.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False}, echo=False)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def init_db():
    """Create all tables if they don't exist."""
    Base.metadata.create_all(bind=engine)
    print("[Database] Tables initialised.")


@contextmanager
def get_session():
    """Provide a transactional scope around a series of operations."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def log_event(result, scores: dict, snapshot_url: str = None, evidence_id: str = None):
    """Log an incident to SQLite."""
    if os.getenv("SENTRIX_EVAL_MODE") == "1":
        print("[Database-EVAL] Mock Log Event")
        return
    try:
        with get_session() as session:
            scores_copy = {k: v for k, v in scores.items() if isinstance(v, (int, float))}
            ev = EventLog(
                tci=round(result.tci, 4),
                level=result.level,
                status=result.status,
                incident_type=result.incident_type,
                reason=result.reason,
                is_authorized=not scores.get("unauthorized", False),
                snapshot_path=snapshot_path,
                evidence_file=evidence_meta.file if evidence_meta else None,
                evidence_hash=evidence_meta.sha256 if evidence_meta else None,
                engine_scores_json=json.dumps(scores_copy),
                camera_id="CAM_COMBINED",
            )
            session.add(ev)
    except Exception as e:
        print(f"[Database] log_event error: {e}")


def save_dispatch_package(pkg):
    """Insert a DispatchPackageModel row."""
    try:
        with get_session() as session:
            row = DispatchPackageModel(
                id=pkg.id,
                created_at=datetime.fromisoformat(pkg.created_at),
                incident_type=pkg.incident_type,
                level=pkg.level,
                tci=pkg.tci,
                user_name=pkg.user_name,
                user_address=pkg.user_address,
                user_phone=pkg.user_phone,
                camera_location=pkg.camera_location,
                snapshot_url=pkg.snapshot_url,
                evidence_hash=pkg.evidence_hash,
                recommended_authority=pkg.recommended_authority,
                status=pkg.status,
                payload_json=pkg.payload_json,
            )
            session.add(row)
    except Exception as e:
        print(f"[Database] save_dispatch_package error: {e}")


def get_dispatch_package(package_id):
    """Retrieve a DispatchPackageModel by id, or None."""
    try:
        with get_session() as session:
            return session.query(DispatchPackageModel).filter_by(id=package_id).first()
    except Exception as e:
        print(f"[Database] get_dispatch_package error: {e}")
        return None


def update_dispatch_status(package_id, new_status):
    """Update status of a dispatch package."""
    try:
        with get_session() as session:
            row = session.query(DispatchPackageModel).filter_by(id=package_id).first()
            if row:
                row.status = new_status
    except Exception as e:
        print(f"[Database] update_dispatch_status error: {e}")


def get_latest_pending_dispatch():
    """Return the most recent PENDING dispatch package dict, or None."""
    try:
        with get_session() as session:
            row = (
                session.query(DispatchPackageModel)
                .filter_by(status="PENDING")
                .order_by(DispatchPackageModel.created_at.desc())
                .first()
            )
            if row:
                return {
                    "id": row.id,
                    "incident_type": row.incident_type,
                    "level": row.level,
                    "tci": row.tci,
                    "user_name": row.user_name,
                    "user_address": row.user_address,
                    "user_phone": row.user_phone,
                    "camera_location": row.camera_location,
                    "snapshot_url": row.snapshot_url,
                    "evidence_hash": row.evidence_hash,
                    "recommended_authority": row.recommended_authority,
                    "status": row.status,
                    "created_at": str(row.created_at),
                }
            return None
    except Exception as e:
        print(f"[Database] get_latest_pending_dispatch error: {e}")
        return None


def get_recent_events(limit=50):
    """Return the most recent EventLog rows as dicts."""
    try:
        with get_session() as session:
            rows = (
                session.query(EventLog)
                .order_by(EventLog.created_at.desc())
                .limit(limit)
                .all()
            )
            result = []
            for r in rows:
                result.append({
                    "id": r.id,
                    "created_at": str(r.created_at),
                    "tci": r.tci,
                    "level": r.level,
                    "status": r.status,
                    "incident_type": r.incident_type,
                    "reason": r.reason,
                    "snapshot_path": r.snapshot_path,
                    "evidence_file": r.evidence_file,
                    "evidence_hash": r.evidence_hash,
                    "camera_id": r.camera_id,
                })
            return result
    except Exception as e:
        print(f"[Database] get_recent_events error: {e}")
        return []


def get_all_dispatch_packages():
    """Return all dispatch packages as dicts."""
    try:
        with get_session() as session:
            rows = (
                session.query(DispatchPackageModel)
                .order_by(DispatchPackageModel.created_at.desc())
                .all()
            )
            return [
                {
                    "id": r.id,
                    "created_at": str(r.created_at),
                    "incident_type": r.incident_type,
                    "level": r.level,
                    "tci": r.tci,
                    "user_name": r.user_name,
                    "user_address": r.user_address,
                    "recommended_authority": r.recommended_authority,
                    "status": r.status,
                    "snapshot_url": r.snapshot_url,
                    "evidence_hash": r.evidence_hash,
                }
                for r in rows
            ]
    except Exception as e:
        print(f"[Database] get_all_dispatch_packages error: {e}")
        return []


# ── Retention helpers ──────────────────────────────────────────────────────────

def prune_old_events(days: int = None):
    """
    Delete EventLog rows older than `days` days where level < 4.
    Preserves all Level 4/5 records (critical incidents) regardless of age.
    `days` defaults to the RETENTION_DAYS env var, or 30 if not set.
    """
    if days is None:
        try:
            days = int(os.getenv("RETENTION_DAYS", "30"))
        except ValueError:
            days = 30

    cutoff = datetime.utcnow() - timedelta(days=days)
    try:
        with get_session() as session:
            deleted = (
                session.query(EventLog)
                .filter(EventLog.created_at < cutoff, EventLog.level < 4)
                .delete(synchronize_session=False)
            )
        if deleted:
            print(f"[Database] Pruned {deleted} old event(s) older than {days} days.")
    except Exception as e:
        print(f"[Database] prune_old_events error: {e}")


def prune_old_snapshots(days: int = None):
    """
    Delete JPEG snapshot files from static/alerts/ that are older than `days` days.
    Encrypted evidence bundles (.enc + .json) are NEVER auto-deleted.
    """
    if days is None:
        try:
            days = int(os.getenv("RETENTION_DAYS", "30"))
        except ValueError:
            days = 30

    alerts_dir = "static/alerts"
    if not os.path.isdir(alerts_dir):
        return

    cutoff_ts = datetime.utcnow().timestamp() - (days * 86400)
    deleted = 0
    try:
        for fname in os.listdir(alerts_dir):
            if not fname.lower().endswith((".jpg", ".jpeg", ".png")):
                continue  # Only delete snapshot images, never .enc/.json
            fpath = os.path.join(alerts_dir, fname)
            try:
                if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff_ts:
                    os.remove(fpath)
                    deleted += 1
            except Exception:
                continue
        if deleted:
            print(f"[Database] Pruned {deleted} old snapshot(s) older than {days} days.")
    except Exception as e:
        print(f"[Database] prune_old_snapshots error: {e}")