"""HTTP and WebSocket routes.

The security fix that mattered most is structural rather than a code change
here: ``var/`` is no longer under a ``StaticFiles`` mount. Previously the app
mounted ``/static`` on a directory that contained the evidence vault, alert
snapshots and enrolled face photographs, and FastAPI applies no authentication
to static mounts. Anyone who could reach the port could download:

* ``/static/alerts/evidence/*.json`` — every incident's scores and hashes
* ``/static/alerts/evidence/*.enc`` — the encrypted vault
* ``/static/alerts/*.jpg`` — plaintext incident snapshots
* ``/static/authorized_faces/*.jpg`` — enrolled biometric photographs

``docs/4`` stated that "all /api/* and /ws/* endpoints require a valid
HMAC-signed session cookie", which was true and the wrong scope.

Now ``/static`` serves only CSS, JS, images and the siren sound that ship with
the package, and every artifact is served through an authenticated route that
resolves paths inside a fixed base directory.

The same pages serve two audiences (docs/7 §9): the device admin on the LAN
and Portal users whose requests the link replays in-process under
``/d/<device_id>``. Every URL a page, a script or this module builds goes
through the base helper (:func:`base_url`, the ``url()`` Jinja global), so
links, fetches, images and the websocket work under both prefixes. Pages
answer 307 to ``/login`` when signed out; APIs and media answer 401 JSON
(``auth.require_lc``). Role rules for tunnelled users are enforced by the
link's path table; the pages only hide what a role cannot use
(:data:`ROLE_CAPABILITIES`).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import (APIRouter, Body, Depends, File, Form, HTTPException,
                     Request, UploadFile, status)
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               Response)
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from starlette.websockets import WebSocket, WebSocketDisconnect

from sentrix import mode as runtime_mode
from sentrix import services
from sentrix.config import settings
from sentrix.evidence.vault import vault
from sentrix.link import tunnel_auth
from sentrix.paths import FACES_DIR, SNAPSHOT_DIR, STATIC_DIR, TEMPLATES_DIR
from sentrix.pipeline.engine import audio_status, get_pipeline
from sentrix.pipeline.state import state
from sentrix.storage import database
from sentrix.utils.logging import get_logger, telemetry
from sentrix.utils.redact import redact_tree, redact_url
from sentrix.perception.identity import person_name
from sentrix.sos import current_service as sos_service
from sentrix.web import auth
from sentrix.web.hardening import websocket_origin_ok
from sentrix.web.security import (RateLimiter, generate_token, login_limiter,
                                  sanitize_filename, validate_image)

log = get_logger("routes")
router = APIRouter()

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
#: Events page size when the query does not say (the pre-M10 page showed 100).
EVENTS_PAGE_SIZE = 100
#: Incident types the filters accept (``IncidentRecord.incident_type`` values).
_TYPE = re.compile(r"^[a-z_]{1,32}$")
#: How often an open websocket re-checks that its session is still valid.
WS_REAUTH_SECONDS = 60.0

#: What each console role may do (docs/7 §10). The pages show only what the
#: role can use; tunnelled requests are *enforced* by the link's path table
#: (``sentrix/link/policy.py``) before they reach these routes, and are never
#: restated here. A Desk operator sees the dashboard and the live view only.
#: ``record`` (training recordings, D20) is the device admin's alone: the path
#: table refuses its routes to every tunnelled role.
ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "device_admin": frozenset({"pages", "media", "gallery_view", "gallery_write",
                               "verdict", "sos", "dispatch", "mode", "siren_test",
                               "record"}),
    "owner": frozenset({"pages", "media", "gallery_view", "gallery_write",
                        "verdict", "sos", "dispatch", "mode", "siren_test"}),
    "member": frozenset({"pages", "media", "gallery_view", "verdict", "sos",
                         "dispatch", "mode"}),
    "operator": frozenset(),
}

#: Navigation: (active_page key, path, label, capability needed or None).
NAV_ITEMS = (
    ("dashboard", "/dashboard", "Dashboard", None),
    ("live", "/live", "Live Feed", None),
    ("events", "/events", "Events", "pages"),
    ("alerts", "/alerts", "Alerts", "pages"),
    ("evidence", "/evidence", "Evidence", "pages"),
    ("dispatch", "/dispatch", "Dispatch", "pages"),
    ("sos", "/sos", "SOS", "pages"),
    ("authorized", "/authorized", "Access", "pages"),
    ("node", "/node", "Node", "pages"),
)


def _password() -> str:
    return os.getenv("SENTRIX_PASSWORD", "").strip()


def _secure_cookies() -> bool:
    return os.getenv("SENTRIX_SECURE_COOKIES", "").strip().lower() in ("1", "true", "yes")


def _test_mode() -> bool:
    """Decision D13 (display value). A function so tests can switch it."""
    return bool(settings.test_mode)


async def require_auth(request: Request) -> bool:
    """Every LC page: the device admin's session or a verified tunnel
    identity (``sentrix.web.auth.principal_of``). The principal is on
    ``request.state.lc_principal``.

    Unset password or no session: the browser goes to the login page, which
    says what to do, instead of a raw JSON 500 on the first page a newcomer
    opens. Tunnelled requests get 401 JSON instead (the Portal never rewrites
    a redirect to the device's login). APIs and media use
    ``auth.require_lc()`` and answer 401 JSON.
    """
    principal = auth.principal_of(request)
    if principal is None:
        raise auth.unauthenticated(request, legacy_redirect=True)
    request.state.lc_principal = principal
    return True


def _principal(request: Request) -> auth.LcPrincipal:
    principal = getattr(request.state, "lc_principal", None)
    if principal is None:                 # a route without require_auth
        raise auth.unauthenticated(request)
    return principal


def _safe_path(base: Path, name: str) -> Path:
    """Resolve ``name`` strictly inside ``base``, or 404.

    Belt and braces alongside ``sanitize_filename``: this catches symlinks and
    any traversal the string sanitiser misses.
    """
    candidate = (base / sanitize_filename(name)).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return candidate


# ── URLs, redaction and the page context ──────────────────────────────────────
def base_url(request: Any, path: str) -> str:
    """``path`` under this request's prefix: ``/d/<id>/x`` through the Portal,
    ``/x`` on the LAN (docs/7 §9.5). Every URL built in Python goes through
    here; templates use the ``url()`` global, scripts ``Sentrix.url()``."""
    if not path.startswith("/"):
        path = "/" + path
    return tunnel_auth.base_for(request) + path


def media_url(request: Any, kind: str, name: str) -> str:
    """``/media/<kind>/<name>`` (name URL-encoded) under the request's prefix."""
    return base_url(request, f"/media/{kind}/{quote(str(name), safe='')}")


def redacted(value: Any) -> Any:
    """``value`` with credentials removed from every string in it (camera
    URLs such as ``rtsp://user:pw@host``, secret query values), recursively.
    Applied to every console JSON output (docs/7 §12)."""
    return redact_tree(value)


def capabilities(principal: auth.LcPrincipal | None) -> frozenset[str]:
    if principal is None:
        return frozenset()
    return ROLE_CAPABILITIES.get(principal.role, frozenset())


def call_target() -> dict[str, str]:
    """The "Call 112 now" button (docs/7 §7.10, D13). Outside test mode it
    dials 112; in test mode it never does: it dials the SENTRIX Desk number
    (``state.sos.desk_number`` or the profile's), or is disabled when none is
    configured. Built here, so no script ever holds an emergency number."""
    if not _test_mode():
        return {"href": "tel:112", "note": ""}
    desk = (state.get().get("sos") or {}).get("desk_number")
    if not desk:
        try:
            from sentrix.link import profile
            desk = profile.desk_number()
        except Exception as exc:                # the button must still render
            log.debug("desk number unavailable: %s", exc)
            desk = None
    from sentrix.alerting.guard import is_emergency_number
    number = re.sub(r"[^\d+]", "", str(desk or ""))
    if number and not is_emergency_number(number):
        return {"href": f"tel:{number}",
                "note": f"Test mode: this calls the SENTRIX Desk ({number}), not 112."}
    return {"href": "", "note": "Test mode: no SENTRIX Desk number is configured "
                                "(SENTRIX_DESK_NUMBER), so this button is disabled."}


@lru_cache(maxsize=256)
def _asset_version(path: str, mtime_ns: int) -> str:
    return hashlib.sha256(f"{path}:{mtime_ns}".encode()).hexdigest()[:10]


def static_path(path: str) -> str:
    """``/static/<path>?v=<stamp>``: the Portal caches static files for an
    hour, so a new build must change the URL."""
    path = path.lstrip("/")
    try:
        stamp = _asset_version(path, (STATIC_DIR / path).stat().st_mtime_ns)
    except OSError:
        return f"/static/{path}"
    return f"/static/{path}?v={stamp}"


@pass_context
def _jinja_url(context, path: str) -> str:
    if not str(path).startswith("/"):
        path = "/" + str(path)
    return f"{context.get('base', '')}{path}"


@pass_context
def _jinja_static_url(context, path: str) -> str:
    return f"{context.get('base', '')}{static_path(path)}"


def _detection_label(item: Any) -> str:
    """Evidence "Objects" cell: ``person 0.82`` instead of a raw dict."""
    if isinstance(item, dict):
        label = str(item.get("label") or "object")
        conf = item.get("conf")
        try:
            return f"{label} {float(conf):.2f}"
        except (TypeError, ValueError):
            return label
    text = str(item)
    match = re.search(r"label=['\"]?([\w -]+)['\"]?.*?conf=([\d.]+)", text)
    if match:
        return f"{match.group(1)} {float(match.group(2)):.2f}"
    return text[:40]


def _actor_label(ref: str | None) -> str:
    """``verdict_by`` / ``sent_by`` for people: who, not an audit string."""
    if not ref:
        return ""
    if ref == "lc:device_admin":
        return "device admin"
    kind, _, ident = ref.partition(":")
    names = {"portal": "Portal user", "desk": "SENTRIX Desk", "contact": "contact"}
    return f"{names.get(kind, kind)} {ident[:8]}".strip()


def _recipient_list(items: Any) -> str:
    """``sent_to`` (masked addresses, or dicts carrying one) as one line."""
    from sentrix.utils.redact import mask_recipient
    shown = []
    for item in items or []:
        if isinstance(item, dict):
            item = (item.get("masked") or item.get("recipient_masked")
                    or mask_recipient(item.get("address")) or item.get("ref") or "")
        if item:
            shown.append(str(item))
    return ", ".join(shown)


def _console_context(request: Request) -> dict[str, Any]:
    """Request-scoped values every template reads (a context processor)."""
    principal = getattr(request.state, "lc_principal", None) or auth.principal_of(request)
    nonce = request.scope.get("csp_nonce") or ""
    return {
        "base": tunnel_auth.base_for(request),
        "csp_nonce": nonce,
        "csrf_token": (principal.csrf or "") if principal else "",
        "principal": principal,
        "role": principal.role if principal else None,
        "tunnelled": bool(principal and principal.kind == "tunnel"),
        "caps": capabilities(principal),
        "test_mode": _test_mode(),
    }


templates = Jinja2Templates(directory=str(TEMPLATES_DIR),
                            context_processors=[_console_context])
templates.env.globals.update(url=_jinja_url, static_url=_jinja_static_url,
                             nav_items=NAV_ITEMS)
templates.env.filters["detection"] = _detection_label
templates.env.filters["actor_label"] = _actor_label
templates.env.filters["recipient_list"] = _recipient_list


def render(request: Request, name: str, page: str, status_code: int = 200,
           **context: Any) -> HTMLResponse:
    """A console page: ``page`` is the nav key (``active_page``), so the
    highlight does not depend on the URL prefix."""
    live = state.get()
    context.update(request=request, active_page=page, call=call_target(),
                   sos=live.get("sos") or {}, remote_viewers=live.get("remote_viewers", 0),
                   channels_warn=bool((live.get("channels") or {}).get("warn")))
    return templates.TemplateResponse(request=request, name=name, context=context,
                                      status_code=status_code)


# ── auth ──────────────────────────────────────────────────────────────────────
def _login_page(request: Request, error: str | None = None,
                status_code: int = 200) -> HTMLResponse:
    context: dict[str, Any] = {"request": request}
    if error:
        context["error"] = error
    return templates.TemplateResponse(request=request, name="login.html",
                                      context=context, status_code=status_code)


@router.get("/", response_class=RedirectResponse)
async def root() -> RedirectResponse:
    # Redirects stay unprefixed: the Portal rewrites "Location: /x" itself.
    return RedirectResponse(url="/dashboard")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    if not _password():
        return _login_page(request, "No password is configured. Set SENTRIX_PASSWORD in "
                                    ".env (copy .env.example) and restart SENTRIX.")
    return _login_page(request)


@router.post("/login")
async def login(request: Request, password: str = Form(...)):
    client = request.client.host if request.client else "unknown"
    if not login_limiter.allow(client):
        log.warning("login rate limit hit from %s", client)
        return _login_page(request, "Too many attempts. Try again shortly.", 429)

    expected = _password()
    if not expected:
        return _login_page(request, "No password is configured. Set SENTRIX_PASSWORD "
                                    "in .env (copy .env.example) and restart SENTRIX.", 500)

    if not hmac.compare_digest(password.encode(), expected.encode()):
        return _login_page(request, "Invalid password", 401)

    login_limiter.reset(client)
    response = RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    response.set_cookie(settings.web.cookie_name, generate_token(expected), httponly=True,
                        secure=_secure_cookies(), samesite="lax", max_age=43_200)
    response.delete_cookie(auth.LEGACY_COOKIE)
    return response


@router.get("/logout", response_class=HTMLResponse)
async def logout_page(request: Request):
    """A confirmation page: signing out is a POST (a link must not log
    anyone out on someone else's behalf)."""
    principal = auth.principal_of(request)
    if principal is None:
        return RedirectResponse(url="/login")
    request.state.lc_principal = principal
    return render(request, "logout.html", "logout")


@router.post("/logout")
async def logout(request: Request):
    """Ends the LC session (the hardening checked the CSRF header)."""
    if auth.wants_json(request):
        response: Response = JSONResponse({"ok": True, "redirect": "/login"})
    else:
        response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(settings.web.cookie_name)
    response.delete_cookie(auth.LEGACY_COOKIE)
    return response


# ── pages ─────────────────────────────────────────────────────────────────────
@router.get("/dashboard", response_class=HTMLResponse,
            dependencies=[Depends(require_auth)])
async def dashboard(request: Request) -> HTMLResponse:
    return render(request, "dashboard.html", "dashboard",
                  score_keys=("vision", "audio", "motion", "behaviour", "identity",
                              "weapon", "fire", "theft", "harmful", "is_night"))


@router.get("/live", response_class=HTMLResponse,
            dependencies=[Depends(require_auth)])
async def live(request: Request) -> HTMLResponse:
    return render(request, "live.html", "live")


def _local_day(value: str | None, *, end: bool) -> datetime | None:
    """A ``YYYY-MM-DD`` in the device's time zone as a naive UTC bound
    (incidents are stored naive UTC); ``end`` is the next day's midnight."""
    if not value:
        return None
    try:
        day = date.fromisoformat(value)
    except ValueError:
        return None
    if end:
        day = day + timedelta(days=1)
    local = datetime.combine(day, time.min).astimezone()
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def incident_filters(*, incident_type: str | None = None, level: int | str | None = None,
                     verdict: str | None = None, since: str | None = None,
                     until: str | None = None) -> dict[str, Any]:
    """Validated filters; anything malformed is dropped, never an error."""
    filters: dict[str, Any] = {}
    if incident_type and _TYPE.match(incident_type):
        filters["type"] = incident_type
    try:
        if level not in (None, "") and 1 <= int(level) <= 5:
            filters["level"] = int(level)
    except (TypeError, ValueError):
        pass
    if verdict and verdict.upper() in ("TRUE", "FALSE", "NONE"):
        filters["verdict"] = verdict.upper()
    for key, value, end in (("since", since, False), ("until", until, True)):
        bound = _local_day(value, end=end)
        if bound is not None:
            filters[key] = value
            filters[f"_{key}_utc"] = bound
    return filters


def query_incidents(filters: dict[str, Any], limit: int = EVENTS_PAGE_SIZE,
                    offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """A page of incidents (newest first) and the number that match
    (``database.recent_incidents`` / ``count_incidents``, limit <= 500)."""
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    if not any(not key.startswith("_") for key in filters):
        stats = database.incident_stats()
        return database.recent_incidents(limit, offset), int(stats.get("total") or 0)
    where = {"incident_type": filters.get("type"), "min_level": filters.get("level"),
             "verdict": filters.get("verdict"), "since": filters.get("_since_utc"),
             "until": filters.get("_until_utc")}
    return database.recent_incidents(limit, offset, **where), database.count_incidents(**where)


def owner_packages(packages: list[dict]) -> list[dict]:
    """Dispatch packages as the household may see them (docs/7 §7.8): a
    duress request's package looks like a decline's (``dispatch.owner_view``),
    and a silent request's package (duress at a disarm or on a closed request)
    is not shown at all, as in the SOS history."""
    from sentrix.alerting.dispatch import owner_view
    from sentrix.storage import requests
    shown = []
    cache: dict[str, dict | None] = {}
    for package in packages:
        request_id = package.get("request_id")
        request = None
        if request_id:
            if request_id not in cache:
                try:
                    cache[request_id] = requests.get_request(request_id)
                except Exception as exc:
                    log.error("request lookup failed: %s", exc)
                    cache[request_id] = None
            request = cache[request_id]
        flags = (request or {}).get("flags") or {}
        if flags.get("silent"):
            continue
        shown.append(owner_view(package, request) if request else package)
    return shown


def request_views(request_ids: list[str | None]) -> dict[str, dict[str, Any]]:
    """Owner view (duress hidden) of the assistance requests these rows link
    to: ``{id: {short_id, state, label}}``."""
    ids = sorted({rid for rid in request_ids if rid})
    if not ids:
        return {}
    from sentrix.sos.states import owner_view
    from sentrix.storage.models import AssistanceRequestRecord
    views: dict[str, dict[str, Any]] = {}
    try:
        with database.session_scope() as session:
            for row in (session.query(AssistanceRequestRecord)
                        .filter(AssistanceRequestRecord.id.in_(ids)).all()):
                shown = owner_view(row.state, json.loads(row.flags_json or "{}"))
                views[row.id] = {"short_id": f"SX-{row.id[:6].upper()}",
                                 "state": shown["state"], "label": shown["label"]}
    except Exception as exc:
        log.error("request lookup failed: %s", exc)
    return views


def _page_links(request: Request, path: str, filters: dict[str, Any], offset: int,
                limit: int, total: int) -> dict[str, str | None]:
    params = {key: value for key, value in filters.items() if not key.startswith("_")}
    if "type" in params:
        params["incident_type"] = params.pop("type")
    if limit != EVENTS_PAGE_SIZE:
        params["limit"] = limit

    def link(at: int) -> str:
        query = urlencode({**params, **({"offset": at} if at else {})})
        return base_url(request, path) + (f"?{query}" if query else "")

    return {"prev": link(max(0, offset - limit)) if offset > 0 else None,
            "next": link(offset + limit) if offset + limit < total else None}


@router.get("/events", response_class=HTMLResponse,
            dependencies=[Depends(require_auth)])
async def events(request: Request, incident_type: str | None = None,
                 level: str | None = None, verdict: str | None = None,
                 since: str | None = None, until: str | None = None,
                 offset: int = 0, limit: int = EVENTS_PAGE_SIZE) -> HTMLResponse:
    filters = incident_filters(incident_type=incident_type, level=level,
                               verdict=verdict, since=since, until=until)
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    rows, total = await asyncio.to_thread(query_incidents, filters, limit, offset)
    for row in rows:
        if row.get("snapshot_path"):
            row["snapshot_url"] = media_url(request, "snapshot", row["snapshot_path"])
        if row.get("evidence_bundle"):
            row["evidence_url"] = media_url(request, "evidence", row["evidence_bundle"])
    requests_by_id = request_views([row.get("request_id") for row in rows])
    return render(request, "events.html", "events", events=rows,
                  stats=database.incident_stats(), filters=filters, total=total,
                  offset=offset, limit=limit, requests=requests_by_id,
                  pages=_page_links(request, "/events", filters, offset, limit, total),
                  sos_url=base_url(request, "/sos"))


@router.get("/alerts", response_class=HTMLResponse,
            dependencies=[Depends(require_auth)])
async def alerts(request: Request) -> HTMLResponse:
    images = [{"url": media_url(request, "snapshot", p.name), "name": p.name}
              for p in sorted(SNAPSHOT_DIR.glob("*.jpg"), reverse=True)[:120]]
    return render(request, "alerts.html", "alerts", images=images)


def _camera_rooms() -> dict[str, str]:
    """camera_id -> room label from the household profile."""
    try:
        from sentrix.link import profile
        return {str(c.get("camera_id")): str(c.get("room_label"))
                for c in profile.effective().get("cameras") or []
                if isinstance(c, dict) and c.get("camera_id") and c.get("room_label")}
    except Exception as exc:
        log.debug("camera labels unavailable: %s", exc)
        return {}


@router.get("/evidence", response_class=HTMLResponse,
            dependencies=[Depends(require_auth)])
async def evidence(request: Request) -> HTMLResponse:
    items = await asyncio.to_thread(vault.list_records, 200)
    chain = await asyncio.to_thread(vault.verify_chain)
    for item in items:
        # vault.view_url is device-relative; prefix it for the tunnel.
        item["view_url"] = media_url(request, "evidence", item.get("bundle_id", ""))
    return render(request, "evidence.html", "evidence", items=items, chain=chain,
                  rooms=_camera_rooms())


@router.get("/dispatch", response_class=HTMLResponse,
            dependencies=[Depends(require_auth)])
async def dispatch_page(request: Request) -> HTMLResponse:
    from sentrix.alerting.dispatch import AUTHORITIES, SENDABLE_STATUSES
    packages = owner_packages(database.all_dispatches())
    return render(request, "dispatch.html", "dispatch", packages=packages,
                  requests=request_views([p.get("request_id") for p in packages]),
                  authorities=AUTHORITIES, sendable=SENDABLE_STATUSES,
                  sos_url=base_url(request, "/sos"))


@router.get("/authorized", response_class=HTMLResponse,
            dependencies=[Depends(require_auth)])
async def authorized(request: Request) -> HTMLResponse:
    pipeline = get_pipeline()
    enrolled = pipeline.identity.enrolled_files
    faces = [{"filename": p.name, "url": media_url(request, "face", p.name),
              "person": person_name(p.stem), "enrolled": p.name in enrolled}
             for p in (sorted(FACES_DIR.iterdir()) if FACES_DIR.is_dir() else [])
             if p.suffix.lower() in (".jpg", ".jpeg", ".png")]
    return render(request, "authorized.html", "authorized", faces=faces,
                  backend=pipeline.identity.backend)


# ── authenticated media ───────────────────────────────────────────────────────
@router.get("/media/snapshot/{name}", dependencies=[Depends(auth.require_lc())])
async def media_snapshot(name: str) -> Response:
    path = _safe_path(SNAPSHOT_DIR, name)
    return Response(path.read_bytes(), media_type="image/jpeg")


@router.get("/media/face/{name}", dependencies=[Depends(auth.require_lc())])
async def media_face(name: str) -> Response:
    path = _safe_path(FACES_DIR, name)
    suffix = path.suffix.lower()
    return Response(path.read_bytes(),
                    media_type="image/png" if suffix == ".png" else "image/jpeg")


@router.get("/media/evidence/{bundle_id}", dependencies=[Depends(auth.require_lc())])
async def media_evidence(bundle_id: str) -> Response:
    """Decrypt one evidence bundle for authenticated review."""
    payload = vault.decrypt(sanitize_filename(bundle_id))
    if payload is None:
        raise HTTPException(status_code=404,
                            detail="Bundle not found or key unavailable")
    return Response(payload, media_type="image/jpeg")


# ── API ───────────────────────────────────────────────────────────────────────
@router.get("/api/metrics", dependencies=[Depends(auth.require_lc())])
async def api_metrics() -> JSONResponse:
    return JSONResponse(redacted(state.get()))


def _recording_brief(pipeline: Any) -> dict:
    """Is a training recording running (D20)? Read from the recorder only if
    the dashboard already built it: health never creates one. Never the
    recording's name: it carries the admin's free-text label, and this
    payload also goes to the Portal as the link's ``hello.health``."""
    recorder = getattr(pipeline, "recorder", None)
    if recorder is None or not hasattr(recorder, "active"):
        return {"active": False}
    try:
        return {"active": bool(recorder.active)}
    except Exception as exc:
        log.debug("recorder status failed: %s", exc)
        return {"active": False}


def health_payload() -> dict:
    """The ``/api/health`` fields (also the link's ``hello.health``). Redacted:
    no camera credentials, no addresses."""
    pipeline = get_pipeline()
    live = state.get()
    payload = {
        "camera": pipeline.source.online,
        # Redacted: rtsp://user:pw@host must never leave this route (docs/7 §12).
        "camera_source": redact_url(pipeline.source.name),
        "video_fps": live.get("video_fps", 0.0),
        "ai_hz": live.get("ai_hz", 0.0),
        "frame_age_ms": live.get("frame_age_ms"),
        "detector": pipeline.detector.available,
        "scene_model": pipeline.scene.weights_name,
        "scene_device": str(pipeline.scene.device) if pipeline.scene.available else None,
        "scene_thresholds": pipeline.scene.thresholds if pipeline.scene.available else None,
        "scene_runtime": (getattr(pipeline.scene, "runtime", None)
                          if pipeline.scene.available else None),
        "threat_sources": settings.threat.sources,
        "armed": runtime_mode.is_armed(),
        "violence_model_loaded": pipeline.violence is not None,
        "identity_backend": pipeline.identity.backend,
        "identity_model": getattr(pipeline.identity, "model_name", None),
        # True while no face backend works, i.e. nobody can be authorised now.
        "identity_failing_closed": pipeline.identity.backend is None,
        "enrolled_faces": len(pipeline.identity.enrolled),
        "audio": bool(pipeline.audio and pipeline.audio.available),
        # Which microphone, at what native rate (None with audio off).
        "microphone": audio_status(getattr(pipeline, "audio", None)),
        "recording": _recording_brief(pipeline),
        "fusion_source": pipeline.tci.model_source,
        "siren_backend": pipeline.siren.name,
        "siren_is_real": pipeline.siren.is_real,
        "notifier_online": __import__(
            "sentrix.alerting.notifier", fromlist=["notifier"]).notifier.online,
        "evidence_key_persistent": vault.persistent,
        "session": pipeline.session_id,
        "latency": telemetry.stats(),
        # services, send_guard, test_mode, mode (docs/7 §9.3)
        **services.health_fields(),
    }
    # Service health blocks are written by other tracks: redact them too.
    return redacted(payload)


@router.get("/api/health", dependencies=[Depends(auth.require_lc())])
async def api_health() -> JSONResponse:
    return JSONResponse(health_payload())


@router.get("/api/config", dependencies=[Depends(auth.require_lc())])
async def api_config() -> JSONResponse:
    """Expose the live tunables so the dashboard shows the real thresholds."""
    return JSONResponse(redacted(settings.to_dict()))


@router.get("/api/incidents", dependencies=[Depends(auth.require_lc())])
async def api_incidents(limit: int = 100, offset: int = 0,
                        incident_type: str | None = None, level: str | None = None,
                        verdict: str | None = None, since: str | None = None,
                        until: str | None = None) -> JSONResponse:
    """Newest first; ``limit`` <= 500, page with ``offset``; optional
    filters ``incident_type``, ``level`` (minimum peak level), ``verdict``
    (TRUE / FALSE / NONE) and ``since`` / ``until`` (dates, device time)."""
    filters = incident_filters(incident_type=incident_type, level=level,
                               verdict=verdict, since=since, until=until)
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    rows, total = await asyncio.to_thread(query_incidents, filters, limit, offset)
    return JSONResponse(redacted({
        "incidents": rows, "stats": database.incident_stats(),
        "limit": limit, "offset": offset, "total": total,
        "filters": {k: v for k, v in filters.items() if not k.startswith("_")}}))


@router.post("/api/incidents/{incident_id}/verdict",
             dependencies=[Depends(auth.require_lc())])
async def api_verdict(request: Request, incident_id: str, verdict: str = Form(...),
                      note: str = Form(None), pin: str = Form(None)) -> JSONResponse:
    """Operator feedback: was this a real threat?

    Every labelled incident is a training row for the fusion model, which is
    the dataset the retraining notebook could not find. "False alarm" goes
    through the SOS service (docs/7 §7.7): with an open request it needs the
    SOS PIN, and the duress PIN quietly asks for help.
    """
    principal = _principal(request)
    actor = principal.to_actor()
    incident_id = sanitize_filename(incident_id)
    verdict = (verdict or "").upper()
    note = (note or "").strip()[:500] or None
    if verdict == "FALSE":
        result = await asyncio.to_thread(sos_service().false_verdict, incident_id,
                                         pin=pin, actor=actor, channel=principal.channel,
                                         note=note)
        if not result["ok"]:
            raise auth.LcError(result["code"], result["message"])
        return JSONResponse({"status": "ok", "verdict": "FALSE", "by": actor.ref})
    ok = database.set_verdict(incident_id, verdict, note, by=actor.ref,
                              channel=principal.channel,
                              actor_type=actor.event_actor_type)
    if not ok:
        raise HTTPException(status_code=400, detail="Unknown incident or verdict")
    return JSONResponse({"status": "ok", "verdict": verdict, "by": actor.ref})


@router.get("/api/evidence/verify", dependencies=[Depends(auth.require_lc())])
async def api_verify_chain() -> JSONResponse:
    return JSONResponse(await asyncio.to_thread(vault.verify_chain))


@router.get("/api/dispatch/latest", dependencies=[Depends(auth.require_lc())])
async def api_dispatch_latest(request_id: str | None = None) -> JSONResponse:
    """The newest PENDING package, or with ``request_id`` that request's
    package in any status (the SOS panel keeps its address after approval).
    Owner view: a duress package looks declined, a silent one is absent."""
    package = (database.dispatch_for_request(request_id[:64]) if request_id
               else database.latest_pending_dispatch())
    shown = owner_packages([package] if package else [])
    return JSONResponse(redacted(shown[0] if shown else {}))


@router.post("/api/dispatch/{package_id}/send/{authority}",
             dependencies=[Depends(auth.require_lc())])
async def api_dispatch_send(request: Request, package_id: str,
                            authority: str) -> JSONResponse:
    """"Send now". A package of an assistance request approves that request
    (the SOS service); otherwise the guarded legacy send. 409 for a package
    that is already SENT, CANCELLED or EXPIRED."""
    from sentrix.alerting.dispatch import AUTHORITIES
    if authority.upper() not in AUTHORITIES:
        raise HTTPException(status_code=400,
                            detail=f"authority must be one of {', '.join(AUTHORITIES)}")
    principal = _principal(request)
    package_id = sanitize_filename(package_id)
    result = await asyncio.to_thread(sos_service().approve_package, package_id,
                                     authority.upper(), actor=principal.to_actor(),
                                     channel=principal.channel)
    if not result["ok"]:
        raise auth.LcError(result["code"], result["message"])
    package = database.get_dispatch(package_id) or {}
    return JSONResponse({"status": "sent" if package.get("status") == "SENT" else "failed",
                         "message": result["message"],
                         "detail": package.get("error") or result["message"]})


@router.post("/api/mode", dependencies=[Depends(auth.require_lc())])
async def api_mode(request: Request, armed: bool = Body(..., embed=True),
                   pin: str | None = Body(None, embed=True)) -> JSONResponse:
    """HOME/ARMED (docs/7 §9.2). Arming needs no PIN; disarming needs the SOS
    PIN (the duress PIN disarms visibly and opens a silent request)."""
    principal = _principal(request)
    result = await asyncio.to_thread(sos_service().set_mode, bool(armed), pin=pin,
                                     actor=principal.to_actor())
    if not result["ok"]:
        raise auth.LcError(result["code"], result["message"])
    return JSONResponse({"mode": runtime_mode.snapshot()})


# ── enrolment ─────────────────────────────────────────────────────────────────
capture_limiter = RateLimiter(max_attempts=20, window_seconds=60.0)


def _enrol_filename(person: str, suffix: str = ".jpg") -> str:
    """A free file name for another photo of ``person``: alice.jpg, alice__2.jpg ..."""
    stem = sanitize_filename(person).replace(" ", "_").strip("._-")[:60] or "person"
    candidate, n = f"{stem}{suffix}", 2
    while (FACES_DIR / candidate).exists():
        candidate, n = f"{stem}__{n}{suffix}", n + 1
    return candidate


async def _enrol_image(image, person: str, payload: bytes | None, suffix: str,
                       via: str, by: str) -> JSONResponse:
    """Assess, store and enrol one image; the gallery is updated in place."""
    pipeline = get_pipeline()
    check = await asyncio.to_thread(pipeline.identity.assess, image, person)
    if not check.ok:
        pipeline.events.audit("gallery_rejected", person=person, via=via,
                              reason=check.reason, by=by)
        return JSONResponse({"status": "rejected", "person": person,
                             "detail": check.reason}, status_code=422)
    FACES_DIR.mkdir(parents=True, exist_ok=True)
    filename = _enrol_filename(person, suffix)
    if payload is None:
        import cv2
        ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
        if not ok:
            raise HTTPException(status_code=500, detail="could not encode the frame")
        payload = buffer.tobytes()
    (FACES_DIR / filename).write_bytes(payload)
    name = pipeline.identity.enrol(filename, check.embedding)
    pipeline.events.audit("gallery_enrolled", person=name, file=filename, via=via, by=by)
    log.info("enrolled %s from %s (%s)", name, via, filename)
    return JSONResponse({"status": "ok", "person": name, "filename": filename})


@router.post("/authorized/upload", dependencies=[Depends(auth.require_lc())])
async def upload_face(request: Request, file: UploadFile = File(...),
                      person: str = Form(None)) -> JSONResponse:
    principal = _principal(request)
    payload = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 10 MB")

    ok, reason = validate_image(file.filename or "", file.content_type, payload)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)
    name = sanitize_filename(file.filename or "")
    if not name:
        raise HTTPException(status_code=400, detail="Invalid filename")

    import cv2
    import numpy as np
    image = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise HTTPException(status_code=400, detail="could not decode the image")
    chosen = sanitize_filename(person or "").replace(" ", "_").strip("._-")
    who = person_name(chosen or Path(name).stem)
    return await _enrol_image(image, who, payload, Path(name).suffix.lower(), "upload",
                              principal.actor)


@router.post("/api/authorized/capture", dependencies=[Depends(auth.require_lc())])
async def capture_face(request: Request, person: str = Form(...)) -> JSONResponse:
    """Enrol from the monitoring camera's current frame.

    The page used to ask the browser for the webcam, which the server already
    holds, so it failed with NotReadableError on the same machine.
    """
    principal = _principal(request)
    if not capture_limiter.allow(auth.rate_key(request)):
        raise HTTPException(status_code=429, detail="Too many captures; wait a minute")
    who = person_name(sanitize_filename(person).replace(" ", "_").strip("._-"))
    if not who:
        raise HTTPException(status_code=400, detail="Enter a name first")
    frame = get_pipeline().latest_raw()
    if frame is None:
        raise HTTPException(status_code=409, detail="No camera frame available")
    return await _enrol_image(frame, who, None, ".jpg", "camera", principal.actor)


@router.get("/api/authorized/preview.jpg", dependencies=[Depends(auth.require_lc())])
async def capture_preview() -> Response:
    """The camera frame enrolment would capture, without the HUD."""
    import cv2
    frame = get_pipeline().latest_raw()
    if frame is None:
        raise HTTPException(status_code=404, detail="No camera frame available")
    ok, buffer = await asyncio.to_thread(cv2.imencode, ".jpg", frame,
                                         [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok:
        raise HTTPException(status_code=500, detail="encode failed")
    return Response(buffer.tobytes(), media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@router.delete("/api/authorized/{filename}", dependencies=[Depends(auth.require_lc())])
async def delete_face(request: Request, filename: str) -> JSONResponse:
    principal = _principal(request)
    path = _safe_path(FACES_DIR, filename)
    path.unlink()
    pipeline = get_pipeline()
    pipeline.identity.remove(path.name)
    pipeline.events.audit("gallery_removed", person=person_name(path.stem), file=path.name,
                          by=principal.actor)
    return JSONResponse({"status": "ok"})


# ── websocket ─────────────────────────────────────────────────────────────────
def ws_payload() -> str:
    """The live state as the websocket sends it: redacted, never raising."""
    try:
        return json.dumps(redacted(state.get()), default=str)
    except (TypeError, ValueError):
        return state.ws_payload()


@router.websocket("/ws/threat")
async def ws_threat(websocket: WebSocket) -> None:
    """Live state at ``telemetry_fps``. A foreign ``Origin`` is refused at the
    handshake (cross-site websocket hijacking); a missing or expired session
    is accepted and closed with 1008, so the page can say "sign in again"
    instead of retrying forever."""
    if not websocket_origin_ok(websocket.headers):
        log.warning("websocket refused: foreign origin %s", websocket.headers.get("origin"))
        await websocket.close(code=1008)
        return
    principal = auth.principal_of(websocket)
    await websocket.accept()
    if principal is None:
        await websocket.close(code=1008)
        return
    interval = 1.0 / max(settings.pipeline.telemetry_fps, 0.5)
    checked = _time.monotonic()
    try:
        while True:
            if _time.monotonic() - checked >= WS_REAUTH_SECONDS:
                checked = _time.monotonic()
                if auth.principal_of(websocket) is None:
                    await websocket.close(code=1008)
                    return
            await websocket.send_text(ws_payload())
            await asyncio.sleep(interval)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.debug("websocket closed: %s", exc)


__all__ = ["router", "require_auth", "health_payload", "render", "base_url",
           "media_url", "redacted", "capabilities", "call_target", "query_incidents",
           "incident_filters", "request_views", "ROLE_CAPABILITIES", "templates"]
