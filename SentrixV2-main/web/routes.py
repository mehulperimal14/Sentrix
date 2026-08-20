# web/routes.py
#
# ARCHITECTURE: FastAPI router. All page routes render Jinja2 templates.
# All /api/* routes return JSON. WebSocket at /ws/threat sends state every 0.5s.
# Dispatch actions (POST) call DispatchService and return JSON status.
#
# SECURITY:
#   - check_auth() verifies HMAC-SHA256 signed session tokens (not raw passwords).
#   - All sensitive endpoints (upload, delete, API, WebSocket) require valid session.
#   - File uploads are sanitized and validated before writing to disk.

import asyncio
import os

from fastapi import APIRouter, Request, UploadFile, File, Depends, Form, Cookie, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.websockets import WebSocket, WebSocketDisconnect

import db.database as database
from core.engine_instance  import engine, get_system_engine
from core.health_monitor   import health_monitor
from core.dispatch_service import dispatch_service
from core.encrypted_evidence import encrypted_evidence
from core.state            import state
from core.security         import verify_token, generate_token, sanitize_filename, validate_image_upload

router    = APIRouter()
templates = Jinja2Templates(directory="templates")

AUTHORIZED_DIR = "static/authorized_faces"
ALERTS_DIR     = "static/alerts"


# Root redirect
@router.get("/", response_class=RedirectResponse)
async def root():
    return RedirectResponse(url="/dashboard")


# ── Auth Dependency ────────────────────────────────────────────────────────────

async def check_auth(request: Request, session_token: str = Cookie(None)):
    """Verify HMAC-signed session token. Redirect to login on failure."""
    expected_pwd = os.getenv("SENTRIX_PASSWORD")
    if not expected_pwd:
        raise HTTPException(status_code=500, detail="Server configuration error: password not set.")
    if not verify_token(session_token, expected_pwd):
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/login"},
        )
    return True


# ── Auth Routes ────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_view(request: Request):
    return templates.TemplateResponse(request=request, name="login.html", context={"request": request})


@router.post("/login")
async def login_post(request: Request, password: str = Form(...)):
    expected_pwd = os.getenv("SENTRIX_PASSWORD")
    if not expected_pwd:
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"request": request, "error": "Server misconfiguration"},
        )
    if password == expected_pwd:
        # Issue HMAC-signed session token (not the raw password)
        token = generate_token(password)
        response = RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
        response.set_cookie(
            key="session_token", value=token,
            httponly=True, secure=False,   # Set secure=True behind HTTPS
            samesite="lax", max_age=43200, # 12 hours
        )
        return response
    else:
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"request": request, "error": "Invalid password"},
        )


@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/login")
    response.delete_cookie("session_token")
    return response


# ── Page Routes (all require auth) ────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(check_auth)])
async def dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="dashboard.html", context={"request": request})


@router.get("/live", response_class=HTMLResponse, dependencies=[Depends(check_auth)])
async def live_view(request: Request):
    return templates.TemplateResponse(request=request, name="live.html", context={"request": request})


@router.get("/events", response_class=HTMLResponse, dependencies=[Depends(check_auth)])
async def events_view(request: Request):
    events = database.get_recent_events(limit=100)
    return templates.TemplateResponse(request=request, name="events.html", context={"request": request, "events": events})


@router.get("/alerts", response_class=HTMLResponse, dependencies=[Depends(check_auth)])
async def alerts_view(request: Request):
    images = []
    if os.path.isdir(ALERTS_DIR):
        for fname in sorted(os.listdir(ALERTS_DIR), reverse=True):
            if fname.lower().endswith((".jpg", ".jpeg", ".png")):
                images.append(f"/static/alerts/{fname}")
    return templates.TemplateResponse(request=request, name="alerts.html", context={"request": request, "images": images})


@router.get("/evidence", response_class=HTMLResponse, dependencies=[Depends(check_auth)])
async def evidence_view(request: Request):
    items = encrypted_evidence.list_evidence()
    return templates.TemplateResponse(request=request, name="evidence.html", context={"request": request, "items": items})


@router.get("/dispatch", response_class=HTMLResponse, dependencies=[Depends(check_auth)])
async def dispatch_view(request: Request):
    packages = database.get_all_dispatch_packages()
    return templates.TemplateResponse(request=request, name="dispatch.html", context={"request": request, "packages": packages})


@router.get("/authorized", response_class=HTMLResponse, dependencies=[Depends(check_auth)])
async def authorized_view(request: Request):
    faces = []
    if os.path.isdir(AUTHORIZED_DIR):
        for fname in os.listdir(AUTHORIZED_DIR):
            if fname.lower().endswith((".jpg", ".jpeg", ".png")):
                faces.append({"filename": fname, "url": f"/static/authorized_faces/{fname}"})
    return templates.TemplateResponse(request=request, name="authorized.html", context={"request": request, "faces": faces})


# ── Mutating Routes (all require auth) ────────────────────────────────────────

@router.post("/authorized/upload", dependencies=[Depends(check_auth)])
async def upload_face(file: UploadFile = File(...)):
    """Upload a face image for enrollment. Auth-guarded and sanitized."""
    try:
        # Validate file type before writing
        if not validate_image_upload(file.filename, file.content_type):
            raise HTTPException(status_code=400, detail="Only JPEG and PNG image files are allowed.")

        os.makedirs(AUTHORIZED_DIR, exist_ok=True)
        safe_filename = sanitize_filename(file.filename)
        if not safe_filename or safe_filename == "upload":
            raise HTTPException(status_code=400, detail="Invalid filename.")

        dest = os.path.join(AUTHORIZED_DIR, safe_filename)
        content = await file.read()

        # Basic file size guard (10MB max)
        if len(content) > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="File too large. Maximum 10MB.")

        with open(dest, "wb") as f:
            f.write(content)

        # Reload face encodings hot
        get_system_engine().face.reload_encodings()
        return JSONResponse({"status": "ok", "filename": safe_filename})
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)


@router.delete("/api/authorized/{filename}", dependencies=[Depends(check_auth)])
async def delete_authorized_face(filename: str):
    """Delete an enrolled face image. Auth-guarded and path-traversal safe."""
    safe_filename = sanitize_filename(filename)
    target_path = os.path.join(AUTHORIZED_DIR, safe_filename)

    if os.path.exists(target_path):
        os.remove(target_path)
        get_system_engine().face.reload_encodings()
        return JSONResponse({"status": "success", "message": "Face deleted"})

    raise HTTPException(status_code=404, detail="File not found")


# ── API Routes (all require auth) ─────────────────────────────────────────────

@router.get("/api/metrics", dependencies=[Depends(check_auth)])
async def get_metrics():
    return JSONResponse(content=state.get())


@router.get("/api/health", dependencies=[Depends(check_auth)])
async def get_health():
    return JSONResponse(content=health_monitor.get())


@router.get("/api/dispatch/latest", dependencies=[Depends(check_auth)])
async def dispatch_latest():
    pkg = database.get_latest_pending_dispatch()
    return JSONResponse(content=pkg or {})


@router.post("/api/dispatch/{dispatch_id}/send/{authority}", dependencies=[Depends(check_auth)])
async def dispatch_send(dispatch_id: str, authority: str):
    authority = authority.upper()
    if authority not in ("POLICE", "FIRE"):
        return JSONResponse({"status": "error", "detail": "authority must be POLICE or FIRE"}, status_code=400)
    success = await asyncio.to_thread(dispatch_service.dispatch, dispatch_id, authority)
    return JSONResponse({"status": "sent" if success else "error"})


# ── WebSocket — pushes state every 0.5s (requires auth cookie) ────────────────

@router.websocket("/ws/threat")
async def ws_threat(websocket: WebSocket):
    # Validate session token from cookie before accepting
    session_token = websocket.cookies.get("session_token")
    expected_pwd  = os.getenv("SENTRIX_PASSWORD")
    if not expected_pwd or not verify_token(session_token, expected_pwd):
        await websocket.close(code=1008)  # 1008 = Policy Violation
        return

    await websocket.accept()
    try:
        while True:
            payload = state.get_ws_payload()
            await websocket.send_text(payload)
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[WS] /ws/threat error: {e}")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass