"""Portal application factory.

``create_app(settings)`` builds a self-contained app: its own database (tables
created on build, idempotent), templates, static files, every router listed in
``sentrix_portal.routes.ROUTER_MODULES`` and ``GET /healthz``. Tests pass a
``PortalSettings`` pointing at a temporary SQLite file.
"""

from __future__ import annotations

import importlib
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

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"


def create_app(settings: PortalSettings | None = None) -> FastAPI:
    settings = (settings or PortalSettings.from_env()).checked()
    portal_settings.set_current(settings)
    database = Database(settings.database_url)
    database.init()

    app = FastAPI(
        title="SENTRIX Portal",
        version="0.1.0",
        docs_url="/docs" if settings.api_docs else None,
        redoc_url="/redoc" if settings.api_docs else None,
        openapi_url="/openapi.json" if settings.api_docs else None,
    )
    app.state.settings = settings
    app.state.db = database
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.globals.update(test_mode=settings.test_mode,
                                 desk_number=settings.desk_number)
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    for name in ROUTER_MODULES:
        app.include_router(importlib.import_module(name).router)

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


__all__ = ["create_app", "TEMPLATES_DIR", "STATIC_DIR"]
