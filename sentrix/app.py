"""FastAPI application factory and the live processing loop."""

from __future__ import annotations

import asyncio
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
# Windows without developer mode cannot symlink inside the Hugging Face cache;
# the copy fallback works, so silence its long warning.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

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


def _start_services(pipeline, app: FastAPI, loop: asyncio.AbstractEventLoop) -> None:
    """Build and start the enabled background services (docs/7 §3.6) and
    hand the SOS service to the pipeline. Every service is off unless its own
    env flag is set, so this is a no-op on a default install.

    ``ctx.extras``: ``app`` (this LC app: the tunnel executor replays requests
    into it with ``httpx.ASGITransport``) and ``loop`` (the server's event
    loop: a service thread schedules coroutines on it with
    ``asyncio.run_coroutine_threadsafe``; nothing may block it).
    """
    from sentrix import services
    from sentrix.config import settings
    from sentrix.pipeline.engine import get_pipeline
    from sentrix.pipeline.state import state

    if not settings.test_mode:
        log.warning("SENTRIX_TEST_MODE is OFF: live alerts can reach anyone the "
                    "profile names once their channel switches are on")
    ctx = services.ServiceContext(settings=settings, state=state,
                                  get_pipeline=get_pipeline,
                                  registry=services.registry,
                                  extras={"app": app, "loop": loop})
    services.registry.load_builtin(ctx)
    services.registry.start_all()
    sos = services.registry.get("sos")
    if sos is not None:
        pipeline.sos = sos
    if services.registry.get("channels") is None:
        _legacy_channels_block(settings, state)


def _legacy_channels_block(settings, state) -> None:
    """Without the channel router, the only outbound path is the legacy
    Twilio notifier (guarded): report it, so the dashboard's "no outbound
    channel" banner is true. The banner (``warn``) is shown only with SOS on."""
    from sentrix.alerting.notifier import notifier
    from sentrix.pipeline.state import CHANNELS_IDLE

    ready = bool(notifier.online)
    state.update(channels={**CHANNELS_IDLE, "legacy_twilio": ready,
                           "outbound_ready": ready,
                           "warn": bool(settings.sos.enabled and not ready)})


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start-up: database, pipeline, persisted HOME/ARMED, services, then the
    interrupted-incident cleanup (so the link mirrors it) and the live loop.
    Shutdown, in this order: the live loop, the pipeline (its worker drains
    queued side effects such as an SOS trigger and saves the open incident,
    while the services still run), then the services in reverse order."""
    from sentrix import listeners, services
    from sentrix import mode as runtime_mode
    from sentrix.pipeline.engine import get_pipeline, shutdown_pipeline
    from sentrix.storage import database

    global _thread
    log.info("starting SENTRIX")
    ensure_runtime_dirs()
    database.init_db()

    if not os.getenv("SENTRIX_PASSWORD", "").strip():
        log.warning("SENTRIX_PASSWORD is not set — the dashboard will refuse "
                    "to authenticate. Copy .env.example to .env and set it.")

    pipeline = get_pipeline()   # build eagerly so startup failures surface here
    runtime_mode.load()         # a power cut must not silently disarm the house
    try:
        database.prune(int(os.getenv("RETENTION_DAYS", "30")))
    except Exception as exc:
        log.warning("retention prune skipped: %s", exc)

    # Before the loop, so the first L3 can reach SOS.
    _start_services(pipeline, app, asyncio.get_running_loop())
    # After the services, so the link client hears the closures.
    database.close_interrupted(pipeline.session_id)

    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="sentrix-pipeline")
    _thread.start()
    log.info("dashboard: http://%s:%s/dashboard", os.getenv("SENTRIX_HOST", "127.0.0.1"),
             os.getenv("SENTRIX_PORT", "8000"))

    yield

    log.info("stopping SENTRIX")
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=5.0)
    shutdown_pipeline()            # drains queued side effects; services still listen
    services.registry.clear()      # stops link, SOS, channels, gpio, node (reverse order)
    listeners.clear()
    log.info("shutdown complete")


def create_app() -> FastAPI:
    from sentrix.config import settings
    from sentrix.web import extensions
    from sentrix.web.routes import router as main_router
    from sentrix.web.streaming import router as stream_router

    # The API map is not public: /docs, /redoc and /openapi.json exist only
    # with SENTRIX_API_DOCS=1 (docs/7 §9.1).
    docs = settings.web.api_docs
    app = FastAPI(
        title="SENTRIX",
        description="Multimodal edge threat intelligence and physical security",
        version="3.0.0",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )

    # Package assets only. Runtime output lives in var/ and is served through
    # authenticated /media/* routes — see web/routes.py for why.
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(main_router)
    app.include_router(stream_router)
    # Track routers (/api/sos, /api/node, ...) and the hardening middleware.
    extensions.install(app)
    return app


app = create_app()


def main() -> None:
    import uvicorn
    uvicorn.run(app, host=os.getenv("SENTRIX_HOST", "127.0.0.1"),
                port=int(os.getenv("SENTRIX_PORT", "8000")), log_level="warning")


if __name__ == "__main__":
    main()
