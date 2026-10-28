"""The device app (docs/9_FRONTEND.md, decision D22).

``frontend/apps/device`` builds into ``static/app/``; its hashed assets are
served by the ``/static`` mount and every app path gets ``index.html``. Included
after the API routers so they always win; server paths below are never answered
with the app, so a wrong API path stays a JSON 404. The app itself asks
``/api/session`` and goes to its own sign-in when needed, so serving the shell
needs no authentication (it holds no data).

The shell is for the LAN only: owners and members use the Portal app, which
calls this device's JSON API through ``/d/{id}/api/...``; a tunnelled request
for an app path is a JSON 404 (the path table refuses it first anyway). The
hardening middleware gives the shell its CSP and security headers like every
other response; the build has no inline script or style, so ``'self'`` covers
all of it.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from sentrix.web import auth

router = APIRouter()

APP_DIR = Path(__file__).resolve().parent / "static" / "app"
INDEX = APP_DIR / "index.html"

SERVER_PREFIXES = ("api", "ws", "video", "media", "static", "openapi.json", "docs",
                   "redoc", "favicon.ico")

_NO_STORE = {"Cache-Control": "no-cache, must-revalidate"}


def _not_found() -> JSONResponse:
    return JSONResponse({"detail": "not found", "code": "not_found"}, status_code=404)


def app_index(request: Request) -> FileResponse | JSONResponse:
    if auth.is_tunnelled(request):
        return _not_found()
    if not INDEX.is_file():
        return JSONResponse({"detail": "The device app has not been built "
                                       "(cd frontend && npm run build).",
                             "code": "not_built"}, status_code=503)
    return FileResponse(INDEX, media_type="text/html", headers=_NO_STORE)


@router.get("/", include_in_schema=False, response_model=None)
async def spa_root(request: Request) -> FileResponse | JSONResponse:
    return app_index(request)


@router.get("/{path:path}", include_in_schema=False, response_model=None)
async def spa_path(request: Request, path: str) -> FileResponse | JSONResponse:
    if path.split("/", 1)[0] in SERVER_PREFIXES:
        return _not_found()
    return app_index(request)


@router.api_route("/{path:path}", methods=["POST", "PUT", "PATCH", "DELETE"],
                  include_in_schema=False, response_model=None)
async def no_such_action(path: str) -> JSONResponse:
    """A write to a path no route takes is a JSON 404, as before the app
    existed (not a 405 from the GET-only catch-all above)."""
    return _not_found()
