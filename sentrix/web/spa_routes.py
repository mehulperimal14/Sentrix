"""The device app (docs/9_FRONTEND.md, decision D22).

``frontend/apps/device`` builds into ``static/app/``; its hashed assets are
served by the ``/static`` mount and every app path gets ``index.html``. Included
after the API routers so they always win; server paths below are never answered
with the app, so a wrong API path stays a JSON 404. The app itself asks
``/api/session`` and goes to its own sign-in when needed, so serving the shell
needs no authentication (it holds no data).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

router = APIRouter()

APP_DIR = Path(__file__).resolve().parent / "static" / "app"
INDEX = APP_DIR / "index.html"

SERVER_PREFIXES = ("api", "ws", "video", "media", "static", "openapi.json", "docs",
                   "redoc", "favicon.ico")

_NO_STORE = {"Cache-Control": "no-cache, must-revalidate"}


def app_index() -> FileResponse | JSONResponse:
    if not INDEX.is_file():
        return JSONResponse({"detail": "The device app has not been built "
                                       "(cd frontend && npm run build).",
                             "code": "not_built"}, status_code=503)
    return FileResponse(INDEX, media_type="text/html", headers=_NO_STORE)


@router.get("/", include_in_schema=False)
async def spa_root() -> FileResponse | JSONResponse:
    return app_index()


@router.get("/{path:path}", include_in_schema=False)
async def spa_path(path: str) -> FileResponse | JSONResponse:
    if path.split("/", 1)[0] in SERVER_PREFIXES:
        return JSONResponse({"detail": "not found", "code": "not_found"}, status_code=404)
    return app_index()
