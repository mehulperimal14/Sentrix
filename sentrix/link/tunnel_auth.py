"""Tunnel auth bridge: how the LC knows a request came through the Portal.

The Portal forwards ``/d/{device_id}/...`` over the link; the device's tunnel
executor (track T-LINK) replays the request *in-process* into the LC FastAPI
app with ``httpx.ASGITransport``. It adds the headers below, including a
secret that exists only in this process's memory (made at import, never
written, logged or sent). The LC accepts a Portal identity **only** when that
secret matches, so the same headers arriving from the network are ignored
(and the LC hardening strips them).

Header contract (docs/7 §5.8):

``x-sentrix-tunnel``        the per-process secret (:func:`process_secret`)
``x-sentrix-user-id``       Portal user id (audit; never a password)
``x-sentrix-user-name``     display name (sanitised, <= 100 chars)
``x-sentrix-role``          owner | member | operator   (company_admin never tunnels)
``x-sentrix-step-up-at``    unix seconds of the user's last password/PIN step-up, or ""
``x-sentrix-request-id``    the tunnel request id (correlates device and Portal audit)
``x-sentrix-base``          URL prefix of the Portal twin, ``/d/<device_id>``
``x-sentrix-csrf``          the Portal session's CSRF token: the LC renders it into
                            ``<meta name="csrf-token">`` so page scripts send it back
                            as ``X-CSRF-Token``; the Portal checks it before forwarding

Stdlib only.
"""

from __future__ import annotations

import hmac
import re
import secrets
from dataclasses import dataclass
from typing import Any, Mapping

HEADER_SECRET = "x-sentrix-tunnel"
HEADER_USER_ID = "x-sentrix-user-id"
HEADER_USER_NAME = "x-sentrix-user-name"
HEADER_ROLE = "x-sentrix-role"
HEADER_STEP_UP = "x-sentrix-step-up-at"
HEADER_REQUEST_ID = "x-sentrix-request-id"
HEADER_BASE = "x-sentrix-base"
HEADER_CSRF = "x-sentrix-csrf"

#: Every bridge header; the LC removes them from requests that fail ``verify``.
BRIDGE_HEADERS = (HEADER_SECRET, HEADER_USER_ID, HEADER_USER_NAME, HEADER_ROLE,
                  HEADER_STEP_UP, HEADER_REQUEST_ID, HEADER_BASE, HEADER_CSRF)

#: Roles a tunnelled request may carry. company_admin has no customer access.
TUNNEL_ROLES = ("owner", "member", "operator")
#: Request-scope key where the LC stores the verified identity.
SCOPE_KEY = "sentrix_tunnel"

_SECRET = secrets.token_urlsafe(32)
_BASE = re.compile(r"^/d/[A-Za-z0-9_-]{1,64}$")
_SAFE_NAME = re.compile(r"[^\w .@'-]")


def process_secret() -> str:
    """The in-memory secret. Only the in-process tunnel executor may read it."""
    return _SECRET


@dataclass(frozen=True)
class TunnelIdentity:
    user_id: str
    user_name: str
    role: str
    step_up_at: float | None
    request_id: str | None
    base: str
    csrf: str | None

    @property
    def actor(self) -> str:
        """Audit actor string, e.g. ``portal:7f3a...``."""
        return f"portal:{self.user_id}"

    def stepped_up_within(self, seconds: float, now: float) -> bool:
        return self.step_up_at is not None and 0 <= now - self.step_up_at <= seconds


def build_headers(*, user_id: str, user_name: str, role: str, base: str,
                  step_up_at: float | None = None, request_id: str | None = None,
                  csrf: str | None = None) -> dict[str, str]:
    """Headers the device's tunnel executor adds to a replayed request."""
    if role not in TUNNEL_ROLES:
        raise ValueError(f"role {role!r} may not use the tunnel")
    return {
        HEADER_SECRET: _SECRET, HEADER_USER_ID: str(user_id),
        HEADER_USER_NAME: _SAFE_NAME.sub("", str(user_name))[:100],
        HEADER_ROLE: role, HEADER_STEP_UP: "" if step_up_at is None else str(int(step_up_at)),
        HEADER_REQUEST_ID: request_id or "", HEADER_BASE: base, HEADER_CSRF: csrf or "",
    }


def _get(headers: Mapping[str, str] | Any, name: str) -> str | None:
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    value = getter(name)
    if value is None:
        value = getter(name.title())
    return value


def verify(headers: Mapping[str, str] | Any) -> TunnelIdentity | None:
    """The tunnelled identity, or None (no secret, wrong secret, bad fields).

    ``headers`` is any mapping with ``.get`` (Starlette ``Headers`` is
    case-insensitive). Constant-time secret comparison.
    """
    supplied = _get(headers, HEADER_SECRET)
    if not supplied or not hmac.compare_digest(supplied.encode(), _SECRET.encode()):
        return None
    role = (_get(headers, HEADER_ROLE) or "").strip()
    user_id = (_get(headers, HEADER_USER_ID) or "").strip()
    base = (_get(headers, HEADER_BASE) or "").strip()
    if role not in TUNNEL_ROLES or not user_id or not _BASE.match(base):
        return None
    raw_step = (_get(headers, HEADER_STEP_UP) or "").strip()
    try:
        step_up = float(raw_step) if raw_step else None
    except ValueError:
        step_up = None
    return TunnelIdentity(
        user_id=user_id[:64],
        user_name=_SAFE_NAME.sub("", _get(headers, HEADER_USER_NAME) or "")[:100],
        role=role, step_up_at=step_up,
        request_id=(_get(headers, HEADER_REQUEST_ID) or None),
        base=base, csrf=(_get(headers, HEADER_CSRF) or None))


def identity_of(request: Any) -> TunnelIdentity | None:
    """The identity the LC hardening stored on this request's scope, if any."""
    scope = getattr(request, "scope", None) or {}
    return scope.get(SCOPE_KEY)


def base_for(request: Any) -> str:
    """URL prefix for links and fetches: ``/d/<id>`` when tunnelled, else ""."""
    identity = identity_of(request)
    return identity.base if identity is not None else ""


__all__ = ["HEADER_SECRET", "HEADER_USER_ID", "HEADER_USER_NAME", "HEADER_ROLE",
           "HEADER_STEP_UP", "HEADER_REQUEST_ID", "HEADER_BASE", "HEADER_CSRF",
           "BRIDGE_HEADERS", "TUNNEL_ROLES", "SCOPE_KEY", "TunnelIdentity",
           "process_secret", "build_headers", "verify", "identity_of", "base_for"]
