"""Portal application factory.

``create_app(settings)`` builds a self-contained app: its own database (tables
created on build, idempotent; missing columns added), templates, static
files, every router listed in ``sentrix_portal.routes.ROUTER_MODULES``, the
middleware in :data:`MIDDLEWARE_MODULES`, ``GET /healthz`` and a lifespan that
starts :data:`BACKGROUND_MODULES` when ``settings.workers`` (``PORTAL_WORKERS=1``;
off in tests). Tests pass ``PortalSettings.for_tests(tmp_path)``.

Extension contracts (docs/7 §4):

* A background module exports ``start(app) -> stop``. ``start`` runs on the
  event loop inside the lifespan (it may ``asyncio.create_task``; it reads
  ``app.state.db`` and ``app.state.settings``) and returns ``stop``, a
  callable that may return an awaitable; stops run in reverse order. A module
  that does not exist yet is skipped; one that fails to start is logged and
  skipped (the Portal keeps serving pages).
* A middleware module exports ``install(app)``. It must set
  ``request.state.csp_nonce`` for templates. A failure is fatal: the Portal
  faces the Internet and must not start without its headers.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from sentrix_portal import settings as portal_settings
from sentrix_portal.db import Database
from sentrix_portal.routes import ROUTER_MODULES
from sentrix_portal.settings import PortalSettings

log = logging.getLogger("sentrix_portal.app")

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

#: Started in this order when ``settings.workers``; owners in docs/7 §2.
BACKGROUND_MODULES: tuple[str, ...] = (
    "sentrix_portal.services.hub",        # T-LINK  heartbeat watcher: device offline after 45 s
    "sentrix_portal.services.sos",        # T-LINK  cloud SOS timers while a device is offline
    "sentrix_portal.services.notify",     # T-CH    notification worker, Telegram polling
    "sentrix_portal.services.retention",  # T-ACCT  retention caps (snapshots 30 d, audit 1 y, ...)
)
#: Installed in this order; failure is fatal.
MIDDLEWARE_MODULES: tuple[str, ...] = (
    "sentrix_portal.hardening",           # T-ACCT  CSP nonce + security headers (not on /d/*)
)


def _template_context(request) -> dict:
    return {"csp_nonce": getattr(request.state, "csp_nonce", "")}


async def _start_background(app: FastAPI) -> list[tuple[str, object]]:
    stops = []
    for name in BACKGROUND_MODULES:
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name and name.startswith(exc.name):
                continue
            log.error("background module %s: import failed: %s", name, exc)
            continue
        start = getattr(module, "start", None)
        if start is None:
            continue
        try:
            stop = start(app)
            if inspect.isawaitable(stop):
                stop = await stop
            stops.append((name, stop))
            log.info("background module %s started", name)
        except Exception as exc:
            log.error("background module %s failed to start: %s", name, exc)
    return stops


async def _stop_background(stops: list[tuple[str, object]]) -> None:
    for name, stop in reversed(stops):
        try:
            result = stop() if callable(stop) else None
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            log.error("background module %s failed to stop: %s", name, exc)


def _bootstrap_admin(app: FastAPI) -> None:
    """The first company admin of a deploy without a shell (``accounts.bootstrap_admin``):
    its activation link goes to stdout once, where only the deploy's owner reads it."""
    username = os.getenv("PORTAL_BOOTSTRAP_ADMIN", "").strip()
    if not username:
        return
    from sentrix_portal.services import accounts
    try:
        with app.state.db.session() as db:
            link = accounts.bootstrap_admin(db, username)
    except Exception as exc:
        log.error("PORTAL_BOOTSTRAP_ADMIN failed: %s", exc)
        return
    if link:
        print(f"SENTRIX Portal: company admin '{username.lower()}' - activate it once at "
              f"{link} (single use, 72 h; TOTP is set up at the first sign-in). "
              "Remove PORTAL_BOOTSTRAP_ADMIN afterwards.", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _bootstrap_admin(app)
    stops = await _start_background(app) if app.state.settings.workers else []
    app.state.background = [name for name, _ in stops]
    try:
        yield
    finally:
        await _stop_background(stops)


def create_app(settings: PortalSettings | None = None) -> FastAPI:
    settings = (settings or PortalSettings.from_env()).checked()
    portal_settings.set_current(settings)
    database = Database(settings.database_url)
    database.init()

    app = FastAPI(
        title="SENTRIX Portal",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.api_docs else None,
        redoc_url="/redoc" if settings.api_docs else None,
        openapi_url="/openapi.json" if settings.api_docs else None,
    )
    app.state.settings = settings
    app.state.db = database
    app.state.background = []
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR),
                                context_processors=[_template_context])
    templates.env.globals.update(test_mode=settings.test_mode,
                                 desk_number=settings.desk_number)
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    for name in ROUTER_MODULES:
        app.include_router(importlib.import_module(name).router)
    for name in MIDDLEWARE_MODULES:
        importlib.import_module(name).install(app)        # fatal on failure

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> JSONResponse:
        try:
            with database.session() as db:
                db.execute(text("SELECT 1"))
            db_state = "ok"
        except Exception:
            db_state = "error"
        return JSONResponse({"status": "ok" if db_state == "ok" else "degraded",
                             "db": db_state, "test_mode": settings.test_mode,
                             "version": app.version},
                            status_code=200 if db_state == "ok" else 503)

    return app


__all__ = ["create_app", "TEMPLATES_DIR", "STATIC_DIR", "BACKGROUND_MODULES",
           "MIDDLEWARE_MODULES", "lifespan"]
