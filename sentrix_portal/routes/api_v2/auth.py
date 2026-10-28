"""Session, sign-in and account (API v2): ``GET /session``, ``/auth/*``
(login, logout, activation, step-up) and ``/account/*`` (profile, mobile
and e-mail confirmation, password, two-factor, sessions). The rules are
``services.account_flows``, the same the HTML pages use.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from sentrix.utils.redact import mask_phone
from sentrix_portal import security
from sentrix_portal import settings as portal_settings
from sentrix_portal.db import DB
from sentrix_portal.models import Household, User, UserSession, utcnow
from sentrix_portal.routes.api_v2.common import (ANY, OWNER, SIGNED_IN, ApiError,
                                                 DEFAULT_TIME_ZONE, Ok, check_login_csrf,
                                                 ip_of, raise_for, user_of, z)
from sentrix_portal.services import account_flows as flows
from sentrix_portal.services import accounts
from sentrix_portal.services.sos_views import call_112

router = APIRouter(tags=["session"])

Role = Literal["company_admin", "operator", "owner", "member"]
TEST_BANNER = "SENTRIX DEMO - TEST MODE, NOT A REAL EMERGENCY SERVICE."
NOT_STAFF = "Two-factor authentication is set up for SENTRIX staff accounts only."


# ── the session ───────────────────────────────────────────────────────────────
class TotpInfo(BaseModel):
    required: bool
    enabled: bool
    verified: bool


class SessionUser(BaseModel):
    id: str
    username: str
    display_name: str
    role: Role
    totp: TotpInfo


class SessionHousehold(BaseModel):
    id: str
    name: str | None
    customer_code: str | None
    status: str | None
    setup_step: int
    setup_done: bool
    time_zone: str
    erasure_due: str | None


class TestModeInfo(BaseModel):
    on: bool
    desk_number: str | None
    banner: str | None


class SessionOut(BaseModel):
    signed_in: bool
    user: SessionUser | None = None
    household: SessionHousehold | None = None
    test_mode: TestModeInfo
    #: The session's CSRF token (``X-CSRF-Token`` on every change).
    csrf_token: str | None = None
    #: Before sign-in: the double-submit token for login and activation
    #: (the same value is set as the ``portal_csrf`` cookie).
    login_csrf_token: str | None = None
    landing: str | None = None
    stepped_up: bool = False
    step_up_until: str | None = None
    step_up_seconds: int
    password_min_length: int


def _test_mode(request: Request) -> TestModeInfo:
    settings = request.app.state.settings
    if not settings.test_mode:
        return TestModeInfo(on=False, desk_number=None, banner=None)
    href = call_112(settings)["href"]
    number = href[4:] if href else None
    banner = f"{TEST_BANNER} SENTRIX does not contact 112 itself"
    banner += f"; the simulated Desk is {number}." if number else "."
    return TestModeInfo(on=True, desk_number=number, banner=banner)


def session_out(db: Session, request: Request, *, user: User, csrf_token: str,
                totp_verified: bool, step_up_at) -> SessionOut:
    settings = portal_settings.current()
    household = db.get(Household, user.household_id) if user.household_id else None
    landing = flows.landing_for(user.role, household)
    if user.role in security.TOTP_ROLES and not totp_verified:
        landing = "/account/2fa"
    until = (accounts.as_utc(step_up_at) + timedelta(seconds=settings.step_up_seconds)
             if step_up_at is not None else None)
    stepped = until is not None and utcnow() <= until
    return SessionOut(
        signed_in=True,
        user=SessionUser(id=user.id, username=user.username,
                         display_name=user.display_name or user.username, role=user.role,
                         totp=TotpInfo(required=user.role in security.TOTP_ROLES,
                                       enabled=bool(user.totp_enabled),
                                       verified=bool(totp_verified))),
        household=None if household is None else SessionHousehold(
            id=household.id, name=household.display_name,
            customer_code=household.customer_code, status=household.status,
            setup_step=int(household.onboarding_step or 1),
            setup_done=household.onboarding_completed_at is not None,
            time_zone=DEFAULT_TIME_ZONE,
            erasure_due=z(accounts.erasure_due_at(household))),
        test_mode=_test_mode(request), csrf_token=csrf_token, landing=landing,
        stepped_up=stepped, step_up_until=z(until) if stepped else None,
        step_up_seconds=settings.step_up_seconds,
        password_min_length=accounts.PASSWORD_MIN_LENGTH)


@router.get("/session", response_model=SessionOut)
def get_session(request: Request, response: Response, db: Session = DB) -> SessionOut:
    """Who is signed in (or not), the household, the test-mode banner and the
    CSRF token to send back. A caller without a session gets the login
    token (and its ``portal_csrf`` cookie) for sign-in and activation."""
    principal = security.principal_of(request, db)
    if principal is None:
        token = security.login_csrf_token(request)
        security.set_login_csrf(response, token)
        return SessionOut(signed_in=False, test_mode=_test_mode(request),
                          login_csrf_token=token,
                          step_up_seconds=portal_settings.current().step_up_seconds,
                          password_min_length=accounts.PASSWORD_MIN_LENGTH)
    return session_out(db, request, user=user_of(db, principal),
                       csrf_token=principal.csrf_token,
                       totp_verified=principal.totp_verified, step_up_at=principal.step_up_at)


# ── sign-in ───────────────────────────────────────────────────────────────────
class LoginIn(BaseModel):
    username: str = ""
    password: str = ""
    next: str | None = None


class SignedInOut(BaseModel):
    session: SessionOut
    #: Where to go now: staff pass two-factor first.
    next: str


def _signed_in(db: Session, request: Request, response: Response, outcome,
               next_url: str = "") -> SignedInOut:
    user, row = outcome.data["user"], outcome.data["session"]
    security.set_session_cookie(response, outcome.data["raw"])
    if user.role in security.TOTP_ROLES:
        target = "/account/2fa"
    else:
        target = next_url or flows.landing_for(user.role, outcome.data["household"])
    return SignedInOut(session=session_out(db, request, user=user, csrf_token=row.csrf_token,
                                           totp_verified=bool(row.totp_verified),
                                           step_up_at=row.step_up_at), next=target)


@router.post("/auth/login", response_model=SignedInOut)
async def login(body: LoginIn, request: Request, response: Response,
                db: Session = DB) -> SignedInOut:
    """Username and password, with the login CSRF token of ``GET /session``."""
    check_login_csrf(request, "The form expired. Please sign in again.")
    outcome = await flows.login(db, username=body.username, password=body.password,
                                ip=ip_of(request), user_agent=request.headers.get("user-agent"))
    raise_for(outcome, db)
    return _signed_in(db, request, response, outcome, security.safe_next(body.next, ""))


@router.post("/auth/logout", response_model=Ok)
async def logout(request: Request, response: Response, db: Session = DB) -> Ok:
    """Ends this session (CSRF header). A stale session just clears the cookie."""
    principal = security.principal_of(request, db)
    if principal is not None:
        supplied = await security.supplied_csrf(request, header_only=True)
        if not security.verify_csrf(principal, supplied):
            raise ApiError("csrf")
        flows.logout(db, principal, ip=ip_of(request))
    security.clear_session_cookie(response)
    return Ok()


class ActivationLinkOut(BaseModel):
    username: str
    password_min_length: int


class ActivateIn(BaseModel):
    username: str = ""
    code: str = ""
    password: str = ""
    password_confirm: str = ""


class ActivateLinkIn(BaseModel):
    password: str = ""
    password_confirm: str = ""


@router.get("/auth/activate/{code}", response_model=ActivationLinkOut)
def activation_link(code: str, db: Session = DB) -> ActivationLinkOut:
    found = accounts.activation_for_link(db, code)
    if found is None:
        raise ApiError("not_found", flows.LINK_NOT_VALID)
    return ActivationLinkOut(username=found[1].username,
                             password_min_length=accounts.PASSWORD_MIN_LENGTH)


async def _activate(request: Request, response: Response, db: Session,
                    code_in_path: str | None, body) -> SignedInOut:
    check_login_csrf(request, "The form expired. Please try again.")
    outcome = await flows.activate(db, code_in_path=code_in_path,
                                   username=getattr(body, "username", ""),
                                   code=getattr(body, "code", ""), password=body.password,
                                   password_confirm=body.password_confirm, ip=ip_of(request),
                                   user_agent=request.headers.get("user-agent"))
    raise_for(outcome, db)
    return _signed_in(db, request, response, outcome)


@router.post("/auth/activate", response_model=SignedInOut)
async def activate(body: ActivateIn, request: Request, response: Response,
                   db: Session = DB) -> SignedInOut:
    """Username + the activation code from the letter, and a new password."""
    return await _activate(request, response, db, None, body)


@router.post("/auth/activate/{code}", response_model=SignedInOut)
async def activate_link(code: str, body: ActivateLinkIn, request: Request,
                        response: Response, db: Session = DB) -> SignedInOut:
    """The e-mailed activation (or reset) link, and a new password."""
    return await _activate(request, response, db, code, body)


class StepUpIn(BaseModel):
    password: str = ""


class StepUpOut(BaseModel):
    ok: bool = True
    step_up_until: str | None


@router.post("/auth/step-up", response_model=StepUpOut)
async def step_up(body: StepUpIn, request: Request, db: Session = DB,
                  principal=Depends(ANY)) -> StepUpOut:
    """Re-enter the password: sensitive changes are allowed for a few minutes."""
    outcome = await flows.step_up(db, principal, password=body.password, ip=ip_of(request))
    raise_for(outcome, db)
    row = db.get(UserSession, principal.session_id_hash)
    seconds = portal_settings.current().step_up_seconds
    return StepUpOut(step_up_until=z(accounts.as_utc(row.step_up_at) + timedelta(seconds=seconds))
                     if row is not None and row.step_up_at else None)


# ── account ───────────────────────────────────────────────────────────────────
class AccountOut(BaseModel):
    id: str
    username: str
    display_name: str | None
    role: Role
    email: str | None
    email_verified: bool
    phone_masked: str | None
    phone_verified: bool
    two_factor: Literal["on", "required", "not_used"]
    stepped_up: bool


class AccountIn(BaseModel):
    display_name: str | None = None
    email: str | None = None


def _account(db: Session, principal) -> AccountOut:
    user = user_of(db, principal)
    two_factor = ("on" if user.totp_enabled else
                  "required" if user.role in security.TOTP_ROLES else "not_used")
    return AccountOut(id=user.id, username=user.username, display_name=user.display_name,
                      role=user.role, email=user.email,
                      email_verified=user.email_verified_at is not None,
                      phone_masked=mask_phone(accounts.user_phone(user)) or None,
                      phone_verified=user.phone_verified_at is not None,
                      two_factor=two_factor, stepped_up=principal.stepped_up())


@router.get("/account", response_model=AccountOut)
def get_account(db: Session = DB, principal=Depends(ANY)) -> AccountOut:
    return _account(db, principal)


@router.patch("/account", response_model=AccountOut)
async def update_account(body: AccountIn, request: Request, db: Session = DB,
                         principal=Depends(ANY)) -> AccountOut:
    """Name and e-mail; a new e-mail needs a fresh password (step-up)."""
    outcome = await flows.update_account(db, principal, display_name=body.display_name,
                                         email=body.email, ip=ip_of(request))
    raise_for(outcome, db)
    return _account(db, principal)


class PhoneCodeIn(BaseModel):
    code: str = ""


@router.post("/account/phone/send", response_model=Ok)
async def send_phone_code(request: Request, db: Session = DB, principal=Depends(OWNER)) -> Ok:
    """The household's own device texts a 6-digit code to the owner's mobile."""
    outcome = await flows.send_phone_code(db, principal, ip=ip_of(request))
    raise_for(outcome, db)
    return Ok(message=outcome.message)


@router.post("/account/phone/confirm", response_model=Ok)
async def confirm_phone_code(body: PhoneCodeIn, request: Request, db: Session = DB,
                             principal=Depends(OWNER)) -> Ok:
    outcome = await flows.confirm_phone_code(db, principal, code=body.code, ip=ip_of(request))
    raise_for(outcome, db)
    return Ok(message=outcome.message)


@router.post("/account/email/resend", response_model=Ok)
def resend_email(db: Session = DB, principal=Depends(ANY)) -> Ok:
    return Ok(message=flows.resend_email_verification(db, principal).message)


class LinkState(BaseModel):
    valid: bool = True


@router.get("/account/verify-email/{token}", response_model=LinkState)
def email_link(token: str, db: Session = DB) -> LinkState:
    if not flows.email_link_valid(db, token):
        raise ApiError("not_found", flows.EMAIL_LINK_NOT_VALID)
    return LinkState()


@router.post("/account/verify-email/{token}", response_model=Ok)
def confirm_email(token: str, request: Request, db: Session = DB) -> Ok:
    """Confirm the address (no session needed: the login CSRF token)."""
    check_login_csrf(request, "Open the link again.")
    outcome = flows.confirm_email(db, token, ip=ip_of(request))
    raise_for(outcome, db)
    return Ok(message=outcome.message)


class PasswordIn(BaseModel):
    current_password: str = ""
    password: str = ""
    password_confirm: str = ""


@router.post("/account/password", response_model=Ok)
async def change_password(body: PasswordIn, request: Request, db: Session = DB,
                          principal=Depends(ANY)) -> Ok:
    """Other sessions are signed out; this one stays and counts as confirmed."""
    outcome = await flows.change_password(db, principal, current=body.current_password,
                                          password=body.password,
                                          password_confirm=body.password_confirm,
                                          ip=ip_of(request))
    raise_for(outcome, db)
    return Ok(message=outcome.message)


# ── two-factor (company_admin, operator) ──────────────────────────────────────
class TotpSetup(BaseModel):
    #: The setup key, grouped by four (shown only until TOTP is on).
    key: str
    #: ``otpauth://`` URI for the QR code the app draws.
    uri: str


class TwoFactorOut(BaseModel):
    required: bool
    enabled: bool
    verified: bool
    stepped_up: bool
    recovery_codes_left: int
    setup: TotpSetup | None


class CodeIn(BaseModel):
    code: str = ""


class VerifiedOut(BaseModel):
    ok: bool = True
    next: str


class RecoveryCodesOut(BaseModel):
    #: Shown once: each works once if the phone is lost.
    recovery_codes: list[str]


def _two_factor(db: Session, principal, user: User) -> TwoFactorOut:
    state = flows.twofa_state(db, principal, user)
    return TwoFactorOut(**{**state, "setup": TotpSetup(**state["setup"]) if state["setup"]
                           else None})


def _staff_user(db: Session, principal, request: Request, *, wait: bool) -> User:
    user = user_of(db, principal)
    if not flows.twofa_applies(user):
        raise ApiError("conflict", NOT_STAFF)
    waiting = flows.twofa_wait(db, user, ip_of(request)) if wait else None
    if waiting is not None:
        raise_for(waiting, db)
    return user


@router.get("/account/2fa", response_model=TwoFactorOut)
def two_factor(db: Session = DB, principal=Depends(SIGNED_IN)) -> TwoFactorOut:
    return _two_factor(db, principal, user_of(db, principal))


@router.post("/account/2fa/verify", response_model=VerifiedOut)
def two_factor_verify(body: CodeIn, request: Request, db: Session = DB,
                      principal=Depends(SIGNED_IN)) -> VerifiedOut:
    """The every-session check: an authenticator code or a recovery code."""
    user = _staff_user(db, principal, request, wait=True)
    if not user.totp_enabled:
        raise ApiError("conflict", "Set up the authenticator first.")
    raise_for(flows.twofa_verify(db, principal, user, code=body.code, ip=ip_of(request)),
              db)
    return VerifiedOut(next=flows.landing_for(user.role))


@router.post("/account/2fa/setup", response_model=RecoveryCodesOut)
def two_factor_setup(body: CodeIn, request: Request, db: Session = DB,
                     principal=Depends(SIGNED_IN)) -> RecoveryCodesOut:
    """Turn TOTP on with a first code; the recovery codes are returned once."""
    user = _staff_user(db, principal, request, wait=True)
    if user.totp_enabled:
        raise ApiError("conflict", "Two-factor authentication is already on.")
    outcome = flows.twofa_setup(db, principal, user, code=body.code, ip=ip_of(request))
    raise_for(outcome, db)
    return RecoveryCodesOut(recovery_codes=outcome.data["codes"])


@router.post("/account/2fa/restart", response_model=TwoFactorOut)
def two_factor_restart(request: Request, db: Session = DB,
                       principal=Depends(SIGNED_IN)) -> TwoFactorOut:
    """Start the setup again with a new key."""
    user = _staff_user(db, principal, request, wait=False)
    if user.totp_enabled:
        raise ApiError("conflict", "Two-factor authentication is already on.")
    flows.twofa_restart(user)
    db.flush()
    return _two_factor(db, principal, user)


@router.post("/account/2fa/recovery-codes", response_model=RecoveryCodesOut)
def two_factor_recovery_codes(request: Request, db: Session = DB,
                              principal=Depends(SIGNED_IN)) -> RecoveryCodesOut:
    """New recovery codes (the old ones stop working); needs the password."""
    user = _staff_user(db, principal, request, wait=False)
    if not (user.totp_enabled and principal.totp_verified):
        raise ApiError("totp_required")
    outcome = flows.twofa_regenerate(db, principal, user, ip=ip_of(request))
    raise_for(outcome, db)
    return RecoveryCodesOut(recovery_codes=outcome.data["codes"])


# ── sessions ──────────────────────────────────────────────────────────────────
class SessionRow(BaseModel):
    #: An opaque reference (never the session id or its digest).
    id: str
    user_agent: str | None
    created_at: str | None
    last_seen_at: str | None
    current: bool


class SessionsOut(BaseModel):
    sessions: list[SessionRow]


class RevokedOut(BaseModel):
    revoked: int
    #: This browser's session was one of them (the cookie is cleared).
    signed_out: bool


def session_ref(id_hash: str) -> str:
    return security.token_digest(f"session-ref:{id_hash}")[:24]


@router.get("/account/sessions", response_model=SessionsOut)
def sessions(db: Session = DB, principal=Depends(ANY)) -> SessionsOut:
    return SessionsOut(sessions=[
        SessionRow(id=session_ref(row.id_hash), user_agent=row.user_agent,
                   created_at=z(row.created_at), last_seen_at=z(row.last_seen_at),
                   current=row.id_hash == principal.session_id_hash)
        for row in flows.live_sessions(db, principal)])


def _revoked(response: Response, revoked: tuple[int, bool]) -> RevokedOut:
    count, signed_out = revoked
    if signed_out:
        security.clear_session_cookie(response)
    return RevokedOut(revoked=count, signed_out=signed_out)


@router.delete("/account/sessions/{session_id}", response_model=RevokedOut)
def revoke_session(session_id: str, request: Request, response: Response, db: Session = DB,
                   principal=Depends(ANY)) -> RevokedOut:
    target = next((row.id_hash for row in flows.live_sessions(db, principal)
                   if session_ref(row.id_hash) == session_id), None)
    if target is None:
        raise ApiError("not_found", "That session has already ended.")
    return _revoked(response, flows.revoke_sessions(db, principal, target, ip=ip_of(request)))


@router.post("/account/sessions/revoke-others", response_model=RevokedOut)
def revoke_other_sessions(request: Request, response: Response, db: Session = DB,
                          principal=Depends(ANY)) -> RevokedOut:
    return _revoked(response, flows.revoke_sessions(db, principal, "others", ip=ip_of(request)))


__all__ = ["router", "SessionOut", "session_out", "session_ref"]
