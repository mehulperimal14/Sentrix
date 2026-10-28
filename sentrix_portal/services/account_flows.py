"""Sign-in and account flows: the rules behind ``routes/auth.py`` (HTML) and
``routes/api_v2/auth.py`` (JSON), so both apply the same back-off, audit
events, messages and step-up rules (D14, docs/7 §10).

Each flow takes the request's facts (``ip``, ``user_agent``), the signed-in
:class:`security.Principal` where there is one, and the submitted values
(strings, as a form posts them), and returns an :class:`Outcome`. The CSRF
check stays with the caller (form field or header, before anything else).

Owner: track T-ACCT.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from sentrix_portal import security
from sentrix_portal.models import AuditLog, Household, User, UserSession, utcnow
from sentrix_portal.services import accounts
from sentrix_portal.services.outcome import Outcome, done, failed, invalid

#: Roles that use the household pages.
HOUSEHOLD_ROLES = ("owner", "member")
#: A staff action this recent, then a customer sign-in from a new address,
#: raises ``security.admin_then_new_ip`` (docs/7 §10).
ADMIN_THEN_NEW_IP_WINDOW = timedelta(days=7)

PHONE_SMS = ("SENTRIX code {code}: it confirms this number for SOS callbacks. Valid 10 "
             "minutes. SENTRIX staff never ask for it.")
#: What ``/account?saved=<key>`` says after a mobile-number action (the page's
#: own wording, ``auth/account.html``) and the JSON API's error code for it.
PHONE_RESULTS = {
    "phone_sent": ("ok", "Your device is sending the code by SMS. Enter it below within "
                         "10 minutes."),
    "phone_ok": ("ok", "Mobile number confirmed."),
    "phone_failed": ("device_offline", "The device could not send the SMS (offline, no "
                                       "modem, or test mode refused it). Try again later."),
    "phone_bad": ("validation", "That code is not right or has expired."),
    "phone_wait": ("rate_limited", "Too many codes or attempts; wait a while."),
    "phone_no_device": ("conflict", "Claim your device first: it sends the code from its "
                                    "own SIM."),
    "phone_missing": ("conflict", "Add your mobile number first (setup step 1)."),
}
STEP_UP_FAILED = "That password was not right."
STEP_UP_WAIT = "Too many attempts; wait a minute."
LINK_NOT_VALID = ("This activation link was already used or has expired. Ask SENTRIX to send "
                  "a new one.")
CODE_NOT_VALID = ("That username and code do not match a valid activation code (codes work "
                  "once and expire after 72 hours).")
EMAIL_LINK_NOT_VALID = "This confirmation link was already used or has expired."
EMAIL_CONFIRMED = ("Thank you. SENTRIX will use this address for alerts and account "
                   "recovery.")


def landing_for(role: str, household: Household | None = None) -> str:
    """Where a role starts after signing in."""
    if role == "company_admin":
        return "/admin"
    if role == "operator":
        return "/desk"
    if role == "owner" and household is not None and household.onboarding_completed_at is None:
        return "/onboarding"
    return "/home"


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _household(db: Session, user: User) -> Household | None:
    return db.get(Household, user.household_id) if user.household_id else None


def _sign_in(db: Session, user: User, *, ip: str | None,
             user_agent: str | None) -> tuple[str, UserSession]:
    raw, row = security.create_session(db, user, ip=ip, user_agent=user_agent)
    user.last_login_at = utcnow()
    user.last_login_ip_hash = security.ip_digest(ip)
    return raw, row


# ── sign-in ───────────────────────────────────────────────────────────────────
def admin_then_new_ip(db: Session, user: User, previous_ip_hash: str | None,
                      ip: str | None) -> None:
    """A staff action on the account, then a sign-in from an address the
    customer never used: tell the customer and the audit log."""
    current = security.ip_digest(ip)
    if (user.role not in HOUSEHOLD_ROLES or not user.household_id or not previous_ip_hash
            or previous_ip_hash == current):
        return
    since = utcnow() - ADMIN_THEN_NEW_IP_WINDOW
    staff_action = (db.query(AuditLog)
                    .filter(AuditLog.household_id == user.household_id,
                            AuditLog.actor_type == "admin", AuditLog.at >= since)
                    .order_by(AuditLog.id.desc()).first())
    if staff_action is None:
        return
    security.audit(db, "security.admin_then_new_ip", actor_type="system",
                   household_id=user.household_id, target_type="user", target_id=user.id,
                   ip=ip, detail={"admin_event": staff_action.event,
                                  "admin_event_id": staff_action.id})
    accounts.email_user(
        db, user, template="account.security", step=f"newip:{staff_action.id}:{current}",
        subject="SENTRIX: new sign-in to your account",
        text=("Your SENTRIX account was signed in to from a new address shortly after "
              "SENTRIX staff changed something on it. If this was not you, change your "
              "password and sign out every session under Account > Sessions."))


async def login(db: Session, *, username: Any, password: Any, ip: str | None,
                user_agent: str | None) -> Outcome:
    """Username + password (the caller checked the login CSRF token). On
    success ``data`` holds ``raw`` (the cookie value), ``user``, ``session``
    and ``household``."""
    username = security.attempt_key(_text(username))
    wait = security.backoff_remaining(db, username, ip)
    if wait:
        # Not checked, not recorded, not audited: a blocked flood writes nothing.
        return failed("rate_limited", f"Too many attempts. Wait {wait} s and try again.",
                      status=429, retry_after=wait, username=username)
    user = db.query(User).filter_by(username=username).first() if username else None
    password = _text(password)
    ok = await run_in_threadpool(security.verify_password,
                                 user.password_hash if user else None, password)
    if not ok or user.status != "active":
        security.record_attempt(db, username, ip, False)
        security.audit(db, "auth.login_failed", actor_type="user",
                       actor_id=user.id if user else None,
                       household_id=user.household_id if user else None, ip=ip,
                       detail={"reason": "credentials" if not ok else user.status})
        if ok:
            return failed("forbidden", "This account is disabled. Contact SENTRIX.",
                          status=401, username=username)
        return failed("not_signed_in", "Wrong username or password.", status=401,
                      username=username)
    security.record_attempt(db, username, ip, True)
    if security.needs_rehash(user.password_hash):
        user.password_hash = await run_in_threadpool(security.hash_password, password)
    previous_ip_hash = user.last_login_ip_hash
    raw, row = _sign_in(db, user, ip=ip, user_agent=user_agent)
    security.audit(db, "auth.login", actor_type="user", actor_id=user.id,
                   household_id=user.household_id, target_type="user", target_id=user.id,
                   ip=ip)
    admin_then_new_ip(db, user, previous_ip_hash, ip)
    return done(raw=raw, user=user, session=row, household=_household(db, user))


def logout(db: Session, principal: security.Principal, *, ip: str | None) -> None:
    security.revoke_session(db, principal.session_id_hash, is_hash=True)
    security.audit(db, "auth.logout", actor_type="user", actor_id=principal.user_id,
                   household_id=principal.household_id, ip=ip)


# ── activation (set your own password; D14) ───────────────────────────────────
async def activate(db: Session, *, code_in_path: str | None, username: Any, code: Any,
                   password: Any, password_confirm: Any, ip: str | None,
                   user_agent: str | None) -> Outcome:
    """A typed (username, code) or the code of an ``/activate/<code>`` link,
    and the new password twice. On success the user is signed in (``data``
    as :func:`login`). ``data["known_user"]`` is the link's user on a
    password error (the page keeps showing their username)."""
    typed_username = security.attempt_key(_text(username))
    key = security.ACTIVATE_KEY + (typed_username if code_in_path is None else "link")
    wait = security.backoff_remaining(db, key, ip)
    if wait:
        return failed("rate_limited", f"Too many attempts. Wait {wait} s and try again.",
                      status=429, retry_after=wait, username=typed_username)
    if code_in_path is not None:
        found = accounts.activation_for_link(db, code_in_path)
    else:
        found = accounts.activation_for_typed(db, typed_username, _text(code))
    if found is None:
        security.record_attempt(db, key, ip, False)
        security.audit(db, "auth.activation_failed", ip=ip,
                       detail={"via": "link" if code_in_path else "code"})
        return failed("validation", CODE_NOT_VALID, status=400,
                      errors={"code": CODE_NOT_VALID}, username=typed_username,
                      link_failed=code_in_path is not None)
    row, user = found
    password = _text(password)
    errors = accounts.FormErrors()
    problem = accounts.password_problem(password, username=user.username)
    if problem:
        errors.add("password", problem)
    elif password != password_confirm:
        errors.add("password_confirm", "The two passwords do not match.")
    if errors:
        return invalid(errors, username=user.username,
                       known_user=user if code_in_path else None)
    password_hash = await run_in_threadpool(security.hash_password, password)
    accounts.complete_activation(db, row, user, password_hash, ip=ip)
    security.record_attempt(db, key, ip, True)
    if user.email and not user.email_verified_at:
        accounts.send_email_verification(db, user)
    raw, session_row = _sign_in(db, user, ip=ip, user_agent=user_agent)
    return done(raw=raw, user=user, session=session_row, household=_household(db, user))


# ── account ───────────────────────────────────────────────────────────────────
async def update_account(db: Session, principal: security.Principal, *, display_name: Any,
                         email: Any, ip: str | None) -> Outcome:
    """Display name and e-mail. A new e-mail (the recovery channel) needs a
    fresh password and is verified again."""
    user = db.get(User, principal.user_id)
    errors = accounts.FormErrors()
    name = accounts.clean_text(errors, "display_name", display_name, label="Name",
                               max_len=80, required=True, min_len=2)
    email = accounts.clean_email(errors, "email", email, required=user.role != "member")
    changed_email = email != user.email
    if changed_email and not principal.stepped_up():
        errors.add("email", "Confirm your password below before changing the e-mail.")
    if errors:
        return invalid(errors, step_up_field="email" if changed_email
                       and not principal.stepped_up() else None)
    user.display_name = name
    if changed_email:
        user.email, user.email_verified_at = email, None
        accounts.revoke_tokens(db, purpose="email_verify", subject_type="user",
                               subject_id=user.id)
        accounts.send_email_verification(db, user)
    security.audit(db, "account.updated", actor_type="user", actor_id=user.id,
                   household_id=user.household_id, target_type="user", target_id=user.id,
                   ip=ip, detail={"email_changed": changed_email})
    if user.role == "owner" and user.household_id:
        accounts.profile_changed(db, user.household_id, actor_id=user.id, what="owner", ip=ip)
        db.commit()
        await accounts.push_profile(db, user.household_id)
    return done("Saved.")


def _phone(key: str, *, retry_after: int = 0) -> Outcome:
    code, message = PHONE_RESULTS[key]
    if code == "ok":
        return done(message, saved=key)
    return failed(code, message, status=303, retry_after=retry_after, saved=key,
                  errors={"code": message} if code == "validation" else None)


async def send_phone_code(db: Session, principal: security.Principal, *,
                          ip: str | None) -> Outcome:
    """Confirm the owner's mobile with a code sent by SMS from the household's
    own device (``sms.send``: the device only texts numbers in its profile,
    through its send guard)."""
    from sentrix.actor import Actor
    from sentrix_portal.services import hub
    user = db.get(User, principal.user_id)
    phone = accounts.user_phone(user)
    if not phone:
        return _phone("phone_missing")
    devices = accounts.claimed_devices(db, user.household_id)
    if not devices:
        return _phone("phone_no_device")
    wait = accounts.phone_code_wait(db, user)
    if wait:
        return _phone("phone_wait", retry_after=wait)
    code, _ = accounts.mint_phone_code(db, user)
    db.commit()
    stepped = principal.step_up_at.timestamp() if principal.step_up_at else None
    actor = Actor("owner", id=user.id, name=principal.display_name, step_up_at=stepped)
    result = await hub.send_command(devices[0].id, "sms.send",
                                    {"to": phone, "text": PHONE_SMS.format(code=code),
                                     "request_id": None},
                                    actor.to_dict(), queue_if_offline=False)
    security.audit(db, "account.phone_code_sent", actor_type="user", actor_id=user.id,
                   household_id=user.household_id, target_type="device",
                   target_id=devices[0].id, ip=ip,
                   detail={"ok": bool(result.get("ok")), "code": result.get("code")})
    return _phone("phone_sent" if result.get("ok") else "phone_failed")


async def confirm_phone_code(db: Session, principal: security.Principal, *, code: Any,
                             ip: str | None) -> Outcome:
    user = db.get(User, principal.user_id)
    if not accounts.user_phone(user):
        return _phone("phone_missing")
    key = security.PHONE_KEY + user.id
    wait = security.backoff_remaining(db, key, ip)
    if wait:
        return _phone("phone_wait", retry_after=wait)
    if not accounts.confirm_phone_code(db, user, _text(code) or None):
        security.record_attempt(db, key, ip, False)
        return _phone("phone_bad")
    security.record_attempt(db, key, ip, True)
    security.audit(db, "account.phone_verified", actor_type="user", actor_id=user.id,
                   household_id=user.household_id, target_type="user", target_id=user.id,
                   ip=ip)
    accounts.profile_changed(db, user.household_id, actor_id=user.id, what="owner_phone",
                             ip=ip)
    db.commit()
    await accounts.push_profile(db, user.household_id)
    return _phone("phone_ok")


def resend_email_verification(db: Session, principal: security.Principal) -> Outcome:
    user = db.get(User, principal.user_id)
    if user.email and not user.email_verified_at:
        accounts.send_email_verification(db, user)
    return done("A new confirmation link was sent.")


def email_link_valid(db: Session, token: str) -> bool:
    return accounts.live_token(db, token, "email_verify") is not None


def confirm_email(db: Session, token: str, *, ip: str | None) -> Outcome:
    row = accounts.live_token(db, token, "email_verify")
    user = db.get(User, row.subject_id) if row else None
    if row is None or user is None or not user.email:
        return failed("not_found", EMAIL_LINK_NOT_VALID, status=404)
    row.used_at = user.email_verified_at = utcnow()
    security.audit(db, "auth.email_verified", actor_type="user", actor_id=user.id,
                   household_id=user.household_id, target_type="user", target_id=user.id,
                   ip=ip)
    return done(EMAIL_CONFIRMED)


async def change_password(db: Session, principal: security.Principal, *, current: Any,
                          password: Any, password_confirm: Any, ip: str | None) -> Outcome:
    user = db.get(User, principal.user_id)
    key = security.STEP_UP_KEY + user.id
    errors = accounts.FormErrors()
    wait = security.backoff_remaining(db, key, ip)
    if wait:
        errors.add("current_password", f"Too many attempts. Wait {wait} s.")
    elif not await run_in_threadpool(security.verify_password, user.password_hash,
                                     _text(current)):
        security.record_attempt(db, key, ip, False)
        errors.add("current_password", "That is not your current password.")
    new = _text(password)
    if not errors:
        problem = accounts.password_problem(new, username=user.username)
        if problem:
            errors.add("password", problem)
        elif new != password_confirm:
            errors.add("password_confirm", "The two passwords do not match.")
    if errors:
        if wait:
            return failed("rate_limited", errors["current_password"], status=429,
                          errors=errors, retry_after=wait)
        return invalid(errors)
    security.record_attempt(db, key, ip, True)
    user.password_hash = await run_in_threadpool(security.hash_password, new)
    user.password_changed_at = utcnow()
    security.revoke_user_sessions(db, user.id, keep_hash=principal.session_id_hash)
    security.mark_step_up(db, principal)
    security.audit(db, "auth.password_changed", actor_type="user", actor_id=user.id,
                   household_id=user.household_id, target_type="user", target_id=user.id,
                   ip=ip)
    accounts.email_user(db, user, template="account.security",
                        step=f"password:{int(utcnow().timestamp())}",
                        subject="SENTRIX: your password was changed",
                        text="Your SENTRIX password was changed and your other sessions "
                             "were signed out. If this was not you, contact SENTRIX.")
    return done("Password changed. Your other sessions were signed out.")


async def step_up(db: Session, principal: security.Principal, *, password: Any,
                  ip: str | None) -> Outcome:
    """Re-enter the password: sensitive actions for ``step_up_seconds``.
    ``data["stepup"]`` is the page's ``?stepup=`` value (failed | wait)."""
    user = db.get(User, principal.user_id)
    key = security.STEP_UP_KEY + user.id
    wait = security.backoff_remaining(db, key, ip)
    if wait:
        return failed("rate_limited", STEP_UP_WAIT, status=303, retry_after=wait,
                      stepup="wait")
    if not await run_in_threadpool(security.verify_password, user.password_hash,
                                   _text(password)):
        security.record_attempt(db, key, ip, False)
        security.audit(db, "auth.step_up_failed", actor_type="user", actor_id=user.id,
                       household_id=user.household_id, ip=ip)
        return failed("validation", STEP_UP_FAILED, status=303,
                      errors={"password": STEP_UP_FAILED}, stepup="failed")
    security.record_attempt(db, key, ip, True)
    security.mark_step_up(db, principal)
    return done()


# ── TOTP (company_admin, operator) ────────────────────────────────────────────
def twofa_state(db: Session, principal: security.Principal, user: User, *,
                show_setup: bool = True) -> dict[str, Any]:
    """What the two-factor page shows. While a staff account has no
    authenticator yet, a setup key exists (made here once, sealed) and is
    shown with its ``otpauth://`` URI until TOTP is on."""
    state: dict[str, Any] = {
        "required": user.role in security.TOTP_ROLES, "enabled": bool(user.totp_enabled),
        "verified": principal.totp_verified, "stepped_up": principal.stepped_up(),
        "recovery_codes_left": len(user.recovery_codes or []), "setup": None}
    if show_setup and not user.totp_enabled and user.role in security.TOTP_ROLES:
        secret = security.unseal(user.totp_secret_enc) if user.totp_secret_enc else None
        if not secret:
            secret = security.new_totp_secret()
            user.totp_secret_enc = security.seal(secret)
            db.flush()
        state["setup"] = {
            "key": " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
            "uri": security.totp_uri(secret, user.username)}
    return state


def twofa_applies(user: User) -> bool:
    return user.role in security.TOTP_ROLES or bool(user.totp_enabled)


def twofa_wait(db: Session, user: User, ip: str | None) -> Outcome | None:
    wait = security.backoff_remaining(db, security.TOTP_KEY + user.id, ip)
    if wait:
        return failed("rate_limited", f"Too many attempts. Wait {wait} s.", status=429,
                      retry_after=wait)
    return None


def twofa_verify(db: Session, principal: security.Principal, user: User, *, code: Any,
                 ip: str | None) -> Outcome:
    """The every-session check: a TOTP code or a single-use recovery code."""
    key = security.TOTP_KEY + user.id
    code = _text(code)
    secret = security.unseal(user.totp_secret_enc)
    if security.verify_totp(secret, code, user_id=user.id):
        used = None
    elif security.consume_recovery_code(user, code):
        used = "recovery_code"
    else:
        security.record_attempt(db, key, ip, False)
        security.audit(db, "auth.totp_failed", actor_type="user", actor_id=user.id, ip=ip)
        return failed("validation", "That code is not right.", status=400,
                      errors={"code": "That code is not right."})
    security.record_attempt(db, key, ip, True)
    security.mark_totp_verified(db, principal)
    if used:
        security.audit(db, "auth.recovery_code_used", actor_type="user", actor_id=user.id,
                       ip=ip, detail={"remaining": len(user.recovery_codes or [])})
    return done()


def twofa_setup(db: Session, principal: security.Principal, user: User, *, code: Any,
                ip: str | None) -> Outcome:
    """Turn TOTP on with a first code; ``data["codes"]``: the recovery codes,
    shown once."""
    key = security.TOTP_KEY + user.id
    secret = security.unseal(user.totp_secret_enc) if user.totp_secret_enc else None
    if not security.verify_totp(secret, _text(code) or None, user_id=user.id):
        security.record_attempt(db, key, ip, False)
        message = ("That code is not right. Check the time on your phone and try the next "
                   "code.")
        return failed("validation", message, status=400, errors={"code": message})
    security.record_attempt(db, key, ip, True)
    codes, digests = security.new_recovery_codes()
    user.totp_enabled, user.recovery_codes = True, digests
    security.mark_totp_verified(db, principal)
    security.audit(db, "auth.totp_enabled", actor_type="user", actor_id=user.id, ip=ip)
    return done(codes=codes)


def twofa_restart(user: User) -> None:
    """Start the setup again with a new key."""
    user.totp_secret_enc = None


def twofa_regenerate(db: Session, principal: security.Principal, user: User, *,
                     ip: str | None) -> Outcome:
    """New recovery codes (the old ones stop working); needs the password."""
    if not principal.stepped_up():
        return failed("step_up_required", "Confirm your password first.", status=403)
    codes, digests = security.new_recovery_codes()
    user.recovery_codes = digests
    security.audit(db, "auth.recovery_codes_regenerated", actor_type="user",
                   actor_id=user.id, ip=ip)
    return done(codes=codes)


# ── sessions ──────────────────────────────────────────────────────────────────
def live_sessions(db: Session, principal: security.Principal) -> list[UserSession]:
    now = utcnow()
    rows = (db.query(UserSession).filter_by(user_id=principal.user_id, revoked_at=None)
            .order_by(UserSession.last_seen_at.desc()).all())
    return [r for r in rows if accounts.as_utc(r.expires_at) > now]


def revoke_sessions(db: Session, principal: security.Principal, target: Any, *,
                    ip: str | None) -> tuple[int, bool]:
    """``target``: a session's id hash (only the user's own) or ``others``.
    Returns (sessions signed out, whether this session was one of them)."""
    count = 0
    if target == "others":
        count = security.revoke_user_sessions(db, principal.user_id,
                                              keep_hash=principal.session_id_hash)
    elif isinstance(target, str):
        row = db.get(UserSession, target)
        if row is not None and row.user_id == principal.user_id:   # only your own
            count = int(security.revoke_session(db, target, is_hash=True))
    if count:
        security.audit(db, "auth.session_revoked", actor_type="user",
                       actor_id=principal.user_id, household_id=principal.household_id,
                       ip=ip, detail={"count": count})
    return count, bool(count and target == principal.session_id_hash)


__all__ = ["HOUSEHOLD_ROLES", "PHONE_SMS", "PHONE_RESULTS", "landing_for", "login", "logout",
           "activate", "admin_then_new_ip", "update_account", "send_phone_code",
           "confirm_phone_code", "resend_email_verification", "email_link_valid",
           "confirm_email", "change_password", "step_up", "twofa_state", "twofa_applies",
           "twofa_wait", "twofa_verify", "twofa_setup", "twofa_restart", "twofa_regenerate",
           "live_sessions", "revoke_sessions", "LINK_NOT_VALID", "CODE_NOT_VALID",
           "EMAIL_LINK_NOT_VALID", "EMAIL_CONFIRMED", "STEP_UP_FAILED", "STEP_UP_WAIT"]
