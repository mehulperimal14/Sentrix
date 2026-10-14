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

from sqlalchemy import (Boolean, Column, DateTime, Float, Index, Integer,
                        LargeBinary, String, Text)
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
    #: Who set the verdict ("lc:device_admin", "portal:<user id>", "desk:<id>")
    #: and when (UTC). Added by migration (docs/7 §6).
    verdict_by = Column(String(64), nullable=True)
    verdict_at = Column(DateTime, nullable=True)
    #: The assistance request this incident opened or was attached to.
    request_id = Column(String(36), nullable=True, index=True)


class DispatchRecord(Base):
    """Emergency package: one per assistance request (L3+), or per L4 incident
    when no SOS service runs (the pre-portal behaviour)."""

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
    #: PENDING | SENT | CANCELLED | EXPIRED | FAILED (docs/7 §6).
    status = Column(String(16), default="PENDING", index=True)
    payload_json = Column(Text)
    # Added by migration (docs/7 §6).
    request_id = Column(String(36), nullable=True, index=True)
    #: The authority actually chosen at approval: POLICE | FIRE | MEDICAL.
    chosen_authority = Column(String(10), nullable=True)
    sent_at = Column(DateTime, nullable=True)
    sent_by = Column(String(64), nullable=True)
    #: Masked recipients and channels the package went to (JSON list).
    sent_to_json = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)


class EnrolledFace(Base):
    """Authorised resident enrolled for face verification."""

    __tablename__ = "enrolled_faces"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), index=True)
    filename = Column(String(255), unique=True)
    backend = Column(String(32))
    created_at = Column(DateTime, default=_utcnow)
    # Added by migration: the household member this face belongs to, the
    # consent that allowed it, and who enrolled it (docs/7 §6).
    member_id = Column(String(36), nullable=True)
    consent_id = Column(String(36), nullable=True)
    enrolled_by = Column(String(64), nullable=True)


# ── portal and SOS (docs/7_PORTAL_AND_SOS.md §6) ──────────────────────────────
class AssistanceRequestRecord(Base):
    """One SOS assistance request (decisions D8, D9).

    Created on the device at a confirmed L3 commit with an id the device makes
    (uuid4), so the cloud copy has the same id. ``state`` is one of
    ``sentrix.sos.states.STATES``. The policy (window, safety defaults,
    contacts) is frozen into ``policy_json`` when the request opens; later
    settings changes never reach it.
    """

    __tablename__ = "assistance_requests"

    id = Column(String(36), primary_key=True)
    incident_id = Column(String(36), index=True)             # the opening incident
    incident_ids_json = Column(Text, default="[]")            # every attached incident
    state = Column(String(16), index=True)
    urgency = Column(String(8), default="NORMAL")             # NORMAL | HIGH
    #: sentrix.sos.states.FLAGS -> bool (duress, device_offline, ...).
    flags_json = Column(Text, default="{}")
    trigger_level = Column(Integer)
    peak_level = Column(Integer)
    incident_type = Column(String(32))                        # weapon | fire | intrusion
    evidence_state = Column(String(16))                       # confirmed | corroborated
    created_at = Column(DateTime, default=_utcnow, index=True)
    updated_at = Column(DateTime, default=_utcnow)
    expires_at = Column(DateTime, nullable=True)
    seen_at = Column(DateTime, nullable=True)
    responded_at = Column(DateTime, nullable=True)
    responded_by = Column(String(64), nullable=True)          # actor id
    response_channel = Column(String(16), nullable=True)      # sentrix.sos.states.CHANNELS
    authority = Column(String(10), nullable=True)             # POLICE | FIRE | MEDICAL
    sos_text_full = Column(Text, nullable=True)
    sos_text_sms = Column(Text, nullable=True)
    snapshot_path = Column(String(255), nullable=True)
    evidence_bundle = Column(String(128), nullable=True)
    evidence_hash = Column(String(64), nullable=True)
    package_id = Column(String(36), nullable=True)
    policy_json = Column(Text, default="{}")
    profile_version = Column(Integer, nullable=True)
    camera_id = Column(String(32), default="CAM_0")
    test_mode = Column(Boolean, default=True)
    #: Bumped on every transition; the cloud applies events in this order.
    version = Column(Integer, default=0)
    #: device | cloud: who is the authority right now (docs/7 §7.6).
    authority_side = Column(String(8), default="device")
    #: True once the Portal acked the creation event.
    cloud_synced = Column(Boolean, default=False)
    #: "Request help now" on an EXPIRED/DECLINED request opens a linked one.
    parent_request_id = Column(String(36), nullable=True)
    reminder_step = Column(Integer, default=0)
    next_timer_at = Column(DateTime, nullable=True, index=True)
    repage_count = Column(Integer, default=0)
    outcome = Column(String(32), nullable=True)               # Desk resolve outcome
    session_id = Column(String(32), nullable=True)


class RequestEventRecord(Base):
    """Audit trail of one request: every transition and notable event.

    Hash-chained per (request, origin) (``sentrix.storage.requests.
    append_event``): the device's events and the cloud's events (imported
    from ``sos.sync``) form two chains, each numbered 1, 2, 3 ... so neither
    side ever uses up the other's sequence numbers. ``restricted`` rows
    (duress) are never shown to owners or members.
    """

    __tablename__ = "request_events"
    __table_args__ = (Index("ux_request_events_origin_seq", "request_id", "origin",
                            "seq", unique=True),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    request_id = Column(String(36), index=True)
    seq = Column(Integer)                                     # 1, 2, 3 ... per (request, origin)
    at = Column(DateTime, default=_utcnow)
    from_state = Column(String(16), nullable=True)
    to_state = Column(String(16), nullable=True)
    event = Column(String(32))                                # sentrix.sos.states.EVENTS
    actor_type = Column(String(16))                           # sentrix.sos.states.ACTORS
    actor_id = Column(String(64), nullable=True)
    channel = Column(String(16), nullable=True)
    detail_json = Column(Text, default="{}")
    restricted = Column(Boolean, default=False)
    origin = Column(String(8), default="device")              # device | cloud
    prev_hash = Column(String(64), nullable=True)
    hash = Column(String(64))
    sig = Column(String(64), nullable=True)                   # optional HMAC (reserved)


class LinkOutboxRecord(Base):
    """Device -> Portal events kept until acked (at-least-once, link protocol v1).

    ``seq`` is the device-wide event number the Portal dedupes on. Priority 0
    (SOS) is sent before priority 1 (everything else); ``seq`` never changes.
    """

    __tablename__ = "link_outbox"
    #: AUTOINCREMENT on new databases: a seq is never reused, even after the
    #: newest row is pruned (existing databases keep link_store's guard).
    __table_args__ = {"sqlite_autoincrement": True}

    seq = Column(Integer, primary_key=True, autoincrement=True)
    kind = Column(String(32), index=True)                     # docs/7 §5.5
    priority = Column(Integer, default=1, index=True)
    dedupe_key = Column(String(128), unique=True, nullable=True)
    payload_json = Column(Text)
    created_at = Column(DateTime, default=_utcnow)
    sent_at = Column(DateTime, nullable=True)
    acked_at = Column(DateTime, nullable=True, index=True)
    attempts = Column(Integer, default=0)


class ProfileCacheRecord(Base):
    """The household profile pushed by the Portal, encrypted at rest.

    ``blob`` = AES-256-GCM (nonce || ciphertext), key from HKDF over
    EVIDENCE_AES_KEY with info ``b"sentrix-profile-v1"``: protection against
    casual copying only (the key is on the same card). Carries SOS/duress PIN
    hashes only, never Portal password hashes (decision D14).
    """

    __tablename__ = "profile_cache"

    version = Column(Integer, primary_key=True)
    received_at = Column(DateTime, default=_utcnow)
    sha256 = Column(String(64))
    blob = Column(LargeBinary)
    active = Column(Boolean, default=False, index=True)


class NotificationRecord(Base):
    """One outbound message in the device's channel-router outbox.

    ``dedupe_key`` = ``request|recipient_ref|channel|step``: one reminder step
    never reaches the same recipient twice on one channel.
    """

    __tablename__ = "notifications"

    id = Column(String(36), primary_key=True)
    request_id = Column(String(36), nullable=True, index=True)
    recipient_ref = Column(String(64))                        # owner | contact:<id> | desk | user:<id>
    recipient_masked = Column(String(64))
    address = Column(String(128))                             # E.164 / e-mail / tg:<chat>
    channel = Column(String(16), index=True)
    template = Column(String(48))
    step = Column(String(32))
    dedupe_key = Column(String(160), unique=True)
    #: QUEUED | SENDING | SENT | DELIVERED | FAILED | SUPPRESSED
    status = Column(String(12), default="QUEUED", index=True)
    attempts = Column(Integer, default=0)
    next_attempt_at = Column(DateTime, nullable=True, index=True)
    provider_id = Column(String(128), nullable=True)
    error = Column(Text, nullable=True)
    subject = Column(String(200), nullable=True)
    body = Column(Text)
    #: Rendering the transport needs and the dedupe key does not: recipient
    #: name and role, e-mail HTML, Telegram buttons, urgency (JSON).
    meta_json = Column(Text, nullable=True)
    test_mode = Column(Boolean, default=True)
    created_at = Column(DateTime, default=_utcnow)
    sent_at = Column(DateTime, nullable=True)


class RemoteAuditRecord(Base):
    """Every tunnelled request and link command the device served or refused.

    The device does not trust the Portal: media, frames, gallery changes,
    siren tests and SMS sends from the cloud are rate-limited, checked against
    the profile's consent flags, and recorded here (shown on the Node page).
    """

    __tablename__ = "remote_audit"

    id = Column(Integer, primary_key=True, autoincrement=True)
    at = Column(DateTime, default=_utcnow, index=True)
    user_id = Column(String(64), nullable=True)
    user_name = Column(String(100), nullable=True)
    role = Column(String(16), nullable=True)
    kind = Column(String(16))                                 # tunnel | command | video | state
    target = Column(String(255))                              # method + path, or command name
    allowed = Column(Boolean)
    status = Column(Integer, nullable=True)
    reason = Column(String(255), nullable=True)
    request_id = Column(String(64), nullable=True)


__all__ = ["Base", "IncidentRecord", "DispatchRecord", "EnrolledFace",
           "AssistanceRequestRecord", "RequestEventRecord", "LinkOutboxRecord",
           "ProfileCacheRecord", "NotificationRecord", "RemoteAuditRecord"]
