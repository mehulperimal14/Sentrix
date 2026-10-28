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

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from sentrix.utils.redact import mask_phone
from sentrix_portal import security
from sentrix_portal.db import DB
from sentrix_portal.models import Household, User
from sentrix_portal.services import account_flows as flows
from sentrix_portal.services import accounts

router = APIRouter()

#: Roles that use the household pages.
HOUSEHOLD_ROLES = flows.HOUSEHOLD_ROLES


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


landing_for = flows.landing_for


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


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, db: Session = DB):
    form = await request.form()
    username = security.attempt_key(form.get("username"))
    next_url = security.safe_next(form.get("next"), "")
    if not security.verify_login_csrf(request, form.get("csrf_token")):
        return _login_form(request, status_code=403, username=username, next_url=next_url,
                           error="The form expired. Please sign in again.")
    outcome = await flows.login(db, username=form.get("username"),
                                password=form.get("password"),
                                ip=security.client_ip(request),
                                user_agent=request.headers.get("user-agent"))
    if not outcome.ok:
        return _login_form(request, status_code=outcome.status, username=username,
                           next_url=next_url, error=outcome.message)
    user = outcome.data["user"]
    if user.role in security.TOTP_ROLES:
        target = "/account/2fa" + (f"?next={quote(next_url, safe='/')}" if next_url else "")
    else:
        target = next_url or landing_for(user.role, outcome.data["household"])
    response = see_other(target)
    security.set_session_cookie(response, outcome.data["raw"])
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
        flows.logout(db, principal, ip=security.client_ip(request))
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
        return message_page(request, "Link not valid", flows.LINK_NOT_VALID, status_code=404,
                            link=("/login", "Sign in"))
    return _activation_form(request, code_in_path=code, known_user=found[1])


async def _activate(request: Request, db: Session, code_in_path: str | None):
    form = await request.form()
    typed_username = security.attempt_key(form.get("username"))
    if not security.verify_login_csrf(request, form.get("csrf_token")):
        return _activation_form(request, code_in_path=code_in_path, username=typed_username,
                                error="The form expired. Please try again.", status_code=403)
    outcome = await flows.activate(db, code_in_path=code_in_path,
                                   username=form.get("username"), code=form.get("code"),
                                   password=form.get("password"),
                                   password_confirm=form.get("password_confirm"),
                                   ip=security.client_ip(request),
                                   user_agent=request.headers.get("user-agent"))
    if outcome.code == "rate_limited":
        return _activation_form(request, code_in_path=code_in_path, username=typed_username,
                                error=outcome.message, status_code=429)
    if "link_failed" in outcome.data:
        return _activation_form(request, code_in_path=None, username=typed_username,
                                error=outcome.message, status_code=400)
    if not outcome.ok:
        return _activation_form(request, code_in_path=code_in_path,
                                username=outcome.data["username"], errors=outcome.errors,
                                status_code=400, known_user=outcome.data["known_user"])
    user = outcome.data["user"]
    target = ("/account/2fa" if user.role in security.TOTP_ROLES
              else landing_for(user.role, outcome.data["household"]))
    response = see_other(target)
    security.set_session_cookie(response, outcome.data["raw"])
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
    outcome = await flows.update_account(db, principal, display_name=form.get("display_name"),
                                         email=form.get("email"),
                                         ip=security.client_ip(request))
    if not outcome.ok:
        return _account_page(request, db, principal, errors=outcome.errors, status_code=400)
    return see_other("/account?saved=1")


PHONE_SMS = flows.PHONE_SMS


@router.post("/account/phone")
async def phone_verify(request: Request, db: Session = DB,
                       principal=Depends(security.require_role("owner"))):
    """Confirm the owner's mobile with a code sent by SMS from the household's
    own device (``sms.send``: the device only texts numbers in its profile,
    through its send guard). ``action=send`` or ``action=confirm``."""
    form = await request.form()
    ip = security.client_ip(request)
    if form.get("action") == "send":
        outcome = await flows.send_phone_code(db, principal, ip=ip)
    else:
        outcome = await flows.confirm_phone_code(db, principal, code=form.get("code"), ip=ip)
    return see_other("/account?saved=" + outcome.data["saved"])


@router.post("/account/verify-email", response_class=HTMLResponse)
def account_resend_verification(request: Request, db: Session = DB,
                                principal=Depends(security.require_role())):
    flows.resend_email_verification(db, principal)
    return see_other("/account?saved=verify")


@router.get("/account/verify-email/{token}", response_class=HTMLResponse)
def verify_email_page(request: Request, token: str, db: Session = DB):
    if not flows.email_link_valid(db, token):
        return message_page(request, "Link not valid", flows.EMAIL_LINK_NOT_VALID,
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
    outcome = flows.confirm_email(db, token, ip=security.client_ip(request))
    if not outcome.ok:
        return message_page(request, "Link not valid", outcome.message, status_code=404)
    return message_page(request, "E-mail confirmed", outcome.message,
                        link=("/login", "Continue"))


@router.get("/account/password", response_class=HTMLResponse)
def password_page(request: Request, principal=Depends(security.require_role())):
    return render(request, "auth/password.html",
                  {"errors": {}, "min_length": accounts.PASSWORD_MIN_LENGTH},
                  active_page="account")


@router.post("/account/password", response_class=HTMLResponse)
async def password_submit(request: Request, db: Session = DB,
                          principal=Depends(security.require_role())):
    form = await request.form()
    outcome = await flows.change_password(db, principal,
                                          current=form.get("current_password"),
                                          password=form.get("password"),
                                          password_confirm=form.get("password_confirm"),
                                          ip=security.client_ip(request))
    if not outcome.ok:
        return render(request, "auth/password.html",
                      {"errors": outcome.errors, "min_length": accounts.PASSWORD_MIN_LENGTH},
                      status_code=outcome.status, active_page="account")
    return see_other("/account?saved=password")


@router.post("/account/step-up")
async def step_up(request: Request, db: Session = DB,
                  principal=Depends(security.require_role())):
    """Re-enter the password: sensitive actions for ``step_up_seconds``."""
    form = await request.form()
    target = security.safe_next(form.get("next"), "/account")
    separator = "&" if "?" in target else "?"
    outcome = await flows.step_up(db, principal, password=form.get("password"),
                                  ip=security.client_ip(request))
    if not outcome.ok:
        return see_other(f"{target}{separator}stepup={outcome.data['stepup']}")
    return see_other(target)


# ── TOTP (company_admin, operator) ────────────────────────────────────────────
def _twofa_page(request: Request, db: Session, principal, user: User, *,
                error: str | None = None, codes: list[str] | None = None,
                status_code: int = 200, next_url: str = "") -> HTMLResponse:
    state = flows.twofa_state(db, principal, user, show_setup=not codes)
    context: dict[str, Any] = {"user": user, "error": error, "codes": codes,
                               "required": state["required"], "verified": state["verified"],
                               "next": next_url, "stepped_up": state["stepped_up"],
                               "remaining": state["recovery_codes_left"]}
    if state["setup"]:
        context.update(secret=state["setup"]["key"], qr=qr_svg(state["setup"]["uri"]))
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
    if not flows.twofa_applies(user):
        return see_other("/account")
    if action in ("verify", "setup"):
        waiting = flows.twofa_wait(db, user, ip)
        if waiting is not None:
            return _twofa_page(request, db, principal, user, status_code=429,
                               error=waiting.message, next_url=next_url)
    if action == "verify" and user.totp_enabled:
        outcome = flows.twofa_verify(db, principal, user, code=form.get("code"), ip=ip)
        if not outcome.ok:
            return _twofa_page(request, db, principal, user, status_code=400,
                               error=outcome.message, next_url=next_url)
        return see_other(landing)
    if action == "setup" and not user.totp_enabled:
        outcome = flows.twofa_setup(db, principal, user, code=form.get("code"), ip=ip)
        if not outcome.ok:
            return _twofa_page(request, db, principal, user, status_code=400,
                               error=outcome.message, next_url=next_url)
        return _twofa_page(request, db, principal, user, codes=outcome.data["codes"],
                           next_url=next_url)
    if action == "restart" and not user.totp_enabled:
        flows.twofa_restart(user)
        return see_other("/account/2fa")
    if action == "regenerate" and user.totp_enabled and principal.totp_verified:
        outcome = flows.twofa_regenerate(db, principal, user, ip=ip)
        if not outcome.ok:
            return _twofa_page(request, db, principal, user, status_code=403,
                               error=outcome.message, next_url=next_url)
        return _twofa_page(request, db, principal, user, codes=outcome.data["codes"])
    return see_other("/account/2fa")


# ── sessions ──────────────────────────────────────────────────────────────────
@router.get("/account/sessions", response_class=HTMLResponse)
def sessions_page(request: Request, db: Session = DB,
                  principal=Depends(security.require_role())):
    return render(request, "auth/sessions.html",
                  {"sessions": flows.live_sessions(db, principal),
                   "current": principal.session_id_hash},
                  active_page="account")


@router.post("/account/sessions")
async def sessions_revoke(request: Request, db: Session = DB,
                          principal=Depends(security.require_role())):
    form = await request.form()
    _, signed_out = flows.revoke_sessions(db, principal, form.get("session"),
                                          ip=security.client_ip(request))
    if signed_out:
        response = see_other("/login")
        security.clear_session_cookie(response)
        return response
    return see_other("/account/sessions")


__all__ = ["router", "render", "see_other", "message_page", "landing_for", "user_of",
           "household_of", "qr_svg", "HOUSEHOLD_ROLES"]


