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
import hmac
import os
from pathlib import Path

from fastapi import (APIRouter, Body, Depends, File, Form, HTTPException,
                     Request, UploadFile, status)
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               Response)
from fastapi.templating import Jinja2Templates
from starlette.websockets import WebSocket, WebSocketDisconnect

from sentrix import mode as runtime_mode
from sentrix import services
from sentrix.config import settings
from sentrix.evidence.vault import vault
from sentrix.paths import FACES_DIR, SNAPSHOT_DIR, TEMPLATES_DIR
from sentrix.pipeline.engine import get_pipeline
from sentrix.pipeline.state import state
from sentrix.storage import database
from sentrix.utils.logging import get_logger, telemetry
from sentrix.utils.redact import redact_url
from sentrix.perception.identity import person_name
from sentrix.sos import current_service as sos_service
from sentrix.web import auth
from sentrix.web.security import (RateLimiter, generate_token, login_limiter,
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


async def require_auth(request: Request) -> bool:
    """Every LC page and API: the device admin's session or a verified tunnel
    identity (``sentrix.web.auth.principal_of``). The principal is on
    ``request.state.lc_principal``.

    Unset password or no session: the browser goes to the login page, which
    says what to do, instead of a raw JSON 500 on the first page a newcomer
    opens. Tunnelled requests get 401 JSON instead (the Portal never rewrites
    a redirect to the device's login). T-WEB moves the APIs to 401 JSON too
    (``auth.require_lc``).
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


# ── auth ──────────────────────────────────────────────────────────────────────
@router.get("/", response_class=RedirectResponse)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    context = {"request": request}
    if not _password():
        context["error"] = ("No password is configured. Set SENTRIX_PASSWORD in "
                            ".env (copy .env.example) and restart SENTRIX.")
    return templates.TemplateResponse(request=request, name="login.html",
                                      context=context)


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
                     "error": "No password is configured. Set SENTRIX_PASSWORD "
                              "in .env (copy .env.example) and restart SENTRIX."})

    if not hmac.compare_digest(password.encode(), expected.encode()):
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
    enrolled = get_pipeline().identity.enrolled_files
    faces = [{"filename": p.name, "url": f"/media/face/{p.name}",
              "person": person_name(p.stem), "enrolled": p.name in enrolled}
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


def health_payload() -> dict:
    """The ``/api/health`` fields (also the link's ``hello.health``). Redacted:
    no camera credentials, no addresses."""
    pipeline = get_pipeline()
    live = state.get()
    return {
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
        "threat_sources": settings.threat.sources,
        "armed": runtime_mode.is_armed(),
        "violence_model_loaded": pipeline.violence is not None,
        "identity_backend": pipeline.identity.backend,
        # True while no face backend works, i.e. nobody can be authorised now.
        "identity_failing_closed": pipeline.identity.backend is None,
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
        # services, send_guard, test_mode, mode (docs/7 §9.3)
        **services.health_fields(),
    }


@router.get("/api/health", dependencies=[Depends(require_auth)])
async def api_health() -> JSONResponse:
    return JSONResponse(health_payload())


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
    if verdict == "FALSE":
        result = await asyncio.to_thread(sos_service().false_verdict, incident_id,
                                         pin=pin, actor=actor, channel=principal.channel,
                                         note=note)
        if not result["ok"]:
            raise auth.LcError(result["code"], result["message"])
        return JSONResponse({"status": "ok"})
    ok = database.set_verdict(incident_id, verdict, note, by=actor.ref,
                              channel=principal.channel,
                              actor_type=actor.event_actor_type)
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
                         "message": result["message"]})


@router.post("/api/mode", dependencies=[Depends(require_auth)])
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
                       via: str) -> JSONResponse:
    """Assess, store and enrol one image; the gallery is updated in place."""
    pipeline = get_pipeline()
    check = await asyncio.to_thread(pipeline.identity.assess, image, person)
    if not check.ok:
        pipeline.events.audit("gallery_rejected", person=person, via=via,
                              reason=check.reason)
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
    pipeline.events.audit("gallery_enrolled", person=name, file=filename, via=via)
    log.info("enrolled %s from %s (%s)", name, via, filename)
    return JSONResponse({"status": "ok", "person": name, "filename": filename})


@router.post("/authorized/upload", dependencies=[Depends(require_auth)])
async def upload_face(file: UploadFile = File(...),
                      person: str = Form(None)) -> JSONResponse:
    payload = await file.read()
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
    who = person_name(Path(person or name).stem)
    return await _enrol_image(image, who, payload, Path(name).suffix.lower(), "upload")


@router.post("/api/authorized/capture", dependencies=[Depends(require_auth)])
async def capture_face(request: Request, person: str = Form(...)) -> JSONResponse:
    """Enrol from the monitoring camera's current frame.

    The page used to ask the browser for the webcam, which the server already
    holds, so it failed with NotReadableError on the same machine.
    """
    client = request.client.host if request.client else "unknown"
    if not capture_limiter.allow(client):
        raise HTTPException(status_code=429, detail="Too many captures; wait a minute")
    who = person_name(sanitize_filename(person).replace(" ", "_").strip("._-"))
    if not who:
        raise HTTPException(status_code=400, detail="Enter a name first")
    frame = get_pipeline().latest_raw()
    if frame is None:
        raise HTTPException(status_code=409, detail="No camera frame available")
    return await _enrol_image(frame, who, None, ".jpg", "camera")


@router.get("/api/authorized/preview.jpg", dependencies=[Depends(require_auth)])
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


@router.delete("/api/authorized/{filename}", dependencies=[Depends(require_auth)])
async def delete_face(filename: str) -> JSONResponse:
    path = _safe_path(FACES_DIR, filename)
    path.unlink()
    pipeline = get_pipeline()
    pipeline.identity.remove(path.name)
    pipeline.events.audit("gallery_removed", person=person_name(path.stem), file=path.name)
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
