"""Portal security headers and the per-request CSP nonce.

Owner: track T-ACCT (the foundation ships this default). Installed by
``create_app`` from ``MIDDLEWARE_MODULES``.

* Every HTTP request gets ``request.state.csp_nonce`` (templates render it as
  ``{{ csp_nonce }}`` on their external ``<script>`` tags).
* Portal pages get ``Content-Security-Policy`` (nonce scripts only),
  ``X-Frame-Options: DENY``, ``X-Content-Type-Options``, ``Referrer-Policy``.
* **Tunnelled console responses (``/d/{device_id}/...``) get no CSP and no
  frame header from the Portal**: the device's LC sends its own CSP with its
  own nonce, and two policies on one response combine (the stricter wins), so
  a Portal policy would block the console's nonce scripts. Those headers pass
  through from the device untouched.

Pure ASGI (not ``BaseHTTPMiddleware``) so the MJPEG relay streams unbuffered.
"""

from __future__ import annotations

import secrets

from fastapi import FastAPI
from starlette.datastructures import MutableHeaders

TUNNEL_PREFIX = "/d/"


def content_security_policy(nonce: str) -> str:
    return ("default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            "style-src 'self'; img-src 'self' data: blob:; "
            "connect-src 'self'; font-src 'self'; object-src 'none'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


class SecurityHeaders:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        nonce = secrets.token_urlsafe(16)
        scope.setdefault("state", {})["csp_nonce"] = nonce
        tunnelled = scope.get("path", "").startswith(TUNNEL_PREFIX)

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if not tunnelled:
                    headers.setdefault("content-security-policy",
                                       content_security_policy(nonce))
                    headers.setdefault("x-frame-options", "DENY")
                headers.setdefault("x-content-type-options", "nosniff")
                headers.setdefault("referrer-policy", "same-origin")
            await send(message)

        await self.app(scope, receive, send_with_headers)


def install(app: FastAPI) -> None:
    app.add_middleware(SecurityHeaders)


__all__ = ["install", "SecurityHeaders", "content_security_policy", "TUNNEL_PREFIX"]
