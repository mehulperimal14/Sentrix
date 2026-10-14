"""Who is calling the Local Console: one principal for every console route.

Two kinds of caller reach the LC app:

* ``lc``: a browser on the LAN with the ``lc_session`` cookie (the device
  admin, who knows ``SENTRIX_PASSWORD``);
* ``tunnel``: a Portal user (owner, member, Desk operator) whose request the
  link's tunnel executor replayed in-process with the verified bridge headers
  (``sentrix.link.tunnel_auth``; the hardening middleware stores the identity
  on the request scope).

Every console router (T-WEB's pages and APIs, T-SOS's ``/api/sos``, T-HW's
``/api/node``, T-LINK's ``/api/link``, T-CH's ``/api/channels``) uses
:func:`require_lc` and reads the :class:`LcPrincipal` it returns. **Roles,
consents, step-up and rate limits of tunnelled requests are enforced in one
place only: the link's path table (``sentrix/link/policy.py``), before the
in-process call.** Console routes enforce only their own business rules (the
SOS PIN, 409 on a sent package, and ``require_lc("device_admin")`` for the
few LC-only routes: ``/api/link/reconnect``, ``/api/channels/test``); they
never restate the path table.

Unauthenticated: an LC page answers 307 to ``/login`` (as before); an API,
media or fetch request, and anything tunnelled, answers 401 JSON
(``{"detail", "code": "unauthorised"}``), never a redirect the Portal would
have to rewrite.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import HTTPException, Request

from sentrix.actor import Actor
from sentrix.config import settings
from sentrix.link import tunnel_auth

#: Roles a console principal can have.
LC_ROLES = ("device_admin", "owner", "member", "operator")
#: The pre-M10 cookie name, still accepted until T-WEB's rename lands.
LEGACY_COOKIE = "session_token"


class LcError(HTTPException):
    """A JSON API error with a docs/7 §13 code: ``{"detail", "code"}``.
    ``sentrix.web.extensions.install`` registers the handler."""

    def __init__(self, code: str, message: str | None = None,
                 headers: dict[str, str] | None = None) -> None:
        from sentrix.link.protocol import ERROR_CODES
        super().__init__(status_code=ERROR_CODES.get(code, 400),
                         detail=message or code, headers=headers)
        self.code = code


@dataclass(frozen=True)
class LcPrincipal:
    kind: str                   # lc | tunnel
    role: str                   # LC_ROLES
    actor: str                  # Actor.ref: lc:device_admin | portal:<id> | desk:<id>
    user_id: str | None
    user_name: str | None
    step_up_at: float | None    # unix seconds (tunnel); None for the device admin
    csrf: str | None            # the token page scripts send back as X-CSRF-Token

    @property
    def channel(self) -> str:
        """Where an SOS answer came from (``sentrix.sos.states.CHANNELS``)."""
        return "lc" if self.kind == "lc" else ("desk" if self.role == "operator" else "portal")

    def to_actor(self) -> Actor:
        if self.kind == "lc":
            return Actor.device_admin()
        return Actor(type=self.role, id=self.user_id, name=self.user_name,
                     step_up_at=self.step_up_at)

    def stepped_up_within(self, seconds: float, now: float | None = None) -> bool:
        """The device admin's password session counts as stepped up."""
        if self.kind == "lc":
            return True
        now = time.time() if now is None else now
        return self.step_up_at is not None and 0 <= now - self.step_up_at <= seconds


def _password() -> str:
    return os.getenv("SENTRIX_PASSWORD", "").strip()


def session_cookie(request: Any) -> str | None:
    cookies = getattr(request, "cookies", None) or {}
    return cookies.get(settings.web.cookie_name) or cookies.get(LEGACY_COOKIE)


def csrf_for_session(cookie: str) -> str:
    """The LC session's CSRF token: HMAC(session key, "csrf:" + cookie).
    Stateless and bound to the session (docs/7 §9.1)."""
    from sentrix.web.security import _signing_key
    return hmac.new(_signing_key(_password()), f"csrf:{cookie}".encode(),
                    hashlib.sha256).hexdigest()


def is_tunnelled(request: Any) -> bool:
    """Replayed by the tunnel executor (verified or not)."""
    if tunnel_auth.identity_of(request) is not None:
        return True
    client = getattr(request, "client", None)
    return bool(client) and getattr(client, "host", None) == tunnel_auth.TUNNEL_CLIENT[0]


def principal_of(request: Any) -> LcPrincipal | None:
    """The caller, or None. A tunnelled request is never a device admin, even
    if it carried an LC cookie."""
    from sentrix.web.security import verify_token

    identity = tunnel_auth.identity_of(request)
    if identity is not None:
        return LcPrincipal(kind="tunnel", role=identity.role, actor=identity.actor,
                           user_id=identity.user_id, user_name=identity.user_name or None,
                           step_up_at=identity.step_up_at, csrf=identity.csrf)
    if is_tunnelled(request):
        return None
    password = _password()
    cookie = session_cookie(request)
    if password and cookie and verify_token(cookie, password):
        return LcPrincipal(kind="lc", role="device_admin", actor="lc:device_admin",
                           user_id=None, user_name="Device admin", step_up_at=None,
                           csrf=csrf_for_session(cookie))
    return None


def wants_json(request: Any) -> bool:
    """An API, media, websocket or script request (or anything tunnelled):
    errors are JSON, never a redirect to the login page."""
    if is_tunnelled(request):
        return True
    path = request.url.path if getattr(request, "url", None) else ""
    if path.startswith(("/api/", "/media/", "/ws/")):
        return True
    headers = getattr(request, "headers", None) or {}
    if headers.get("x-requested-with") or headers.get(settings.web.csrf_header.lower()):
        return True
    accept = headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


def unauthenticated(request: Any, *, legacy_redirect: bool = False) -> HTTPException:
    """401 JSON for APIs and tunnelled requests, else 307 to ``/login``.
    ``legacy_redirect`` keeps the pre-M10 behaviour (307 for every LC
    request) for ``routes.require_auth`` until T-WEB moves its APIs over."""
    if is_tunnelled(request) or (not legacy_redirect and wants_json(request)):
        return LcError("unauthorised", "not signed in")
    return HTTPException(status_code=307, headers={"Location": "/login"})


def require_lc(*roles: str, step_up_seconds: float | None = None) -> Callable[..., Any]:
    """Dependency: an authenticated console principal (any role if none given).

    Pass roles only for LC-only routes (``require_lc("device_admin")``);
    tunnelled role rules live in ``sentrix/link/policy.py``. ``step_up_seconds``
    applies to tunnel principals (the device admin's session counts as a
    step-up) and exists for the same LC-only cases; the path table enforces
    step-up for tunnelled routes. Sets ``request.state.lc_principal``.
    """
    unknown = set(roles) - set(LC_ROLES)
    if unknown:
        raise ValueError(f"unknown LC roles {sorted(unknown)}")

    async def dependency(request: Request) -> LcPrincipal:
        principal = principal_of(request)
        if principal is None:
            raise unauthenticated(request)
        if roles and principal.role not in roles:
            raise LcError("forbidden_role", f"{principal.role} may not use this")
        if step_up_seconds is not None and not principal.stepped_up_within(step_up_seconds):
            raise LcError("step_up_required", "re-enter your password or PIN first")
        request.state.lc_principal = principal
        return principal

    return dependency


def rate_key(request: Any) -> str:
    """Key for console rate limiters: the actor for tunnelled users (they all
    arrive from ``TUNNEL_CLIENT``), the client address otherwise."""
    principal = principal_of(request)
    if principal is not None and principal.kind == "tunnel":
        return principal.actor
    client = getattr(request, "client", None)
    return f"ip:{getattr(client, 'host', None) or 'unknown'}"


__all__ = ["LcPrincipal", "LcError", "LC_ROLES", "LEGACY_COOKIE", "principal_of",
           "require_lc", "unauthenticated", "wants_json", "is_tunnelled",
           "session_cookie", "csrf_for_session", "rate_key"]
