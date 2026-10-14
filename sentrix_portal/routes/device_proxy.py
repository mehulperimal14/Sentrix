"""Tunnel parity: /d/{device_id}/{path} forwarded to the device's LC;
/d/{device_id}/video and /d/{device_id}/ws/threat relayed from frame/state
messages; offline mirror pages.

Decision D10: the Portal does not re-implement console pages. A request to
``/d/<device_id>/<path>`` is checked here (signed in; the device belongs to
the user's household, or the Desk under its rule; the Portal's CSRF header on
every state-changing request), forwarded over the link with the user's actor,
the URL base and the session's CSRF token, and replayed by the device into
its own console. The device enforces its own path table on top
(``sentrix/link/policy.py``). Responses keep the console's own CSP (the
Portal adds none on ``/d/``) and lose every cookie; ``Location: /x`` becomes
``/d/<id>/x``; ``/api/*`` and ``/media/*`` are ``no-store``.

While the device is offline: pages show the mirror (incidents, packages, open
requests) with "Device offline" and verdict buttons that queue a
``verdict.set`` command ("queued, will sync"); APIs answer 503
``device_offline``.

Owner: track T-LINK.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import timezone
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session
from starlette.routing import Match
from starlette.websockets import WebSocketDisconnect

from sentrix.actor import Actor
from sentrix.sos import states as S
from sentrix_portal import security
from sentrix_portal.db import DB
from sentrix_portal.models import (AssistanceRequest, Device, DispatchMirror,
                                   IncidentMirror, utcnow)
from sentrix_portal.services import sos as sos_service
from sentrix_portal.services.hub import UUID_RE, Viewer, hub_for

router = APIRouter()

#: Request headers forwarded to the device (docs/7 §5.7).
FORWARD_HEADERS = ("accept", "accept-language", "content-type", "x-csrf-token",
                   "x-requested-with", "if-none-match", "user-agent")
#: Response headers the Portal never passes on.
DROP_RESPONSE = frozenset({"connection", "keep-alive", "proxy-authenticate",
                           "proxy-authorization", "te", "trailer", "trailers",
                           "transfer-encoding", "upgrade", "set-cookie", "content-length",
                           "content-encoding"})
#: Never tunnelled, whatever the user asks (the device refuses them too).
NEVER_EXACT = frozenset({"/login", "/logout", "/docs", "/redoc", "/openapi.json", "/setup",
                         "/video"})
NEVER_PREFIXES = ("/setup/", "/docs/", "/redoc/", "/ws/")
METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]
#: What the Desk may open through the tunnel (the device's table says the
#: same; the Portal checks it first so nothing else is even forwarded).
DESK_PATHS = frozenset({"/dashboard", "/live", "/api/health", "/api/metrics"})
_tunnel_role = security.require_role("owner", "member", "operator", csrf="header")
_view_role = security.require_role("owner", "member", "operator", csrf=False)


def _cfg():
    from sentrix.config import settings
    return settings


def actor_of(principal: security.Principal) -> dict:
    step = principal.step_up_at.timestamp() if principal.step_up_at else None
    return Actor(type=principal.role, id=principal.user_id, name=principal.display_name,
                 step_up_at=step).to_dict()


def authorize_device(db: Session, principal: security.Principal, device_id: str) -> Device:
    """The device, if this user may reach it; 404 otherwise (tenant isolation:
    another household's device does not exist for you)."""
    if not UUID_RE.match(device_id or ""):
        raise HTTPException(404, detail="not_found")
    device = db.get(Device, device_id)
    if device is None or device.state != "CLAIMED":
        raise HTTPException(404, detail="not_found")
    if principal.role in ("owner", "member"):
        if not principal.household_id or device.household_id != principal.household_id:
            raise HTTPException(404, detail="not_found")
        return device
    if principal.role == "operator":
        on_help_path = (db.query(AssistanceRequest)
                        .filter(AssistanceRequest.device_id == device.id,
                                AssistanceRequest.state.in_(sorted(sos_service.DESK_STATES)))
                        .first())
        if on_help_path is None:
            raise HTTPException(403, detail="forbidden_role")
        if not sos_service.consent_active(db, device.household_id, "desk_live_view"):
            raise HTTPException(403, detail="consent_required")
        return device
    raise HTTPException(403, detail="forbidden_role")


def _open_request(db: Session, device_id: str) -> bool:
    return sos_service.open_request_for(db, device_id) is not None


def _wants_html(request: Request) -> bool:
    return request.method == "GET" and "text/html" in request.headers.get("accept", "")


# ── entry points ──────────────────────────────────────────────────────────────
@router.get("/d/{device_id}", include_in_schema=False)
@router.get("/d/{device_id}/", include_in_schema=False)
async def device_root(device_id: str) -> RedirectResponse:
    return RedirectResponse(f"/d/{device_id}/dashboard", status_code=307)


@router.get("/d/{device_id}/video")
async def video_relay(device_id: str, request: Request, db: Session = DB,
                      principal: security.Principal = Depends(_view_role)):
    """MJPEG from the device's FRAME messages (adaptive 2-15 fps on the
    device; the console's ``<img src=.../video>`` works unchanged)."""
    device = authorize_device(db, principal, device_id)
    hub = hub_for(request.app)
    if not hub.online(device.id):
        return JSONResponse({"detail": "the device is offline", "code": "device_offline"},
                            status_code=503)
    cap = int(_cfg().link.max_viewers)
    exempt = _open_request(db, device.id)
    keys = {v.key for v in hub.video_viewers.get(device.id, set())}
    if not exempt and principal.session_id_hash not in keys and len(keys) >= cap:
        return JSONResponse({"detail": f"at most {cap} remote viewers", "code": "rate_limited"},
                            status_code=429, headers={"Retry-After": "30"})
    db.commit()
    viewer = Viewer(key=principal.session_id_hash, role=principal.role,
                    actor=actor_of(principal))
    refusal = await hub.add_video_viewer(device.id, viewer)
    if refusal is not None:
        code = str(refusal.get("code") or "forbidden_role")
        return JSONResponse({"detail": refusal.get("message") or code, "code": code},
                            status_code=403)

    async def frames():
        # Starlette cancels this generator when the viewer disconnects.
        try:
            while True:
                try:
                    seq, _ts, jpeg = await asyncio.wait_for(viewer.queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n")
        finally:
            await hub.remove_video_viewer(device.id, viewer)

    return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store"})


def _same_origin(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    if not origin:
        return True                                  # not a browser
    host = websocket.headers.get("host", "")
    allowed = {host, urlsplit(_portal_base()).netloc}
    return urlsplit(origin).netloc in allowed


def _portal_base() -> str:
    from sentrix_portal import settings as portal_settings
    return portal_settings.current().base_url


def _ws_principal(websocket: WebSocket) -> security.Principal | None:
    from sentrix_portal import settings as portal_settings
    settings = portal_settings.current()
    with websocket.app.state.db.session() as db:
        loaded = security.load_session(db, websocket.cookies.get(settings.cookie_name))
        if loaded is None:
            return None
        row, user = loaded
        principal = security.Principal(
            user_id=user.id, username=user.username,
            display_name=user.display_name or user.username, role=user.role,
            household_id=user.household_id, session_id_hash=row.id_hash,
            csrf_token=row.csrf_token, totp_verified=bool(row.totp_verified),
            step_up_at=row.step_up_at.replace(tzinfo=timezone.utc)
            if row.step_up_at and row.step_up_at.tzinfo is None else row.step_up_at)
    if principal.role in security.TOTP_ROLES and not principal.totp_verified:
        return None
    return principal


def _ws_device(websocket: WebSocket, principal, device_id: str) -> str | None:
    with websocket.app.state.db.session() as db:
        try:
            return authorize_device(db, principal, device_id).id
        except HTTPException:
            return None


@router.websocket("/d/{device_id}/ws/threat")
async def state_relay(websocket: WebSocket, device_id: str) -> None:
    """The console's live state: each device ``state`` payload as the same
    JSON the LC websocket sends (+ ``remote_video`` while video is relayed);
    while the device is offline: ``camera_online=false``, ``ai_hz=0``."""
    if not _same_origin(websocket):
        await websocket.close(code=1008)
        return
    principal = await asyncio.to_thread(_ws_principal, websocket)
    if principal is None or principal.role not in ("owner", "member", "operator"):
        await websocket.close(code=1008)
        return
    device = await asyncio.to_thread(_ws_device, websocket, principal, device_id)
    if device is None:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    hub = hub_for(websocket.app)
    viewer = Viewer(key=principal.session_id_hash, role=principal.role,
                    actor=actor_of(principal))
    await hub.add_state_viewer(device, viewer)

    async def watch_client() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return

    closer = asyncio.ensure_future(watch_client())
    try:
        while not closer.done():
            getter = asyncio.ensure_future(viewer.queue.get())
            done, _ = await asyncio.wait({getter, closer}, timeout=2.0,
                                         return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                payload = getter.result()
            else:
                getter.cancel()
                if closer.done():
                    break
                payload = hub.latest_state(device)
                if payload is None or hub.online(device):
                    continue                     # online but quiet: keep waiting
            await websocket.send_text(json.dumps(payload, default=str))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        closer.cancel()
        await hub.remove_state_viewer(device, viewer)


class TunnelRoute(APIRoute):
    """The catch-all yields to every explicit route: it reports a partial
    match, so any route that matches fully (registered before or after it)
    wins, and the tunnel serves only what nothing else claims."""

    def matches(self, scope):
        match, child_scope = super().matches(scope)
        if match == Match.FULL:
            return Match.PARTIAL, child_scope
        return match, child_scope


async def tunnel(device_id: str, path: str, request: Request,
                 db: Session = DB,
                 principal: security.Principal = Depends(_tunnel_role)):
    """Every other console route, replayed by the device in-process."""
    device = authorize_device(db, principal, device_id)
    inner = "/" + path
    if inner in NEVER_EXACT or inner.startswith(NEVER_PREFIXES):
        return JSONResponse({"detail": "not available through the Portal",
                             "code": "not_found"}, status_code=404)
    if principal.role == "operator" and not (
            request.method in ("GET", "HEAD")
            and (inner in DESK_PATHS or inner.startswith("/static/"))):
        return JSONResponse({"detail": "the Desk may only view the live console",
                             "code": "forbidden_role"}, status_code=403)
    hub = hub_for(request.app)
    if not hub.online(device.id):
        return await _offline(request, db, principal, device, inner)

    body = await _read_body(request)
    if body is None:
        return JSONResponse({"detail": "request body too large", "code": "too_large"},
                            status_code=413)
    headers = [[name, request.headers[name]] for name in FORWARD_HEADERS
               if name in request.headers]
    slow = request.method == "POST" and (
        inner in ("/authorized/upload", "/api/authorized/capture")
        or "multipart/" in request.headers.get("content-type", ""))
    cfg = _cfg().link
    timeout = cfg.tunnel_upload_timeout_seconds if slow else cfg.tunnel_timeout_seconds
    db.commit()                                   # no write lock while we wait
    reply = await hub.tunnel(device.id, method=request.method, path=inner,
                             query=request.url.query, headers=headers, body=body,
                             actor=actor_of(principal), base=f"/d/{device.id}",
                             csrf=principal.csrf_token, timeout=timeout)
    if reply.code == "device_offline":
        return await _offline(request, db, principal, device, inner)
    return _response(reply, device.id, inner)


router.add_api_route("/d/{device_id}/{path:path}", tunnel, methods=METHODS,
                     route_class_override=TunnelRoute, include_in_schema=False)


async def _read_body(request: Request) -> bytes | None:
    """The body, streamed with the 11 MiB cap (None when larger)."""
    limit = int(_cfg().link.tunnel_max_request_bytes)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        return None
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _response(reply, device_id: str, inner: str) -> Response:
    headers: dict[str, str] = {}
    for name, value in reply.headers:
        lower = name.lower()
        if lower in DROP_RESPONSE or lower.startswith("x-sentrix-"):
            continue
        if lower == "location" and value.startswith("/") and not value.startswith("//"):
            value = f"/d/{device_id}{value}"
        headers[lower] = value
    if inner.startswith(("/api/", "/media/")):
        headers["cache-control"] = "no-store"
    elif inner.startswith("/static/"):
        headers["cache-control"] = "public, max-age=3600"
    media_type = headers.pop("content-type", None)
    return Response(content=reply.body, status_code=reply.status, headers=headers,
                    media_type=media_type)


# ── offline ───────────────────────────────────────────────────────────────────
async def _offline(request: Request, db: Session, principal: security.Principal,
                   device: Device, inner: str) -> Response:
    parts = inner.strip("/").split("/")
    if (request.method == "POST" and len(parts) == 4 and parts[:2] == ["api", "incidents"]
            and parts[3] == "verdict" and principal.role in ("owner", "member")):
        return await _queue_verdict(request, principal, device, parts[2])
    if _wants_html(request):
        return _offline_page(request, db, principal, device)
    return JSONResponse({"detail": "the device is offline", "code": "device_offline"},
                        status_code=503, headers={"Cache-Control": "no-store"})


async def _queue_verdict(request: Request, principal, device: Device,
                         incident_id: str) -> JSONResponse:
    from sentrix_portal.services import hub as hub_module
    if not UUID_RE.match(incident_id):
        return JSONResponse({"detail": "unknown incident", "code": "not_found"}, 404)
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            form = json.loads((await request.body()) or b"{}")
        except ValueError:
            form = {}
    elif "multipart/" in content_type:
        form = dict(await request.form())
    else:
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode("utf-8",
                                                                           "replace")).items()}
    verdict = str(form.get("verdict") or "").upper()
    if verdict not in ("TRUE", "FALSE"):
        return JSONResponse({"detail": "verdict must be TRUE or FALSE",
                             "code": "invalid_args"}, 400)
    note = str(form.get("note") or "")[:500] or None
    result = await hub_module.send_command(
        device.id, "verdict.set", {"incident_id": incident_id, "verdict": verdict,
                                   "note": note}, actor_of(principal), queue_if_offline=True)
    if result.get("queued"):
        return JSONResponse({"status": "queued, will sync", "queued": True,
                             "command_id": result.get("command_id")}, status_code=202)
    return JSONResponse({"detail": result.get("message"), "code": result.get("code")},
                        status_code=result.get("status") or 503)


def _offline_page(request: Request, db: Session, principal, device: Device) -> Response:
    household = principal.role in ("owner", "member")      # the Desk sees no history
    incidents = (db.query(IncidentMirror).filter_by(device_id=device.id)
                 .order_by(IncidentMirror.started_at.desc()).limit(50).all()) if household else []
    packages = (db.query(DispatchMirror).filter_by(device_id=device.id)
                .order_by(DispatchMirror.created_at.desc()).limit(20).all()) if household else []
    requests_open = [sos_service.request_view(r, "desk" if principal.role == "operator"
                                              else "owner")
                     for r in db.query(AssistanceRequest)
                     .filter(AssistanceRequest.device_id == device.id,
                             AssistanceRequest.state.in_(sorted(S.OPEN_STATES))).all()]
    return request.app.state.templates.TemplateResponse(
        request=request, name="device/offline.html", status_code=503,
        context={"principal": principal, "active_page": "home", "device": device,
                 "incidents": incidents, "packages": packages, "requests": requests_open,
                 "can_label": principal.role in ("owner", "member"),
                 "now": utcnow(), "monotonic": time.monotonic()})


__all__ = ["router", "authorize_device", "actor_of", "FORWARD_HEADERS", "TunnelRoute"]
