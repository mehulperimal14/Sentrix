"""The device console's JSON API for the React apps (docs/9 §3, §4).

Owner: the frontend v2 work (decision D22). Included by
``sentrix.web.extensions``. Everything the server-rendered pages only put into
HTML (who is signed in, the call button, the lists of the Events, Alerts,
Evidence, Dispatch and Access pages, the Wi-Fi setup page) is here as JSON.
The routes call the same functions the pages call; the existing ``/api/*``
paths are unchanged.

==========  ===========================  =========================  ====================
Method      Path                         Who                        Tunnel class
==========  ===========================  =========================  ====================
GET         /api/session                 anyone (signed out too)    read_api: owner, member
POST        /api/login                   LAN, signed out            never
POST        /api/logout                  LAN                        never
GET         /api/state                   any principal              read_api: owner, member
GET         /api/activity                any principal              read_api: owner, member
GET         /api/snapshots               device admin               lc_only
GET         /api/evidence                device admin               lc_only
GET         /api/dispatch                device admin               lc_only
GET         /api/authorized              device admin               lc_only
GET         /api/setup/wifi              LAN, setup mode            never
POST        /api/setup/wifi/scan         LAN, setup mode, code      never
POST        /api/setup/wifi/connect      LAN, setup mode, code      never
==========  ===========================  =========================  ====================

**Errors** (docs/9 §4): ``{"error": {"code", "message", "fields"?,
"retry_after"?}}``, never a redirect. :class:`ApiRoute` turns the console's
own errors (``auth.LcError``'s ``unauthorised`` → ``not_signed_in``,
``forbidden_role`` → ``forbidden``, ``invalid_args`` → ``validation``) and
FastAPI's validation errors into that envelope for these routes only; the
older ``/api/*`` routes keep ``{"detail", "code"}``. The hardening's CSRF
refusal (403, ``{"detail", "code": "csrf"}``) happens before any route.

**CSRF**: unchanged. On the LAN every state-changing request of a signed-in
browser carries ``X-CSRF-Token`` = ``session.csrf_token``, and a request the
browser marks cross-site is refused signed in or not (login CSRF); the
hardening middleware checks both before routing. Through the Portal, the
Portal checked its own token.

**Times** are ISO 8601 UTC with ``Z``. **Never** in a response: the console
password, session cookies, the setup code or its digest, the bootstrap secret,
the Wi-Fi passphrase, PIN hashes, keys.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException

from sentrix.config import settings
from sentrix.link import tunnel_auth
from sentrix.utils.logging import get_logger
from sentrix.web import api_models as M
from sentrix.web import auth
from sentrix.web import routes as console
from sentrix.web import setup_routes

log = get_logger("api_routes")

#: The household time zone when the profile names none (docs/9 §4).
DEFAULT_TIME_ZONE = "Asia/Kolkata"
#: The login limiter's window (``security.login_limiter``), for Retry-After.
LOGIN_RETRY_AFTER = 300
_SERIAL = re.compile(r"^[A-Za-z0-9-]{1,32}$")
_DEVICE_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")
_TIME_ZONE = re.compile(r"^[A-Za-z_]+(/[A-Za-z0-9_+-]+){0,2}$")

#: Console error codes (``sentrix.link.protocol.ERROR_CODES``) as docs/9 names them.
_CODES = {"unauthorised": "not_signed_in", "forbidden_role": "forbidden",
          "invalid_args": "validation", "unprocessable": "validation"}
#: A plain HTTPException's status as a docs/9 code.
_STATUS_CODES = {400: "validation", 401: "not_signed_in", 403: "forbidden", 404: "not_found",
                 405: "not_allowed", 409: "conflict", 413: "too_large", 422: "validation",
                 429: "rate_limited", 503: "unavailable"}
#: Messages for codes whose console text is meant for developers.
_MESSAGES = {"not_signed_in": "Sign in first.",
             "forbidden": "Your account cannot do this on this device.",
             "not_found": "Not found.",
             "step_up_required": "Confirm your password on the SENTRIX Portal first.",
             "consent_required": "The household has not allowed this."}


# ── errors ────────────────────────────────────────────────────────────────────
class ApiError(Exception):
    """A docs/9 error: HTTP status plus ``{"error": {...}}``."""

    def __init__(self, status: int, code: str, message: str, *,
                 fields: dict[str, str] | None = None, retry_after: int | None = None,
                 reason: str | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.fields, self.retry_after, self.reason = fields, retry_after, reason


def error_response(status: int, code: str, message: str, *,
                   fields: dict[str, str] | None = None, retry_after: int | None = None,
                   reason: str | None = None,
                   headers: dict[str, str] | None = None) -> JSONResponse:
    body: dict[str, Any] = {"code": code, "message": message}
    if fields:
        body["fields"] = fields
    if retry_after is not None:
        body["retry_after"] = int(retry_after)
        headers = {**(headers or {}), "Retry-After": str(int(retry_after))}
    if reason:
        body["reason"] = reason
    return JSONResponse({"error": body}, status_code=status, headers=headers)


def _field_errors(exc: RequestValidationError) -> dict[str, str]:
    """``{"password": "Field required"}``: the last name in each error's
    location, the message without pydantic's prefix. Never the input (it can
    be a password or a Wi-Fi passphrase)."""
    fields: dict[str, str] = {}
    for error in exc.errors():
        loc = [str(part) for part in error.get("loc", ()) if part not in ("body", "query",
                                                                           "path", "header")]
        name = loc[-1] if loc else "body"
        message = str(error.get("msg") or "Invalid value")
        message = re.sub(r"^(Value error|Assertion failed), ", "", message)
        fields.setdefault(name, message)
    return fields


def _from_lc(exc: auth.LcError) -> JSONResponse:
    code = _CODES.get(exc.code, exc.code)
    status = exc.status_code
    if code == "validation" and status == 400:
        status = 422
    message = _MESSAGES.get(code) or str(exc.detail or code)
    headers = dict(exc.headers or {})
    retry = headers.pop("Retry-After", None)
    return error_response(status, code, message, headers=headers or None,
                          retry_after=int(retry) if retry and str(retry).isdigit() else None)


class ApiRoute(APIRoute):
    """Every error of these routes in the docs/9 envelope, never a redirect."""

    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original(request)
            except ApiError as exc:
                return error_response(exc.status, exc.code, exc.message, fields=exc.fields,
                                      retry_after=exc.retry_after, reason=exc.reason)
            except RequestValidationError as exc:
                return error_response(422, "validation", "Check the highlighted fields.",
                                      fields=_field_errors(exc))
            except auth.LcError as exc:
                return _from_lc(exc)
            except StarletteHTTPException as exc:
                code = _STATUS_CODES.get(exc.status_code, "error")
                message = _MESSAGES.get(code) or (exc.detail if isinstance(exc.detail, str)
                                                  else "The request failed.")
                return error_response(exc.status_code, code, message,
                                      headers=dict(exc.headers or {}) or None)

        return handler


_ERRORS = {status: {"model": M.ApiErrorEnvelope}
           for status in (401, 403, 404, 409, 422, 429)}
router = APIRouter(route_class=ApiRoute, responses=_ERRORS, tags=["device app"])

#: Any console principal (LAN device admin, or a tunnelled Portal user).
signed_in = auth.require_lc()
#: The device admin on the LAN (the path table also refuses these to the tunnel).
device_admin = auth.require_lc("device_admin")


# ── helpers ───────────────────────────────────────────────────────────────────
def iso_z(value: Any) -> str | None:
    """A datetime, ISO string (naive = UTC) or epoch seconds as ISO 8601 UTC
    with ``Z``; None when there is nothing usable."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            moment = datetime.fromtimestamp(float(value), tz=timezone.utc)
        elif isinstance(value, datetime):
            moment = value
        else:
            moment = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _offset(cursor: str | None) -> int:
    """An opaque cursor (today: the row offset) back to an offset."""
    if cursor in (None, ""):
        return 0
    if not str(cursor).isdigit() or len(str(cursor)) > 7:
        raise ApiError(422, "validation", "Check the highlighted fields.",
                       fields={"cursor": "Not a cursor this list gave out"})
    return int(cursor)


def _next_cursor(offset: int, limit: int, total: int) -> str | None:
    return str(offset + limit) if offset + limit < total else None


def _password() -> str:
    return os.getenv("SENTRIX_PASSWORD", "").strip()


def _secure_cookies() -> bool:
    return os.getenv("SENTRIX_SECURE_COOKIES", "").strip().lower() in ("1", "true", "yes")


def _profile() -> dict[str, Any]:
    try:
        from sentrix.link import profile
        return profile.effective() or {}
    except Exception as exc:
        log.debug("profile unavailable: %s", exc)
        return {}


def _portal_profile() -> bool:
    try:
        from sentrix.link import profile
        return profile.current() is not None
    except Exception:
        return False


def _device_identity() -> dict[str, str | None]:
    """``device_id`` and ``serial`` from the identity file, nothing else
    (it also holds the bootstrap secret and the setup code's digest)."""
    try:
        from sentrix.link import profile
        data = json.loads((profile.identity_dir() / "identity.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        data = None
    if not isinstance(data, dict):
        return {"id": None, "serial": None}
    device_id = str(data.get("device_id") or "").lower()
    serial = str(data.get("serial") or "")
    return {"id": device_id if _DEVICE_ID.match(device_id) else None,
            "serial": serial if _SERIAL.match(serial) else None}


def _consents() -> dict[str, Any]:
    """The pushed profile's consents, as the path table reads them."""
    consents = _profile().get("consents")
    return consents if isinstance(consents, dict) else {}


def media_access(principal: auth.LcPrincipal | None) -> dict[str, bool]:
    """Would the path table let this caller fetch media (docs/7 §5.7)?"""
    if principal is None or principal.role == "operator":
        return {"snapshots": False, "evidence": False, "faces": False}
    if principal.kind == "lc":
        return {"snapshots": True, "evidence": True, "faces": True}
    consents = _consents()
    remote = bool(consents.get("remote_view"))
    return {"snapshots": remote, "evidence": remote,
            "faces": remote and bool(consents.get("face_recognition"))}


def call_info() -> dict[str, Any]:
    """``routes.call_target`` as data, with notes that name no setting."""
    target = console.call_target()
    href = target.get("href") or ""
    number = href[4:] if href.startswith("tel:") else None
    desk = bool(number) and console._test_mode()
    if not href:
        note = ("Test mode: no SENTRIX Desk number is set on this device, so this "
                "button is off.")
    elif desk:
        note = f"Test mode: this calls the SENTRIX Desk ({number}), not 112."
    else:
        note = ""
    return {"enabled": bool(href), "href": href or None, "number": number, "desk": desk,
            "note": note}


def _sos_features(principal: auth.LcPrincipal) -> dict[str, bool]:
    from sentrix.sos import current_service
    test_mode = console._test_mode()
    caps = console.capabilities(principal)
    return {"enabled": getattr(current_service(), "enabled", True) is not False,
            "intrusion_requests": bool(settings.sos.intrusion_requests),
            "demo_close_incident": test_mode and "sos" in caps,
            "demo_trigger": test_mode and principal.role == "device_admin"}


def session_payload(request: Request, principal: auth.LcPrincipal | None) -> dict[str, Any]:
    """The ``GET /api/session`` body (blocking: reads the profile and the
    identity file)."""
    tunnelled = auth.is_tunnelled(request)
    profile = _profile()
    zone = str(profile.get("time_zone") or "")
    payload: dict[str, Any] = {
        "signed_in": principal is not None,
        "via": "portal" if tunnelled else "lan",
        "csrf_header": settings.web.csrf_header,
        "base": tunnel_auth.base_for(request),
        "test_mode": console._test_mode(),
        "setup_mode": (not tunnelled) and bool(setup_routes.setup_mode_enabled()),
        "password_configured": bool(_password()),
        "time_zone": zone if _TIME_ZONE.match(zone) else DEFAULT_TIME_ZONE,
        "server_time": iso_z(datetime.now(timezone.utc)),
    }
    if principal is None:
        return payload
    live = console.state.get()
    link = live.get("link") or {}
    cameras = [c for c in profile.get("cameras") or [] if isinstance(c, dict)]
    claimed = bool(link.get("claimed")) or _portal_profile()
    identity = _device_identity()
    payload.update(
        role=principal.role,
        user={"id": principal.user_id, "name": principal.user_name},
        capabilities=sorted(console.capabilities(principal)),
        lan_admin=principal.kind == "lc",
        csrf_token=principal.csrf if principal.kind == "lc" else None,
        step_up_at=iso_z(principal.step_up_at) if principal.kind == "tunnel" else None,
        device={"id": identity["id"] or link.get("device_id"), "serial": identity["serial"],
                "name": None,
                "room": (str(cameras[0].get("room_label")) if cameras
                         and cameras[0].get("room_label") else None),
                "profile": settings.node.profile},
        claimed=claimed,
        link={"enabled": bool(link.get("enabled")), "state": link.get("state"),
              "connected": bool(link.get("connected")), "claimed": claimed,
              "remote_viewers": int(live.get("remote_viewers") or 0)},
        call=call_info(),
        media=media_access(principal),
        sos=_sos_features(principal),
    )
    return console.redacted(payload)


def _request_ref(views: dict[str, dict], request_id: str | None) -> dict | None:
    view = views.get(request_id or "")
    return {"id": request_id, **view} if view else None


# ── session ───────────────────────────────────────────────────────────────────
@router.get("/api/session", response_model=M.SessionInfo)
async def api_session(request: Request) -> dict[str, Any]:
    """Who is calling, what they may do, and the device's identity. Answers
    signed out too (``signed_in: false`` and only the device-wide flags), so
    the app knows to show the sign-in or the setup screen."""
    principal = auth.principal_of(request)
    return await asyncio.to_thread(session_payload, request, principal)


def _lan_only(request: Request) -> None:
    """Dependency: sign-in never exists through the tunnel (checked before
    the body, so a tunnelled request learns nothing from a 422)."""
    if auth.is_tunnelled(request):
        raise ApiError(404, "not_found", "Not found.")


@router.post("/api/login", response_model=M.SessionInfo, dependencies=[Depends(_lan_only)],
             responses={503: {"model": M.ApiErrorEnvelope}})
async def api_login(request: Request, body: M.LoginBody) -> JSONResponse:
    """The device admin's sign-in: the same password, rate limit (8 tries in
    5 minutes per address, shared with ``POST /login``) and 12-hour
    ``lc_session`` cookie as the HTML form. Answers the new session."""
    from sentrix.web.security import generate_token, login_limiter
    client = request.client.host if request.client else "unknown"
    if not login_limiter.allow(client):
        log.warning("login rate limit hit from %s", client)
        raise ApiError(429, "rate_limited", "Too many attempts. Try again in a few minutes.",
                       retry_after=LOGIN_RETRY_AFTER)
    expected = _password()
    if not expected:
        raise ApiError(503, "not_configured", "This device has no console password yet. "
                                              "Whoever installed it has to set one.")
    if not hmac.compare_digest(body.password.encode(), expected.encode()):
        raise ApiError(401, "wrong_password", "That password is not right.")
    login_limiter.reset(client)
    cookie = generate_token(expected)
    principal = auth.LcPrincipal(kind="lc", role="device_admin", actor="lc:device_admin",
                                 user_id=None, user_name="Device admin", step_up_at=None,
                                 csrf=auth.csrf_for_session(cookie))
    payload = await asyncio.to_thread(session_payload, request, principal)
    response = JSONResponse(M.SessionInfo.model_validate(payload).model_dump(mode="json"))
    response.set_cookie(settings.web.cookie_name, cookie, httponly=True,
                        secure=_secure_cookies(), samesite="lax", max_age=43_200)
    response.delete_cookie(auth.LEGACY_COOKIE)
    return response


@router.post("/api/logout", response_model=M.OkResponse, dependencies=[Depends(_lan_only)])
async def api_logout() -> JSONResponse:
    """Ends the LAN session (signed out already: the same answer)."""
    response = JSONResponse({"ok": True})
    response.delete_cookie(settings.web.cookie_name)
    response.delete_cookie(auth.LEGACY_COOKIE)
    return response


# ── live state ────────────────────────────────────────────────────────────────
@router.get("/api/state", response_model=M.LiveState)
async def api_state(_principal: auth.LcPrincipal = Depends(signed_in)) -> Response:
    """One message of ``/ws/threat``, byte for byte, so a page renders before
    the socket connects."""
    return Response(console.ws_payload(), media_type="application/json")


# ── activity ──────────────────────────────────────────────────────────────────
def _activity_row(row: dict[str, Any], *, rooms: dict[str, str], views: dict[str, dict],
                  request: Request, media: dict[str, bool]) -> dict[str, Any]:
    snapshot, bundle = row.get("snapshot_path"), row.get("evidence_bundle")
    camera = row.get("camera_id")
    return {
        "id": row["id"], "started_at": iso_z(row.get("started_iso") or row.get("started_at")),
        "ended_at": iso_z(row.get("ended_at")), "ongoing": not row.get("ended_at"),
        "duration_seconds": row.get("duration_seconds"), "peak_level": row.get("peak_level"),
        "peak_tci": row.get("peak_tci"), "status": row.get("status"),
        "incident_type": row.get("incident_type"), "reason": row.get("reason"),
        "authorized": row.get("authorized"), "camera_id": camera,
        "room": rooms.get(str(camera)) if camera else None,
        "verdict": row.get("verdict"), "verdict_note": row.get("verdict_note"),
        "verdict_by": console._actor_label(row.get("verdict_by")) or None,
        "verdict_at": iso_z(row.get("verdict_at")),
        "request": _request_ref(views, row.get("request_id")),
        "has_snapshot": bool(snapshot),
        "snapshot_url": (console.media_url(request, "snapshot", snapshot)
                         if snapshot and media["snapshots"] else None),
        "has_evidence": bool(bundle),
        "evidence_url": (console.media_url(request, "evidence", bundle)
                         if bundle and media["evidence"] else None),
    }


@router.get("/api/activity", response_model=M.ActivityPage)
async def api_activity(request: Request,
                       limit: int = Query(50, ge=1, le=200),
                       cursor: str | None = Query(None, max_length=16),
                       incident_type: str | None = Query(None, max_length=32),
                       level: int | None = Query(None, ge=1, le=5,
                                                 description="Minimum peak level"),
                       verdict: str | None = Query(None, pattern="^(?i:true|false|none)$"),
                       since: str | None = Query(None, max_length=10,
                                                 description="YYYY-MM-DD, device date"),
                       until: str | None = Query(None, max_length=10),
                       principal: auth.LcPrincipal = Depends(signed_in)) -> dict[str, Any]:
    """The device's incidents, newest first: when, how high, why, verdict,
    the assistance request (owner view: duress hidden) and media links the
    caller may open. ``POST /api/incidents/{id}/verdict`` sets a verdict."""
    offset = _offset(cursor)
    filters = console.incident_filters(incident_type=incident_type, level=level,
                                       verdict=verdict, since=since, until=until)

    def read() -> dict[str, Any]:
        rows, total = console.query_incidents(filters, limit, offset)
        views = console.request_views([row.get("request_id") for row in rows])
        rooms = console._camera_rooms()
        media = media_access(principal)
        stats = console.database.incident_stats() or {}
        items = [_activity_row(row, rooms=rooms, views=views, request=request, media=media)
                 for row in rows]
        return {"items": items, "total": total, "limit": limit,
                "cursor": str(offset) if offset else None,
                "next_cursor": _next_cursor(offset, limit, total), "stats": stats,
                "filters": {k: v for k, v in filters.items() if not k.startswith("_")}}

    return console.redacted(await asyncio.to_thread(read))


@router.get("/api/snapshots", response_model=M.SnapshotPage)
async def api_snapshots(request: Request, limit: int = Query(60, ge=1, le=200),
                        cursor: str | None = Query(None, max_length=16),
                        _principal: auth.LcPrincipal = Depends(device_admin)) -> dict[str, Any]:
    """Alert snapshots, newest first, with the incident each belongs to."""
    offset = _offset(cursor)

    def read() -> dict[str, Any]:
        directory = console.SNAPSHOT_DIR
        names = sorted((p.name for p in directory.glob("*.jpg")), reverse=True) \
            if directory.is_dir() else []
        page = names[offset:offset + limit]
        owners = _incidents_by_snapshot(page)
        items = []
        for name in page:
            try:
                stat = (directory / name).stat()
                taken, size = iso_z(stat.st_mtime), int(stat.st_size)
            except OSError:
                taken, size = None, None
            items.append({"name": name, "url": console.media_url(request, "snapshot", name),
                          "taken_at": taken, "bytes": size, "incident": owners.get(name)})
        return {"items": items, "total": len(names), "limit": limit,
                "cursor": str(offset) if offset else None,
                "next_cursor": _next_cursor(offset, limit, len(names))}

    return await asyncio.to_thread(read)


def _incidents_by_snapshot(names: list[str]) -> dict[str, dict[str, Any]]:
    if not names:
        return {}
    from sentrix.storage.models import IncidentRecord
    found: dict[str, dict[str, Any]] = {}
    try:
        with console.database.session_scope() as session:
            for row in (session.query(IncidentRecord)
                        .filter(IncidentRecord.snapshot_path.in_(names)).all()):
                found[row.snapshot_path] = {"id": row.id, "incident_type": row.incident_type,
                                            "peak_level": row.peak_level}
    except Exception as exc:
        log.error("snapshot incident lookup failed: %s", exc)
    return found


_DETECTION = re.compile(r"label=['\"]?([\w -]+)['\"]?.*?conf=([\d.]+)")


def _detection(item: Any) -> dict[str, Any] | None:
    """A sidecar detection (a dict, or the repr string old bundles kept)."""
    if isinstance(item, dict):
        try:
            conf = float(item["conf"]) if item.get("conf") is not None else None
        except (TypeError, ValueError):
            conf = None
        track = item.get("track_id")
        return {"label": str(item.get("label") or "object")[:40], "conf": conf,
                "track_id": track if isinstance(track, int) else None}
    match = _DETECTION.search(str(item))
    if match:
        return {"label": match.group(1)[:40], "conf": float(match.group(2)), "track_id": None}
    return None


def _chain(chain: dict[str, Any]) -> dict[str, Any]:
    problems = [{"line": p.get("line"), "bundle": p.get("bundle"),
                 "error": str(p.get("error") or "problem")}
                for p in chain.get("problems") or [] if isinstance(p, dict)]
    return {"ok": bool(chain.get("ok")), "entries": int(chain.get("entries") or 0),
            "note": chain.get("note"), "problems": problems[:20],
            "problem_count": len(problems),
            # verify_chain reports an OSError's text (a file path): not for a page.
            "error": "The evidence manifest could not be read." if chain.get("error") else None}


@router.get("/api/evidence", response_model=M.EvidencePage)
async def api_evidence(request: Request, limit: int = Query(50, ge=1, le=200),
                       cursor: str | None = Query(None, max_length=16),
                       _principal: auth.LcPrincipal = Depends(device_admin)) -> dict[str, Any]:
    """Encrypted evidence bundles, newest first, and the hash-chain check.
    ``view_url`` opens the decrypted image (``/media/evidence/{id}``)."""
    offset = _offset(cursor)

    def read() -> dict[str, Any]:
        from sentrix.evidence.vault import MANIFEST_NAME
        vault = console.vault
        records = vault.list_records(offset + limit)[offset:offset + limit]
        total = sum(1 for p in vault.dir.glob("*.json") if p.name != MANIFEST_NAME) \
            if vault.dir.is_dir() else 0
        rooms = console._camera_rooms()
        items = []
        for record in records:
            bundle = str(record.get("bundle_id") or "")
            if not bundle:
                continue
            camera = record.get("camera_id")
            detections = [d for d in (_detection(x) for x in record.get("detections") or [])
                          if d is not None]
            items.append({
                "bundle_id": bundle, "timestamp": iso_z(record.get("timestamp")),
                "incident_id": record.get("incident_id"),
                "camera_id": str(camera) if camera is not None else None,
                "room": rooms.get(str(camera)) if camera is not None else None,
                "level": record.get("level"), "tci": record.get("tci"),
                "status": record.get("status"), "incident_type": record.get("incident_type"),
                "reason": record.get("reason"), "detections": detections,
                "sha256": record.get("sha256"),
                "view_url": console.media_url(request, "evidence", bundle)})
        return {"chain": _chain(vault.verify_chain()), "items": items, "total": total,
                "limit": limit, "cursor": str(offset) if offset else None,
                "next_cursor": _next_cursor(offset, limit, total)}

    return console.redacted(await asyncio.to_thread(read))


def _dispatch_row(package: dict[str, Any], views: dict[str, dict]) -> dict[str, Any]:
    from sentrix.alerting.dispatch import SENDABLE_STATUSES
    from sentrix.utils.redact import mask_recipient
    sent_to = []
    for item in package.get("sent_to") or []:
        if isinstance(item, dict):
            item = (item.get("masked") or item.get("recipient_masked")
                    or mask_recipient(item.get("address")) or item.get("ref") or "")
        if item:
            sent_to.append(str(item))
    return {
        "id": package["id"], "created_at": iso_z(package.get("created_iso")
                                                 or package.get("created_at")),
        "incident_id": package.get("incident_id"), "incident_type": package.get("incident_type"),
        "level": package.get("level"), "tci": package.get("tci"),
        "authority": package.get("authority"),
        "chosen_authority": package.get("chosen_authority"),
        "status": package.get("status"), "error": package.get("error"),
        "location": package.get("user_address"),
        "camera_location": package.get("camera_location"),
        "sent_at": iso_z(package.get("sent_at")),
        "sent_by": console._actor_label(package.get("sent_by")) or None,
        "sent_to": sent_to, "expires_at": iso_z(package.get("expires_at")),
        "request": _request_ref(views, package.get("request_id")),
        "sendable": package.get("status") in SENDABLE_STATUSES,
    }


@router.get("/api/dispatch", response_model=M.DispatchPage)
async def api_dispatch(limit: int = Query(50, ge=1, le=200),
                       cursor: str | None = Query(None, max_length=16),
                       _principal: auth.LcPrincipal = Depends(device_admin)) -> dict[str, Any]:
    """Dispatch packages, newest first, as the household may see them (a
    duress package reads as cancelled, a silent one is absent). "Send now"
    is ``POST /api/dispatch/{id}/send/{authority}``."""
    from sentrix.alerting.dispatch import AUTHORITIES, SENDABLE_STATUSES
    offset = _offset(cursor)
    if offset > 1000:
        raise ApiError(422, "validation", "Check the highlighted fields.",
                       fields={"cursor": "At most the newest 1,200 packages are listed"})

    def read() -> dict[str, Any]:
        rows = console.database.all_dispatches(offset + limit + 1)
        more = len(rows) > offset + limit
        page = console.owner_packages(rows[offset:offset + limit])
        views = console.request_views([p.get("request_id") for p in page])
        return {"items": [_dispatch_row(p, views) for p in page],
                "authorities": list(AUTHORITIES), "sendable_statuses": list(SENDABLE_STATUSES),
                "limit": limit, "cursor": str(offset) if offset else None,
                "next_cursor": str(offset + limit) if more else None}

    return console.redacted(await asyncio.to_thread(read))


@router.get("/api/authorized", response_model=M.FaceGallery)
async def api_faces(request: Request,
                    _principal: auth.LcPrincipal = Depends(device_admin)) -> dict[str, Any]:
    """The enrolled faces. Enrol with ``POST /api/authorized/capture`` (the
    camera's frame) or ``POST /authorized/upload``; remove with
    ``DELETE /api/authorized/{filename}``."""
    from sentrix.perception.identity import person_name

    def read() -> dict[str, Any]:
        pipeline = console.get_pipeline()
        enrolled = pipeline.identity.enrolled_files
        directory = console.FACES_DIR
        items = [{"filename": p.name, "person": person_name(p.stem),
                  "enrolled": p.name in enrolled,
                  "url": console.media_url(request, "face", p.name)}
                 for p in (sorted(directory.iterdir()) if directory.is_dir() else [])
                 if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png")]
        backend = pipeline.identity.backend
        return {"backend": backend, "available": backend is not None, "items": items}

    return await asyncio.to_thread(read)


# ── Wi-Fi setup mode ──────────────────────────────────────────────────────────
def _setup_only(request: Request) -> None:
    """Dependency: outside setup mode, or through the tunnel, these routes
    do not exist (checked before the body is validated)."""
    if not setup_routes.setup_mode_enabled() or auth.is_tunnelled(request):
        raise ApiError(404, "not_found", "Not found.")


def _check_setup_code(request: Request, code: str) -> None:
    """The label's setup code; shares the HTML page's limiter (5 tries per
    10 minutes per address)."""
    client = request.client.host if request.client else "unknown"
    if not setup_routes.setup_limiter.allow(client):
        raise ApiError(429, "rate_limited", "Too many wrong codes. Wait 10 minutes and "
                                            "try again.", retry_after=600)
    digest = setup_routes._setup_digest()
    if digest is None:
        raise ApiError(409, "unsupported", "This unit has no setup code on file, so Wi-Fi "
                                           "cannot be set up here. Connect it with a network "
                                           "cable instead.")
    if not setup_routes.code_matches(code, digest):
        raise ApiError(403, "wrong_code", "That is not the setup code printed on the label.",
                       fields={"code": "Not the code on the label"})
    setup_routes.setup_limiter.reset(client)


def _wifi_control():
    network = setup_routes._network()
    if network is None:
        raise ApiError(409, "unsupported", "Wi-Fi control is not available on this unit. "
                                           "Connect it with a network cable.")
    return network


def _status(network) -> dict[str, Any]:
    raw = setup_routes._network_status(network)
    return {"connected": bool(raw.get("connected")),
            "ssid": str(raw["ssid"])[:32] if raw.get("ssid") else None,
            "ip": str(raw["ip"])[:64] if raw.get("ip") else None,
            "via": str(raw["via"])[:16] if raw.get("via") else None}


def _signal(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


@router.get("/api/setup/wifi", response_model=M.SetupWifiInfo,
            dependencies=[Depends(_setup_only)])
async def api_setup_wifi() -> dict[str, Any]:
    """Setup mode only (the unit's button), on the LAN only, no session."""
    return {"setup_mode": True, "network_available": setup_routes._network() is not None,
            "test_mode": bool(settings.test_mode)}


@router.post("/api/setup/wifi/scan", response_model=M.WifiScanResult,
             dependencies=[Depends(_setup_only)])
async def api_setup_scan(request: Request, body: M.SetupCodeBody) -> dict[str, Any]:
    """Networks in range (needs the setup code)."""
    _check_setup_code(request, body.code)
    network = _wifi_control()
    try:
        found = await asyncio.to_thread(network.scan)
    except Exception as exc:
        log.warning("wifi scan failed: %s", exc)
        raise ApiError(502, "wifi_failed", "Could not look for networks. Try again.") from None
    networks = [{"ssid": str(item["ssid"])[:32], "signal": _signal(item.get("signal")),
                 "security": str(item.get("security") or "")[:32]}
                for item in found or [] if isinstance(item, dict) and item.get("ssid")]
    return {"networks": networks, "status": await asyncio.to_thread(_status, network)}


@router.post("/api/setup/wifi/connect", response_model=M.WifiConnectResult,
             dependencies=[Depends(_setup_only)],
             responses={502: {"model": M.ApiErrorEnvelope}})
async def api_setup_connect(request: Request, body: M.WifiConnectBody) -> dict[str, Any]:
    """Join a network. The passphrase is passed through, never logged and
    never echoed (not even in a validation error)."""
    _check_setup_code(request, body.code)
    network = _wifi_control()
    log.info("setup: joining Wi-Fi network %r", body.ssid)
    try:
        result = dict(await asyncio.to_thread(network.connect, body.ssid, body.psk or "") or {})
    except Exception as exc:
        log.warning("wifi connect failed: %s", exc)
        result = {"ok": False, "error": None}
    if not result.get("ok"):
        reason = str(result.get("error") or "")[:200] or None
        raise ApiError(502, "wifi_failed", "Could not join that network. Check the password "
                                           "and that the network is in range.", reason=reason)
    return {"ok": True, "status": await asyncio.to_thread(_status, network)}


__all__ = ["router", "ApiRoute", "ApiError", "session_payload", "media_access", "call_info",
           "iso_z", "DEFAULT_TIME_ZONE"]
