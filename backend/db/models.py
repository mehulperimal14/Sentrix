# db/models.py
#
# ARCHITECTURE: SQLite database models via SQLAlchemy (sync).
# EventLog stores one row per processed frame at Level 2+ for audit trail.
# DispatchPackageModel stores emergency dispatch packages created at Level 4/5.
# AuthorizedPerson stores enrolled face identities for recognition.

from sqlalchemy import Column, Integer, String, DateTime, Boolean, Float, Text
from sqlalchemy.orm import declarative_base
from datetime import datetime

Base = declarative_base()


class AuthorizedPerson(Base):
    """Enrolled faces / known persons for identity verification."""
    __tablename__ = "authorized"

    id         = Column(Integer, primary_key=True, index=True)
    name       = Column(String(100))
    person_id  = Column(String(50), unique=True)
    image_path = Column(String(255))
    created_at = Column(DateTime, default=datetime.utcnow)


class EventLog(Base):
    """One row per threat event logged by SystemEngine (Level 2+)."""
    __tablename__ = "events"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    created_at         = Column(DateTime, default=datetime.utcnow, index=True)
    tci                = Column(Float)
    level              = Column(Integer, index=True)
    status             = Column(String(20))
    incident_type      = Column(String(50), index=True)
    reason             = Column(String(255))
    person_id          = Column(String(50), nullable=True)
    is_authorized      = Column(Boolean, default=False)
    snapshot_path      = Column(String(255), nullable=True)
    evidence_file      = Column(String(255), nullable=True)
    evidence_hash      = Column(String(64), nullable=True)
    engine_scores_json = Column(Text, nullable=True)
    camera_id          = Column(String(20), default="CAM_1")


class DispatchPackageModel(Base):
    """Emergency dispatch packages created at Level 4/5."""
    __tablename__ = "dispatch_packages"

    id                    = Column(String(36), primary_key=True)
    created_at            = Column(DateTime, default=datetime.utcnow, index=True)
    incident_type         = Column(String(50))
    level                 = Column(Integer)
    tci                   = Column(Float)
    user_name             = Column(String(100))
    user_address          = Column(Text)
    user_phone            = Column(String(20))
    camera_location       = Column(String(100))
    snapshot_url          = Column(Text, nullable=True)
    evidence_hash         = Column(String(64), nullable=True)
    recommended_authority = Column(String(10))
    status                = Column(String(20), default="PENDING", index=True)
    payload_json          = Column(Text)