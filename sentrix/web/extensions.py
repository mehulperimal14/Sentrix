"""Extension points of the Local Console app, so tracks add routes and
middleware without editing ``sentrix/app.py`` or ``sentrix/web/routes.py``.

``ROUTER_MODULES``: each module exports ``router: fastapi.APIRouter``; they are
included in this order after the core routers. ``MIDDLEWARE_MODULES``: each
exports ``install(app) -> None``. A module that fails to import is logged and
skipped, never fatal (a broken extension must not take the alarm down).
Owners: docs/7 §2.
"""

from __future__ import annotations

import importlib

from fastapi import FastAPI

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


def install(app: FastAPI) -> list[str]:
    """Include every extension router and middleware; returns what loaded."""
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
            log.error("middleware extension %s not loaded: %s", name, exc)
    return loaded


__all__ = ["ROUTER_MODULES", "MIDDLEWARE_MODULES", "install"]
