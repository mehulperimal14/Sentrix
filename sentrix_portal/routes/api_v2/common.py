"""Shared pieces of the JSON API v2 (docs/9 §4): the error model and its
handlers, the role dependencies (``csrf="header"``), time formatting,
turning a JSON body into the form values the flows validate, and mapping a
flow's :class:`Outcome` or an SOS result onto the error model.

API routes never redirect: a missing session is 401 ``not_signed_in``, a
wrong role 403 ``forbidden``, a missing CSRF header 403 ``csrf``, staff
without TOTP 403 ``totp_required``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import (http_exception_handler,
                                        request_validation_exception_handler)
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from sentrix_portal import security
from sentrix_portal import settings as portal_settings
from sentrix_portal.models import Household, User
from sentrix_portal.services.outcome import Outcome

PREFIX = "/api/v2"
#: The household time zone every time is shown in (no per-household setting yet).
DEFAULT_TIME_ZONE = "Asia/Kolkata"

#: HTTP status of each error code (docs/9 §4).
STATUS = {"not_signed_in": 401, "forbidden": 403, "csrf": 403, "step_up_required": 403,
          "totp_required": 403, "not_found": 404, "conflict": 409, "device_offline": 503,
          "validation": 422, "rate_limited": 429, "test_mode_only": 403}
#: Plain English for the codes the security dependency raises.
MESSAGES = {"not_signed_in": "Please sign in.",
            "forbidden": "You cannot do that here.",
            "csrf": "The page expired. Reload it and try again.",
            "step_up_required": "Confirm your password first.",
            "totp_required": "Enter the code from your authenticator app first.",
            "not_found": "Not found.",
            "conflict": "This has changed meanwhile. Reload and try again.",
            "device_offline": "The device is offline.",
            "validation": "Check the highlighted fields.",
            "rate_limited": "Too many attempts. Wait a little and try again.",
            "test_mode_only": "This works only in test mode."}


# ── the error model ───────────────────────────────────────────────────────────
class ErrorDetail(BaseModel):
    code: str
    message: str
    fields: dict[str, str] | None = None
    retry_after: int | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


#: Documented on every API v2 route (the generated types know the shape).
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"model": ErrorResponse} for status in (401, 403, 404, 409, 422, 429, 503)}


class ApiError(Exception):
    def __init__(self, code: str, message: str | None = None, *, status: int | None = None,
                 fields: dict[str, str] | None = None, retry_after: int | None = None):
        super().__init__(code)
        self.code = code
        self.status = status or STATUS.get(code, 400)
        self.message = message or MESSAGES.get(code, "Something went wrong.")
        self.fields = dict(fields) if fields else None
        self.retry_after = int(retry_after) if retry_after else None


def error_response(error: ApiError) -> JSONResponse:
    body: dict[str, Any] = {"code": error.code, "message": error.message}
    if error.fields:
        body["fields"] = error.fields
    if error.retry_after:
        body["retry_after"] = error.retry_after
    headers = {"Cache-Control": "no-store"}
    if error.retry_after:
        headers["Retry-After"] = str(error.retry_after)
    return JSONResponse({"error": body}, status_code=error.status, headers=headers)


def _is_api(request: Request) -> bool:
    return request.url.path.startswith(PREFIX + "/") or request.url.path == PREFIX


_DETAIL_CODES = {"csrf": "csrf", "totp_required": "totp_required",
                 "step_up_required": "step_up_required", "consent_required": "forbidden",
                 "forbidden_role": "forbidden"}
_STATUS_CODES = {400: "validation", 401: "not_signed_in", 403: "forbidden", 404: "not_found",
                 405: "not_found", 409: "conflict", 413: "validation", 422: "validation",
                 429: "rate_limited", 503: "device_offline"}


def _from_http(exc: StarletteHTTPException) -> ApiError:
    detail = exc.detail if isinstance(exc.detail, str) else ""
    code = _DETAIL_CODES.get(detail) if exc.status_code == 403 else None
    code = code or _STATUS_CODES.get(exc.status_code, "conflict")
    return ApiError(code, MESSAGES.get(code), status=exc.status_code)


def _field_message(error: dict) -> tuple[str, str]:
    loc = [str(part) for part in error.get("loc", ()) if part not in ("body", "query", "path")]
    name = ".".join(loc) or "body"
    kind = error.get("type", "")
    if kind == "missing":
        message = "This is required."
    elif kind.startswith(("int_", "float_")):
        message = "Enter a whole number." if kind.startswith("int_") else "Enter a number."
    elif kind.startswith("bool"):
        message = "Choose yes or no."
    elif kind in ("literal_error", "enum"):
        message = "Choose one of the options."
    elif kind.startswith("string_too_long"):
        message = "This is too long."
    else:
        message = "Enter a valid value."
    return name, message


def install(app: FastAPI) -> None:
    """The error model for every ``/api/v2`` response (the rest of the
    Portal keeps FastAPI's own handlers)."""

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return error_response(exc)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if not _is_api(request):
            return await http_exception_handler(request, exc)
        return error_response(_from_http(exc))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        if not _is_api(request):
            return await request_validation_exception_handler(request, exc)
        fields: dict[str, str] = {}
        for error in exc.errors():
            name, message = _field_message(error)
            fields.setdefault(name, message)
        return error_response(ApiError("validation", MESSAGES["validation"], fields=fields))


# ── dependencies ──────────────────────────────────────────────────────────────
ANY = security.require_role(csrf="header")
SIGNED_IN = security.require_signed_in(csrf="header")
HOUSEHOLD = security.require_role("owner", "member", csrf="header")
OWNER = security.require_role("owner", csrf="header")
OPERATOR = security.require_role("operator", csrf="header")
ADMIN = security.require_role("company_admin", csrf="header")
HOUSEHOLD_OR_DESK = security.require_role("owner", "member", "operator", csrf="header")


def check_login_csrf(request: Request, message: str | None = None) -> None:
    """A request without a session (sign-in, activation, token pages) proves
    it came from the SPA with the double-submit token of ``GET /session``."""
    supplied = request.headers.get(portal_settings.current().csrf_header)
    if not security.verify_login_csrf(request, supplied):
        raise ApiError("csrf", message)


def user_of(db: Session, principal: security.Principal) -> User:
    return db.get(User, principal.user_id)


def household_of(db: Session, principal: security.Principal) -> Household:
    household = db.get(Household, principal.household_id) if principal.household_id else None
    if household is None:
        raise ApiError("not_found", "There is no household on this account.")
    return household


def ip_of(request: Request) -> str | None:
    return security.client_ip(request)


# ── values ────────────────────────────────────────────────────────────────────
def z(value: datetime | str | None) -> str | None:
    """ISO 8601 UTC with ``Z`` (stored times are UTC; SQLite drops the zone)."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean_detail(detail: dict | None) -> dict[str, Any]:
    """An audit or timeline row's detail for the app: times (``*_at``,
    ``due``) in UTC with ``Z``, and no hash or digest of anything."""
    out: dict[str, Any] = {}
    for key, value in (detail or {}).items():
        if key.endswith(("hash", "digest")):
            continue
        if isinstance(value, str) and (key.endswith("_at") or key in ("due", "before")):
            value = z(value) or value
        out[key] = value
    return out


def form_values(body: BaseModel | dict | None) -> dict[str, str]:
    """A JSON body as the form the HTML page would have posted: text as is,
    numbers as text, a ticked box as ``"on"`` and an unticked one absent."""
    raw = body.model_dump() if isinstance(body, BaseModel) else dict(body or {})
    values: dict[str, str] = {}
    for name, value in raw.items():
        if value is None or value is False:
            continue
        if value is True:
            values[name] = "on"
        elif isinstance(value, float) and value.is_integer() and name not in ("lat", "lon"):
            values[name] = str(int(value))
        else:
            values[name] = str(value)
    return values


def raise_for(outcome: Outcome, db: Session) -> None:
    """A refused flow as the error model (field errors keep the page's text).

    What the flow recorded on the way (a failed attempt for the back-off,
    its audit row, a claim attempt) is committed first, as the page's
    response did: raising would otherwise roll it back."""
    if outcome.ok:
        return
    db.commit()
    code = outcome.code if outcome.code in STATUS else "validation"
    raise ApiError(code, outcome.message or next(iter(outcome.errors.values()), None),
                   fields=dict(outcome.errors) or None,
                   retry_after=outcome.retry_after or None)


#: An SOS result code (``services.sos.result``) as (API code, default message).
SOS_CODES = {
    "pin_invalid": ("validation", "That PIN is not right."),
    "pin_locked": ("rate_limited", "Too many wrong PINs. Try again in 10 minutes; asking "
                                   "for help still works."),
    "conflict": ("conflict", "This request has moved on."),
    "device_offline": ("device_offline", "The device is offline."),
    "timeout": ("device_offline", "The device did not answer in time. Try again."),
    "device_busy": ("device_offline", "The device is busy. Try again."),
    "internal": ("device_offline", "The device could not answer. Try again."),
    "forbidden_role": ("forbidden", "You cannot do that here."),
    "consent_required": ("forbidden", "You cannot do that here."),
    "unauthorised": ("forbidden", "You cannot do that here."),
    "step_up_required": ("step_up_required", "Confirm your password first."),
    "test_mode_only": ("test_mode_only", "This works only in test mode."),
    "not_found": ("not_found", "This request was not found."),
    "rate_limited": ("rate_limited", "Too many attempts. Wait a little and try again."),
    "invalid_args": ("validation", "Choose a valid answer."),
}
#: Seconds a locked PIN waits (``services.sos.PIN_WINDOW``, the device's own).
PIN_LOCK_SECONDS = 600


def raise_for_sos(result: dict[str, Any], messages: dict[str, str], db: Session, *,
                  field: str | None = None) -> None:
    """A refused SOS answer as the error model; ``messages`` is the page's
    own wording for its audience (owner, contact or Desk). What the answer
    recorded (a wrong PIN, a used link, its audit row) is committed first."""
    if result.get("ok"):
        return
    db.commit()
    code = result.get("code") or "conflict"
    api_code, default = SOS_CODES.get(code, ("conflict", "This request has moved on."))
    message = messages.get(code) or default
    fields = None
    if code == "pin_invalid":
        fields = {"pin": message}
    elif code == "invalid_args" and field:
        fields = {field: message}
    raise ApiError(api_code, message, fields=fields,
                   retry_after=PIN_LOCK_SECONDS if code == "pin_locked" else None)


class Ok(BaseModel):
    ok: bool = True
    message: str | None = None


__all__ = ["PREFIX", "DEFAULT_TIME_ZONE", "STATUS", "MESSAGES", "ErrorDetail",
           "ErrorResponse", "ERROR_RESPONSES", "ApiError", "error_response", "install", "ANY",
           "SIGNED_IN", "HOUSEHOLD", "OWNER", "OPERATOR", "ADMIN", "HOUSEHOLD_OR_DESK",
           "check_login_csrf", "user_of", "household_of", "ip_of", "z", "clean_detail",
           "form_values",
           "raise_for", "SOS_CODES", "raise_for_sos", "Ok"]
