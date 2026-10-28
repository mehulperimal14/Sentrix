"""The Portal's single-page app (docs/9_FRONTEND.md, decision D22).

``frontend/apps/portal`` builds into ``static/app/``; its hashed assets are
served by the ``/static`` mount and every app path gets ``index.html``. This
router must be included LAST: API, tunnel, hook and link routes win because
they are registered first, and the paths below are never answered with the app
so a typo in an API path stays a JSON 404.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from sentrix_portal.routes.device_proxy import TunnelRoute

#: Its routes yield to every other route (a partial match, like the tunnel's
#: catch-all), so the tunnel and the API always win over the app.
router = APIRouter(route_class=TunnelRoute)

APP_DIR = Path(__file__).resolve().parent.parent / "static" / "app"
INDEX = APP_DIR / "index.html"

#: First path segments that belong to the server, never to the app.
SERVER_PREFIXES = ("api", "d", "hooks", "static", "healthz", "link", "openapi.json",
                   "docs", "redoc", "favicon.ico")

_NO_STORE = {"Cache-Control": "no-cache, must-revalidate"}


def app_index() -> FileResponse | JSONResponse:
    if not INDEX.is_file():
        return JSONResponse({"error": {"code": "not_built",
                                       "message": "The Portal app has not been built "
                                                  "(cd frontend && npm run build)."}},
                            status_code=503)
    return FileResponse(INDEX, media_type="text/html", headers=_NO_STORE)


@router.get("/", include_in_schema=False, response_model=None)
async def spa_root() -> FileResponse | JSONResponse:
    return app_index()


@router.get("/{path:path}", include_in_schema=False, response_model=None)
async def spa_path(path: str, request: Request) -> FileResponse | JSONResponse:
    first = path.split("/", 1)[0]
    if first in SERVER_PREFIXES:
        return JSONResponse({"error": {"code": "not_found", "message": "Not found."}},
                            status_code=404)
    return app_index()


@router.api_route("/{path:path}", methods=["POST", "PUT", "PATCH", "DELETE"],
                  include_in_schema=False, response_model=None)
async def spa_write(path: str) -> JSONResponse:
    """A write to a path nothing claims is a JSON 404, never the app."""
    return JSONResponse({"error": {"code": "not_found", "message": "Not found."}},
                        status_code=404)
