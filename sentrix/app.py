"""FastAPI application factory and the live processing loop."""

from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from sentrix.paths import ROOT_DIR, STATIC_DIR, ensure_runtime_dirs
from sentrix.utils.logging import configure, get_logger

# Load .env before anything reads os.getenv at import time.
load_dotenv(ROOT_DIR / ".env")
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

configure()
log = get_logger("app")

_stop = threading.Event()
_thread: threading.Thread | None = None


def _loop() -> None:
    """Live loop: capture and preview on this thread, inference on its own.

    ``Pipeline.run_live`` publishes every camera frame (with the latest
    overlay) to the preview and hands the newest one to the inference thread,
    which runs at most ``inference_fps``. The video never waits for the AI.
    """
    from sentrix.pipeline.engine import get_pipeline

    try:
        get_pipeline().run_live(_stop)
    except Exception as exc:
        log.error("live loop stopped: %s", exc, exc_info=True)
    log.info("processing loop exited")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from sentrix.pipeline.engine import get_pipeline, shutdown_pipeline
    from sentrix.storage import database

    global _thread
    log.info("starting SENTRIX")
    ensure_runtime_dirs()
    database.init_db()

    if not os.getenv("SENTRIX_PASSWORD", "").strip():
        log.warning("SENTRIX_PASSWORD is not set — the dashboard will refuse "
                    "to authenticate. Copy .env.example to .env and set it.")

    get_pipeline()          # build eagerly so startup failures surface here
    try:
        database.prune(int(os.getenv("RETENTION_DAYS", "30")))
    except Exception as exc:
        log.warning("retention prune skipped: %s", exc)

    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="sentrix-pipeline")
    _thread.start()
    log.info("dashboard: http://127.0.0.1:8000/dashboard")

    yield

    log.info("stopping SENTRIX")
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=5.0)
    shutdown_pipeline()
    log.info("shutdown complete")


def create_app() -> FastAPI:
    from sentrix.web.routes import router as main_router
    from sentrix.web.streaming import router as stream_router

    app = FastAPI(
        title="SENTRIX",
        description="Multimodal edge threat intelligence and physical security",
        version="3.0.0",
        lifespan=lifespan,
    )

    # Package assets only. Runtime output lives in var/ and is served through
    # authenticated /media/* routes — see web/routes.py for why.
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(main_router)
    app.include_router(stream_router)
    return app


app = create_app()


def main() -> None:
    import uvicorn
    uvicorn.run(app, host=os.getenv("SENTRIX_HOST", "127.0.0.1"),
                port=int(os.getenv("SENTRIX_PORT", "8000")), log_level="warning")


if __name__ == "__main__":
    main()
