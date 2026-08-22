# app.py
#
# ARCHITECTURE: FastAPI application entry point.
# Uses lifespan context to init DB and engines at startup, then starts the
# background processing loop in a daemon thread (~30 fps).
# Mounts static files, includes page/API/WebSocket router and streaming router.
# Shutdown: signals stop_event, joins the processing thread (3s timeout),
#   then calls SystemEngine.shutdown() to release cameras, audio, and voice threads.
# Run with: python app.py

import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

# ── Path bootstrap ────────────────────────────────────────────────────────────
# Resolve the repo root (one level above backend/) and add it to sys.path so
# all intra-repo imports (ai/, core/, db/, etc.) continue to resolve correctly
# regardless of the working directory used to launch the app.
_REPO_ROOT = Path(__file__).resolve().parent.parent  # SentrixV2-main/
if str(_REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "backend"))

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

load_dotenv()

import db.database as database
import core.engine_instance as engine_instance
from web.routes    import router as main_router
from web.streaming import router as streaming_router

import logging
# Suppress repetitive HTTP GET logs from Uvicorn
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

# Graceful shutdown event — set during lifespan teardown
_stop_event = threading.Event()


def processing_loop():
    """Background thread: calls SystemEngine.process() at ~30 fps."""
    sys_engine = engine_instance.get_system_engine()
    while not _stop_event.is_set():
        try:
            sys_engine.process()
        except Exception as e:
            print(f"[App] Processing loop error (non-fatal): {e}")
        time.sleep(0.033)  # ~30 fps cap
    print("[App] Processing loop exited cleanly.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[App] Starting SENTRIX...")
    database.init_db()
    engine_instance.initialize_all()

    # Run once-at-boot retention prune (non-fatal if it fails)
    try:
        database.prune_old_events()
        database.prune_old_snapshots()
    except Exception as e:
        print(f"[App] Retention prune error (non-fatal): {e}")

    bg_thread = threading.Thread(target=processing_loop, daemon=True, name="sentrix-processor")
    bg_thread.start()

    print("[App] Dashboard: http://127.0.0.1:8000/dashboard")
    yield

    # ── Shutdown ──────────────────────────────────────────────────
    print("[App] Shutting down SENTRIX...")
    _stop_event.set()

    # Give the processing thread up to 3 seconds to finish its current frame
    bg_thread.join(timeout=3.0)
    if bg_thread.is_alive():
        print("[App] Processing thread did not stop in time — continuing teardown.")

    # Release hardware and background AI threads
    try:
        engine_instance.get_system_engine().shutdown()
    except Exception as e:
        print(f"[App] Engine shutdown error (non-fatal): {e}")

    print("[App] Shutdown complete.")


app = FastAPI(
    title="SENTRIX",
    description="Intelligent Multimodal Home Security System",
    version="2.0.0",
    lifespan=lifespan,
)

# Static files are now at frontend/static/ relative to the repo root
_STATIC_DIR = str(_REPO_ROOT / "frontend" / "static")
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
app.include_router(main_router)
app.include_router(streaming_router)

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)