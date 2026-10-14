"""Accounts, households, devices and the device profile (docs/7 §4.2, §5.11, §10).

Owner: track T-ACCT. The routers of the track (auth, admin, onboarding,
household, settings, privacy, home, consent), ``tools/provision_device.py``
and the retention worker call these functions; no FastAPI in here.

Rules:

* Any change to what a profile contains (household, contacts, consents,
  PINs, window, safety defaults, channels, cameras, mode) calls
  :func:`bump_profile_version` **in the same transaction** (through
  :func:`profile_changed`), then (after the commit) ``hub.push_profile`` for
  every claimed device (:func:`push_profile`). The hub never bumps the version.
* ``build_profile`` returns exactly the §5.11 JSON: never a password hash, a
  TOTP secret, a session id, contacts' e-mail addresses or recovery codes
  (the device refuses such a profile: ``sentrix.link.profile.validate``), and
  only CONFIRMED contacts (the only ones an SOS reaches).
* Credentials (D14): the company never sets or sees a customer password. A
  customer account starts ``pending_activation`` with a single-use activation
  code (72 h, hashed, rate limited); printing the welcome letter is the only
  moment a code exists in clear, and only while the account was never
  activated. A reset of an active account goes to the customer's e-mail only.
* Codes and tokens are stored as HMAC digests (``security.code_digest`` /
  ``token_digest``): claim codes, activation codes, bootstrap secrets,
  consent and Telegram link tokens.
* Audit details carry ids and masked values, never names, phone numbers or
  e-mail addresses (the audit chain outlives an account erasure by a year).
* The Portal cannot send SMS: a contact with neither Telegram nor e-mail is
  unreachable while the device is offline (:func:`contact_unreachable_offline`
  drives the warning on the wizard and the household page).
* Telegram: :func:`mint_telegram_link` makes the ``/start <code>`` code that
  T-CH's bot consumes (``action_tokens`` purpose ``telegram_link``, single
  use, 24 h): it looks the code up by ``security.token_digest(code)``, links
  the private chat in ``telegram_links`` and sets ``used_at``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from sentrix.alerting import guard
from sentrix.alerting.channels.base import Message, Recipient
from sentrix.sos import states as sos_states
from sentrix.utils.redact import mask_email, mask_phone
from sentrix_portal import security
from sentrix_portal import settings as portal_settings
from sentrix_portal.models import (CONSENT_PURPOSES, ActionToken, ActivationCode,
                                   AssistanceRequest, AuditLog, Camera, ClaimAttempt,
                                   Consent, Device, DeviceCommand, DeviceEvent,
                                   DispatchMirror, EmergencyContact, EvidenceMetaMirror,
                                   Household, HouseholdMember, IncidentMirror,
                                   LoginAttempt, Notification, PushSubscription,
                                   RequestEvent, SnapshotMirror, SosSnapshot,
                                   TelegramLink, User, UserSession, utcnow)

log = logging.getLogger("sentrix_portal.accounts")

# ── vocabulary and limits ─────────────────────────────────────────────────────
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
EMAIL_RE = re.compile(r"^[^@\s<>\"'`,;]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}$")
SERIAL_RE = re.compile(r"^SX5-\d{6}$")
PINCODE_RE = re.compile(r"^[1-8]\d{5}$")
SERIAL_PREFIX = "SX5-"

#: Activation and reset codes: 12 characters of the RFC 8628 alphabet
#: (about 52 bits), typed from the letter or carried by the e-mailed link.
ACTIVATION_CODE_LENGTH = 12
#: Wrong codes against one account's outstanding code before it is burned.
ACTIVATION_MAX_ATTEMPTS = 10
PASSWORD_MIN_LENGTH = 10
PASSWORD_MAX_LENGTH = 128
CONSENT_TOKEN_SECONDS = 14 * 86400
OPT_OUT_TOKEN_SECONDS = 365 * 86400
EMAIL_VERIFY_SECONDS = 72 * 3600
TELEGRAM_LINK_SECONDS = 24 * 3600
#: An erasure request can be cancelled for this long; then the retention
#: worker erases the household (design §4.4: within 30 days of closure).
ERASURE_GRACE_DAYS = 7
MAX_CONTACTS = 3
MAX_MEMBERS = 12
OCCUPANTS_MAX = 20
AGE_BANDS = ("adult", "minor", "senior")
#: Hindi templates are Tier 2; the wizard offers English only.
LANGUAGES = {"en": "English"}
HOUSEHOLD_STATUSES = ("active", "disabled", "erasing", "closed")
CHANNEL_KEYS = ("email", "telegram", "sms")
DEFAULT_CHANNELS = {"email": True, "telegram": False, "sms": True}
STAFF_ROLES = ("company_admin", "operator")
INDIAN_STATES = (
    "Andaman and Nicobar Islands", "Andhra Pradesh", "Arunachal Pradesh", "Assam",
    "Bihar", "Chandigarh", "Chhattisgarh", "Dadra and Nagar Haveli and Daman and Diu",
    "Delhi", "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jammu and Kashmir",
    "Jharkhand", "Karnataka", "Kerala", "Ladakh", "Lakshadweep", "Madhya Pradesh",
    "Maharashtra", "Manipur", "Meghalaya", "Mizoram", "Nagaland", "Odisha",
    "Puducherry", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana",
    "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal")

#: One toggle per purpose (DPDP Rule 3). ``account_sos`` is the service itself.
PURPOSES: dict[str, dict[str, str]] = {
    "account_sos": {
        "title": "Account and SOS",
        "text": "Your name, mobile number, e-mail, home address, household details and "
                "emergency contacts, to identify you and to prepare and send the SOS "
                "alerts you approve (or that your safety defaults send)."},
    "face_recognition": {
        "title": "Face recognition of household members",
        "text": "Recognising enrolled adults so they do not raise intrusion alarms. "
                "Face photos and their embeddings stay on the device and never reach "
                "the SENTRIX server. Faces of minors are never enrolled."},
    "medical_notes": {
        "title": "Medical and accessibility notes",
        "text": "Notes such as \"senior uses a wheelchair\", stored encrypted, and added "
                "to an SOS only when you switch on \"include in SOS\"."},
    "sos_snapshot": {
        "title": "SOS snapshot upload",
        "text": "One camera picture per assistance request is copied to the SENTRIX "
                "server so your contacts and the Desk can see it even if the device is "
                "destroyed. It may show faces and is deleted after 30 days."},
    "remote_view": {
        "title": "Remote live view",
        "text": "Watching the camera and the device's pages from outside the home "
                "through the SENTRIX server. Video passes through the server in memory "
                "(not end-to-end encrypted) and is never stored."},
    "desk_live_view": {
        "title": "Desk live view during an SOS",
        "text": "The SENTRIX Desk may watch the live camera only while a help request "
                "of yours is open."},
    "model_improvement": {
        "title": "Model improvement",
        "text": "Using your true/false alarm labels (never images) to improve the "
                "detection models."},
}
#: Wizard defaults (step 7): the snapshot and remote view on, the rest off.
DEFAULT_CONSENTS = {"account_sos": True, "face_recognition": False, "medical_notes": False,
                    "sos_snapshot": True, "remote_view": True, "desk_live_view": False,
                    "model_improvement": False}
OPTIONAL_PURPOSES = tuple(p for p in CONSENT_PURPOSES if p != "account_sos")

ONBOARDING_STEPS = {1: "About you", 2: "Home address", 3: "Household",
                    4: "Emergency contacts", 5: "Device", 6: "Alerts and SOS",
                    7: "Consent and review"}
LAST_STEP = 7

#: Common passwords and PINs refused outright (on top of the pattern rules).
COMMON_PASSWORDS = frozenset({
    "password", "password1", "password12", "password123", "password1234",
    "1234567890", "0987654321", "qwertyuiop", "qwerty1234", "qwerty12345",
    "iloveyou12", "admin12345", "administrator", "welcome123", "welcome1234",
    "letmein123", "sentrix123", "sentrix1234", "abcdefghij", "abc1234567",
    "passw0rd12", "india12345", "1q2w3e4r5t", "zaq12wsxcde", "trustno1234"})
COMMON_PINS = frozenset({
    "112233", "121314", "123321", "123654", "147258", "147852", "159357", "159753",
    "258369", "321123", "369258", "456123", "520520", "696969", "741852", "753951",
    "789456", "852456", "963852", "102030", "100200", "110110", "202020"})


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; every stored time is UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def iso(value: datetime | None) -> str | None:
    value = as_utc(value)
    return value.isoformat() if value else None


def portal_config():
    """The Portal settings of the running app."""
    return portal_settings.current()


def sos_config():
    """``sentrix.config.settings.sos`` (window choices, PIN rules)."""
    from sentrix.config import settings as device_settings
    return device_settings.sos


# ── validation ────────────────────────────────────────────────────────────────
class FormErrors(dict):
    """field -> message; the first message for a field wins."""

    def add(self, name: str, message: str) -> None:
        self.setdefault(name, message)


def _raw(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def clean_text(errors: FormErrors, name: str, value: Any, *, label: str,
               max_len: int, required: bool = False, min_len: int = 1) -> str | None:
    text = " ".join(_raw(value).split())
    if not text:
        if required:
            errors.add(name, f"{label} is required.")
        return None
    if any(ord(c) < 32 for c in text):
        errors.add(name, f"{label} contains control characters.")
        return None
    if len(text) < min_len or len(text) > max_len:
        errors.add(name, f"{label} must be {min_len}-{max_len} characters.")
        return None
    return text


def normalize_phone(value: Any) -> str | None:
    """Indian numbers to E.164: ``98123 45610``, ``098123-45610``,
    ``+91 98123 45610`` -> ``+919812345610``; None when it is not one."""
    text = re.sub(r"[\s().-]", "", _raw(value))
    if text.startswith("+91"):
        national = text[3:]
    elif text.startswith("+"):
        return None
    elif len(text) == 12 and text.startswith("91"):
        national = text[2:]
    elif len(text) == 11 and text.startswith("0"):
        national = text[1:]
    else:
        national = text
    if not re.fullmatch(r"[1-9]\d{9}", national):
        return None
    return "+91" + national


def clean_phone(errors: FormErrors, name: str, value: Any, *, label: str,
                required: bool = True, mobile: bool = True) -> str | None:
    if not _raw(value):
        if required:
            errors.add(name, f"{label} is required.")
        return None
    phone = normalize_phone(value)
    if phone is None or (mobile and phone[3] not in "6789"):
        kind = "mobile number" if mobile else "phone number"
        errors.add(name, f"{label}: enter an Indian {kind} as +91 and 10 digits.")
        return None
    return phone


def clean_email(errors: FormErrors, name: str, value: Any, *, label: str = "E-mail",
                required: bool = False) -> str | None:
    text = _raw(value)
    if not text:
        if required:
            errors.add(name, f"{label} is required.")
        return None
    if len(text) > 254 or not EMAIL_RE.match(text):
        errors.add(name, f"{label}: enter a valid e-mail address.")
        return None
    local, _, domain = text.partition("@")
    return f"{local}@{domain.lower()}"


def clean_int(errors: FormErrors, name: str, value: Any, *, label: str, lo: int,
              hi: int, required: bool = True) -> int | None:
    text = _raw(value)
    if not text:
        if required:
            errors.add(name, f"{label} is required.")
        return None
    if not re.fullmatch(r"\d{1,4}", text) or not lo <= int(text) <= hi:
        errors.add(name, f"{label} must be a whole number from {lo} to {hi}.")
        return None
    return int(text)


def clean_choice(errors: FormErrors, name: str, value: Any, choices, *, label: str,
                 required: bool = True) -> str | None:
    text = _raw(value)
    if not text:
        if required:
            errors.add(name, f"{label} is required.")
        return None
    if text not in choices:
        errors.add(name, f"Choose a valid {label.lower()}.")
        return None
    return text


def clean_pincode(errors: FormErrors, name: str, value: Any) -> str | None:
    text = re.sub(r"\s", "", _raw(value))
    if not text:
        errors.add(name, "PIN code is required.")
        return None
    if not PINCODE_RE.match(text):
        errors.add(name, "PIN code: 6 digits, the first from 1 to 8.")
        return None
    return text


def clean_coordinates(errors: FormErrors, lat: Any, lon: Any) -> tuple[float | None, float | None]:
    lat_text, lon_text = _raw(lat), _raw(lon)
    if not lat_text and not lon_text:
        return None, None
    try:
        lat_value, lon_value = float(lat_text), float(lon_text)
    except ValueError:
        errors.add("lat", "Enter both latitude and longitude as decimal degrees.")
        return None, None
    # India and its islands, with a margin (6-37.5 N, 68-97.5 E).
    if not (6.0 <= lat_value <= 37.5 and 68.0 <= lon_value <= 97.5):
        errors.add("lat", "That location is outside India.")
        return None, None
    return round(lat_value, 5), round(lon_value, 5)


_ENTRY_CODE_RE = re.compile(r"\b(code|pin|password|passcode|otp)\b\D{0,12}\d{3,}", re.I)


def clean_entry_note(errors: FormErrors, value: Any) -> str | None:
    note = clean_text(errors, "entry_note", value, label="Entry note", max_len=120)
    if note and _ENTRY_CODE_RE.search(note):
        errors.add("entry_note", "Never put door or gate codes in the entry note: it is "
                                 "sent to your contacts and the Desk in an SOS.")
        return None
    return note


def clean_username(errors: FormErrors, name: str, value: Any) -> str | None:
    text = _raw(value).lower()
    if not USERNAME_RE.match(text):
        errors.add(name, "Username: 3-32 characters, lower-case letters, digits, "
                         "'.', '_' or '-', starting with a letter or digit.")
        return None
    return text


def password_problem(password: str | None, *, username: str | None = None) -> str | None:
    """None when acceptable (NIST 800-63B style: length, not common, not the
    username; no composition rules)."""
    password = password or ""
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"Use at least {PASSWORD_MIN_LENGTH} characters."
    if len(password) > PASSWORD_MAX_LENGTH:
        return f"Use at most {PASSWORD_MAX_LENGTH} characters."
    lowered = password.lower()
    if lowered in COMMON_PASSWORDS or len(set(password)) < 4:
        return "This password is too common or too simple. Choose another."
    if username and len(username) >= 3 and username.lower() in lowered:
        return "The password must not contain your username."
    return None


def _weak_pin(pin: str) -> bool:
    digits = [int(c) for c in pin]
    if len(set(digits)) <= 2:                       # 111111, 121212, 100100
        return True
    if len({(b - a) % 10 for a, b in zip(digits, digits[1:])}) == 1:
        return True                                  # 123456, 987654, 135791
    if pin[:3] == pin[3:] or pin == pin[::-1]:      # 123123, 123321
        return True
    return pin in COMMON_PINS


def pin_problem(pin: str | None, *, phone: str | None = None,
                pincode: str | None = None) -> str | None:
    """None when ``pin`` may be an SOS or duress PIN."""
    length = sos_config().pin_length
    pin = (pin or "").strip()
    if not re.fullmatch(rf"\d{{{length}}}", pin):
        return f"The PIN must be exactly {length} digits."
    if _weak_pin(pin):
        return ("This PIN is too easy to guess (repeated, sequential or common "
                "digits). Choose another.")
    if phone and phone.endswith(pin):
        return "Do not use the end of your mobile number as a PIN."
    if pincode and pin == pincode:
        return "Do not use your PIN code as a PIN."
    return None


def digit_difference(a: str, b: str) -> int:
    """Positions where two PINs of the same length differ."""
    if len(a) != len(b):
        return max(len(a), len(b))
    return sum(1 for x, y in zip(a, b) if x != y)


def check_pins(errors: FormErrors, sos_pin: str, sos_confirm: str, duress_pin: str,
               duress_confirm: str, *, phone: str | None, pincode: str | None,
               contacts_exist: bool) -> tuple[str | None, str | None]:
    """Validate the PIN form (both PINs are always entered together, so the
    duress rule can be checked on the digits). Returns (sos, duress or None)."""
    sos_pin, duress_pin = _raw(sos_pin), _raw(duress_pin)
    problem = pin_problem(sos_pin, phone=phone, pincode=pincode)
    if problem:
        errors.add("sos_pin", problem)
    elif sos_pin != _raw(sos_confirm):
        errors.add("sos_pin_confirm", "The two SOS PINs do not match.")
    if not duress_pin:
        return (None if errors else sos_pin), None
    if not contacts_exist:
        errors.add("duress_pin", "A duress PIN needs at least one emergency contact: "
                                 "it alerts them silently.")
        return None, None
    problem = pin_problem(duress_pin, phone=phone, pincode=pincode)
    minimum = sos_config().duress_min_digit_difference
    if problem:
        errors.add("duress_pin", problem)
    elif digit_difference(sos_pin, duress_pin) < minimum:
        errors.add("duress_pin", f"The duress PIN must differ from the SOS PIN in at "
                                 f"least {minimum} digits.")
    elif duress_pin != _raw(duress_confirm):
        errors.add("duress_pin_confirm", "The two duress PINs do not match.")
    if errors:
        return None, None
    return sos_pin, duress_pin


# ── e-mail through the channel contract (T-CH) ────────────────────────────────
def _recipient_role(user: User) -> str:
    return user.role if user.role in ("owner", "member", "operator") else "operator"


def send_email(db: Session, recipient: Recipient, *, template: str, step: str,
               subject: str, text: str) -> str | None:
    """Queue one e-mail with ``services.notify.enqueue`` (T-CH: outbox, guard,
    transports). Returns the outbox id, or None (no channel, duplicate)."""
    from sentrix_portal.services import notify
    message = Message(template=template, step=step, text=text, subject=subject)
    try:
        return notify.enqueue(db, recipient, message)
    except Exception as exc:                          # never break the page
        log.error("could not queue %s for %s: %s", template, recipient.ref, exc)
        return None


def email_user(db: Session, user: User, *, template: str, step: str, subject: str,
               text: str) -> str | None:
    if not user.email:
        return None
    recipient = Recipient(ref=f"user:{user.id}", name=user.display_name or user.username,
                          channel="email", address=user.email, role=_recipient_role(user))
    return send_email(db, recipient, template=template, step=step, subject=subject,
                      text=text)


# ── users and activation ──────────────────────────────────────────────────────
def user_phone(user: User | None) -> str | None:
    return security.unseal(user.phone_enc) if user and user.phone_enc else None


def household_owner(db: Session, household_id: str) -> User | None:
    return (db.query(User).filter_by(household_id=household_id, role="owner")
            .order_by(User.created_at).first())


def activation_url(code: str) -> str:
    return f"{portal_config().base_url}/activate/{security.normalize_code(code)}"


def issue_activation_code(db: Session, user: User, *, purpose: str = "activate",
                          created_by: str | None = None,
                          actor_type: str = "admin") -> tuple[str, ActivationCode]:
    """A fresh single-use code; any outstanding code of the user stops working
    (one valid code at a time). Returns (code in clear, row)."""
    if purpose not in ("activate", "reset"):
        raise ValueError(f"unknown activation purpose {purpose!r}")
    now = utcnow()
    for row in db.query(ActivationCode).filter_by(user_id=user.id, used_at=None):
        if as_utc(row.expires_at) > now:
            row.expires_at = now
    code = security.new_code(ACTIVATION_CODE_LENGTH, 4)
    row = ActivationCode(user_id=user.id, purpose=purpose,
                         code_hash=security.code_digest(code), created_by=created_by,
                         created_at=now,
                         expires_at=now + timedelta(seconds=portal_config().activation_ttl_seconds))
    db.add(row)
    db.flush()
    security.audit(db, "auth.activation_issued", actor_type=actor_type, actor_id=created_by,
                   household_id=user.household_id, target_type="user", target_id=user.id,
                   detail={"purpose": purpose, "code_id": row.id,
                           "expires_at": iso(row.expires_at)})
    return code, row


def _usable(row: ActivationCode | None, now: datetime) -> bool:
    return (row is not None and row.used_at is None and as_utc(row.expires_at) > now
            and (row.attempts or 0) < ACTIVATION_MAX_ATTEMPTS)


def activation_for_link(db: Session, code: str) -> tuple[ActivationCode, User] | None:
    """The live code behind an ``/activate/<code>`` link, with its user."""
    if not security.normalize_code(code):
        return None
    row = (db.query(ActivationCode)
           .filter_by(code_hash=security.code_digest(code)).first())
    if not _usable(row, utcnow()):
        return None
    user = db.get(User, row.user_id)
    if user is None or not _activatable(user, row.purpose):
        return None
    return row, user


def _activatable(user: User, purpose: str) -> bool:
    if purpose == "activate":
        return user.status == "pending_activation"
    return user.status == "active"


def activation_for_typed(db: Session, username: str, code: str) -> tuple[ActivationCode, User] | None:
    """A typed (username, code). A wrong code counts against the account's
    outstanding codes (burned after ``ACTIVATION_MAX_ATTEMPTS``)."""
    user = db.query(User).filter_by(username=security.attempt_key(username)).first()
    if user is None:
        return None
    now = utcnow()
    outstanding = [r for r in db.query(ActivationCode).filter_by(user_id=user.id, used_at=None)
                   if _usable(r, now)]
    digest = security.code_digest(code) if security.normalize_code(code) else None
    for row in outstanding:
        if digest and hmac.compare_digest(row.code_hash, digest):
            return (row, user) if _activatable(user, row.purpose) else None
    for row in outstanding:
        row.attempts = (row.attempts or 0) + 1
    db.flush()
    return None


def complete_activation(db: Session, row: ActivationCode, user: User, password_hash: str,
                        *, ip: str | None) -> None:
    """Set the customer's own password (hashed by the caller off the event
    loop), use the code up, end every other session of the account."""
    now = utcnow()
    row.used_at = now
    for other in db.query(ActivationCode).filter_by(user_id=user.id, used_at=None):
        if other.id != row.id and as_utc(other.expires_at) > now:
            other.expires_at = now
    user.password_hash = password_hash
    user.password_changed_at = now
    if user.status == "pending_activation":
        user.status = "active"
    security.revoke_user_sessions(db, user.id)
    security.audit(db, "auth.activated" if row.purpose == "activate" else "auth.password_changed",
                   actor_type="user", actor_id=user.id, household_id=user.household_id,
                   target_type="user", target_id=user.id, ip=ip,
                   detail={"purpose": row.purpose, "code_id": row.id})
    db.flush()


def activation_text(user: User, code: str, *, purpose: str) -> tuple[str, str]:
    """(subject, text) of the activation or reset e-mail."""
    expires = portal_config().activation_ttl_seconds // 3600
    url = activation_url(code)
    if purpose == "reset":
        subject = "SENTRIX: set a new password"
        intro = ("SENTRIX staff sent you a link to set a new password for your SENTRIX "
                 "account. Your current password keeps working until you use it; if you "
                 "did not ask for this, ignore this e-mail and tell us.")
    else:
        subject = "SENTRIX: activate your account"
        intro = "Your SENTRIX account is ready. Choose your own password to activate it."
    text = (f"{intro}\n\nUsername: {user.username}\nCode: {code}\nLink: {url}\n\n"
            f"The code works once and expires in {expires} hours. SENTRIX staff never "
            f"ask for your password.")
    return subject, text


def send_activation_email(db: Session, user: User, *, purpose: str,
                          created_by: str | None, actor_type: str = "admin") -> str | None:
    """Admin "reset" / re-send (D14): a new code by e-mail only, so the
    admin never sees it. Returns the outbox id, or None."""
    if not user.email:
        return None
    code, row = issue_activation_code(db, user, purpose=purpose, created_by=created_by,
                                      actor_type=actor_type)
    subject, text = activation_text(user, code, purpose=purpose)
    return email_user(db, user, template=f"account.{'reset' if purpose == 'reset' else 'activation'}",
                      step=f"activation:{row.id}", subject=subject, text=text)


def mint_email_verification(db: Session, user: User) -> tuple[str, ActionToken]:
    """A single-use ``email_verify`` token for the user's current address
    (earlier ones are revoked: changing the address voids them)."""
    revoke_tokens(db, purpose="email_verify", subject_type="user", subject_id=user.id)
    raw = security.new_token(32)
    row = ActionToken(token_hash=security.token_digest(raw), purpose="email_verify",
                      household_id=user.household_id, subject_type="user",
                      subject_id=user.id,
                      expires_at=utcnow() + timedelta(seconds=EMAIL_VERIFY_SECONDS))
    db.add(row)
    db.flush()
    return raw, row


def send_email_verification(db: Session, user: User) -> str | None:
    if not user.email:
        return None
    raw, row = mint_email_verification(db, user)
    url = f"{portal_config().base_url}/account/verify-email/{raw}"
    return email_user(db, user, template="account.verify_email", step=f"verify:{row.id}",
                      subject="SENTRIX: confirm your e-mail address",
                      text=(f"Confirm that this is your e-mail address for SENTRIX alerts "
                            f"and account recovery: {url}\nThe link expires in "
                            f"{EMAIL_VERIFY_SECONDS // 3600} hours."))


def revoke_tokens(db: Session, *, purpose: str, subject_type: str, subject_id: str) -> int:
    now, count = utcnow(), 0
    for row in db.query(ActionToken).filter_by(purpose=purpose, subject_type=subject_type,
                                               subject_id=subject_id, used_at=None,
                                               revoked_at=None):
        row.revoked_at = now
        count += 1
    db.flush()
    return count


def live_token(db: Session, raw: str | None, purpose: str) -> ActionToken | None:
    """An unused, unrevoked, unexpired token of ``purpose``."""
    if not raw or len(raw) > 128:
        return None
    row = db.query(ActionToken).filter_by(token_hash=security.token_digest(raw)).first()
    if (row is None or row.purpose != purpose or row.used_at is not None
            or row.revoked_at is not None or as_utc(row.expires_at) <= utcnow()):
        return None
    return row


# ── customers and staff ───────────────────────────────────────────────────────
def next_customer_code(db: Session) -> str:
    number = db.query(Household).count() + 1
    while db.query(Household).filter_by(customer_code=f"SX-{number:04d}").first():
        number += 1
    return f"SX-{number:04d}"


def username_taken(db: Session, username: str) -> bool:
    return db.query(User).filter_by(username=username).first() is not None


def create_customer(db: Session, *, name: str, username: str, email: str,
                    phone: str | None, city: str | None,
                    created_by: str | None) -> tuple[Household, User]:
    """A household and its owner (``pending_activation``, no password)."""
    sos = sos_config()
    household = Household(customer_code=next_customer_code(db), display_name=name,
                          city=city, status="active", created_by=created_by,
                          sos_window_s=sos.window_seconds if sos.window_seconds in
                          sos.window_choices else 1800,
                          safety_defaults=sos.safety_defaults,
                          channels=dict(DEFAULT_CHANNELS), occupants={}, nearest={})
    db.add(household)
    db.flush()
    owner = User(username=username, display_name=name, email=email,
                 phone_enc=security.seal(phone) if phone else None, role="owner",
                 household_id=household.id, status="pending_activation",
                 created_by=created_by)
    db.add(owner)
    db.flush()
    security.audit(db, "admin.customer_created", actor_type="admin", actor_id=created_by,
                   household_id=household.id, target_type="household",
                   target_id=household.id,
                   detail={"customer_code": household.customer_code, "owner_id": owner.id})
    return household, owner


def create_staff_user(db: Session, *, role: str, username: str, display_name: str,
                      email: str | None, created_by: str | None) -> User:
    if role not in STAFF_ROLES:
        raise ValueError(f"not a staff role: {role!r}")
    user = User(username=username, display_name=display_name, email=email, role=role,
                status="pending_activation", created_by=created_by)
    db.add(user)
    db.flush()
    security.audit(db, "admin.staff_created", actor_type="admin" if created_by else "system",
                   actor_id=created_by, target_type="user", target_id=user.id,
                   detail={"role": role})
    return user


def set_household_status(db: Session, household: Household, status: str, *,
                         actor_id: str | None, ip: str | None = None) -> None:
    """Admin deactivate / reactivate. Deactivating ends every session and
    blocks sign-in; nothing is deleted (erasure is the customer's choice)."""
    if status not in ("active", "disabled"):
        raise ValueError(status)
    household.status = status
    for user in db.query(User).filter_by(household_id=household.id):
        if status == "disabled" and user.status in ("active", "pending_activation"):
            user.status = "disabled"
            security.revoke_user_sessions(db, user.id)
        elif status == "active" and user.status == "disabled":
            user.status = "active" if user.password_hash else "pending_activation"
    security.audit(db, "admin.customer_deactivated" if status == "disabled"
                   else "admin.customer_reactivated", actor_type="admin", actor_id=actor_id,
                   household_id=household.id, target_type="household",
                   target_id=household.id, ip=ip)
    db.flush()


# ── devices ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ProvisionedDevice:
    """What exists in clear exactly once, at provisioning: the label's claim
    and setup codes and the identity file's bootstrap secret."""

    device: Device
    claim_code: str
    bootstrap_secret: str
    setup_code: str
    identity: dict[str, Any]


def normalize_serial(value: Any) -> str:
    """``sx5 000123`` / ``SX5000123`` -> ``SX5-000123`` (as typed from a label)."""
    text = re.sub(r"[\s-]", "", _raw(value)).upper()
    if re.fullmatch(r"SX5\d{6}", text):
        return f"{SERIAL_PREFIX}{text[3:]}"
    return text[:16]


def next_serial(db: Session) -> str:
    numbers = [int(s[len(SERIAL_PREFIX):]) for (s,) in db.query(Device.serial)
               if s and SERIAL_RE.match(s)]
    return f"{SERIAL_PREFIX}{(max(numbers) + 1) if numbers else 1:06d}"


def setup_code_digest(code: str) -> str:
    """``identity.json`` ``setup_code_digest``: SHA-256 hex of the code
    upper-cased with every character but A-Z and 0-9 removed (the device's
    ``/setup/wifi`` applies the same rule to what is typed)."""
    return hashlib.sha256(security.normalize_code(code).encode()).hexdigest()


def provision_device(db: Session, *, serial: str | None = None, name: str | None = None,
                     created_by: str | None = None,
                     actor_type: str = "admin") -> ProvisionedDevice:
    """A new UNASSIGNED device: UUID4 id, claim code (single use, hashed),
    bootstrap secret (hashed) and the one-time Wi-Fi setup code. Raises
    ``ValueError`` for a malformed or existing serial."""
    serial = normalize_serial(serial) if serial else next_serial(db)
    if not SERIAL_RE.match(serial):
        raise ValueError("serial must look like SX5-000123")
    if db.query(Device).filter_by(serial=serial).first():
        raise ValueError(f"serial {serial} is already provisioned")
    claim_code = security.new_code(8, 4)
    bootstrap = security.new_token(32)
    setup_code = security.new_code(8, 4)
    now = utcnow()
    device = Device(id=str(uuid.uuid4()), serial=serial, name=name, state="UNASSIGNED",
                    claim_code_hash=security.code_digest(claim_code),
                    bootstrap_hash=security.token_digest(bootstrap), created_at=now)
    db.add(device)
    db.flush()
    identity = {"device_id": device.id, "serial": serial, "bootstrap_secret": bootstrap,
                "setup_code_digest": setup_code_digest(setup_code),
                "provisioned_at": now.isoformat()}
    security.audit(db, "admin.device_provisioned", actor_type=actor_type, actor_id=created_by,
                   target_type="device", target_id=device.id, detail={"serial": serial})
    return ProvisionedDevice(device, claim_code, bootstrap, setup_code, identity)


def rotate_claim_code(db: Session, device: Device, *, actor_id: str | None) -> str:
    """A new claim code (the old one stops working); refused once claimed."""
    if device.state not in ("UNASSIGNED", "ASSIGNED"):
        raise ValueError("only an unclaimed device gets a new claim code")
    code = security.new_code(8, 4)
    device.claim_code_hash = security.code_digest(code)
    security.audit(db, "admin.claim_code_reissued", actor_type="admin", actor_id=actor_id,
                   household_id=device.household_id, target_type="device",
                   target_id=device.id, detail={"serial": device.serial})
    db.flush()
    return code


def assign_device(db: Session, device: Device, household: Household, *,
                  actor_id: str | None, actor_type: str = "admin") -> None:
    """Pre-assignment at the point of sale; the customer still has to claim."""
    if device.state != "UNASSIGNED":
        raise ValueError(f"device {device.serial} is {device.state}, not UNASSIGNED")
    if household.status != "active":
        raise ValueError("the customer account is not active")
    device.household_id = household.id
    device.state = "ASSIGNED"
    device.assigned_at = utcnow()
    security.audit(db, "admin.device_assigned", actor_type=actor_type, actor_id=actor_id,
                   household_id=household.id, target_type="device", target_id=device.id,
                   detail={"serial": device.serial})
    db.flush()


def release_device(db: Session, device: Device, *, actor_id: str | None,
                   retire: bool = False, event: str = "admin.device_transferred") -> str | None:
    """Back to UNASSIGNED with a new claim code (transfer), or RETIRED. The
    caller unpairs a claimed device over the link first (``hub.unpair``).
    Returns the new claim code (None when retired)."""
    previous = device.household_id
    db.query(Camera).filter_by(device_id=device.id).delete(synchronize_session=False)
    device.household_id = None
    device.token_hash = None
    device.claimed_at = device.claimed_by = device.assigned_at = None
    device.profile_version_acked = 0
    code = None
    if retire:
        device.state = "RETIRED"
        device.claim_code_hash = device.bootstrap_hash = None
    else:
        device.state = "UNASSIGNED"
        code = security.new_code(8, 4)
        device.claim_code_hash = security.code_digest(code)
    security.audit(db, "admin.device_retired" if retire else event, actor_type="admin",
                   actor_id=actor_id, household_id=previous, target_type="device",
                   target_id=device.id, detail={"serial": device.serial})
    db.flush()
    return code


def claimed_devices(db: Session, household_id: str) -> list[Device]:
    return (db.query(Device).filter_by(household_id=household_id, state="CLAIMED")
            .order_by(Device.claimed_at, Device.serial).all())


def household_devices(db: Session, household_id: str) -> list[Device]:
    return (db.query(Device).filter(Device.household_id == household_id,
                                    Device.state.in_(("ASSIGNED", "CLAIMED")))
            .order_by(Device.serial).all())


@dataclass
class ClaimOutcome:
    ok: bool
    device: Device | None = None
    error: str | None = None
    retry_after: int = 0


CLAIM_REFUSED = ("The serial number or claim code is not right, or this device is not "
                 "assigned to your account. Check the label and the welcome letter.")


def claim_retry_after(db: Session, user_id: str, serial: str, *,
                      now: datetime | None = None) -> int:
    """Seconds until another claim may be tried (5 failures / 15 min per
    account and per serial; docs/7 §5.2)."""
    settings = portal_config()
    now = now or utcnow()
    since = now - timedelta(seconds=settings.claim_window_seconds)
    waits = []
    for column, value in ((ClaimAttempt.user_id, user_id), (ClaimAttempt.serial, serial)):
        failures = (db.query(ClaimAttempt)
                    .filter(column == value, ClaimAttempt.success.is_(False),
                            ClaimAttempt.at >= since)
                    .order_by(ClaimAttempt.at).all())
        if len(failures) >= settings.claim_attempts:
            oldest = as_utc(failures[-settings.claim_attempts].at)
            waits.append(int((oldest + timedelta(seconds=settings.claim_window_seconds)
                              - now).total_seconds()) + 1)
    return max([0, *waits])


def claim_device(db: Session, *, user: User, serial: Any, code: Any,
                 ip: str | None) -> ClaimOutcome:
    """Bind a pre-assigned device to the user's household (proves physical
    possession of the label or letter). Single-use code; rate limited."""
    serial = normalize_serial(serial)
    wait = claim_retry_after(db, user.id, serial)
    if wait:
        return ClaimOutcome(False, error="Too many claim attempts. Try again in "
                                         f"{max(1, wait // 60)} min.", retry_after=wait)
    device = db.query(Device).filter_by(serial=serial).first() if serial else None
    if device is not None and device.state == "CLAIMED" and \
            device.household_id == user.household_id:
        return ClaimOutcome(False, device=device,
                            error="This device is already claimed by your household.")
    ok = (device is not None and device.state == "ASSIGNED"
          and device.household_id == user.household_id and device.claim_code_hash
          and security.normalize_code(code)
          and hmac.compare_digest(device.claim_code_hash,
                                           security.code_digest(code)))
    db.add(ClaimAttempt(serial=serial, user_id=user.id, ip_hash=security.ip_digest(ip),
                        at=utcnow(), success=bool(ok)))
    if not ok:
        security.audit(db, "device.claim_failed", actor_type="user", actor_id=user.id,
                       household_id=user.household_id, target_type="device",
                       target_id=device.id if device else None, ip=ip,
                       detail={"serial": serial})
        db.flush()
        return ClaimOutcome(False, error=CLAIM_REFUSED)
    now = utcnow()
    device.state = "CLAIMED"
    device.claimed_at = now
    device.claimed_by = user.id
    device.claim_code_hash = None                     # single use
    if not db.query(Camera).filter_by(device_id=device.id).first():
        db.add(Camera(device_id=device.id, camera_id="CAM_0"))
    security.audit(db, "device.claimed", actor_type="user", actor_id=user.id,
                   household_id=user.household_id, target_type="device",
                   target_id=device.id, ip=ip, detail={"serial": serial})
    profile_changed(db, user.household_id, actor_id=user.id, what="device_claimed", ip=ip)
    return ClaimOutcome(True, device=device)


def device_cameras(db: Session, device_id: str) -> list[Camera]:
    return db.query(Camera).filter_by(device_id=device_id).order_by(Camera.camera_id).all()


def qr_svg(data: str) -> str:
    """An inline SVG QR code for labels and letters (no script, no style
    attribute: CSP-safe); its size comes from the ``.qr`` CSS class."""
    import io

    import qrcode
    import qrcode.image.svg
    image = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=10,
                        border=2)
    buffer = io.BytesIO()
    image.save(buffer)
    svg = buffer.getvalue().decode("utf-8")
    svg = svg[svg.index("<svg"):]
    head, rest = svg.split(">", 1)
    head = re.sub(r'\s(?:width|height)="[^"]*"', "", head)
    return head.replace("<svg", '<svg class="qr" role="img" aria-label="QR code"', 1) + ">" + rest


# ── profile versions and pushes ───────────────────────────────────────────────
def bump_profile_version(db, household_id: str) -> int:
    """+1 on ``households.profile_version`` inside the caller's transaction."""
    household = db.get(Household, household_id)
    if household is None:
        raise LookupError(f"unknown household {household_id}")
    household.profile_version = (household.profile_version or 0) + 1
    db.flush()
    return household.profile_version


def profile_changed(db: Session, household_id: str, *, actor_id: str | None, what: str,
                    actor_type: str = "user", ip: str | None = None,
                    detail: dict | None = None) -> int:
    """Bump the version and audit ``profile.updated``; the caller commits and
    then calls :func:`push_profile`."""
    version = bump_profile_version(db, household_id)
    security.audit(db, "profile.updated", actor_type=actor_type, actor_id=actor_id,
                   household_id=household_id, target_type="household",
                   target_id=household_id, ip=ip,
                   detail={"what": what, "version": version, **(detail or {})})
    return version


async def push_profile(db: Session, household_id: str) -> dict[str, int]:
    """After the commit: ``hub.push_profile`` for every claimed device
    (0 = offline, it goes out after the device's next ``welcome``)."""
    from sentrix_portal.services import hub
    pushed = {}
    for device in claimed_devices(db, household_id):
        try:
            pushed[device.id] = await hub.push_profile(db, device.id)
        except Exception as exc:                      # the change is saved anyway
            log.error("profile push to %s failed: %s", device.id, exc)
            pushed[device.id] = 0
    return pushed


# ── address and household details (wizard steps 2 and 3, /household) ─────────
ADDRESS_FIELDS = ("house", "building", "street", "locality", "landmark", "city",
                  "district", "state", "pincode", "floor", "lat", "lon", "entry_note")
NEAREST_KINDS = (("police", "Nearest police station"), ("fire", "Nearest fire station"),
                 ("hospital", "Nearest hospital"))


def clean_address(errors: FormErrors, form) -> dict[str, Any]:
    values = {
        "house": clean_text(errors, "house", form.get("house"), label="House / flat number",
                            max_len=80, required=True),
        "building": clean_text(errors, "building", form.get("building"),
                               label="Building / society", max_len=80),
        "street": clean_text(errors, "street", form.get("street"), label="Street",
                             max_len=120, required=True),
        "locality": clean_text(errors, "locality", form.get("locality"),
                               label="Locality / sector", max_len=120, required=True),
        "landmark": clean_text(errors, "landmark", form.get("landmark"), label="Landmark",
                               max_len=120, required=True),
        "city": clean_text(errors, "city", form.get("city"), label="City", max_len=80,
                           required=True),
        "district": clean_text(errors, "district", form.get("district"), label="District",
                               max_len=80),
        "state": clean_choice(errors, "state", form.get("state"), INDIAN_STATES,
                              label="State"),
        "pincode": clean_pincode(errors, "pincode", form.get("pincode")),
        "floor": clean_text(errors, "floor", form.get("floor"), label="Floor", max_len=20),
        "entry_note": clean_entry_note(errors, form.get("entry_note")),
    }
    values["lat"], values["lon"] = clean_coordinates(errors, form.get("lat"), form.get("lon"))
    nearest = {}
    for kind, label in NEAREST_KINDS:
        name = clean_text(errors, f"{kind}_name", form.get(f"{kind}_name"), label=label,
                          max_len=80)
        phone = clean_phone(errors, f"{kind}_phone", form.get(f"{kind}_phone"),
                            label=f"{label} phone", required=False, mobile=False)
        if phone and not name:
            errors.add(f"{kind}_name", f"{label}: add the name with the phone number.")
        if name:
            nearest[kind] = {"name": name, "phone": phone}
    values["nearest"] = nearest
    return values


def apply_address(household: Household, values: dict[str, Any]) -> None:
    for name in ADDRESS_FIELDS:
        setattr(household, name, values[name])
    household.nearest = values["nearest"]


def clean_household_details(errors: FormErrors, form) -> dict[str, Any]:
    adults = clean_int(errors, "adults", form.get("adults"), label="Adults", lo=1,
                       hi=OCCUPANTS_MAX)
    children = clean_int(errors, "children", form.get("children") or "0", label="Children",
                         lo=0, hi=OCCUPANTS_MAX)
    seniors = clean_int(errors, "seniors", form.get("seniors") or "0", label="Seniors",
                        lo=0, hi=OCCUPANTS_MAX)
    pets = clean_text(errors, "pets", form.get("pets"), label="Pets", max_len=60)
    notes = clean_text(errors, "medical", form.get("medical"),
                       label="Medical / accessibility notes", max_len=200)
    consent = bool(form.get("medical_consent"))
    if notes and not consent:
        errors.add("medical_consent", "Tick the separate consent to store medical notes, "
                                      "or leave the notes empty.")
    return {"occupants": {"adults": adults, "children": children, "seniors": seniors},
            "pets": pets, "medical": notes, "medical_consent": consent,
            "include_medical": bool(form.get("include_medical")) and bool(notes)}


def apply_household_details(db: Session, household: Household, values: dict[str, Any], *,
                            user: User, method: str, ip: str | None) -> None:
    household.occupants = values["occupants"]
    household.pets = values["pets"]
    if values["medical"]:
        set_consent(db, household, "medical_notes", True, user=user, method=method, ip=ip)
        household.medical_enc = security.seal(values["medical"])
        household.include_medical = values["include_medical"]
    else:
        household.medical_enc, household.include_medical = None, False
        if not values["medical_consent"]:
            set_consent(db, household, "medical_notes", False, user=user, method=method, ip=ip)


# ── SOS settings (wizard step 6, /settings) ──────────────────────────────────
def window_choices() -> tuple[int, ...]:
    return tuple(sos_config().window_choices)


def clean_sos_settings(errors: FormErrors, form, *,
                       safety_was_on: bool = True) -> dict[str, Any]:
    """Window (30/45/60 min), safety defaults (on by default, D9; turning
    them off needs an explicit acknowledgement) and the owner's channels."""
    raw = _raw(form.get("window"))
    window = int(raw) if raw.isdigit() and int(raw) in window_choices() else None
    if window is None:
        errors.add("window", "Choose a window of 30, 45 or 60 minutes.")
    safety = bool(form.get("safety_defaults"))
    if not safety and safety_was_on and not form.get("safety_off_ack"):
        errors.add("safety_off_ack", "Safety defaults alert your confirmed contacts when "
                                     "seconds count. To switch them off, tick the box to "
                                     "confirm you understand.")
    channels = {key: bool(form.get(f"channel_{key}")) for key in CHANNEL_KEYS}
    return {"window": window, "safety_defaults": safety, "channels": channels}


def sos_settings_change(household: Household, values: dict[str, Any]) -> list[str]:
    """What a settings form changes; the window and safety defaults are the
    "safety settings" that need a fresh password once set (docs/7 §10)."""
    changes = []
    if values["window"] != household.sos_window_s:
        changes.append("window")
    if values["safety_defaults"] != bool(household.safety_defaults):
        changes.append("safety_defaults")
    if values["channels"] != household_channels(household):
        changes.append("channels")
    return changes


def apply_sos_settings(household: Household, values: dict[str, Any]) -> None:
    household.sos_window_s = values["window"]
    household.safety_defaults = values["safety_defaults"]
    household.channels = dict(values["channels"])


def apply_pins(db: Session, household: Household, sos_hash: str, duress_hash: str | None, *,
               user: User, ip: str | None) -> None:
    """Store Argon2id hashes (made off the event loop by the caller): the
    device verifies PINs itself from the pushed profile."""
    household.sos_pin_hash = sos_hash
    household.duress_pin_hash = duress_hash
    household.pin_version = int(household.pin_version or 0) + 1
    security.audit(db, "profile.pins_changed", actor_type="user", actor_id=user.id,
                   household_id=household.id, target_type="household",
                   target_id=household.id, ip=ip,
                   detail={"pin_version": household.pin_version,
                           "duress_pin_set": duress_hash is not None})


# ── household members ─────────────────────────────────────────────────────────
def clean_member(errors: FormErrors, form) -> dict[str, Any]:
    """Name, relation, age band and an optional invitation e-mail. Minors
    get no Portal account (and their faces are never enrolled)."""
    name = clean_text(errors, "member_name", form.get("member_name"), label="Name",
                      max_len=80, required=True, min_len=2)
    relation = clean_text(errors, "member_relation", form.get("member_relation"),
                          label="Relation", max_len=40)
    band = clean_choice(errors, "age_band", form.get("age_band"), AGE_BANDS,
                        label="Age band")
    email = clean_email(errors, "invite_email", form.get("invite_email"),
                        label="Invitation e-mail")
    if email and band == "minor":
        errors.add("invite_email", "Minors do not get a Portal account.")
    return {"name": name, "relation": relation, "age_band": band, "invite_email": email}


def _member_username(db: Session, household: Household, name: str) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:16] or "member"
    base = f"{(household.customer_code or 'sx').lower()}-{stem}"[:28]
    candidate, number = base, 1
    while username_taken(db, candidate):
        number += 1
        candidate = f"{base}-{number}"
    return candidate


def add_member(db: Session, household: Household, values: dict[str, Any], *,
               actor: User, ip: str | None) -> tuple[HouseholdMember, User | None]:
    """A household member; with an e-mail, also an invited ``member`` account
    that activates from the e-mailed code (the owner never sees it)."""
    invited = None
    if values["invite_email"]:
        invited = User(username=_member_username(db, household, values["name"]),
                       display_name=values["name"], email=values["invite_email"],
                       role="member", household_id=household.id,
                       status="pending_activation", created_by=actor.id)
        db.add(invited)
        db.flush()
    member = HouseholdMember(household_id=household.id, name=values["name"],
                             relation=values["relation"], age_band=values["age_band"],
                             user_id=invited.id if invited else None)
    db.add(member)
    db.flush()
    security.audit(db, "member.added", actor_type="user", actor_id=actor.id,
                   household_id=household.id, target_type="member", target_id=member.id,
                   ip=ip, detail={"age_band": member.age_band, "invited": bool(invited)})
    if invited is not None:
        send_activation_email(db, invited, purpose="activate", created_by=actor.id,
                              actor_type="user")
    profile_changed(db, household.id, actor_id=actor.id, what="members", ip=ip)
    return member, invited


def remove_member(db: Session, member: HouseholdMember, *, actor: User,
                  ip: str | None) -> None:
    """Remove a member; their Portal account (if any) is closed and signed out."""
    household_id = member.household_id
    if member.user_id:
        user = db.get(User, member.user_id)
        if user is not None and user.role == "member":
            user.status = "closed"
            security.revoke_user_sessions(db, user.id)
    db.delete(member)
    security.audit(db, "member.removed", actor_type="user", actor_id=actor.id,
                   household_id=household_id, target_type="member", target_id=member.id,
                   ip=ip)
    profile_changed(db, household_id, actor_id=actor.id, what="members", ip=ip)
    db.flush()


# ── contacts and their consent ────────────────────────────────────────────────
def contact_phone(contact: EmergencyContact) -> str | None:
    return security.unseal(contact.phone_enc) if contact.phone_enc else None


def confirmed_contacts(db, household_id: str) -> list[EmergencyContact]:
    """The household's CONFIRMED contacts by priority: the only ones an SOS reaches."""
    return (db.query(EmergencyContact)
            .filter_by(household_id=household_id, consent_status="CONFIRMED")
            .order_by(EmergencyContact.priority, EmergencyContact.created_at).all())


def active_contacts(db: Session, household_id: str) -> list[EmergencyContact]:
    """Contacts that count towards the limit of 3 (not declined/revoked)."""
    return (db.query(EmergencyContact)
            .filter(EmergencyContact.household_id == household_id,
                    EmergencyContact.consent_status.in_(("PENDING", "CONFIRMED")))
            .order_by(EmergencyContact.priority, EmergencyContact.created_at).all())


def contact_telegram_linked(db: Session, contact: EmergencyContact) -> bool:
    if contact.telegram_chat_id:
        return True
    return db.query(TelegramLink).filter_by(subject_type="contact", subject_id=contact.id,
                                            status="active").first() is not None


def contact_unreachable_offline(db: Session, contact: EmergencyContact) -> bool:
    """Neither Telegram nor e-mail: only the device's modem can reach them,
    so not while the device is offline."""
    return not contact.email and not contact_telegram_linked(db, contact)


def clean_contact(errors: FormErrors, form, *, owner_phone: str | None,
                  taken_priorities: set[int], taken_phones: set[str]) -> dict[str, Any]:
    name = clean_text(errors, "name", form.get("name"), label="Name", max_len=80,
                      required=True, min_len=2)
    relation = clean_text(errors, "relation", form.get("relation"), label="Relation",
                          max_len=40, required=True)
    phone = clean_phone(errors, "phone", form.get("phone"), label="Mobile")
    email = clean_email(errors, "email", form.get("email"))
    priority = clean_int(errors, "priority", form.get("priority"), label="Priority",
                         lo=1, hi=MAX_CONTACTS)
    if phone and owner_phone and phone == owner_phone:
        errors.add("phone", "A contact must not be your own number.")
    elif phone and phone in taken_phones:
        errors.add("phone", "That number is already one of your contacts.")
    if priority and priority in taken_priorities:
        errors.add("priority", f"Another contact already has priority {priority}.")
    return {"name": name, "relation": relation, "phone": phone, "email": email,
            "priority": priority, "can_reach_30min": bool(form.get("can_reach_30min"))}


def mint_consent_token(db: Session, contact: EmergencyContact, *,
                       seconds: int = CONSENT_TOKEN_SECONDS) -> tuple[str, ActionToken]:
    """A ``contact_consent`` link token; earlier ones of the contact stop working."""
    revoke_tokens(db, purpose="contact_consent", subject_type="contact",
                  subject_id=contact.id)
    raw = security.new_token(32)
    row = ActionToken(token_hash=security.token_digest(raw), purpose="contact_consent",
                      household_id=contact.household_id, subject_type="contact",
                      subject_id=contact.id,
                      expires_at=utcnow() + timedelta(seconds=seconds))
    db.add(row)
    db.flush()
    return raw, row


def consent_url(raw: str) -> str:
    return f"{portal_config().base_url}/consent/{raw}"


def request_contact_consent(db: Session, contact: EmergencyContact, *,
                            owner_name: str) -> tuple[str, str | None]:
    """New link token, e-mailed when the contact has an address. Returns
    (link to show the owner once, outbox id or None)."""
    raw, row = mint_consent_token(db, contact)
    url = consent_url(raw)
    queued = None
    if contact.email:
        recipient = Recipient(ref=f"contact:{contact.id}", name=contact.name,
                              channel="email", address=contact.email, role="contact")
        days = CONSENT_TOKEN_SECONDS // 86400
        queued = send_email(
            db, recipient, template="contact.consent", step=f"consent:{row.id}",
            subject=f"{owner_name} asked you to be an emergency contact",
            text=(f"{owner_name} added you as an emergency contact for their SENTRIX home "
                  f"alarm. If you agree, SENTRIX may send you SOS alerts about their home "
                  f"(what happened, the address, a camera picture on a private page).\n\n"
                  f"Confirm or opt out: {url}\n\nNothing is sent to you unless you "
                  f"confirm. The link expires in {days} days."))
    return url, queued


def add_contact(db: Session, household: Household, values: dict[str, Any], *,
                actor: User, ip: str | None) -> tuple[EmergencyContact, str]:
    """A PENDING contact and its consent request. Returns (contact, link)."""
    contact = EmergencyContact(household_id=household.id, name=values["name"],
                               relation=values["relation"],
                               phone_enc=security.seal(values["phone"]),
                               email=values["email"], priority=values["priority"],
                               can_reach_30min=values["can_reach_30min"],
                               consent_status="PENDING")
    db.add(contact)
    db.flush()
    security.audit(db, "contact.added", actor_type="user", actor_id=actor.id,
                   household_id=household.id, target_type="contact", target_id=contact.id,
                   ip=ip, detail={"priority": contact.priority,
                                  "phone": mask_phone(values["phone"]),
                                  "email": mask_email(values["email"]) or None})
    link, _ = request_contact_consent(db, contact, owner_name=actor.display_name or "A resident")
    return contact, link


def update_contact(db: Session, contact: EmergencyContact, values: dict[str, Any], *,
                   actor: User, ip: str | None) -> str | None:
    """Edit a contact. A new number or e-mail needs a new confirmation (the
    consent was for the old one): back to PENDING. Returns the new link."""
    old_phone, old_email = contact_phone(contact), contact.email
    was_confirmed = contact.consent_status == "CONFIRMED"
    contact.name, contact.relation = values["name"], values["relation"]
    contact.priority, contact.can_reach_30min = values["priority"], values["can_reach_30min"]
    reconfirm = values["phone"] != old_phone or values["email"] != old_email
    contact.phone_enc = security.seal(values["phone"])
    contact.email = values["email"]
    link = None
    if reconfirm:
        contact.consent_status, contact.confirmed_at = "PENDING", None
        _withdraw_subject_consents(db, contact.household_id, "contact", contact.id)
        link, _ = request_contact_consent(db, contact,
                                          owner_name=actor.display_name or "A resident")
    security.audit(db, "contact.updated", actor_type="user", actor_id=actor.id,
                   household_id=contact.household_id, target_type="contact",
                   target_id=contact.id, ip=ip, detail={"reconfirm": reconfirm})
    if was_confirmed:
        profile_changed(db, contact.household_id, actor_id=actor.id, what="contacts", ip=ip)
    db.flush()
    return link


def remove_contact(db: Session, contact: EmergencyContact, *, actor: User,
                   ip: str | None) -> None:
    was_confirmed = contact.consent_status == "CONFIRMED"
    household_id = contact.household_id
    _forget_contact_links(db, contact)
    db.delete(contact)
    security.audit(db, "contact.removed", actor_type="user", actor_id=actor.id,
                   household_id=household_id, target_type="contact", target_id=contact.id,
                   ip=ip)
    if was_confirmed:
        profile_changed(db, household_id, actor_id=actor.id, what="contacts", ip=ip)
    db.flush()


def _forget_contact_links(db: Session, contact: EmergencyContact) -> None:
    now = utcnow()
    for row in db.query(ActionToken).filter_by(subject_type="contact", subject_id=contact.id,
                                               revoked_at=None):
        row.revoked_at = now
    for link in db.query(TelegramLink).filter_by(subject_type="contact",
                                                 subject_id=contact.id, status="active"):
        link.status = "revoked"


def consent_page_target(db: Session, raw: str | None) -> tuple[ActionToken, EmergencyContact] | None:
    row = live_token(db, raw, "contact_consent")
    if row is None or row.subject_type != "contact":
        return None
    contact = db.get(EmergencyContact, row.subject_id)
    if contact is None or contact.household_id != row.household_id:
        return None
    household = db.get(Household, contact.household_id)
    if household is None or household.status not in ("active",):
        return None
    return row, contact


def decide_contact_consent(db: Session, token: ActionToken, contact: EmergencyContact,
                           decision: str, *, ip: str | None) -> str | None:
    """The contact's answer on ``/consent/<token>``. ``confirm`` returns a
    long-lived opt-out link token; ``decline`` erases their phone number
    and e-mail at once (the row goes after ``declined_contact_days``)."""
    if decision == "confirm" and contact.consent_status == "CONFIRMED":
        return None                                   # an opt-out link stays usable
    now = utcnow()
    token.used_at = now
    settings = portal_config()
    if decision == "confirm":
        contact.consent_status, contact.confirmed_at = "CONFIRMED", now
        db.add(Consent(household_id=contact.household_id, subject_type="contact",
                       subject_id=contact.id, purpose="account_sos",
                       notice_version=settings.notice_version, granted_at=now,
                       method="contact_page"))
        security.audit(db, "contact.confirmed", actor_type="contact", actor_id=contact.id,
                       household_id=contact.household_id, target_type="contact",
                       target_id=contact.id, ip=ip)
        profile_changed(db, contact.household_id, actor_id=contact.id, actor_type="contact",
                        what="contacts", ip=ip)
        raw, _ = mint_consent_token(db, contact, seconds=OPT_OUT_TOKEN_SECONDS)
        return raw
    if decision != "decline":
        raise ValueError(decision)
    was_confirmed = contact.consent_status == "CONFIRMED"
    contact.consent_status = "REVOKED" if was_confirmed else "DECLINED"
    contact.phone_enc = contact.email = contact.telegram_chat_id = None
    contact.can_reach_30min = False
    _withdraw_subject_consents(db, contact.household_id, "contact", contact.id)
    db.add(Consent(household_id=contact.household_id, subject_type="contact",
                   subject_id=contact.id, purpose="account_sos",
                   notice_version=settings.notice_version, granted_at=None,
                   withdrawn_at=now, method="contact_page"))
    _forget_contact_links(db, contact)
    security.audit(db, "contact.declined", actor_type="contact", actor_id=contact.id,
                   household_id=contact.household_id, target_type="contact",
                   target_id=contact.id, ip=ip, detail={"after_confirm": was_confirmed})
    if was_confirmed:
        profile_changed(db, contact.household_id, actor_id=contact.id, actor_type="contact",
                        what="contacts", ip=ip)
    db.flush()
    return None


def contact_decided_at(db: Session, contact: EmergencyContact) -> datetime:
    """When a DECLINED/REVOKED contact answered (the withdrawal row)."""
    row = (db.query(Consent).filter(Consent.subject_type == "contact",
                                    Consent.subject_id == contact.id,
                                    Consent.withdrawn_at.isnot(None))
           .order_by(Consent.withdrawn_at.desc()).first())
    return as_utc(row.withdrawn_at) if row else as_utc(contact.created_at)


# ── household purposes (consents) ─────────────────────────────────────────────
def _withdraw_subject_consents(db: Session, household_id: str, subject_type: str,
                               subject_id: str) -> None:
    now = utcnow()
    for row in db.query(Consent).filter_by(household_id=household_id,
                                           subject_type=subject_type,
                                           subject_id=subject_id, withdrawn_at=None):
        if row.granted_at is not None:
            row.withdrawn_at = now


def _active_consent(db: Session, household_id: str, purpose: str) -> Consent | None:
    return (db.query(Consent)
            .filter(Consent.household_id == household_id, Consent.subject_type == "user",
                    Consent.purpose == purpose, Consent.granted_at.isnot(None),
                    Consent.withdrawn_at.is_(None))
            .order_by(Consent.granted_at.desc()).first())


def consent_states(db: Session, household_id: str) -> dict[str, bool]:
    """purpose -> currently granted (the household's own consents)."""
    return {purpose: _active_consent(db, household_id, purpose) is not None
            for purpose in CONSENT_PURPOSES}


def consent_history(db: Session, household_id: str) -> list[Consent]:
    return (db.query(Consent).filter_by(household_id=household_id, subject_type="user")
            .order_by(Consent.granted_at.desc()).all())


def set_consent(db: Session, household: Household, purpose: str, granted: bool, *,
                user: User, method: str, ip: str | None = None) -> bool:
    """Grant or withdraw one purpose (withdrawing is as easy as granting).
    Withdrawing ``medical_notes`` erases the notes at once. Returns whether
    anything changed (the caller bumps the profile once for a batch)."""
    if purpose not in CONSENT_PURPOSES:
        raise ValueError(purpose)
    current = _active_consent(db, household.id, purpose)
    if granted == (current is not None):
        return False
    now = utcnow()
    if granted:
        db.add(Consent(household_id=household.id, subject_type="user", subject_id=user.id,
                       purpose=purpose, notice_version=portal_config().notice_version,
                       granted_at=now, method=method, actor_user_id=user.id))
    else:
        current.withdrawn_at = now
        if purpose == "medical_notes":
            household.medical_enc = None
            household.include_medical = False
    security.audit(db, "consent.granted" if granted else "consent.withdrawn",
                   actor_type="user", actor_id=user.id, household_id=household.id,
                   target_type="consent", target_id=purpose, ip=ip,
                   detail={"purpose": purpose, "method": method,
                           "notice_version": portal_config().notice_version})
    db.flush()
    return True


# ── Telegram linking (T-CH's bot consumes the code) ──────────────────────────
def mint_telegram_link(db, *, subject_type: str, subject_id: str,
                       household_id: str) -> str:
    """A single-use ``telegram_link`` code for ``t.me/<bot>?start=<code>``
    (22 URL-safe characters, within Telegram's 64-character ``start``
    payload). Earlier codes of the subject stop working."""
    if subject_type not in ("user", "contact"):
        raise ValueError(f"cannot link Telegram for {subject_type!r}")
    revoke_tokens(db, purpose="telegram_link", subject_type=subject_type,
                  subject_id=subject_id)
    raw = security.new_token(16)
    db.add(ActionToken(token_hash=security.token_digest(raw), purpose="telegram_link",
                       household_id=household_id, subject_type=subject_type,
                       subject_id=subject_id,
                       expires_at=utcnow() + timedelta(seconds=TELEGRAM_LINK_SECONDS)))
    db.flush()
    return raw


def telegram_deep_link(code: str) -> str | None:
    bot = portal_config().telegram_bot_name.lstrip("@")
    return f"https://t.me/{bot}?start={code}" if bot else None


def telegram_links(db: Session, subject_type: str, subject_id: str) -> list[TelegramLink]:
    return (db.query(TelegramLink).filter_by(subject_type=subject_type,
                                             subject_id=subject_id, status="active")
            .order_by(TelegramLink.linked_at.desc()).all())


# ── the profile (docs/7 §5.11) ────────────────────────────────────────────────
def clamp_window(seconds: int | None) -> int:
    choices = tuple(sos_config().window_choices)
    if seconds in choices:
        return int(seconds)
    return min(choices, key=lambda c: abs(c - (seconds or choices[0])))


def household_channels(household: Household) -> dict[str, bool]:
    stored = household.channels or {}
    return {key: bool(stored.get(key, DEFAULT_CHANNELS[key])) for key in CHANNEL_KEYS}


def medical_notes(household: Household) -> str | None:
    return security.unseal(household.medical_enc) if household.medical_enc else None


def household_members(db: Session, household_id: str) -> list[HouseholdMember]:
    return (db.query(HouseholdMember).filter_by(household_id=household_id)
            .order_by(HouseholdMember.created_at).all())


def build_profile(db, household_id: str, device_id: str) -> dict[str, Any]:
    """The profile pushed to ``device_id`` at the household's current
    ``profile_version`` (docs/7 §5.11). Raises ``LookupError`` when the
    device is not this household's."""
    if db is None:
        # The foundation stub raised NotImplementedError and the foundation
        # contract test still probes it without a session; see
        # INTEGRATION_NOTES_acct.md (drop this guard with that assertion).
        raise NotImplementedError("build_profile needs a Portal database session")
    household = db.get(Household, household_id)
    device = db.get(Device, device_id)
    if household is None or device is None or device.household_id != household_id:
        raise LookupError(f"device {device_id} is not in household {household_id}")
    return _profile(db, household, device)


def preview_profile(db: Session, household: Household) -> dict[str, Any]:
    """The profile the household's first claimed device would get (before
    the claim: the same shape without a device), for the SOS preview."""
    devices = claimed_devices(db, household.id)
    return _profile(db, household, devices[0] if devices else None)


def _profile(db: Session, household: Household, device: Device | None) -> dict[str, Any]:
    settings = portal_config()
    owner = household_owner(db, household.id)
    consents = consent_states(db, household.id)
    include_notes = consents["medical_notes"] and household.include_medical
    located = household.lat is not None and household.lon is not None
    occupants = household.occupants or {}
    profile = {
        "schema": 1, "version": int(household.profile_version or 0),
        "issued_at": utcnow().replace(microsecond=0).isoformat(),
        "device_id": device.id if device else None, "household_id": household.id,
        "customer_code": household.customer_code,
        "language": household.language or "en", "test_mode": bool(settings.test_mode),
        "owner": {"user_id": owner.id if owner else None,
                  "name": (owner.display_name or owner.username) if owner else None,
                  "phone": user_phone(owner),
                  "phone_verified": bool(owner and owner.phone_verified_at)},
        "members": [{"id": m.id, "name": m.name, "relation": m.relation,
                     "age_band": m.age_band, "user_id": m.user_id}
                    for m in household_members(db, household.id)],
        "contacts": [{"id": c.id, "name": c.name, "relation": c.relation,
                      "phone": contact_phone(c), "telegram": contact_telegram_linked(db, c),
                      "priority": c.priority, "consent_status": c.consent_status,
                      "can_reach_30min": bool(c.can_reach_30min)}
                     for c in confirmed_contacts(db, household.id)],
        "desk": {"name": "SENTRIX Desk (simulated)", "phone": settings.desk_number or None,
                 "simulated": True},
        "address": {"house": household.house, "building": household.building,
                    "street": household.street, "locality": household.locality,
                    "landmark": household.landmark, "city": household.city,
                    "district": household.district, "state": household.state,
                    "pincode": household.pincode, "floor": household.floor,
                    "entry_note": household.entry_note, "lat": household.lat,
                    "lon": household.lon,
                    "map_url": (f"https://maps.google.com/?q={household.lat},{household.lon}"
                                if located else None)},
        "household": {"occupants": {k: int(occupants.get(k, 0) or 0)
                                    for k in ("adults", "children", "seniors")},
                      "pets": household.pets,
                      "medical_notes": medical_notes(household) if include_notes else None},
        "nearest": {k: dict(v) for k, v in (household.nearest or {}).items()
                    if isinstance(v, dict)},
        "cameras": [{"camera_id": c.camera_id, "room_label": c.room_label, "floor": c.floor}
                    for c in (device_cameras(db, device.id) if device else [])],
        "sos": {"window_seconds": clamp_window(household.sos_window_s),
                "safety_defaults": bool(household.safety_defaults),
                "contacts_for_single_source_l3":
                    bool(sos_config().contacts_for_single_source_l3),
                "pin_hash": household.sos_pin_hash,
                "duress_pin_hash": household.duress_pin_hash,
                "pin_version": int(household.pin_version or 0),
                "channels": household_channels(household), "templates": None},
        "consents": {**consents, "notice_version": settings.notice_version},
        "mode": {"armed": bool(device.armed) if device else False},
    }
    from sentrix.link import profile as device_profile
    reason = device_profile.validate(profile)
    if reason:                                        # a programming error, never data
        raise ValueError(f"profile would be refused by the device: {reason}")
    return profile


# ── the SOS preview (wizard step 7, settings) ────────────────────────────────
#: Single-source L3 wording (docs/7 §7.10): "possible (one camera check)".
_PREVIEW_WHAT = {"weapon": "possible WEAPON", "fire": "possible FIRE",
                 "intrusion": "INTRUSION while armed"}


def render_sos_preview(profile: dict[str, Any], *, incident_type: str = "weapon",
                       when: datetime | None = None) -> dict[str, str]:
    """``{"full", "sms"}``: what an SOS from this profile would say.

    T-SOS owns the one English template source (``sentrix/sos/templates.py``);
    when it exposes ``render_preview(profile, incident_type=...) -> {"full",
    "sms"}`` that is used, otherwise this Portal-side rendering of the same
    fixed wording (docs/7 §7.10)."""
    try:
        from sentrix.sos import templates as sos_templates
        render = getattr(sos_templates, "render_preview", None)
    except ImportError:
        render = None
    if callable(render):
        try:
            rendered = render(profile, incident_type=incident_type)
            return {"full": str(rendered["full"]), "sms": str(rendered["sms"])}
        except Exception as exc:
            log.error("sentrix.sos.templates.render_preview failed: %s", exc)
    return _portal_preview(profile, incident_type, when or utcnow())


def _join(*parts: Any, sep: str = ", ") -> str:
    return sep.join(str(p) for p in parts if p not in (None, ""))


def _portal_preview(profile: dict[str, Any], incident_type: str,
                    when: datetime) -> dict[str, str]:
    address = profile.get("address") or {}
    owner = profile.get("owner") or {}
    home = profile.get("household") or {}
    occupants = home.get("occupants") or {}
    camera = (profile.get("cameras") or [{}])[0]
    nearest = profile.get("nearest") or {}
    desk = profile.get("desk") or {}
    test = profile.get("test_mode", True)
    resident = owner.get("name") or "the resident"
    what = _PREVIEW_WHAT.get(incident_type, incident_type.upper())

    street = _join(address.get("house"), address.get("building"), address.get("street"),
                   address.get("locality"))
    landmark = f" (landmark: {address['landmark']})" if address.get("landmark") else ""
    floor = f", floor {address['floor']}" if address.get("floor") else ""
    city = _join(address.get("city"), address.get("district"), address.get("state"))
    pets = f". Pets: {home['pets']}" if home.get("pets") else ""
    lines = [guard.TEST_PREFIX] if test else []
    lines += [
        f"SENTRIX SOS - {what} (one camera check) at the home of {resident}.",
        f"WHERE: {_join(camera.get('room_label'), camera.get('floor')) or 'camera'}.",
        f"ADDRESS: {street}{landmark}, {city} - {address.get('pincode') or '?'}{floor}.",
    ]
    if address.get("map_url"):
        lines.append(f"MAP: {address['map_url']}")
    if address.get("entry_note"):
        lines.append(f"ACCESS: {address['entry_note']}")
    lines.append(f"HOME: {occupants.get('adults', 0)} adults, {occupants.get('children', 0)} "
                 f"children, {occupants.get('seniors', 0)} seniors{pets}.")
    if home.get("medical_notes"):
        lines.append(f"NOTES: {home['medical_notes']}")
    lines.append(f"RESIDENT: {resident}, call back {owner.get('phone') or '?'}.")
    stations = []
    for key, label in (("police", "Police"), ("fire", "Fire"), ("hospital", "Hospital")):
        place = nearest.get(key) if isinstance(nearest.get(key), dict) else {}
        if place.get("name"):
            stations.append(f"{label}: {_join(place.get('name'), place.get('phone'), sep=' ')}")
    if stations:
        lines.append("NEAREST: " + "; ".join(stations) + ".")
    contacts = [_join(c.get("name"), f"({c['relation']})" if c.get("relation") else None,
                      sep=" ") for c in profile.get("contacts") or []]
    if contacts:
        lines.append("CONTACTS ALERTED: " + "; ".join(contacts) + ".")
    if test and desk.get("phone"):
        lines.append(f"Demo: call the SENTRIX Desk (simulated) on {desk['phone']} instead "
                     f"of 112.")
    lines.append(f"TIME: {when.strftime('%d %b %Y %H:%M')} UTC.")
    lines.append("SENTRIX does not contact 112 itself.")

    place = _join(address.get("locality"), address.get("city"), address.get("pincode"))
    sms = _join(guard.TEST_PREFIX_GSM if test else None,
                f"SENTRIX SOS: {what.lower()} at {resident}'s home, {place or '?'}.",
                f"Map: {address['map_url']}" if address.get("map_url") else None,
                f"Resident: {owner.get('phone') or '?'}.",
                "SENTRIX does not contact 112 itself.", sep=" ")
    return {"full": "\n".join(lines), "sms": sms}


# ── the privacy notice ────────────────────────────────────────────────────────
def grievance_contact() -> str:
    """Who to complain to before the Data Protection Board
    (``PORTAL_GRIEVANCE_CONTACT`` names the team's address)."""
    import os
    return (os.getenv("PORTAL_GRIEVANCE_CONTACT", "").strip()
            or "the SENTRIX capstone team, CSED, Thapar Institute, Patiala")


def notice_context() -> dict[str, Any]:
    """What the standalone notice (``privacy/_notice_body.html``) renders."""
    from sentrix.config import settings
    return {"purposes": PURPOSES, "notice_version": portal_config().notice_version,
            "grievance": grievance_contact(), "caps": settings.retention,
            "grace_days": ERASURE_GRACE_DAYS}


# ── onboarding ────────────────────────────────────────────────────────────────
def onboarding_done(db: Session, household: Household, owner: User | None) -> dict[int, bool]:
    """Which wizard steps hold what they need."""
    occupants = household.occupants or {}
    devices = claimed_devices(db, household.id)
    named = [d for d in devices if d.name and any(c.room_label for c in device_cameras(db, d.id))]
    return {
        1: bool(owner and owner.display_name and owner.phone_enc and owner.email),
        2: all(getattr(household, f) for f in ("house", "street", "locality", "landmark",
                                                "city", "state", "pincode")),
        3: "adults" in occupants,
        4: bool(active_contacts(db, household.id)),
        5: bool(named),
        6: bool(household.sos_pin_hash),
        7: household.onboarding_completed_at is not None,
    }


def advance_onboarding(household: Household, step: int) -> None:
    household.onboarding_step = max(int(household.onboarding_step or 1),
                                    min(step + 1, LAST_STEP))


# ── the household's own audit trail ──────────────────────────────────────────
#: Rows an owner or member never sees: restricted SOS rows and anything that
#: would reveal a duress approval (docs/7 §7.8).
def _hidden_from_household(row: AuditLog) -> bool:
    detail = row.detail or {}
    return bool(detail.get("restricted") or detail.get("duress"))


def household_audit(db: Session, household_id: str, *, limit: int = 100,
                    offset: int = 0) -> list[dict[str, Any]]:
    names = {u.id: u.display_name or u.username
             for u in db.query(User).filter_by(household_id=household_id)}
    names.update({c.id: c.name for c in db.query(EmergencyContact)
                  .filter_by(household_id=household_id)})
    rows = (db.query(AuditLog).filter_by(household_id=household_id)
            .order_by(AuditLog.id.desc()))
    out, skipped = [], 0
    for row in rows:
        if _hidden_from_household(row):
            continue
        if skipped < offset:
            skipped += 1
            continue
        actor = {"admin": "SENTRIX staff", "system": "SENTRIX", "device": "Your device",
                 "cloud": "SENTRIX"}.get(row.actor_type)
        out.append({"id": row.id, "at": iso(row.at), "event": row.event,
                    "actor": actor or names.get(row.actor_id) or (row.actor_type or "?"),
                    "target_type": row.target_type, "detail": row.detail or {}})
        if len(out) >= limit:
            break
    return out


# ── export and erasure (DPDP) ────────────────────────────────────────────────
_PROMPTING = {sos_states.DRAFTED, sos_states.PENDING, None}


def _owner_may_see(event: RequestEvent, duress_ids: set[str]) -> bool:
    """A duress request reads "Marked as false alarm" everywhere an owner
    looks: only its events from before the answer are shown (docs/7 §7.8)."""
    if event.restricted:
        return False
    if event.request_id in duress_ids:
        return {event.from_state, event.to_state} <= _PROMPTING
    return True


def _owner_view_request(row: AssistanceRequest) -> dict[str, Any]:
    view = sos_states.owner_view(row.state or "", row.flags or {})
    duress = bool((row.flags or {}).get("duress"))
    return {"id": row.id, "device_id": row.device_id, "state": view["state"],
            "label": view["label"], "flags": view["flags"], "incident_ids": row.incident_ids,
            "incident_type": row.incident_type, "trigger_level": row.trigger_level,
            "peak_level": row.peak_level, "created_at": iso(row.created_at),
            "expires_at": iso(row.expires_at),
            # A duress approval must not show who was asked for help, or when.
            "responded_at": None if duress else iso(row.responded_at),
            "authority": None if duress else row.authority,
            "outcome": None if duress else row.outcome,
            "sos_text_full": row.sos_text_full, "sos_text_sms": row.sos_text_sms}


def export_household(db: Session, household_id: str) -> dict[str, Any]:
    """Everything the Portal holds about the household, in clear, for its
    owner (right of access). Never: password/PIN hashes, TOTP secrets,
    recovery codes, session ids, code or token digests, duress events."""
    household = db.get(Household, household_id)
    if household is None:
        raise LookupError(household_id)
    users = db.query(User).filter_by(household_id=household_id).all()
    user_ids = [u.id for u in users]
    devices = db.query(Device).filter_by(household_id=household_id).all()
    device_ids = [d.id for d in devices]
    requests = (db.query(AssistanceRequest).filter_by(household_id=household_id)
                .order_by(AssistanceRequest.created_at).all())
    request_ids = [r.id for r in requests]
    events = (db.query(RequestEvent).filter(RequestEvent.request_id.in_(request_ids),
                                            RequestEvent.restricted.isnot(True))
              .order_by(RequestEvent.request_id, RequestEvent.origin, RequestEvent.seq).all()
              if request_ids else [])
    snapshots = (db.query(SosSnapshot).filter(SosSnapshot.request_id.in_(request_ids)).all()
                 if request_ids else [])
    duress_ids = {r.id for r in requests if (r.flags or {}).get("duress")}
    return {
        "format": "sentrix-portal-export", "version": 1, "exported_at": iso(utcnow()),
        "notice_version": portal_config().notice_version,
        "household": {
            "id": household.id, "customer_code": household.customer_code,
            "display_name": household.display_name, "status": household.status,
            "language": household.language,
            "address": {f: getattr(household, f) for f in (
                "house", "building", "street", "locality", "landmark", "city", "district",
                "state", "pincode", "floor", "lat", "lon", "entry_note")},
            "occupants": household.occupants or {}, "pets": household.pets,
            "medical_notes": medical_notes(household),
            "include_medical": bool(household.include_medical),
            "nearest": household.nearest or {},
            "sos": {"window_seconds": household.sos_window_s,
                    "safety_defaults": bool(household.safety_defaults),
                    "sos_pin_set": bool(household.sos_pin_hash),
                    "duress_pin_set": bool(household.duress_pin_hash),
                    "pin_version": household.pin_version,
                    "channels": household_channels(household)},
            "profile_version": household.profile_version,
            "onboarding_completed_at": iso(household.onboarding_completed_at),
            "created_at": iso(household.created_at), "closed_at": iso(household.closed_at)},
        "users": [{"id": u.id, "username": u.username, "display_name": u.display_name,
                   "role": u.role, "status": u.status, "email": u.email,
                   "email_verified_at": iso(u.email_verified_at), "phone": user_phone(u),
                   "phone_verified_at": iso(u.phone_verified_at),
                   "totp_enabled": bool(u.totp_enabled), "created_at": iso(u.created_at),
                   "last_login_at": iso(u.last_login_at),
                   "sessions": [{"created_at": iso(s.created_at),
                                 "last_seen_at": iso(s.last_seen_at),
                                 "user_agent": s.user_agent,
                                 "revoked_at": iso(s.revoked_at)}
                                for s in db.query(UserSession).filter_by(user_id=u.id)]}
                  for u in users],
        "members": [{"id": m.id, "name": m.name, "relation": m.relation,
                     "age_band": m.age_band, "user_id": m.user_id,
                     "created_at": iso(m.created_at)}
                    for m in household_members(db, household_id)],
        "contacts": [{"id": c.id, "name": c.name, "relation": c.relation,
                      "phone": contact_phone(c), "email": c.email, "priority": c.priority,
                      "can_reach_30min": bool(c.can_reach_30min),
                      "consent_status": c.consent_status,
                      "confirmed_at": iso(c.confirmed_at),
                      "telegram_linked": contact_telegram_linked(db, c)}
                     for c in db.query(EmergencyContact).filter_by(household_id=household_id)],
        "consents": [{"subject_type": c.subject_type, "subject_id": c.subject_id,
                      "purpose": c.purpose, "notice_version": c.notice_version,
                      "granted_at": iso(c.granted_at), "withdrawn_at": iso(c.withdrawn_at),
                      "method": c.method}
                     for c in db.query(Consent).filter_by(household_id=household_id)],
        "devices": [{"id": d.id, "serial": d.serial, "name": d.name, "state": d.state,
                     "firmware": d.firmware, "armed": bool(d.armed),
                     "assigned_at": iso(d.assigned_at), "claimed_at": iso(d.claimed_at),
                     "last_seen_at": iso(d.last_seen_at),
                     "cameras": [{"camera_id": c.camera_id, "room_label": c.room_label,
                                  "floor": c.floor} for c in device_cameras(db, d.id)]}
                    for d in devices],
        "telegram_links": [{"subject_type": t.subject_type, "subject_id": t.subject_id,
                            "linked_at": iso(t.linked_at), "status": t.status}
                           for t in db.query(TelegramLink).filter_by(household_id=household_id)],
        "sos_requests": [_owner_view_request(r) for r in requests],
        "sos_events": [{"request_id": e.request_id, "origin": e.origin, "seq": e.seq,
                        "at": e.at_text or iso(e.at), "event": e.event,
                        "from_state": e.from_state, "to_state": e.to_state,
                        "actor_type": e.actor_type, "channel": e.channel,
                        "detail": e.detail or {}}
                       for e in events if _owner_may_see(e, duress_ids)],
        "sos_snapshots": [{"request_id": s.request_id, "content_type": s.content_type,
                           "sha256": s.sha256, "created_at": iso(s.created_at),
                           "expires_at": iso(s.expires_at),
                           "data_b64": base64.b64encode(s.data or b"").decode()}
                          for s in snapshots],
        "incidents": [{"id": i.id, "device_id": i.device_id, "started_at": iso(i.started_at),
                       "ended_at": iso(i.ended_at), "peak_level": i.peak_level,
                       "incident_type": i.incident_type, "reason": i.reason,
                       "verdict": i.verdict, "verdict_at": iso(i.verdict_at)}
                      for i in db.query(IncidentMirror).filter_by(household_id=household_id)],
        "dispatch_packages": [{"id": p.id, "incident_id": p.incident_id,
                               "request_id": p.request_id, "status": p.status,
                               "authority": p.chosen_authority or p.authority,
                               "created_at": iso(p.created_at), "sent_at": iso(p.sent_at)}
                              for p in db.query(DispatchMirror).filter_by(
                                  household_id=household_id)
                              if p.request_id not in duress_ids],
        "notifications": [{"request_id": n.request_id, "recipient": n.recipient_masked,
                           "channel": n.channel, "template": n.template, "step": n.step,
                           "status": n.status, "created_at": iso(n.created_at),
                           "sent_at": iso(n.sent_at)}
                          for n in db.query(Notification).filter_by(household_id=household_id)
                          if n.request_id not in duress_ids],
        "audit": household_audit(db, household_id, limit=100000),
        "device_ids": device_ids, "user_ids": user_ids,
    }


def erasure_due_at(household: Household) -> datetime | None:
    if household.status != "erasing" or household.closed_at is None:
        return None
    return as_utc(household.closed_at) + timedelta(days=ERASURE_GRACE_DAYS)


def schedule_erasure(db: Session, household: Household, *, user: User,
                     ip: str | None) -> datetime:
    """Close the account: erased by the retention worker after the grace
    period (cancellable until then). The device keeps protecting the home
    locally; the link is unpaired at erasure."""
    household.status = "erasing"
    household.closed_at = utcnow()
    due = erasure_due_at(household)
    security.audit(db, "privacy.erasure_requested", actor_type="user", actor_id=user.id,
                   household_id=household.id, target_type="household",
                   target_id=household.id, ip=ip, detail={"due": iso(due)})
    db.flush()
    return due


def cancel_erasure(db: Session, household: Household, *, user: User, ip: str | None) -> None:
    household.status = "active"
    household.closed_at = None
    security.audit(db, "privacy.erasure_cancelled", actor_type="user", actor_id=user.id,
                   household_id=household.id, target_type="household",
                   target_id=household.id, ip=ip)
    db.flush()


def erase_household(db: Session, household_id: str) -> dict[str, int]:
    """Delete every row of the household (the audit chain keeps its ids for
    a year, without personal data). Devices go back to UNASSIGNED without a
    claim code: the company re-labels them before reuse."""
    counts: dict[str, int] = {}

    def gone(name: str, query) -> None:
        counts[name] = counts.get(name, 0) + query.delete(synchronize_session=False)

    request_ids = [r for (r,) in db.query(AssistanceRequest.id)
                   .filter_by(household_id=household_id)]
    if request_ids:
        gone("request_events", db.query(RequestEvent)
             .filter(RequestEvent.request_id.in_(request_ids)))
        gone("sos_snapshots", db.query(SosSnapshot)
             .filter(SosSnapshot.request_id.in_(request_ids)))
        gone("notifications", db.query(Notification)
             .filter(Notification.request_id.in_(request_ids)))
    gone("assistance_requests", db.query(AssistanceRequest).filter_by(household_id=household_id))
    gone("notifications", db.query(Notification).filter_by(household_id=household_id))
    for model, name in ((IncidentMirror, "incidents_mirror"), (DispatchMirror, "dispatch_mirror"),
                        (EvidenceMetaMirror, "evidence_meta_mirror"),
                        (SnapshotMirror, "snapshot_mirror"), (ActionToken, "action_tokens"),
                        (TelegramLink, "telegram_links"), (Consent, "consents"),
                        (HouseholdMember, "household_members"),
                        (EmergencyContact, "emergency_contacts")):
        gone(name, db.query(model).filter_by(household_id=household_id))
    for device in db.query(Device).filter_by(household_id=household_id).all():
        gone("cameras", db.query(Camera).filter_by(device_id=device.id))
        gone("device_commands", db.query(DeviceCommand).filter_by(device_id=device.id))
        gone("device_events", db.query(DeviceEvent).filter_by(device_id=device.id))
        device.household_id = None
        device.state = "UNASSIGNED"
        device.claim_code_hash = device.token_hash = None
        device.claimed_at = device.claimed_by = device.assigned_at = None
        device.name, device.online, device.armed = None, False, False
        device.last_hello, device.last_heartbeat = {}, {}
        device.profile_version_acked = 0
        counts["devices_released"] = counts.get("devices_released", 0) + 1
    for user in db.query(User).filter_by(household_id=household_id).all():
        gone("sessions", db.query(UserSession).filter_by(user_id=user.id))
        gone("activation_codes", db.query(ActivationCode).filter_by(user_id=user.id))
        gone("push_subscriptions", db.query(PushSubscription).filter_by(user_id=user.id))
        gone("login_attempts", db.query(LoginAttempt).filter(LoginAttempt.username.in_((
            user.username, security.ACTIVATE_KEY + user.username,
            security.STEP_UP_KEY + user.id, security.TOTP_KEY + user.id))))
        gone("claim_attempts", db.query(ClaimAttempt).filter_by(user_id=user.id))
        db.delete(user)
        counts["users"] = counts.get("users", 0) + 1
    db.flush()
    household = db.get(Household, household_id)
    if household is not None:
        db.delete(household)
        counts["households"] = 1
    security.audit(db, "privacy.erased", household_id=household_id, target_type="household",
                   target_id=household_id, detail=counts)
    db.flush()
    return counts


__all__ = ["build_profile", "confirmed_contacts", "bump_profile_version",
           "mint_telegram_link", "FormErrors", "ProvisionedDevice", "ClaimOutcome",
           "provision_device", "claim_device", "assign_device", "release_device",
           "rotate_claim_code", "create_customer", "create_staff_user",
           "issue_activation_code", "complete_activation", "send_activation_email",
           "profile_changed", "push_profile", "add_contact", "update_contact",
           "remove_contact", "decide_contact_consent", "set_consent", "consent_states",
           "render_sos_preview", "export_household", "schedule_erasure",
           "cancel_erasure", "erase_household", "household_audit", "onboarding_done",
           "PURPOSES", "ONBOARDING_STEPS"]
