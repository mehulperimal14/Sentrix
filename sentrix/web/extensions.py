"""Extension points of the Local Console app, so tracks add routes and
middleware without editing ``sentrix/app.py`` or ``sentrix/web/routes.py``.

``ROUTER_MODULES``: each module exports ``router: fastapi.APIRouter``; they are
included in this order after the core routers. A router that fails to import
is logged and skipped (a broken extension page must not take the alarm down).

``MIDDLEWARE_MODULES``: each exports ``install(app) -> None``. These carry the
console's security (tunnel identity, CSRF, CSP), so a failure is **not**
skipped silently: the app installs :func:`_fail_closed` instead, which refuses
every state-changing request with 503 while pages, video and the API reads
keep working (the alarm stays visible; nobody can act without CSRF/CSP), and
tunnel identities are never accepted (only the hardening module sets them).

Owners: docs/7 §2.
"""

from __future__ import annotations

import importlib

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from sentrix.utils.logging import get_logger

log = get_logger("extensions")

ROUTER_MODULES: tuple[str, ...] = (
    "sentrix.web.sos_routes",       # T-SOS   /api/sos/*
    "sentrix.web.node_routes",      # T-HW    /api/node/*
    "sentrix.web.link_routes",      # T-LINK  /api/link/*
    "sentrix.web.channel_routes",   # T-CH    /api/channels/*
    "sentrix.web.setup_routes",     # T-WEB   /sos, /node pages, /setup/wifi, /api/level-history
)

MIDDLEWARE_MODULES: tuple[str, ...] = (
    "sentrix.web.hardening",        # T-WEB   tunnel identity, CSRF, CSP, headers
)

_STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _fail_closed(app: FastAPI, failed: str) -> None:
    @app.middleware("http")
    async def refuse_writes(request: Request, call_next):
        if request.method in _STATE_CHANGING:
            return JSONResponse({"detail": f"console hardening unavailable ({failed}); "
                                           "changes are refused until it loads",
                                 "code": "internal"}, status_code=503)
        return await call_next(request)


def _install_error_handler(app: FastAPI) -> None:
    from sentrix.web.auth import LcError

    @app.exception_handler(LcError)
    async def lc_error(_request: Request, exc: LcError) -> JSONResponse:
        return JSONResponse({"detail": exc.detail, "code": exc.code},
                            status_code=exc.status_code, headers=exc.headers)


def install(app: FastAPI) -> list[str]:
    """Include every extension router and middleware; returns what loaded."""
    _install_error_handler(app)
    loaded = []
    for name in ROUTER_MODULES:
        try:
            module = importlib.import_module(name)
            app.include_router(module.router)
            loaded.append(name)
        except Exception as exc:
            log.error("router extension %s not loaded: %s", name, exc)
    for name in MIDDLEWARE_MODULES:
        try:
            importlib.import_module(name).install(app)
            loaded.append(name)
        except Exception as exc:
            log.critical("middleware %s not loaded (%s): refusing every "
                         "state-changing request", name, exc)
            _fail_closed(app, name)
    return loaded


__all__ = ["ROUTER_MODULES", "MIDDLEWARE_MODULES", "install"]
