"""LC hardening: tunnel identity, CSRF, CSP, security headers.

Owner: track T-WEB. ``install(app)`` is called by ``sentrix.web.extensions``
(docs/7 §5.8, §9.1). One pure-ASGI middleware does all of it, so the MJPEG
stream and the websocket pass through unbuffered:

* **Tunnel identity.** ``sentrix.link.tunnel_auth.verify`` runs on every HTTP
  and websocket request. A verified identity is stored at
  ``scope[tunnel_auth.SCOPE_KEY]`` (``auth.principal_of`` reads it); a request
  that fails verification has every bridge header removed, so headers that
  arrive from the network are inert. Paths the tunnel never carries (sign-in,
  Wi-Fi setup, the API docs, ``/video``, ``/ws/*``) are refused for tunnel
  identities: the Portal relays video and state itself.
* **CSRF.** A state-changing request (POST, PUT, PATCH, DELETE) from an LC
  session must carry ``X-CSRF-Token`` = ``auth.csrf_for_session(cookie)``,
  which ``/api/login`` and ``/api/session`` give the device app. Any LAN
  state-changing request a browser marks as cross-site (``Origin``,
  ``Sec-Fetch-Site``, ``Referer``) is refused, signed in or not (login CSRF).
  Tunnelled requests were checked by the Portal against its own token (their
  browser origin is the Portal's), so neither check applies to them.
  The pre-M10 ``session_token`` cookie is still honoured until it expires
  (12 h): without the header it passes only the cross-site check, so a tab
  opened before the upgrade keeps working.
* **Headers.** The CSP (``'self'`` only: no inline script or style, which the
  device app's build never has; React sets styles through the CSSOM),
  ``X-Frame-Options: DENY``, ``X-Content-Type-Options``, ``Referrer-Policy``,
  ``Permissions-Policy``, and ``Cache-Control: no-store`` on everything except
  ``/static``. Headers a route set itself are kept.
"""

from __future__ import annotations

import hmac
import os
import re
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI
from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import cookie_parser
from starlette.responses import JSONResponse

from sentrix.config import settings
from sentrix.link import tunnel_auth
from sentrix.utils.logging import get_logger

log = get_logger("hardening")

#: Methods that change state and therefore need CSRF protection.
STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
#: Paths the tunnel never carries (docs/7 §5.7); refused for tunnel identities.
#: ``/api/login``, ``/api/logout`` and ``/api/setup/`` are the device app's
#: JSON sign-in and Wi-Fi setup (docs/9).
NEVER_TUNNELLED = ("/login", "/logout", "/setup/", "/docs", "/redoc",
                   "/openapi.json", "/video", "/ws/", "/api/login", "/api/logout",
                   "/api/setup/")
#: ``Sec-Fetch-Site`` values of a request the console's own pages made.
_SAME_ORIGIN_FETCH = frozenset({"same-origin", "none"})
#: A Host header that is safe to echo into the CSP.
_HOST = re.compile(r"^[A-Za-z0-9.\-]+(:\d{1,5})?$|^\[[0-9A-Fa-f:.]+\](:\d{1,5})?$")

_HEADERS = {
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "cross-origin-opener-policy": "same-origin",
}


def csp_enabled() -> bool:
    """``SENTRIX_CSP`` (on by default). A function so tests can switch it."""
    return bool(settings.web.csp)


def _password() -> str:
    return os.getenv("SENTRIX_PASSWORD", "").strip()


# ── tunnel identity ───────────────────────────────────────────────────────────
def _strip_bridge_headers(scope: dict) -> None:
    bridge = {name.encode("latin-1") for name in tunnel_auth.BRIDGE_HEADERS}
    scope["headers"] = [(name, value) for name, value in scope.get("headers", [])
                        if name.lower() not in bridge]


def accept_tunnel(scope: dict) -> tunnel_auth.TunnelIdentity | None:
    """Verify the bridge headers; store the identity, or strip them all."""
    scope.pop(tunnel_auth.SCOPE_KEY, None)
    identity = tunnel_auth.verify(Headers(scope=scope))
    if identity is None:
        _strip_bridge_headers(scope)
        return None
    scope[tunnel_auth.SCOPE_KEY] = identity
    return identity


def never_tunnelled(path: str) -> bool:
    return any(path == prefix.rstrip("/") or path.startswith(prefix)
               for prefix in NEVER_TUNNELLED)


# ── origin checks ─────────────────────────────────────────────────────────────
def _netloc(url: str | None) -> str | None:
    if not url or url == "null":
        return None
    try:
        return urlsplit(url).netloc.lower() or None
    except ValueError:
        return None


def is_cross_site(headers: Headers) -> bool:
    """True when the browser says this request did not come from a page of
    this origin. A request without any of these headers is not from a browser
    page (scripts and tests), which cannot be a cross-site forgery."""
    host = (headers.get("host") or "").lower()
    site = headers.get("sec-fetch-site")
    if site is not None and site.lower() not in _SAME_ORIGIN_FETCH:
        return True
    origin = headers.get("origin")
    if origin is not None:
        return _netloc(origin) != host or not host
    referer = headers.get("referer")
    if referer is not None:
        return _netloc(referer) != host or not host
    return False


def websocket_origin_ok(headers: Headers) -> bool:
    """Cross-site websocket hijacking guard: a browser always sends ``Origin``
    on a websocket handshake, and it must be this console's own origin."""
    origin = headers.get("origin")
    if origin is None:
        return True
    host = (headers.get("host") or "").lower()
    return bool(host) and _netloc(origin) == host


# ── CSRF ──────────────────────────────────────────────────────────────────────
def _session_cookie(headers: Headers) -> tuple[str | None, str | None]:
    """(cookie name, value) of the console session, as ``auth`` picks it."""
    cookies = cookie_parser(headers.get("cookie", ""))
    name = settings.web.cookie_name
    if cookies.get(name):
        return name, cookies[name]
    return None, None


def csrf_refusal(scope: dict, identity: tunnel_auth.TunnelIdentity | None) -> str | None:
    """Why a state-changing request is refused, or None to let it through."""
    if scope.get("method", "GET").upper() not in STATE_CHANGING:
        return None
    if identity is not None:
        # The Portal checked its own CSRF token (and the browser's origin is
        # the Portal's, not this console's) before replaying the request.
        return None
    headers = Headers(scope=scope)
    if is_cross_site(headers):
        return "cross-site request refused"
    from sentrix.web.auth import csrf_for_session
    from sentrix.web.security import verify_token
    _, cookie = _session_cookie(headers)
    password = _password()
    if not cookie or not verify_token(cookie, password):
        return None                         # not signed in: the route answers 401/307
    supplied = headers.get(settings.web.csrf_header)
    if supplied:
        expected = csrf_for_session(cookie)
        if hmac.compare_digest(supplied.encode(), expected.encode()):
            return None
        return "invalid CSRF token; reload the page"
    return "missing CSRF token; reload the page"


# ── response headers ──────────────────────────────────────────────────────────
def content_security_policy(host: str | None = None) -> str:
    """Same-origin script, style, font and media only (no inline code);
    images may be blob:/data: (the capture preview); websockets only to this
    host. Through the tunnel the browser-facing host is the Portal's, which
    ``'self'`` covers (docs/9 §6)."""
    connect = ["'self'"]
    if host and _HOST.match(host):
        connect += [f"ws://{host}", f"wss://{host}"]
    return "; ".join((
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' blob: data:",
        "font-src 'self'",
        f"connect-src {' '.join(connect)}",
        "media-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ))


def security_headers(scope: dict) -> dict[str, str]:
    headers = dict(_HEADERS)
    if csp_enabled():
        host = None
        if tunnel_auth.SCOPE_KEY not in scope:
            host = Headers(scope=scope).get("host")
        headers["content-security-policy"] = content_security_policy(host)
    if not scope.get("path", "").startswith("/static/"):
        headers["cache-control"] = "no-store"
    return headers


class ConsoleHardening:
    """Pure ASGI: tunnel identity, CSRF, CSP and security headers."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        identity = accept_tunnel(scope)

        if identity is not None and never_tunnelled(scope.get("path", "")):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await self._refuse(scope, receive, send, 404, "not_found",
                               "not available through the Portal")
            return
        if scope["type"] == "websocket":
            await self.app(scope, receive, send)
            return

        reason = csrf_refusal(scope, identity)
        if reason is not None:
            log.warning("refused %s %s: %s", scope.get("method"), scope.get("path"), reason)
            await self._refuse(scope, receive, send, 403, "csrf", reason)
            return

        extra = security_headers(scope)

        async def send_with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in extra.items():
                    if name not in headers:
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)

    @staticmethod
    async def _refuse(scope: dict, receive: Any, send: Any, status: int,
                      code: str, message: str) -> None:
        response = JSONResponse({"detail": message, "code": code}, status_code=status,
                                headers=security_headers(scope))
        await response(scope, receive, send)


def install(app: FastAPI) -> None:
    app.add_middleware(ConsoleHardening)


__all__ = ["install", "ConsoleHardening", "accept_tunnel", "csrf_refusal",
           "is_cross_site", "websocket_origin_ok", "never_tunnelled",
           "content_security_policy", "security_headers", "csp_enabled",
           "NEVER_TUNNELLED", "STATE_CHANGING"]
