"""Portal security primitives (docs/7 §10, §11).

* Passwords and PINs: Argon2id (m = 19 MiB, t = 2, p = 1; OWASP baseline).
* Sessions: a random 256-bit id in the ``portal_session`` cookie; the database
  keeps only HMAC(secret, id), so a stolen database cannot be replayed as
  cookies, and sessions can be revoked.
* CSRF: a per-session token (``<meta name="csrf-token">``, sent back as
  ``X-CSRF-Token`` or a ``csrf_token`` form field) on every state-changing
  request; a double-submit cookie (``portal_csrf``) before login.
* Codes and tokens (activation, claim, action links): HMAC(secret, code), single
  use, expiring; low-entropy codes also need rate limits (track T-ACCT).
* ``require_role``: the dependency every Portal page and API uses;
  ``require_signed_in`` the one for the few account pages a user must reach
  before passing TOTP (``/account/2fa``, ``/logout``).
* Back-off per (username, IP) and per address (``backoff_remaining``,
  ``record_attempt``): the wait doubles after a few free failures and is
  capped, and it never blocks another address (the owner's phone): no hard
  lockout that could keep a resident from "Send help" (docs/7 §10).
* TOTP (RFC 6238 via ``pyotp``, 30 s, one step either side, no replay of an
  accepted step) and single-use recovery codes for ``company_admin`` and
  ``operator`` (D14).
* ``audit``: append-only, hash-chained audit rows. The retention worker may
  drop the oldest prefix after a signed boundary row, which
  ``verify_audit_chain`` reports as a retention gap, not tampering.

Track T-ACCT owns this module after the foundation.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from sentrix.link.protocol import canonical_json
from sentrix_portal import settings as portal_settings
from sentrix_portal.db import get_db
from sentrix_portal.models import AuditLog, LoginAttempt, User, UserSession, utcnow

_HASHER = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1,
                         hash_len=32, salt_len=16)
#: RFC 8628 user-code alphabet: no vowels (no accidental words), no 0/O/1/I.
CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"


def _secret() -> bytes:
    return portal_settings.current().secret_key.encode()


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# ── passwords and PINs ────────────────────────────────────────────────────────
def hash_password(password: str) -> str:
    return _HASHER.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    if not stored_hash or password is None:
        # Spend comparable time so a missing account is not distinguishable.
        try:
            _HASHER.verify(_DUMMY_HASH, "x")
        except VerificationError:
            pass
        return False
    try:
        return _HASHER.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _HASHER.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


#: SOS and duress PINs use the same Argon2id parameters; the hash is what the
#: profile pushes to the device (never a password hash; D14).
hash_pin = hash_password
verify_pin = verify_password

_DUMMY_HASH = _HASHER.hash("sentrix-dummy-password")


# ── tokens and codes ──────────────────────────────────────────────────────────
def new_token(nbytes: int = 32) -> str:
    """URL-safe random token (sessions, action links, contact pages)."""
    return secrets.token_urlsafe(nbytes)


def token_digest(token: str) -> str:
    """What the database stores for a token or code: HMAC-SHA256, hex."""
    return hmac.new(_secret(), token.encode(), hashlib.sha256).hexdigest()


def new_code(length: int = 8, group: int = 4) -> str:
    """Human-typed code from ``CODE_ALPHABET``, grouped: ``KTRW-BQXL``."""
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))
    return "-".join(raw[i:i + group] for i in range(0, length, group))


def normalize_code(code: str) -> str:
    """Case, spaces and dashes do not matter when typing a code."""
    return "".join(c for c in (code or "").upper() if c.isalnum())


def code_digest(code: str) -> str:
    return token_digest(normalize_code(code))


def ip_digest(ip: str | None) -> str | None:
    if not ip:
        return None
    return hmac.new(_secret(), f"ip:{ip}".encode(), hashlib.sha256).hexdigest()[:32]


def client_ip(request: Request) -> str | None:
    """The client address; X-Forwarded-For only from a trusted proxy (Caddy)."""
    peer = request.client.host if request.client else None
    trusted = portal_settings.current().trusted_proxies
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and peer in trusted:
        return forwarded.split(",")[0].strip() or peer
    return peer


def safe_next(target: str | None, default: str = "/home") -> str:
    """A same-site path to continue to after a form (``?next=``): absolute
    paths only, never ``//host`` or a scheme, so it cannot become an open
    redirect."""
    if (not target or not target.startswith("/") or target.startswith("//")
            or "\\" in target or any(ord(c) < 32 for c in target)):
        return default
    return target


# ── back-off (no hard lockout) ────────────────────────────────────────────────
#: Failures counted over this window (since the last success of the key).
BACKOFF_WINDOW_SECONDS = 3600
#: Free failures per (key, address) before any wait; then BASE, 2*BASE, ...
#: up to CAP. The wait always ends and never affects another address.
BACKOFF_FREE_FAILURES = 4
BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 900
#: One address failing across many usernames (credential stuffing).
ADDRESS_FREE_FAILURES = 20
#: Key prefixes in ``login_attempts.username`` for the other secrets a person
#: types (usernames never contain ':').
ACTIVATE_KEY = "activate:"
STEP_UP_KEY = "stepup:"
TOTP_KEY = "totp:"
PHONE_KEY = "phone:"


def _wait_for(failures: int, free: int) -> int:
    if failures < free:
        return 0
    return min(BACKOFF_BASE_SECONDS * 2 ** (failures - free), BACKOFF_CAP_SECONDS)


def _remaining(rows: list[LoginAttempt], free: int, now: datetime) -> int:
    failures, last_failure = 0, None
    for row in rows:                                  # newest first
        if row.success:
            break
        failures += 1
        last_failure = last_failure or _aware(row.at)
    wait = _wait_for(failures, free)
    if not wait or last_failure is None:
        return 0
    return max(0, int((last_failure + timedelta(seconds=wait) - now).total_seconds() + 0.999))


def attempt_key(username: str | None) -> str:
    return (username or "").strip().lower()[:64]


def backoff_remaining(db: Session, key: str, ip: str | None, *,
                      now: datetime | None = None) -> int:
    """Seconds before ``key`` may be tried again from ``ip`` (0: now).

    ``key`` is a lower-cased username for sign-in, or one of the prefixed keys
    (``ACTIVATE_KEY + username``, ``STEP_UP_KEY + user id``, ``TOTP_KEY +
    user id``). Counted per (key, address) and, with a larger free budget,
    per address across keys."""
    now = now or utcnow()
    since = now - timedelta(seconds=BACKOFF_WINDOW_SECONDS)
    ip_hash = ip_digest(ip)
    own = (db.query(LoginAttempt)
           .filter(LoginAttempt.username == key, LoginAttempt.ip_hash == ip_hash,
                   LoginAttempt.at >= since)
           .order_by(LoginAttempt.at.desc(), LoginAttempt.id.desc()).limit(64).all())
    address = (db.query(LoginAttempt)
               .filter(LoginAttempt.ip_hash == ip_hash, LoginAttempt.at >= since)
               .order_by(LoginAttempt.at.desc(), LoginAttempt.id.desc()).limit(256).all())
    return max(_remaining(own, BACKOFF_FREE_FAILURES, now),
               _remaining(address, ADDRESS_FREE_FAILURES, now))


def record_attempt(db: Session, key: str, ip: str | None, success: bool) -> None:
    db.add(LoginAttempt(username=key[:64], ip_hash=ip_digest(ip), at=utcnow(),
                        success=bool(success)))
    db.flush()


# ── TOTP and recovery codes (company_admin, operator; D14) ───────────────────
TOTP_ISSUER = "SENTRIX Portal"
TOTP_INTERVAL = 30
RECOVERY_CODE_COUNT = 10
_totp_lock = threading.Lock()
#: Last accepted time step per user: an observed code cannot be replayed.
_totp_last_step: dict[str, int] = {}


def new_totp_secret() -> str:
    import pyotp
    return pyotp.random_base32()


def totp_uri(secret: str, username: str) -> str:
    import pyotp
    return pyotp.TOTP(secret, interval=TOTP_INTERVAL).provisioning_uri(
        name=username, issuer_name=TOTP_ISSUER)


def verify_totp(secret: str | None, code: str | None, *, user_id: str,
                now: float | None = None) -> bool:
    """A 6-digit code for the current step or one step either side, never a
    step at or before the last one accepted for this user."""
    import time

    import pyotp
    digits = "".join(c for c in (code or "") if c.isdigit())
    if not secret or len(digits) != 6:
        return False
    totp = pyotp.TOTP(secret, interval=TOTP_INTERVAL)
    step = int((time.time() if now is None else now) // TOTP_INTERVAL)
    for candidate in (step, step - 1, step + 1):
        if hmac.compare_digest(totp.generate_otp(candidate), digits):
            with _totp_lock:
                if candidate <= _totp_last_step.get(user_id, -1):
                    return False
                _totp_last_step[user_id] = candidate
            return True
    return False


def new_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> tuple[list[str], list[str]]:
    """(codes to show once, digests to store)."""
    codes = [new_code(10, 5) for _ in range(count)]
    return codes, [code_digest(c) for c in codes]


def consume_recovery_code(user: User, code: str | None) -> bool:
    """Single use: a matching digest is removed from ``user.recovery_codes``."""
    if not code or not normalize_code(code):
        return False
    digest = code_digest(code)
    stored = list(user.recovery_codes or [])
    for value in stored:
        if hmac.compare_digest(value, digest):
            stored.remove(value)
            user.recovery_codes = stored          # a new list: JSON change detected
            return True
    return False


# ── column encryption ─────────────────────────────────────────────────────────
def _data_key() -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    material = portal_settings.current().data_key.encode()
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=b"sentrix-portal-columns-v1").derive(material)


def seal(plaintext: str | None) -> str | None:
    """AES-256-GCM for ``*_enc`` columns: base64(nonce || ciphertext)."""
    if plaintext is None:
        return None
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    sealed = AESGCM(_data_key()).encrypt(nonce, plaintext.encode(), None)
    return base64.b64encode(nonce + sealed).decode()


def unseal(token: str | None) -> str | None:
    if not token:
        return None
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = base64.b64decode(token)
    return AESGCM(_data_key()).decrypt(raw[:12], raw[12:], None).decode()


# ── sessions ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Principal:
    """The signed-in user of this request."""

    user_id: str
    username: str
    display_name: str
    role: str
    household_id: str | None
    session_id_hash: str
    csrf_token: str
    totp_verified: bool
    step_up_at: datetime | None

    def stepped_up(self, seconds: int | None = None) -> bool:
        seconds = seconds or portal_settings.current().step_up_seconds
        return (self.step_up_at is not None
                and utcnow() - self.step_up_at <= timedelta(seconds=seconds))


def create_session(db: Session, user: User, *, ip: str | None = None,
                   user_agent: str | None = None,
                   totp_verified: bool = False) -> tuple[str, UserSession]:
    """New session for ``user``; returns (raw cookie value, row). Call at
    login only (a fresh id every login: no fixation)."""
    settings = portal_settings.current()
    raw = new_token(32)
    now = utcnow()
    row = UserSession(id_hash=token_digest(raw), user_id=user.id, created_at=now,
                      expires_at=now + timedelta(seconds=settings.session_seconds),
                      last_seen_at=now, ip_hash=ip_digest(ip),
                      user_agent=(user_agent or "")[:200], csrf_token=new_token(24),
                      totp_verified=totp_verified, step_up_at=now)
    db.add(row)
    db.flush()
    return raw, row


def load_session(db: Session, raw: str | None) -> tuple[UserSession, User] | None:
    """The live session and its active user, or None (expired, idle, revoked)."""
    if not raw:
        return None
    row = db.get(UserSession, token_digest(raw))
    if row is None or row.revoked_at is not None:
        return None
    now = utcnow()
    settings = portal_settings.current()
    if _aware(row.expires_at) <= now:
        return None
    if now - _aware(row.last_seen_at) > timedelta(seconds=settings.session_idle_seconds):
        return None
    user = db.get(User, row.user_id)
    if user is None or user.status != "active":
        return None
    if now - _aware(row.last_seen_at) > timedelta(seconds=60):
        row.last_seen_at = now
    return row, user


def revoke_session(db: Session, raw_or_hash: str, *, is_hash: bool = False) -> bool:
    row = db.get(UserSession, raw_or_hash if is_hash else token_digest(raw_or_hash))
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = utcnow()
    return True


def revoke_user_sessions(db: Session, user_id: str, *, keep_hash: str | None = None) -> int:
    count = 0
    for row in db.query(UserSession).filter_by(user_id=user_id, revoked_at=None):
        if row.id_hash != keep_hash:
            row.revoked_at = utcnow()
            count += 1
    return count


def principal_of(request: Request, db: Session) -> Principal | None:
    settings = portal_settings.current()
    loaded = load_session(db, request.cookies.get(settings.cookie_name))
    if loaded is None:
        return None
    row, user = loaded
    return Principal(user_id=user.id, username=user.username,
                     display_name=user.display_name or user.username, role=user.role,
                     household_id=user.household_id, session_id_hash=row.id_hash,
                     csrf_token=row.csrf_token, totp_verified=bool(row.totp_verified),
                     step_up_at=_aware(row.step_up_at))


def set_session_cookie(response, raw: str) -> None:
    settings = portal_settings.current()
    response.set_cookie(settings.cookie_name, raw, httponly=True,
                        secure=settings.secure_cookies, samesite="lax",
                        max_age=settings.session_seconds, path="/")


def clear_session_cookie(response) -> None:
    response.delete_cookie(portal_settings.current().cookie_name, path="/")


# ── CSRF ──────────────────────────────────────────────────────────────────────
STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def issue_login_csrf(response) -> str:
    """Double-submit token for forms shown before login (login, activation)."""
    settings = portal_settings.current()
    token = new_token(24)
    response.set_cookie(settings.csrf_cookie_name, token, httponly=True,
                        secure=settings.secure_cookies, samesite="strict", path="/")
    return token


def verify_login_csrf(request: Request, supplied: str | None) -> bool:
    cookie = request.cookies.get(portal_settings.current().csrf_cookie_name)
    return bool(cookie and supplied and hmac.compare_digest(cookie, supplied))


def login_csrf_token(request: Request) -> str:
    """The double-submit token for a form shown without a session: the
    browser's current one (so two open tabs keep working), else a new one.
    Set it on the response with :func:`set_login_csrf`."""
    existing = request.cookies.get(portal_settings.current().csrf_cookie_name) or ""
    if 16 <= len(existing) <= 64 and existing.replace("-", "").replace("_", "").isalnum():
        return existing
    return new_token(24)


def set_login_csrf(response, token: str) -> None:
    settings = portal_settings.current()
    response.set_cookie(settings.csrf_cookie_name, token, httponly=True,
                        secure=settings.secure_cookies, samesite="strict", path="/")


async def supplied_csrf(request: Request, *, header_only: bool = False) -> str | None:
    """The CSRF token of a request: the ``X-CSRF-Token`` header, else (unless
    ``header_only``) a ``csrf_token`` form field. ``header_only`` never reads
    the body, so a proxy can still forward it (the tunnel)."""
    header = request.headers.get(portal_settings.current().csrf_header)
    if header or header_only:
        return header or None
    content_type = request.headers.get("content-type", "")
    if content_type.startswith(("application/x-www-form-urlencoded",
                                "multipart/form-data")):
        form = await request.form()            # cached by Starlette for the route
        value = form.get("csrf_token")
        return value if isinstance(value, str) else None
    return None


def verify_csrf(principal: Principal, supplied: str | None) -> bool:
    return bool(supplied) and hmac.compare_digest(principal.csrf_token, supplied)


# ── role dependency ───────────────────────────────────────────────────────────
#: Roles that must have passed TOTP in this session (D14).
TOTP_ROLES = frozenset({"company_admin", "operator"})


def _inner_path(path: str) -> str:
    """``/d/<device id>/x`` -> ``/x`` (the console path behind the tunnel)."""
    if path.startswith("/d/"):
        parts = path.split("/", 3)
        return "/" + (parts[3] if len(parts) > 3 else "")
    return path


def _wants_html(request: Request) -> bool:
    """A browser navigating to a page (a logged-out bookmark of
    ``/d/<id>/dashboard`` included): redirect to the login page. APIs, media,
    the video relay and scripted requests get JSON."""
    if request.method != "GET" or "text/html" not in request.headers.get("accept", ""):
        return False
    return not _inner_path(request.url.path).startswith(("/api/", "/media/", "/ws/",
                                                         "/video"))


def require_role(*roles: str, csrf: bool | str = True,
                 step_up: bool = False) -> Callable[..., Any]:
    """Dependency: a signed-in user with one of ``roles`` (any role if none).

    * not signed in -> 303 to ``/login`` for pages, 401 for APIs;
    * wrong role -> 403;
    * admin/operator without TOTP this session -> 303 to ``/account/2fa``
      (403 for APIs);
    * state-changing method without a valid CSRF token -> 403. ``csrf=True``
      accepts the header or a ``csrf_token`` form field; ``csrf="header"``
      only the ``X-CSRF-Token`` header and never reads the body (the device
      proxy, which must forward form and multipart bodies untouched);
      ``csrf=False`` only for endpoints with their own proof (a signed
      webhook);
    * ``step_up=True``: password/PIN re-entered within ``step_up_seconds``,
      else 403 ``step_up_required``.
    """
    allowed = frozenset(roles)

    async def dependency(request: Request, db: Session = Depends(get_db)) -> Principal:
        principal = _signed_in(request, db)
        if allowed and principal.role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="forbidden_role")
        if principal.role in TOTP_ROLES and not principal.totp_verified:
            if _wants_html(request):
                raise HTTPException(status.HTTP_303_SEE_OTHER,
                                    headers={"Location": "/account/2fa"})
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="totp_required")
        await _check_csrf(request, principal, csrf)
        if step_up and not principal.stepped_up():
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="step_up_required")
        request.state.principal = principal
        return principal

    return dependency


def _signed_in(request: Request, db: Session) -> Principal:
    principal = principal_of(request, db)
    if principal is None:
        if _wants_html(request):
            raise HTTPException(status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="not signed in")
    return principal


async def _check_csrf(request: Request, principal: Principal, csrf: bool | str) -> None:
    if csrf and request.method in STATE_CHANGING:
        supplied = await supplied_csrf(request, header_only=csrf == "header")
        if not verify_csrf(principal, supplied):
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="csrf")


def require_signed_in(*, csrf: bool | str = True) -> Callable[..., Any]:
    """Any signed-in user, **without** the TOTP gate: only for the pages a
    company_admin or operator must reach to pass it (``/account/2fa``) or to
    leave (``/logout``). CSRF as in :func:`require_role`."""

    async def dependency(request: Request, db: Session = Depends(get_db)) -> Principal:
        principal = _signed_in(request, db)
        await _check_csrf(request, principal, csrf)
        request.state.principal = principal
        return principal

    return dependency


def mark_step_up(db: Session, principal: Principal) -> None:
    """The user re-entered a password: sensitive actions are allowed for
    ``step_up_seconds`` (docs/7 §3.5)."""
    row = db.get(UserSession, principal.session_id_hash)
    if row is not None:
        row.step_up_at = utcnow()
        db.flush()


def mark_totp_verified(db: Session, principal: Principal) -> None:
    row = db.get(UserSession, principal.session_id_hash)
    if row is not None:
        row.totp_verified = True
        db.flush()


# ── audit ─────────────────────────────────────────────────────────────────────
_GENESIS = "0" * 64


def _audit_fields(row: AuditLog) -> bytes:
    at = _aware(row.at)
    return canonical_json({
        "at": at.isoformat() if at else None, "household_id": row.household_id,
        "actor_type": row.actor_type, "actor_id": row.actor_id, "event": row.event,
        "target_type": row.target_type, "target_id": row.target_id,
        "ip_hash": row.ip_hash, "detail": row.detail or {}})


def audit(db: Session, event: str, *, actor_type: str = "system",
          actor_id: str | None = None, household_id: str | None = None,
          target_type: str | None = None, target_id: str | None = None,
          ip: str | None = None, detail: dict | None = None) -> AuditLog:
    """Append one audit row (docs/7 §13 event names) in the caller's
    transaction. ``hash = HMAC(secret, prev_hash || canonical(row))``."""
    last = db.query(AuditLog).order_by(AuditLog.id.desc()).first()
    # Stored and hashed as JSON values (a datetime becomes its string), so the
    # chain verifies from what is read back (protocol.canonical_json).
    detail = json.loads(json.dumps(detail or {}, default=str))
    row = AuditLog(at=utcnow(), household_id=household_id, actor_type=actor_type,
                   actor_id=actor_id, event=event, target_type=target_type,
                   target_id=target_id, ip_hash=ip_digest(ip), detail=detail,
                   prev_hash=last.hash if last else _GENESIS)
    row.hash = hmac.new(_secret(), row.prev_hash.encode() + _audit_fields(row),
                        hashlib.sha256).hexdigest()
    db.add(row)
    db.flush()
    return row


#: The signed row the retention worker appends before it drops the oldest
#: prefix of the chain (``services.retention``): it names the hash the first
#: remaining row links to.
AUDIT_PRUNED_EVENT = "retention.audit_pruned"


def prune_audit_before(db: Session, cutoff: datetime) -> int:
    """Drop the audit rows older than ``cutoff`` (a contiguous prefix in id
    order), after appending a signed boundary row, so the chain still
    verifies and reports the cut as a retention gap. Returns rows dropped."""
    last_old = (db.query(AuditLog).filter(AuditLog.at < cutoff)
                .order_by(AuditLog.id.desc()).first())
    if last_old is None:
        return 0
    # The boundary row is appended first, so the chain's tip is always kept.
    count = db.query(AuditLog).filter(AuditLog.id <= last_old.id).count()
    audit(db, AUDIT_PRUNED_EVENT, detail={
        "upto_id": last_old.id, "boundary_hash": last_old.hash, "deleted": count,
        "before": cutoff.isoformat()})
    db.query(AuditLog).filter(AuditLog.id <= last_old.id).delete(
        synchronize_session=False)
    db.flush()
    return count


def verify_audit_chain(db: Session) -> dict[str, Any]:
    """``{"ok", "entries", "problems"}`` (+ ``"retention_gaps"`` when the
    retention worker dropped an old prefix behind a signed boundary row)."""
    problems, prev, count, gaps = [], _GENESIS, 0, []
    rows = db.query(AuditLog).order_by(AuditLog.id).all()
    if rows and rows[0].prev_hash != _GENESIS:
        boundaries = {(r.detail or {}).get("boundary_hash"): r for r in rows
                      if r.event == AUDIT_PRUNED_EVENT}
        boundary = boundaries.get(rows[0].prev_hash)
        if boundary is not None:
            prev = rows[0].prev_hash
            gaps.append({"before_id": rows[0].id,
                         "deleted": (boundary.detail or {}).get("deleted"),
                         "recorded_by": boundary.id})
    for row in rows:
        count += 1
        expected = hmac.new(_secret(), prev.encode() + _audit_fields(row),
                            hashlib.sha256).hexdigest()
        if row.prev_hash != prev or row.hash != expected:
            problems.append(row.id)
        prev = row.hash
    result: dict[str, Any] = {"ok": not problems, "entries": count, "problems": problems}
    if gaps:
        result["retention_gaps"] = gaps
    return result


__all__ = ["hash_password", "verify_password", "needs_rehash", "hash_pin",
           "verify_pin", "new_token", "token_digest", "new_code", "normalize_code",
           "code_digest", "ip_digest", "client_ip", "safe_next", "seal", "unseal",
           "Principal", "create_session", "load_session", "revoke_session",
           "revoke_user_sessions", "principal_of", "set_session_cookie",
           "clear_session_cookie", "issue_login_csrf", "verify_login_csrf",
           "login_csrf_token", "set_login_csrf",
           "supplied_csrf", "verify_csrf", "require_role", "require_signed_in",
           "mark_step_up", "mark_totp_verified", "TOTP_ROLES", "STATE_CHANGING",
           "audit", "verify_audit_chain", "prune_audit_before", "AUDIT_PRUNED_EVENT",
           "CODE_ALPHABET", "backoff_remaining", "record_attempt", "attempt_key",
           "ACTIVATE_KEY", "STEP_UP_KEY", "TOTP_KEY", "PHONE_KEY", "new_totp_secret",
           "totp_uri",
           "verify_totp", "new_recovery_codes", "consume_recovery_code",
           "RECOVERY_CODE_COUNT"]
