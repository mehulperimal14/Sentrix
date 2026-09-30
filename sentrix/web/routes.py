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
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import (APIRouter, Cookie, Depends, File, Form, HTTPException,
                     Request, UploadFile, status)
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               Response)
from fastapi.templating import Jinja2Templates
from starlette.websockets import WebSocket, WebSocketDisconnect

from sentrix.config import settings
from sentrix.evidence.vault import vault
from sentrix.alerting.dispatch import dispatch_service
from sentrix.paths import FACES_DIR, SNAPSHOT_DIR, TEMPLATES_DIR
from sentrix.pipeline.engine import get_pipeline
from sentrix.pipeline.state import state
from sentrix.storage import database
from sentrix.utils.logging import get_logger, telemetry
from sentrix.web.security import (generate_token, login_limiter,
                                  sanitize_filename, validate_image,
                                  verify_token)

log = get_logger("routes")
router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _password() -> str:
    return os.getenv("SENTRIX_PASSWORD", "").strip()


def _secure_cookies() -> bool:
    return os.getenv("SENTRIX_SECURE_COOKIES", "").strip().lower() in ("1", "true", "yes")


async def require_auth(session_token: str | None = Cookie(None)) -> bool:
    password = _password()
    if not password:
        raise HTTPException(status_code=500,
                            detail="SENTRIX_PASSWORD is not configured")
    if not verify_token(session_token, password):
        raise HTTPException(status_code=status.HTTP_307_TEMPORARY_REDIRECT,
                            headers={"Location": "/login"})
    return True


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


# ── auth ──────────────────────────────────────────────────────────────────────
@router.get("/", response_class=RedirectResponse)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="login.html",
                                      context={"request": request})


@router.post("/login")
async def login(request: Request, password: str = Form(...)):
    client = request.client.host if request.client else "unknown"
    if not login_limiter.allow(client):
        log.warning("login rate limit hit from %s", client)
        return templates.TemplateResponse(
            request=request, name="login.html", status_code=429,
            context={"request": request,
                     "error": "Too many attempts. Try again shortly."})

    expected = _password()
    if not expected:
        return templates.TemplateResponse(
            request=request, name="login.html", status_code=500,
            context={"request": request,
                     "error": "Server misconfigured: SENTRIX_PASSWORD unset"})

    if password != expected:
        return templates.TemplateResponse(
            request=request, name="login.html", status_code=401,
            context={"request": request, "error": "Invalid password"})

    login_limiter.reset(client)
    response = RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    response.set_cookie("session_token", generate_token(expected), httponly=True,
                        secure=_secure_cookies(), samesite="lax", max_age=43_200)
    return response


@router.get("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse(url="/login")
    response.delete_cookie("session_token")
    return response


# ── pages ─────────────────────────────────────────────────────────────────────
@router.get("/dashboard", response_class=HTMLResponse,
            dependencies=[Depends(require_auth)])
async def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="dashboard.html",
                                      context={"request": request})


@router.get("/live", response_class=HTMLResponse,
            dependencies=[Depends(require_auth)])
async def live(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="live.html",
                                      context={"request": request})


@router.get("/events", response_class=HTMLResponse,
            dependencies=[Depends(require_auth)])
async def events(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request, name="events.html",
        context={"request": request, "events": database.recent_incidents(100),
                 "stats": database.incident_stats()})


@router.get("/alerts", response_class=HTMLResponse,
            dependencies=[Depends(require_auth)])
async def alerts(request: Request) -> HTMLResponse:
    images = [f"/media/snapshot/{p.name}"
              for p in sorted(SNAPSHOT_DIR.glob("*.jpg"), reverse=True)[:120]]
    return templates.TemplateResponse(request=request, name="alerts.html",
                                      context={"request": request,
                                               "images": images})


@router.get("/evidence", response_class=HTMLResponse,
            dependencies=[Depends(require_auth)])
async def evidence(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request, name="evidence.html",
        context={"request": request, "items": vault.list_records(200),
                 "chain": vault.verify_chain()})


@router.get("/dispatch", response_class=HTMLResponse,
            dependencies=[Depends(require_auth)])
async def dispatch_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request, name="dispatch.html",
        context={"request": request, "packages": database.all_dispatches()})


@router.get("/authorized", response_class=HTMLResponse,
            dependencies=[Depends(require_auth)])
async def authorized(request: Request) -> HTMLResponse:
    faces = [{"filename": p.name, "url": f"/media/face/{p.name}"}
             for p in sorted(FACES_DIR.iterdir())
             if p.suffix.lower() in (".jpg", ".jpeg", ".png")]
    return templates.TemplateResponse(
        request=request, name="authorized.html",
        context={"request": request, "faces": faces,
                 "backend": get_pipeline().identity.backend})


# ── authenticated media ───────────────────────────────────────────────────────
@router.get("/media/snapshot/{name}", dependencies=[Depends(require_auth)])
async def media_snapshot(name: str) -> Response:
    path = _safe_path(SNAPSHOT_DIR, name)
    return Response(path.read_bytes(), media_type="image/jpeg")


@router.get("/media/face/{name}", dependencies=[Depends(require_auth)])
async def media_face(name: str) -> Response:
    path = _safe_path(FACES_DIR, name)
    suffix = path.suffix.lower()
    return Response(path.read_bytes(),
                    media_type="image/png" if suffix == ".png" else "image/jpeg")


@router.get("/media/evidence/{bundle_id}", dependencies=[Depends(require_auth)])
async def media_evidence(bundle_id: str) -> Response:
    """Decrypt one evidence bundle for authenticated review."""
    payload = vault.decrypt(sanitize_filename(bundle_id))
    if payload is None:
        raise HTTPException(status_code=404,
                            detail="Bundle not found or key unavailable")
    return Response(payload, media_type="image/jpeg")


# ── API ───────────────────────────────────────────────────────────────────────
@router.get("/api/metrics", dependencies=[Depends(require_auth)])
async def api_metrics() -> JSONResponse:
    return JSONResponse(state.get())


@router.get("/api/health", dependencies=[Depends(require_auth)])
async def api_health() -> JSONResponse:
    pipeline = get_pipeline()
    live = state.get()
    return JSONResponse({
        "camera": pipeline.source.online,
        "camera_source": pipeline.source.name,
        "video_fps": live.get("video_fps", 0.0),
        "ai_hz": live.get("ai_hz", 0.0),
        "frame_age_ms": live.get("frame_age_ms"),
        "detector": pipeline.detector.available,
        "scene_model": pipeline.scene.weights_name,
        "scene_device": str(pipeline.scene.device) if pipeline.scene.available else None,
        "scene_thresholds": pipeline.scene.thresholds if pipeline.scene.available else None,
        "threat_sources": settings.threat.sources,
        "armed": settings.threat.armed,
        "violence_model_loaded": pipeline.violence is not None,
        "identity_backend": pipeline.identity.backend,
        "identity_fail_closed": pipeline.identity.backend is None,
        "enrolled_faces": len(pipeline.identity.enrolled),
        "audio": bool(pipeline.audio and pipeline.audio.available),
        "fusion_source": pipeline.tci.model_source,
        "siren_backend": pipeline.siren.name,
        "siren_is_real": pipeline.siren.is_real,
        "notifier_online": __import__(
            "sentrix.alerting.notifier", fromlist=["notifier"]).notifier.online,
        "evidence_key_persistent": vault.persistent,
        "session": pipeline.session_id,
        "latency": telemetry.stats(),
    })


@router.get("/api/config", dependencies=[Depends(require_auth)])
async def api_config() -> JSONResponse:
    """Expose the live tunables so the dashboard shows the real thresholds."""
    return JSONResponse(settings.to_dict())


@router.get("/api/incidents", dependencies=[Depends(require_auth)])
async def api_incidents(limit: int = 100) -> JSONResponse:
    return JSONResponse({"incidents": database.recent_incidents(limit),
                         "stats": database.incident_stats()})


@router.post("/api/incidents/{incident_id}/verdict",
             dependencies=[Depends(require_auth)])
async def api_verdict(incident_id: str, verdict: str = Form(...),
                      note: str = Form(None)) -> JSONResponse:
    """Operator feedback: was this a real threat?

    Every labelled incident is a training row for the fusion model, which is
    the dataset the retraining notebook could not find.
    """
    ok = database.set_verdict(sanitize_filename(incident_id), verdict, note)
    if not ok:
        raise HTTPException(status_code=400, detail="Unknown incident or verdict")
    return JSONResponse({"status": "ok"})


@router.get("/api/evidence/verify", dependencies=[Depends(require_auth)])
async def api_verify_chain() -> JSONResponse:
    return JSONResponse(vault.verify_chain())


@router.get("/api/dispatch/latest", dependencies=[Depends(require_auth)])
async def api_dispatch_latest() -> JSONResponse:
    return JSONResponse(database.latest_pending_dispatch() or {})


@router.post("/api/dispatch/{package_id}/send/{authority}",
             dependencies=[Depends(require_auth)])
async def api_dispatch_send(package_id: str, authority: str) -> JSONResponse:
    if authority.upper() not in ("POLICE", "FIRE"):
        raise HTTPException(status_code=400,
                            detail="authority must be POLICE or FIRE")
    sent = await asyncio.to_thread(dispatch_service.send,
                                   sanitize_filename(package_id), authority)
    return JSONResponse({"status": "sent" if sent else "failed"})


# ── enrolment ─────────────────────────────────────────────────────────────────
@router.post("/authorized/upload", dependencies=[Depends(require_auth)])
async def upload_face(file: UploadFile = File(...)) -> JSONResponse:
    payload = await file.read()
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 10 MB")

    ok, reason = validate_image(file.filename or "", file.content_type, payload)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)

    name = sanitize_filename(file.filename or "")
    if not name:
        raise HTTPException(status_code=400, detail="Invalid filename")

    FACES_DIR.mkdir(parents=True, exist_ok=True)
    (FACES_DIR / name).write_bytes(payload)

    pipeline = get_pipeline()
    pipeline.identity.reload()
    if name not in [f"{n}" for n in pipeline.identity.enrolled]:
        # Surfaced rather than silently accepted: an image with no detectable
        # face is stored but contributes nothing to authorisation.
        return JSONResponse({"status": "warning", "filename": name,
                             "detail": "No face detected; not enrolled."})
    return JSONResponse({"status": "ok", "filename": name})


@router.delete("/api/authorized/{filename}", dependencies=[Depends(require_auth)])
async def delete_face(filename: str) -> JSONResponse:
    path = _safe_path(FACES_DIR, filename)
    path.unlink()
    get_pipeline().identity.reload()
    return JSONResponse({"status": "ok"})


# ── websocket ─────────────────────────────────────────────────────────────────
@router.websocket("/ws/threat")
async def ws_threat(websocket: WebSocket) -> None:
    password = _password()
    if not password or not verify_token(websocket.cookies.get("session_token"),
                                        password):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    interval = 1.0 / max(settings.pipeline.telemetry_fps, 0.5)
    try:
        while True:
            await websocket.send_text(state.ws_payload())
            await asyncio.sleep(interval)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.debug("websocket closed: %s", exc)


__all__ = ["router", "require_auth"]
