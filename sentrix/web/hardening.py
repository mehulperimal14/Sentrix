"""LC hardening: tunnel identity, CSRF, CSP nonces, security headers.

Owner: track T-WEB. ``install(app)`` is called by ``sentrix.web.extensions``.
Contract (docs/7 §5.8, §9.1): a middleware that

* verifies ``sentrix.link.tunnel_auth.verify(request.headers)`` and stores the
  identity at ``scope[tunnel_auth.SCOPE_KEY]``; strips every bridge header
  from requests that fail verification;
* checks ``X-CSRF-Token`` on POST/PUT/PATCH/DELETE for LC sessions (tunnelled
  requests were checked by the Portal);
* sets CSP with a per-request nonce (``scope["csp_nonce"]``), X-Frame-Options
  DENY, Referrer-Policy, X-Content-Type-Options.

The foundation ships a no-op so the app runs unchanged until T-WEB lands.
"""

from __future__ import annotations

from fastapi import FastAPI


def install(app: FastAPI) -> None:
    return None


__all__ = ["install"]
