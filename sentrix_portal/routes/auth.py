"""Sign-in and account: GET/POST /login, POST /logout, GET/POST /activate and
/activate/{code}, /account (profile, e-mail), /account/password,
/account/step-up, /account/2fa (TOTP + recovery codes; required for
company_admin and operator), /account/sessions (revoke), GET/POST
/account/verify-email/{token}.

Owner: track T-ACCT. Rules (D14, docs/7 §10):

* back-off per (username, IP) and per address, never a hard lockout (the
  owner's phone on another address is not affected and every wait ends);
* the company never sets or sees a password: accounts start
  ``pending_activation`` and the customer chooses the password with a
  single-use activation code (72 h, hashed, rate limited);
* staff (company_admin, operator) pass TOTP in every session before any
  other page (``security.require_role`` sends them here);
* every POST carries a CSRF token: the session's, or before sign-in the
  ``portal_csrf`` double-submit cookie;
* a customer sign-in from a new address soon after a staff action on the
  account raises ``security.admin_then_new_ip`` (audit + e-mail).

The page helpers at the top (:func:`render`, :func:`see_other`, ...) are
shared by the track's other routers.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from sentrix.utils.redact import mask_phone
from sentrix_portal import security
from sentrix_portal.db import DB
from sentrix_portal.models import AuditLog, Household, User, UserSession, utcnow
from sentrix_portal.services import accounts

router = APIRouter()

#: Roles that use the household pages.
HOUSEHOLD_ROLES = ("owner", "member")
#: A staff action this recent, then a customer sign-in from a new address,
#: raises ``security.admin_then_new_ip`` (docs/7 §10).
ADMIN_THEN_NEW_IP_WINDOW = timedelta(days=7)


# ── page helpers (shared by the T-ACCT routers) ───────────────────────────────
def render(request: Request, name: str, context: dict[str, Any] | None = None, *,
           status_code: int = 200, active_page: str | None = None,
           headers: dict[str, str] | None = None) -> HTMLResponse:
    """A page extending ``base.html``: ``principal`` and ``active_page`` are
    always in the context (the test-mode banner and nav depend on them)."""
    values = {"principal": getattr(request.state, "principal", None),
              "active_page": active_page}
    values.update(context or {})
    return request.app.state.templates.TemplateResponse(
        request, name, values, status_code=status_code, headers=headers)


def see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def message_page(request: Request, title: str, text: str, *, status_code: int = 200,
                 link: tuple[str, str] | None = None,
                 active_page: str | None = None) -> HTMLResponse:
    return render(request, "auth/message.html", {"title": title, "text": text, "link": link},
                  status_code=status_code, active_page=active_page)


def landing_for(role: str, household: Household | None = None) -> str:
    """Where a role starts after signing in."""
    if role == "company_admin":
        return "/admin"
    if role == "operator":
        return "/desk"
    if role == "owner" and household is not None and household.onboarding_completed_at is None:
        return "/onboarding"
    return "/home"


def user_of(db: Session, principal: security.Principal) -> User:
    return db.get(User, principal.user_id)


def household_of(db: Session, principal: security.Principal) -> Household | None:
    return db.get(Household, principal.household_id) if principal.household_id else None


qr_svg = accounts.qr_svg


def _login_form(request: Request, *, status_code: int = 200, error: str | None = None,
                username: str = "", next_url: str = "") -> HTMLResponse:
    token = security.login_csrf_token(request)
    response = render(request, "auth/login.html",
                      {"csrf_token": token, "error": error, "username": username,
                       "next": next_url}, status_code=status_code)
    security.set_login_csrf(response, token)
    return response


# ── sign-in ───────────────────────────────────────────────────────────────────
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "", db: Session = DB):
    principal = security.principal_of(request, db)
    if principal is not None:
        request.state.principal = principal
        return see_other(security.safe_next(next, landing_for(
            principal.role, household_of(db, principal))))
    return _login_form(request, next_url=security.safe_next(next, ""))


def _admin_then_new_ip(db: Session, user: User, previous_ip_hash: str | None,
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


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, db: Session = DB):
    form = await request.form()
    username = security.attempt_key(form.get("username"))
    next_url = security.safe_next(form.get("next"), "")
    if not security.verify_login_csrf(request, form.get("csrf_token")):
        return _login_form(request, status_code=403, username=username, next_url=next_url,
                           error="The form expired. Please sign in again.")
    ip = security.client_ip(request)
    wait = security.backoff_remaining(db, username, ip)
    if wait:
        # Not checked, not recorded, not audited: a blocked flood writes nothing.
        return _login_form(request, status_code=429, username=username, next_url=next_url,
                           error=f"Too many attempts. Wait {wait} s and try again.")
    user = db.query(User).filter_by(username=username).first() if username else None
    password = form.get("password") if isinstance(form.get("password"), str) else ""
    ok = await run_in_threadpool(security.verify_password,
                                 user.password_hash if user else None, password)
    if not ok or user.status != "active":
        security.record_attempt(db, username, ip, False)
        security.audit(db, "auth.login_failed", actor_type="user",
                       actor_id=user.id if user else None,
                       household_id=user.household_id if user else None, ip=ip,
                       detail={"reason": "credentials" if not ok else user.status})
        error = ("This account is disabled. Contact SENTRIX." if ok
                 else "Wrong username or password.")
        return _login_form(request, status_code=401, username=username, next_url=next_url,
                           error=error)
    security.record_attempt(db, username, ip, True)
    if security.needs_rehash(user.password_hash):
        user.password_hash = await run_in_threadpool(security.hash_password, password)
    previous_ip_hash = user.last_login_ip_hash
    raw, _ = security.create_session(db, user, ip=ip,
                                     user_agent=request.headers.get("user-agent"))
    user.last_login_at = utcnow()
    user.last_login_ip_hash = security.ip_digest(ip)
    security.audit(db, "auth.login", actor_type="user", actor_id=user.id,
                   household_id=user.household_id, target_type="user", target_id=user.id,
                   ip=ip)
    _admin_then_new_ip(db, user, previous_ip_hash, ip)
    household = db.get(Household, user.household_id) if user.household_id else None
    if user.role in security.TOTP_ROLES:
        target = "/account/2fa" + (f"?next={quote(next_url, safe='/')}" if next_url else "")
    else:
        target = next_url or landing_for(user.role, household)
    response = see_other(target)
    security.set_session_cookie(response, raw)
    return response


@router.post("/logout")
async def logout(request: Request, db: Session = DB):
    """POST + CSRF. A stale session just goes back to the sign-in page."""
    principal = security.principal_of(request, db)
    response = see_other("/login")
    if principal is not None:
        supplied = await security.supplied_csrf(request)
        if not security.verify_csrf(principal, supplied):
            return Response("csrf", status_code=403)
        security.revoke_session(db, principal.session_id_hash, is_hash=True)
        security.audit(db, "auth.logout", actor_type="user", actor_id=principal.user_id,
                       household_id=principal.household_id, ip=security.client_ip(request))
    security.clear_session_cookie(response)
    return response


# ── activation (set your own password; D14) ───────────────────────────────────
def _activation_form(request: Request, *, code_in_path: str | None = None,
                     username: str = "", error: str | None = None,
                     errors: dict | None = None, status_code: int = 200,
                     known_user: User | None = None) -> HTMLResponse:
    token = security.login_csrf_token(request)
    response = render(request, "auth/activate.html",
                      {"csrf_token": token, "code_in_path": code_in_path,
                       "username": username, "error": error, "errors": errors or {},
                       "known_user": known_user,
                       "min_length": accounts.PASSWORD_MIN_LENGTH},
                      status_code=status_code)
    security.set_login_csrf(response, token)
    return response


@router.get("/activate", response_class=HTMLResponse)
def activate_page(request: Request, u: str = ""):
    return _activation_form(request, username=security.attempt_key(u))


@router.get("/activate/{code}", response_class=HTMLResponse)
def activate_link_page(request: Request, code: str, db: Session = DB):
    found = accounts.activation_for_link(db, code)
    if found is None:
        return message_page(request, "Link not valid",
                            "This activation link was already used or has expired. Ask "
                            "SENTRIX to send a new one.", status_code=404,
                            link=("/login", "Sign in"))
    return _activation_form(request, code_in_path=code, known_user=found[1])


async def _activate(request: Request, db: Session, code_in_path: str | None):
    form = await request.form()
    ip = security.client_ip(request)
    typed_username = security.attempt_key(form.get("username"))
    if not security.verify_login_csrf(request, form.get("csrf_token")):
        return _activation_form(request, code_in_path=code_in_path, username=typed_username,
                                error="The form expired. Please try again.", status_code=403)
    key = security.ACTIVATE_KEY + (typed_username if code_in_path is None else "link")
    wait = security.backoff_remaining(db, key, ip)
    if wait:
        return _activation_form(request, code_in_path=code_in_path, username=typed_username,
                                error=f"Too many attempts. Wait {wait} s and try again.",
                                status_code=429)
    if code_in_path is not None:
        found = accounts.activation_for_link(db, code_in_path)
    else:
        found = accounts.activation_for_typed(db, typed_username, form.get("code") or "")
    if found is None:
        security.record_attempt(db, key, ip, False)
        security.audit(db, "auth.activation_failed", ip=ip,
                       detail={"via": "link" if code_in_path else "code"})
        return _activation_form(request, code_in_path=None, username=typed_username,
                                error="That username and code do not match a valid "
                                      "activation code (codes work once and expire after "
                                      "72 hours).", status_code=400)
    row, user = found
    password = form.get("password") if isinstance(form.get("password"), str) else ""
    errors = accounts.FormErrors()
    problem = accounts.password_problem(password, username=user.username)
    if problem:
        errors.add("password", problem)
    elif password != form.get("password_confirm"):
        errors.add("password_confirm", "The two passwords do not match.")
    if errors:
        return _activation_form(request, code_in_path=code_in_path, username=user.username,
                                errors=errors, status_code=400,
                                known_user=user if code_in_path else None)
    password_hash = await run_in_threadpool(security.hash_password, password)
    accounts.complete_activation(db, row, user, password_hash, ip=ip)
    security.record_attempt(db, key, ip, True)
    if user.email and not user.email_verified_at:
        accounts.send_email_verification(db, user)
    raw, _ = security.create_session(db, user, ip=ip,
                                     user_agent=request.headers.get("user-agent"))
    user.last_login_at = utcnow()
    user.last_login_ip_hash = security.ip_digest(ip)
    household = db.get(Household, user.household_id) if user.household_id else None
    target = ("/account/2fa" if user.role in security.TOTP_ROLES
              else landing_for(user.role, household))
    response = see_other(target)
    security.set_session_cookie(response, raw)
    return response


@router.post("/activate", response_class=HTMLResponse)
async def activate_submit(request: Request, db: Session = DB):
    return await _activate(request, db, None)


@router.post("/activate/{code}", response_class=HTMLResponse)
async def activate_link_submit(request: Request, code: str, db: Session = DB):
    return await _activate(request, db, code)


# ── account ───────────────────────────────────────────────────────────────────
def _account_page(request: Request, db: Session, principal, *, errors=None,
                  saved: str | None = None, status_code: int = 200) -> HTMLResponse:
    user = user_of(db, principal)
    return render(request, "auth/account.html",
                  {"user": user, "errors": errors or {}, "saved": saved,
                   "stepped_up": principal.stepped_up(),
                   "masked_phone": mask_phone(accounts.user_phone(user))},
                  status_code=status_code, active_page="account")


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request, saved: str = "", db: Session = DB,
                 principal=Depends(security.require_role())):
    return _account_page(request, db, principal, saved=saved or None)


@router.post("/account", response_class=HTMLResponse)
async def account_update(request: Request, db: Session = DB,
                         principal=Depends(security.require_role())):
    """Display name and e-mail. A new e-mail (the recovery channel) needs a
    fresh password and is verified again."""
    form = await request.form()
    user = user_of(db, principal)
    errors = accounts.FormErrors()
    name = accounts.clean_text(errors, "display_name", form.get("display_name"),
                               label="Name", max_len=80, required=True, min_len=2)
    email = accounts.clean_email(errors, "email", form.get("email"),
                                 required=user.role != "member")
    changed_email = email != user.email
    if changed_email and not principal.stepped_up():
        errors.add("email", "Confirm your password below before changing the e-mail.")
    if errors:
        return _account_page(request, db, principal, errors=errors, status_code=400)
    user.display_name = name
    if changed_email:
        user.email, user.email_verified_at = email, None
        accounts.revoke_tokens(db, purpose="email_verify", subject_type="user",
                               subject_id=user.id)
        accounts.send_email_verification(db, user)
    security.audit(db, "account.updated", actor_type="user", actor_id=user.id,
                   household_id=user.household_id, target_type="user", target_id=user.id,
                   ip=security.client_ip(request), detail={"email_changed": changed_email})
    if user.role == "owner" and user.household_id:
        accounts.profile_changed(db, user.household_id, actor_id=user.id, what="owner",
                                 ip=security.client_ip(request))
        db.commit()
        await accounts.push_profile(db, user.household_id)
    return see_other("/account?saved=1")


PHONE_SMS = ("SENTRIX code {code}: it confirms this number for SOS callbacks. Valid 10 "
             "minutes. SENTRIX staff never ask for it.")


@router.post("/account/phone")
async def phone_verify(request: Request, db: Session = DB,
                       principal=Depends(security.require_role("owner"))):
    """Confirm the owner's mobile with a code sent by SMS from the household's
    own device (``sms.send``: the device only texts numbers in its profile,
    through its send guard). ``action=send`` or ``action=confirm``."""
    from sentrix.actor import Actor
    from sentrix_portal.services import hub
    form = await request.form()
    user = user_of(db, principal)
    ip = security.client_ip(request)
    phone = accounts.user_phone(user)
    if not phone:
        return see_other("/account?saved=phone_missing")
    if form.get("action") == "send":
        devices = accounts.claimed_devices(db, user.household_id)
        if not devices:
            return see_other("/account?saved=phone_no_device")
        if accounts.phone_code_wait(db, user):
            return see_other("/account?saved=phone_wait")
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
        return see_other("/account?saved=" + ("phone_sent" if result.get("ok")
                                              else "phone_failed"))
    key = security.PHONE_KEY + user.id
    if security.backoff_remaining(db, key, ip):
        return see_other("/account?saved=phone_wait")
    if not accounts.confirm_phone_code(db, user, form.get("code")):
        security.record_attempt(db, key, ip, False)
        return see_other("/account?saved=phone_bad")
    security.record_attempt(db, key, ip, True)
    security.audit(db, "account.phone_verified", actor_type="user", actor_id=user.id,
                   household_id=user.household_id, target_type="user", target_id=user.id,
                   ip=ip)
    accounts.profile_changed(db, user.household_id, actor_id=user.id, what="owner_phone",
                             ip=ip)
    db.commit()
    await accounts.push_profile(db, user.household_id)
    return see_other("/account?saved=phone_ok")


@router.post("/account/verify-email", response_class=HTMLResponse)
def account_resend_verification(request: Request, db: Session = DB,
                                principal=Depends(security.require_role())):
    user = user_of(db, principal)
    if user.email and not user.email_verified_at:
        accounts.send_email_verification(db, user)
    return see_other("/account?saved=verify")


@router.get("/account/verify-email/{token}", response_class=HTMLResponse)
def verify_email_page(request: Request, token: str, db: Session = DB):
    row = accounts.live_token(db, token, "email_verify")
    if row is None:
        return message_page(request, "Link not valid",
                            "This confirmation link was already used or has expired.",
                            status_code=404)
    csrf = security.login_csrf_token(request)
    response = render(request, "auth/verify_email.html", {"csrf_token": csrf})
    security.set_login_csrf(response, csrf)
    return response


@router.post("/account/verify-email/{token}", response_class=HTMLResponse)
async def verify_email_submit(request: Request, token: str, db: Session = DB):
    form = await request.form()
    if not security.verify_login_csrf(request, form.get("csrf_token")):
        return message_page(request, "Form expired", "Open the link again.", status_code=403)
    row = accounts.live_token(db, token, "email_verify")
    user = db.get(User, row.subject_id) if row else None
    if row is None or user is None or not user.email:
        return message_page(request, "Link not valid",
                            "This confirmation link was already used or has expired.",
                            status_code=404)
    row.used_at = user.email_verified_at = utcnow()
    security.audit(db, "auth.email_verified", actor_type="user", actor_id=user.id,
                   household_id=user.household_id, target_type="user", target_id=user.id,
                   ip=security.client_ip(request))
    return message_page(request, "E-mail confirmed",
                        "Thank you. SENTRIX will use this address for alerts and account "
                        "recovery.", link=("/login", "Continue"))


@router.get("/account/password", response_class=HTMLResponse)
def password_page(request: Request, principal=Depends(security.require_role())):
    return render(request, "auth/password.html",
                  {"errors": {}, "min_length": accounts.PASSWORD_MIN_LENGTH},
                  active_page="account")


@router.post("/account/password", response_class=HTMLResponse)
async def password_submit(request: Request, db: Session = DB,
                          principal=Depends(security.require_role())):
    form = await request.form()
    user = user_of(db, principal)
    ip = security.client_ip(request)
    key = security.STEP_UP_KEY + user.id
    errors = accounts.FormErrors()
    wait = security.backoff_remaining(db, key, ip)
    current = form.get("current_password") if isinstance(form.get("current_password"), str) else ""
    if wait:
        errors.add("current_password", f"Too many attempts. Wait {wait} s.")
    elif not await run_in_threadpool(security.verify_password, user.password_hash, current):
        security.record_attempt(db, key, ip, False)
        errors.add("current_password", "That is not your current password.")
    new = form.get("password") if isinstance(form.get("password"), str) else ""
    if not errors:
        problem = accounts.password_problem(new, username=user.username)
        if problem:
            errors.add("password", problem)
        elif new != form.get("password_confirm"):
            errors.add("password_confirm", "The two passwords do not match.")
    if errors:
        return render(request, "auth/password.html",
                      {"errors": errors, "min_length": accounts.PASSWORD_MIN_LENGTH},
                      status_code=400 if not wait else 429, active_page="account")
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
    return see_other("/account?saved=password")


@router.post("/account/step-up")
async def step_up(request: Request, db: Session = DB,
                  principal=Depends(security.require_role())):
    """Re-enter the password: sensitive actions for ``step_up_seconds``."""
    form = await request.form()
    user = user_of(db, principal)
    ip = security.client_ip(request)
    key = security.STEP_UP_KEY + user.id
    target = security.safe_next(form.get("next"), "/account")
    separator = "&" if "?" in target else "?"
    if security.backoff_remaining(db, key, ip):
        return see_other(f"{target}{separator}stepup=wait")
    password = form.get("password") if isinstance(form.get("password"), str) else ""
    if not await run_in_threadpool(security.verify_password, user.password_hash, password):
        security.record_attempt(db, key, ip, False)
        security.audit(db, "auth.step_up_failed", actor_type="user", actor_id=user.id,
                       household_id=user.household_id, ip=ip)
        return see_other(f"{target}{separator}stepup=failed")
    security.record_attempt(db, key, ip, True)
    security.mark_step_up(db, principal)
    return see_other(target)


# ── TOTP (company_admin, operator) ────────────────────────────────────────────
def _twofa_page(request: Request, db: Session, principal, user: User, *,
                error: str | None = None, codes: list[str] | None = None,
                status_code: int = 200, next_url: str = "") -> HTMLResponse:
    context: dict[str, Any] = {"user": user, "error": error, "codes": codes,
                               "required": user.role in security.TOTP_ROLES,
                               "verified": principal.totp_verified, "next": next_url,
                               "stepped_up": principal.stepped_up(),
                               "remaining": len(user.recovery_codes or [])}
    if not user.totp_enabled and user.role in security.TOTP_ROLES and not codes:
        secret = security.unseal(user.totp_secret_enc) if user.totp_secret_enc else None
        if not secret:
            secret = security.new_totp_secret()
            user.totp_secret_enc = security.seal(secret)
            db.flush()
        uri = security.totp_uri(secret, user.username)
        context.update(secret=" ".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
                       qr=qr_svg(uri))
    return render(request, "auth/twofa.html", context, status_code=status_code,
                  active_page="account")


@router.get("/account/2fa", response_class=HTMLResponse)
def twofa_page(request: Request, next: str = "", db: Session = DB,
               principal=Depends(security.require_signed_in())):
    return _twofa_page(request, db, principal, user_of(db, principal),
                       next_url=security.safe_next(next, ""))


@router.post("/account/2fa", response_class=HTMLResponse)
async def twofa_submit(request: Request, db: Session = DB,
                       principal=Depends(security.require_signed_in())):
    form = await request.form()
    user = user_of(db, principal)
    ip = security.client_ip(request)
    action = form.get("action")
    next_url = security.safe_next(form.get("next"), "")
    landing = next_url or landing_for(user.role)
    key = security.TOTP_KEY + user.id
    if user.role not in security.TOTP_ROLES and not user.totp_enabled:
        return see_other("/account")
    wait = security.backoff_remaining(db, key, ip)
    if action in ("verify", "setup") and wait:
        return _twofa_page(request, db, principal, user, status_code=429,
                           error=f"Too many attempts. Wait {wait} s.", next_url=next_url)
    if action == "verify" and user.totp_enabled:
        code = form.get("code") or ""
        secret = security.unseal(user.totp_secret_enc)
        if security.verify_totp(secret, code, user_id=user.id):
            used = None
        elif security.consume_recovery_code(user, code):
            used = "recovery_code"
        else:
            security.record_attempt(db, key, ip, False)
            security.audit(db, "auth.totp_failed", actor_type="user", actor_id=user.id, ip=ip)
            return _twofa_page(request, db, principal, user, status_code=400,
                               error="That code is not right.", next_url=next_url)
        security.record_attempt(db, key, ip, True)
        security.mark_totp_verified(db, principal)
        if used:
            security.audit(db, "auth.recovery_code_used", actor_type="user", actor_id=user.id,
                           ip=ip, detail={"remaining": len(user.recovery_codes or [])})
        return see_other(landing)
    if action == "setup" and not user.totp_enabled:
        secret = security.unseal(user.totp_secret_enc) if user.totp_secret_enc else None
        if not security.verify_totp(secret, form.get("code"), user_id=user.id):
            security.record_attempt(db, key, ip, False)
            return _twofa_page(request, db, principal, user, status_code=400,
                               error="That code is not right. Check the time on your "
                                     "phone and try the next code.", next_url=next_url)
        security.record_attempt(db, key, ip, True)
        codes, digests = security.new_recovery_codes()
        user.totp_enabled, user.recovery_codes = True, digests
        security.mark_totp_verified(db, principal)
        security.audit(db, "auth.totp_enabled", actor_type="user", actor_id=user.id, ip=ip)
        return _twofa_page(request, db, principal, user, codes=codes, next_url=next_url)
    if action == "restart" and not user.totp_enabled:
        user.totp_secret_enc = None
        return see_other("/account/2fa")
    if action == "regenerate" and user.totp_enabled and principal.totp_verified:
        if not principal.stepped_up():
            return _twofa_page(request, db, principal, user, status_code=403,
                               error="Confirm your password first.", next_url=next_url)
        codes, digests = security.new_recovery_codes()
        user.recovery_codes = digests
        security.audit(db, "auth.recovery_codes_regenerated", actor_type="user",
                       actor_id=user.id, ip=ip)
        return _twofa_page(request, db, principal, user, codes=codes)
    return see_other("/account/2fa")


# ── sessions ──────────────────────────────────────────────────────────────────
@router.get("/account/sessions", response_class=HTMLResponse)
def sessions_page(request: Request, db: Session = DB,
                  principal=Depends(security.require_role())):
    now = utcnow()
    rows = (db.query(UserSession).filter_by(user_id=principal.user_id, revoked_at=None)
            .order_by(UserSession.last_seen_at.desc()).all())
    live = [r for r in rows if accounts.as_utc(r.expires_at) > now]
    return render(request, "auth/sessions.html",
                  {"sessions": live, "current": principal.session_id_hash},
                  active_page="account")


@router.post("/account/sessions")
async def sessions_revoke(request: Request, db: Session = DB,
                          principal=Depends(security.require_role())):
    form = await request.form()
    target = form.get("session")
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
                       ip=security.client_ip(request), detail={"count": count})
    if target == principal.session_id_hash and count:
        response = see_other("/login")
        security.clear_session_cookie(response)
        return response
    return see_other("/account/sessions")


__all__ = ["router", "render", "see_other", "message_page", "landing_for", "user_of",
           "household_of", "qr_svg", "HOUSEHOLD_ROLES"]
