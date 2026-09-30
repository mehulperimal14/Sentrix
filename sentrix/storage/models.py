"""SQLAlchemy models.

The important change is granularity. ``EventLog`` previously stored **one row
per processed frame** at level 2 and above. Because the fusion bug pinned the
system at level 2 permanently, that meant roughly six inserts per second,
indefinitely — around 500,000 rows a day for an empty room.

Rows here describe **incidents**, of which there are a handful per day.
Frame-level detail lives in the JSONL event log (``pipeline/events.py``), which
is append-only, compresses well, and is not in the hot path.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import declarative_base

Base = declarative_base()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IncidentRecord(Base):
    """One threat episode, written when the incident closes."""

    __tablename__ = "incidents"

    id = Column(String(36), primary_key=True)
    session_id = Column(String(32), index=True)
    started_at = Column(DateTime, default=_utcnow, index=True)
    ended_at = Column(DateTime, nullable=True)
    duration_seconds = Column(Float, default=0.0)

    peak_level = Column(Integer, index=True)
    peak_tci = Column(Float)
    incident_type = Column(String(32), index=True)
    reason = Column(String(255))

    frames = Column(Integer, default=0)
    evidence_count = Column(Integer, default=0)
    snapshot_path = Column(String(255), nullable=True)
    evidence_bundle = Column(String(128), nullable=True)
    evidence_hash = Column(String(64), nullable=True)

    person_present = Column(Boolean, default=False)
    authorized = Column(Boolean, default=False)
    camera_id = Column(String(32), default="CAM_0")

    peak_features_json = Column(Text, nullable=True)
    #: Per-class evidence votes at the peak tick (added by migration).
    trigger_json = Column(Text, nullable=True)
    actions_json = Column(Text, nullable=True)

    #: Operator feedback. This column is what turns the deployment into a
    #: labelled dataset: every confirmed or dismissed incident becomes ground
    #: truth for retraining the fusion model.
    verdict = Column(String(16), nullable=True, index=True)  # TRUE | FALSE | None
    verdict_note = Column(Text, nullable=True)


class DispatchRecord(Base):
    """Emergency package built at level 4 or above."""

    __tablename__ = "dispatch_packages"

    id = Column(String(36), primary_key=True)
    created_at = Column(DateTime, default=_utcnow, index=True)
    incident_id = Column(String(36), index=True, nullable=True)
    incident_type = Column(String(32))
    level = Column(Integer)
    tci = Column(Float)
    user_name = Column(String(100))
    user_address = Column(Text)
    user_phone = Column(String(24))
    camera_location = Column(String(100))
    evidence_bundle = Column(String(128), nullable=True)
    evidence_hash = Column(String(64), nullable=True)
    authority = Column(String(10))
    status = Column(String(16), default="PENDING", index=True)
    payload_json = Column(Text)


class EnrolledFace(Base):
    """Authorised resident enrolled for face verification."""

    __tablename__ = "enrolled_faces"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), index=True)
    filename = Column(String(255), unique=True)
    backend = Column(String(32))
    created_at = Column(DateTime, default=_utcnow)


__all__ = ["Base", "IncidentRecord", "DispatchRecord", "EnrolledFace"]
