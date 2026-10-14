"""Portal security headers and the per-request CSP nonce.

Owner: track T-ACCT. Installed by ``create_app`` from ``MIDDLEWARE_MODULES``.

* Every HTTP request gets ``request.state.csp_nonce`` (templates render it as
  ``{{ csp_nonce }}`` on their external ``<script>`` tags).
* Portal pages get ``Content-Security-Policy`` (nonce scripts only, no inline
  styles), ``X-Frame-Options: DENY``, ``Permissions-Policy`` (no camera or
  microphone; geolocation only for the Portal's own "use my location" button)
  and ``Cache-Control: no-store`` (pages carry addresses, phone numbers and
  one-time codes; ``/static`` is left cacheable), plus ``X-Content-Type-
  Options`` and ``Referrer-Policy`` on everything. Behind TLS
  (``PORTAL_SECURE_COOKIES=1``) every response also carries HSTS.
* **Tunnelled console responses (``/d/{device_id}/...``) get no CSP, no frame
  header, no permissions policy and no cache header from the Portal**: the
  device's LC sends its own CSP with its own nonce, and two policies on one
  response combine (the stricter wins), so a Portal policy would block the
  console's nonce scripts. Those headers pass through from the device
  untouched (the proxy adds its own ``Cache-Control`` per docs/7 §5.7).

Pure ASGI (not ``BaseHTTPMiddleware``) so the MJPEG relay streams unbuffered.
"""

from __future__ import annotations

import secrets

from fastapi import FastAPI
from starlette.datastructures import MutableHeaders

TUNNEL_PREFIX = "/d/"
STATIC_PREFIX = "/static/"
PERMISSIONS_POLICY = ("camera=(), microphone=(), geolocation=(self), payment=(), "
                      "usb=(), serial=()")
HSTS = "max-age=31536000; includeSubDomains"


def content_security_policy(nonce: str) -> str:
    return ("default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            "style-src 'self'; img-src 'self' data: blob:; "
            "connect-src 'self'; font-src 'self'; object-src 'none'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def _secure_transport(scope) -> bool:
    app = scope.get("app")
    settings = getattr(getattr(app, "state", None), "settings", None)
    return bool(getattr(settings, "secure_cookies", False))


class SecurityHeaders:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        nonce = secrets.token_urlsafe(16)
        scope.setdefault("state", {})["csp_nonce"] = nonce
        path = scope.get("path", "")
        tunnelled = path.startswith(TUNNEL_PREFIX)
        static = path.startswith(STATIC_PREFIX)
        secure = _secure_transport(scope)

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if not tunnelled:
                    headers.setdefault("content-security-policy",
                                       content_security_policy(nonce))
                    headers.setdefault("x-frame-options", "DENY")
                    headers.setdefault("permissions-policy", PERMISSIONS_POLICY)
                    if not static:
                        headers.setdefault("cache-control", "no-store")
                headers.setdefault("x-content-type-options", "nosniff")
                headers.setdefault("referrer-policy", "same-origin")
                if secure:
                    headers.setdefault("strict-transport-security", HSTS)
            await send(message)

        await self.app(scope, receive, send_with_headers)


def install(app: FastAPI) -> None:
    app.add_middleware(SecurityHeaders)


__all__ = ["install", "SecurityHeaders", "content_security_policy", "TUNNEL_PREFIX",
           "PERMISSIONS_POLICY"]
