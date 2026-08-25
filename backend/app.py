# backend/app.py
#
# SENTRIX — FastAPI application entry point.
#
# ARCHITECTURE:
#   - Uses lifespan context manager to init DB and engines at startup.
#   - Starts the background processing loop in a daemon thread (~30 fps).
#   - Mounts static files from runtime/static, templates from runtime/templates.
#   - Shutdown: signals stop_event, joins processing thread (3s), releases engines.
#
# CROSS-PLATFORM: Works on macOS (M-series MPS), Linux, and Windows.
# Run with:  python app.py
#            or: uvicorn app:app --host 0.0.0.0 --port 8000 --reload

import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

# Enable PyTorch MPS fallback for ops like NMS on Apple Silicon
os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"
os.environ["POLARS_SKIP_CPU_CHECK"] = "1"

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

# ── Path Setup ────────────────────────────────────────────────────────────────
# Ensure the backend/ directory is the working root for all relative imports.
BASE_DIR = Path(__file__).parent.resolve()
STATIC_DIR   = BASE_DIR / "runtime" / "static"
TEMPLATE_DIR = BASE_DIR / "runtime" / "templates"
MODELS_DIR   = BASE_DIR / "models"

# Create runtime directories if they don't exist (important on fresh clone)
for d in [
    STATIC_DIR / "alerts" / "evidence",
    STATIC_DIR / "authorized_faces",
    BASE_DIR / "runtime" / "evidence",
]:
    d.mkdir(parents=True, exist_ok=True)

load_dotenv(BASE_DIR / ".env")

# ── Imports (after path setup) ─────────────────────────────────────────────────
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

    bg_thread = threading.Thread(
        target=processing_loop, daemon=True, name="sentrix-processor"
    )
    bg_thread.start()

    print("[App] [OK] Dashboard: http://127.0.0.1:8000/dashboard")
    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
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
    description="Intelligent Multimodal Physical Security & Threat Orchestration Platform",
    version="2.1.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(main_router)
app.include_router(streaming_router)

if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        # Windows-compatible: use "spawn" start method if needed
    )