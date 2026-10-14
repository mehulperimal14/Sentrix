"""Every Portal table (docs/7 §6.2). Created by ``Database.init``.

Conventions: string UUID primary keys (``_uuid``), timezone-aware UTC
timestamps, ``JSON`` columns for structured fields, ``*_enc`` columns hold
AES-GCM ciphertext from ``security.seal`` (phones, medical notes, TOTP
secrets), ``*_hash`` columns hold Argon2id (passwords, PINs) or HMAC-SHA256
(sessions, codes, tokens) digests: never the secret itself. Device-made ids
(assistance requests, incidents, packages) keep the device's value.

Tenant isolation: every row a customer can reach carries ``household_id``;
routes filter on the session's household (gate P2).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer,
                        LargeBinary, String, Text, UniqueConstraint)

from sentrix_portal.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


TS = DateTime(timezone=True)

#: Portal roles (docs/7 §10). Contacts have no login (token pages only).
ROLES = ("company_admin", "operator", "owner", "member")
USER_STATUSES = ("pending_activation", "active", "disabled", "closed")
DEVICE_STATES = ("UNASSIGNED", "ASSIGNED", "CLAIMED", "RETIRED")
CONSENT_STATUSES = ("PENDING", "CONFIRMED", "DECLINED", "REVOKED")
#: Consent purposes (DPDP Rule 3: one toggle each).
CONSENT_PURPOSES = ("account_sos", "face_recognition", "medical_notes", "sos_snapshot",
                    "remote_view", "desk_live_view", "model_improvement")
#: ``action_tokens.purpose`` values (``phone_verify``: T-ACCT's mobile
#: verification codes). A contact's opt-out is a ``consents``
#: row with ``subject_type="contact"``, ``granted_at`` NULL and
#: ``withdrawn_at`` = the decision time (retention counts 7 days from it).
TOKEN_PURPOSES = ("contact_page", "contact_consent", "action_link", "telegram_link",
                  "email_verify", "phone_verify")


# ── accounts ──────────────────────────────────────────────────────────────────
class Household(Base):
    __tablename__ = "households"

    id = Column(String(36), primary_key=True, default=_uuid)
    customer_code = Column(String(16), unique=True, index=True)        # SX-0007
    display_name = Column(String(100))
    #: active | disabled (admin deactivation) | erasing (erasure scheduled;
    #: closed_at = when it was requested) | closed
    status = Column(String(16), default="active")
    language = Column(String(4), default="en")
    # address (onboarding step 2)
    house = Column(String(80))
    building = Column(String(80))
    street = Column(String(120))
    locality = Column(String(120))
    landmark = Column(String(120))
    city = Column(String(80))
    district = Column(String(80))
    state = Column(String(40))
    pincode = Column(String(6))
    floor = Column(String(20))
    lat = Column(Float)
    lon = Column(Float)
    entry_note = Column(String(120))
    # household (step 3)
    occupants = Column(JSON, default=dict)            # {"adults", "children", "seniors"}
    pets = Column(String(60))
    medical_enc = Column(Text)
    include_medical = Column(Boolean, default=False)
    nearest = Column(JSON, default=dict)              # {"police": {name, phone}, ...}
    # alerts and SOS (step 6); frozen per request when one opens
    sos_window_s = Column(Integer, default=1800)
    safety_defaults = Column(Boolean, default=True)
    sos_pin_hash = Column(String(255))
    duress_pin_hash = Column(String(255))
    pin_version = Column(Integer, default=0)
    channels = Column(JSON, default=dict)             # {"telegram": bool, "email": bool, "sms": bool}
    profile_version = Column(Integer, default=0)
    onboarding_step = Column(Integer, default=1)
    onboarding_completed_at = Column(TS)
    created_at = Column(TS, default=utcnow)
    created_by = Column(String(36))
    closed_at = Column(TS)


class User(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=_uuid)
    username = Column(String(64), unique=True, index=True, nullable=False)
    display_name = Column(String(100))
    email = Column(String(254), index=True)
    email_verified_at = Column(TS)
    phone_enc = Column(Text)
    phone_verified_at = Column(TS)
    role = Column(String(16), nullable=False)                           # ROLES
    household_id = Column(String(36), ForeignKey("households.id"), index=True)
    status = Column(String(20), default="pending_activation")           # USER_STATUSES
    #: Null until the customer activates; the company never sets it (D14).
    password_hash = Column(String(255))
    password_changed_at = Column(TS)
    totp_secret_enc = Column(Text)
    totp_enabled = Column(Boolean, default=False)
    recovery_codes = Column(JSON, default=list)       # HMAC digests, single use
    language = Column(String(4), default="en")
    created_at = Column(TS, default=utcnow)
    created_by = Column(String(36))
    last_login_at = Column(TS)
    last_login_ip_hash = Column(String(64))


class ActivationCode(Base):
    """Single-use activation / reset code (72 h, hashed, rate limited; D14)."""

    __tablename__ = "activation_codes"

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String(36), ForeignKey("users.id"), index=True, nullable=False)
    purpose = Column(String(16), default="activate")                    # activate | reset
    code_hash = Column(String(64), unique=True, nullable=False)
    created_at = Column(TS, default=utcnow)
    created_by = Column(String(36))
    expires_at = Column(TS, nullable=False)
    used_at = Column(TS)
    attempts = Column(Integer, default=0)


class UserSession(Base):
    """Server-side session; the cookie holds the raw id, this row its digest."""

    __tablename__ = "sessions"

    id_hash = Column(String(64), primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id"), index=True, nullable=False)
    created_at = Column(TS, default=utcnow)
    expires_at = Column(TS, nullable=False)
    last_seen_at = Column(TS, default=utcnow)
    ip_hash = Column(String(64))
    user_agent = Column(String(200))
    csrf_token = Column(String(64), nullable=False)
    totp_verified = Column(Boolean, default=False)
    step_up_at = Column(TS)
    revoked_at = Column(TS)


class LoginAttempt(Base):
    """Back-off per (username, IP): never a hard lockout that blocks help."""

    __tablename__ = "login_attempts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), index=True)
    ip_hash = Column(String(64), index=True)
    at = Column(TS, default=utcnow, index=True)
    success = Column(Boolean, default=False)


class HouseholdMember(Base):
    __tablename__ = "household_members"

    id = Column(String(36), primary_key=True, default=_uuid)
    household_id = Column(String(36), ForeignKey("households.id"), index=True, nullable=False)
    name = Column(String(80), nullable=False)
    relation = Column(String(40))
    age_band = Column(String(8))                                        # adult | minor | senior
    user_id = Column(String(36), ForeignKey("users.id"))
    consent_id = Column(String(36))
    created_at = Column(TS, default=utcnow)


class EmergencyContact(Base):
    """SOS goes only to CONFIRMED contacts."""

    __tablename__ = "emergency_contacts"

    id = Column(String(36), primary_key=True, default=_uuid)
    household_id = Column(String(36), ForeignKey("households.id"), index=True, nullable=False)
    name = Column(String(80), nullable=False)
    relation = Column(String(40))
    phone_enc = Column(Text)
    email = Column(String(254))
    telegram_chat_id = Column(String(32))
    priority = Column(Integer, default=1)
    can_reach_30min = Column(Boolean, default=False)
    consent_status = Column(String(12), default="PENDING")              # CONSENT_STATUSES
    confirmed_at = Column(TS)
    created_at = Column(TS, default=utcnow)


class Consent(Base):
    __tablename__ = "consents"

    id = Column(String(36), primary_key=True, default=_uuid)
    household_id = Column(String(36), ForeignKey("households.id"), index=True)
    subject_type = Column(String(8))                                    # user | contact | member
    subject_id = Column(String(36), index=True)
    purpose = Column(String(24))                                        # CONSENT_PURPOSES
    notice_version = Column(String(16))
    granted_at = Column(TS, default=utcnow)
    withdrawn_at = Column(TS)
    method = Column(String(24))                                         # wizard | settings | contact_page
    actor_user_id = Column(String(36))


# ── devices ───────────────────────────────────────────────────────────────────
class Device(Base):
    __tablename__ = "devices"

    id = Column(String(36), primary_key=True, default=_uuid)            # device_id
    serial = Column(String(16), unique=True, index=True, nullable=False)  # SX5-000123
    household_id = Column(String(36), ForeignKey("households.id"), index=True)
    name = Column(String(60))
    state = Column(String(12), default="UNASSIGNED")                    # DEVICE_STATES
    claim_code_hash = Column(String(64))
    bootstrap_hash = Column(String(64))
    token_hash = Column(String(64), index=True)
    token_rotated_at = Column(TS)
    firmware = Column(String(64))
    last_seen_at = Column(TS)
    last_hello = Column(JSON, default=dict)
    last_heartbeat = Column(JSON, default=dict)
    online = Column(Boolean, default=False)
    armed = Column(Boolean, default=False)
    #: Highest outbox seq stored from this device (link protocol ``ack``).
    last_acked_seq = Column(Integer, default=0)
    profile_version_acked = Column(Integer, default=0)
    created_at = Column(TS, default=utcnow)
    assigned_at = Column(TS)
    claimed_at = Column(TS)
    claimed_by = Column(String(36))


class ClaimAttempt(Base):
    __tablename__ = "claim_attempts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    serial = Column(String(16), index=True)
    user_id = Column(String(36), index=True)
    ip_hash = Column(String(64))
    at = Column(TS, default=utcnow, index=True)
    success = Column(Boolean, default=False)


class Camera(Base):
    __tablename__ = "cameras"
    __table_args__ = (UniqueConstraint("device_id", "camera_id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    device_id = Column(String(36), ForeignKey("devices.id"), index=True, nullable=False)
    camera_id = Column(String(32), default="CAM_0")
    room_label = Column(String(40))
    floor = Column(String(20))


class DeviceEvent(Base):
    """Every outbox event received (dedupe on (device, seq))."""

    __tablename__ = "device_events"

    device_id = Column(String(36), primary_key=True)
    seq = Column(Integer, primary_key=True)
    kind = Column(String(32), index=True)
    received_at = Column(TS, default=utcnow)
    payload = Column(JSON)


class DeviceCommand(Base):
    """Commands queued for a device (e.g. a verdict set while it was offline)."""

    __tablename__ = "device_commands"

    id = Column(String(36), primary_key=True, default=_uuid)
    device_id = Column(String(36), ForeignKey("devices.id"), index=True, nullable=False)
    name = Column(String(32))                                           # protocol.COMMANDS
    args = Column(JSON, default=dict)
    user_id = Column(String(36))
    created_at = Column(TS, default=utcnow)
    sent_at = Column(TS)
    result = Column(JSON)
    status = Column(String(12), default="QUEUED")                       # QUEUED | SENT | DONE | FAILED


# ── SOS (cloud mirror and authority) ──────────────────────────────────────────
class AssistanceRequest(Base):
    """Same id and fields as the device row (sentrix.storage.models)."""

    __tablename__ = "assistance_requests"

    id = Column(String(36), primary_key=True)
    device_id = Column(String(36), ForeignKey("devices.id"), index=True)
    household_id = Column(String(36), ForeignKey("households.id"), index=True)
    incident_ids = Column(JSON, default=list)
    state = Column(String(16), index=True)
    urgency = Column(String(8), default="NORMAL")
    flags = Column(JSON, default=dict)
    trigger_level = Column(Integer)
    peak_level = Column(Integer)
    incident_type = Column(String(32))
    evidence_state = Column(String(16))
    created_at = Column(TS, default=utcnow, index=True)
    updated_at = Column(TS, default=utcnow)
    expires_at = Column(TS)
    seen_at = Column(TS)
    responded_at = Column(TS)
    responded_by = Column(String(64))
    response_channel = Column(String(16))
    authority = Column(String(10))
    sos_text_full = Column(Text)
    sos_text_sms = Column(Text)
    policy = Column(JSON, default=dict)
    profile_version = Column(Integer)
    test_mode = Column(Boolean, default=True)
    version = Column(Integer, default=0)
    authority_side = Column(String(8), default="device")                # device | cloud
    parent_request_id = Column(String(36))
    package_id = Column(String(36))
    desk_operator_id = Column(String(36))
    desk_acknowledged_at = Column(TS)
    outcome = Column(String(32))
    repage_count = Column(Integer, default=0)
    next_timer_at = Column(TS, index=True)


class RequestEvent(Base):
    __tablename__ = "request_events"
    __table_args__ = (UniqueConstraint("request_id", "seq", "origin"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    request_id = Column(String(36), ForeignKey("assistance_requests.id"), index=True)
    seq = Column(Integer)                                               # per (request, origin)
    origin = Column(String(8), default="device")                       # device | cloud
    at = Column(TS, default=utcnow)
    #: The sender's ISO ``at`` string, verbatim: the chain hash covers it, and
    #: a parsed-and-reformatted timestamp may not reproduce the same bytes.
    at_text = Column(String(40))
    received_at = Column(TS, default=utcnow)
    from_state = Column(String(16))
    to_state = Column(String(16))
    event = Column(String(32))
    actor_type = Column(String(16))
    actor_id = Column(String(64))
    channel = Column(String(16))
    detail = Column(JSON, default=dict)
    restricted = Column(Boolean, default=False)
    prev_hash = Column(String(64))
    hash = Column(String(64))


class SosSnapshot(Base):
    """One consented SOS snapshot per request (may show faces; 30 days; D12)."""

    __tablename__ = "sos_snapshots"

    id = Column(String(36), primary_key=True, default=_uuid)
    request_id = Column(String(36), ForeignKey("assistance_requests.id"), unique=True)
    device_id = Column(String(36), index=True)
    content_type = Column(String(32), default="image/jpeg")
    sha256 = Column(String(64))
    data = Column(LargeBinary)
    created_at = Column(TS, default=utcnow)
    expires_at = Column(TS, index=True)


class Notification(Base):
    """Portal channel-router outbox (same semantics as the device table)."""

    __tablename__ = "notifications"

    id = Column(String(36), primary_key=True, default=_uuid)
    request_id = Column(String(36), index=True)
    household_id = Column(String(36), index=True)
    recipient_ref = Column(String(64))
    recipient_masked = Column(String(64))
    address_enc = Column(Text)
    channel = Column(String(16), index=True)
    template = Column(String(48))
    step = Column(String(32))
    dedupe_key = Column(String(160), unique=True)
    status = Column(String(12), default="QUEUED", index=True)
    attempts = Column(Integer, default=0)
    next_attempt_at = Column(TS, index=True)
    provider_id = Column(String(128))
    error = Column(Text)
    subject = Column(String(200))
    body = Column(Text)
    #: Recipient name/role, e-mail HTML, Telegram buttons, urgency
    #: (``channels.base.message_meta``), so ``due`` rebuilds the exact message.
    meta = Column(JSON, default=dict)
    test_mode = Column(Boolean, default=True)
    created_at = Column(TS, default=utcnow)
    sent_at = Column(TS)


class ActionToken(Base):
    """Single-use bearer tokens: contact pages, e-mail/SMS action links,
    Telegram linking, e-mail verification. Bound to request + recipient;
    expire with the request (+2 h); revoked on decline; every open logged."""

    __tablename__ = "action_tokens"

    id = Column(String(36), primary_key=True, default=_uuid)
    token_hash = Column(String(64), unique=True, nullable=False)
    purpose = Column(String(20))                                        # TOKEN_PURPOSES
    household_id = Column(String(36), index=True)
    request_id = Column(String(36), index=True)
    subject_type = Column(String(8))                                    # user | contact
    subject_id = Column(String(36))
    #: Telegram callback nonce (16 chars, ``channels.base.new_callback_nonce``):
    #: callback_data is "<nonce>:<letter>" (64-byte limit), looked up here.
    nonce = Column(String(32), unique=True, index=True)
    created_at = Column(TS, default=utcnow)
    expires_at = Column(TS, nullable=False)
    used_at = Column(TS)
    revoked_at = Column(TS)
    opened_count = Column(Integer, default=0)


class TelegramLink(Base):
    __tablename__ = "telegram_links"

    id = Column(String(36), primary_key=True, default=_uuid)
    subject_type = Column(String(8))                                    # user | contact
    subject_id = Column(String(36), index=True)
    household_id = Column(String(36), index=True)
    chat_id = Column(String(32), index=True)                            # private chats only
    linked_at = Column(TS, default=utcnow)
    status = Column(String(12), default="active")                       # active | revoked


class PushSubscription(Base):
    """Web push (Tier 2)."""

    __tablename__ = "push_subscriptions"

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String(36), ForeignKey("users.id"), index=True)
    endpoint = Column(Text)
    p256dh = Column(String(200))
    auth = Column(String(64))
    created_at = Column(TS, default=utcnow)


# ── mirrors (metadata only; the device stays the source of truth) ────────────
class IncidentMirror(Base):
    __tablename__ = "incidents_mirror"

    id = Column(String(36), primary_key=True)
    device_id = Column(String(36), index=True)
    household_id = Column(String(36), index=True)
    started_at = Column(TS, index=True)
    ended_at = Column(TS)
    peak_level = Column(Integer, index=True)
    peak_tci = Column(Float)
    incident_type = Column(String(32))
    reason = Column(String(255))
    verdict = Column(String(8))
    verdict_by = Column(String(64))
    verdict_at = Column(TS)
    request_id = Column(String(36))
    data = Column(JSON, default=dict)
    updated_at = Column(TS, default=utcnow)


class DispatchMirror(Base):
    __tablename__ = "dispatch_mirror"

    id = Column(String(36), primary_key=True)
    device_id = Column(String(36), index=True)
    household_id = Column(String(36), index=True)
    incident_id = Column(String(36))
    request_id = Column(String(36), index=True)
    status = Column(String(16))
    authority = Column(String(10))
    chosen_authority = Column(String(10))
    created_at = Column(TS)
    sent_at = Column(TS)
    sent_by = Column(String(64))
    data = Column(JSON, default=dict)


class EvidenceMetaMirror(Base):
    __tablename__ = "evidence_meta_mirror"

    bundle_id = Column(String(128), primary_key=True)
    device_id = Column(String(36), index=True)
    household_id = Column(String(36), index=True)
    incident_id = Column(String(36))
    created_at = Column(TS)
    level = Column(Integer)
    sha256 = Column(String(64))
    data = Column(JSON, default=dict)


class SnapshotMirror(Base):
    __tablename__ = "snapshot_mirror"

    name = Column(String(128), primary_key=True)
    device_id = Column(String(36), primary_key=True)
    household_id = Column(String(36), index=True)
    incident_id = Column(String(36))
    created_at = Column(TS)


# ── audit ─────────────────────────────────────────────────────────────────────
class AuditLog(Base):
    """Append-only, hash-chained (HMAC with PORTAL_SECRET_KEY): security.audit."""

    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    at = Column(TS, default=utcnow, index=True)
    household_id = Column(String(36), index=True)
    actor_type = Column(String(16))                 # user | system | device | contact | admin
    actor_id = Column(String(64))
    event = Column(String(48), index=True)          # docs/7 §13
    target_type = Column(String(24))
    target_id = Column(String(64))
    ip_hash = Column(String(64))
    detail = Column(JSON, default=dict)
    prev_hash = Column(String(64))
    hash = Column(String(64))


__all__ = ["Household", "User", "ActivationCode", "UserSession", "LoginAttempt",
           "HouseholdMember", "EmergencyContact", "Consent", "Device",
           "ClaimAttempt", "Camera", "DeviceEvent", "DeviceCommand",
           "AssistanceRequest", "RequestEvent", "SosSnapshot", "Notification",
           "ActionToken", "TelegramLink", "PushSubscription", "IncidentMirror",
           "DispatchMirror", "EvidenceMetaMirror", "SnapshotMirror", "AuditLog",
           "ROLES", "USER_STATUSES", "DEVICE_STATES", "CONSENT_STATUSES",
           "CONSENT_PURPOSES", "TOKEN_PURPOSES", "utcnow"]
