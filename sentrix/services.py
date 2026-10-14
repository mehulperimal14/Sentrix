"""Background services of the device process: registry, start/stop, health.

One process owns the web app (LC), the SOS service, the link client, the
channel router and its outbox (decision: Pi process layout). Each of those is
a *service* here, built by a factory its track owns, started by the app
lifespan in a fixed order and stopped in reverse. Nothing starts unless its
own env flag is set (decision D13): a tool or test that imports the package,
or even builds the app without its lifespan, starts nothing.

Factories (``BUILTIN_FACTORIES``) are imported lazily; a module that does not
exist yet is skipped, so tracks can land independently. A factory has the
signature ``build_service(ctx: ServiceContext) -> Service | None`` and returns
None when its flag is off.

Measurement code (evaluation harness, replays, benchmarks) calls
:func:`assert_measurement_safe` on its pipeline.
"""

from __future__ import annotations

import importlib
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from sentrix.utils.logging import get_logger

log = get_logger("services")


@runtime_checkable
class Service(Protocol):
    """A background service. ``start`` must return quickly (spawn a thread or
    task); ``stop`` must be idempotent; ``health`` must not block."""

    name: str

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def health(self) -> dict[str, Any]: ...


@dataclass
class ServiceContext:
    """What a factory may use. ``get_pipeline`` is lazy (no models load until
    called); ``registry`` lets a later service find an earlier one (the SOS
    service finds ``channels``)."""

    settings: Any
    state: Any
    get_pipeline: Callable[[], Any]
    registry: "ServiceRegistry"
    extras: dict = field(default_factory=dict)


#: (service name, "module:factory"), in start order. Owners: docs/7 §2.
BUILTIN_FACTORIES: tuple[tuple[str, str], ...] = (
    ("node", "sentrix.io.node:build_service"),                    # T-HW telemetry
    ("gpio", "sentrix.io.gpio:build_service"),                    # T-HW LEDs/inputs
    ("channels", "sentrix.alerting.channels.router:build_service"),  # T-CH router + modem
    ("sos", "sentrix.sos.service:build_service"),                 # T-SOS engine + sweeper
    ("link", "sentrix.link.client:build_service"),                # T-LINK WSS client
)

RUNNING, STOPPED, FAILED, REGISTERED = "running", "stopped", "failed", "registered"


class ServiceRegistry:
    def __init__(self) -> None:
        self._services: dict[str, Any] = {}
        self._status: dict[str, str] = {}
        self._errors: dict[str, str] = {}
        self._lock = threading.Lock()

    # ── registration ──────────────────────────────────────────────────────────
    def register(self, service: Any, name: str | None = None) -> None:
        name = name or getattr(service, "name", None)
        if not name:
            raise ValueError("a service needs a name")
        with self._lock:
            if name in self._services:
                raise ValueError(f"service {name!r} is already registered")
            self._services[name] = service
            self._status[name] = REGISTERED

    def get(self, name: str) -> Any | None:
        with self._lock:
            return self._services.get(name)

    def names(self) -> list[str]:
        with self._lock:
            return list(self._services)

    def load_builtin(self, ctx: ServiceContext,
                     factories: tuple[tuple[str, str], ...] = BUILTIN_FACTORIES) -> list[str]:
        """Build every enabled built-in service; returns the names registered."""
        built = []
        for name, target in factories:
            module_name, _, attr = target.partition(":")
            try:
                module = importlib.import_module(module_name)
            except ModuleNotFoundError as exc:
                if exc.name and module_name.startswith(exc.name):
                    log.debug("service %s: %s not present yet", name, module_name)
                    continue
                self._fail(name, f"import failed: {exc}")
                continue
            except Exception as exc:
                self._fail(name, f"import failed: {exc}")
                continue
            try:
                service = getattr(module, attr)(ctx)
            except Exception as exc:
                self._fail(name, f"factory failed: {exc}")
                continue
            if service is None:
                log.info("service %s: off", name)
                continue
            self.register(service, name)
            built.append(name)
        return built

    def _fail(self, name: str, error: str) -> None:
        log.error("service %s: %s", name, error)
        with self._lock:
            self._status[name] = FAILED
            self._errors[name] = error

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def start_all(self) -> None:
        for name in self.names():
            if self._status.get(name) == RUNNING:
                continue
            try:
                self._services[name].start()
                with self._lock:
                    self._status[name] = RUNNING
                log.info("service %s started", name)
            except Exception as exc:
                self._fail(name, f"start failed: {exc}")

    def stop_all(self) -> None:
        for name in reversed(self.names()):
            if self._status.get(name) != RUNNING:
                continue
            try:
                self._services[name].stop()
            except Exception as exc:
                log.error("service %s: stop failed: %s", name, exc)
            with self._lock:
                self._status[name] = STOPPED

    def running(self) -> list[str]:
        with self._lock:
            return [n for n, s in self._status.items() if s == RUNNING]

    def clear(self) -> None:
        """Stop and forget everything (tests, and the end of the lifespan)."""
        self.stop_all()
        with self._lock:
            self._services.clear()
            self._status.clear()
            self._errors.clear()

    # ── health ────────────────────────────────────────────────────────────────
    def health(self) -> dict[str, dict[str, Any]]:
        """Per service: status plus the service's own ``health()`` fields."""
        out: dict[str, dict[str, Any]] = {}
        with self._lock:
            items = list(self._services.items())
            status, errors = dict(self._status), dict(self._errors)
        for name, service in items:
            entry: dict[str, Any] = {"status": status.get(name, REGISTERED)}
            try:
                entry.update(service.health() or {})
            except Exception as exc:
                entry["health_error"] = str(exc)
            out[name] = entry
        for name, error in errors.items():
            out.setdefault(name, {"status": FAILED})["error"] = error
        return out


#: The process-wide registry the app lifespan uses.
registry = ServiceRegistry()


def health_fields() -> dict[str, Any]:
    """Fields every ``/api/health`` response carries (docs/7 §9.3)."""
    from sentrix import mode
    from sentrix.alerting import guard
    return {"services": registry.health(), "send_guard": guard.status(),
            "test_mode": guard.test_mode(), "mode": mode.snapshot()}


class MeasurementUnsafe(AssertionError):
    """A measurement pipeline could act on the world."""


def assert_measurement_safe(pipeline: Any = None) -> None:
    """Refuse to measure with anything that could alert, link or sweep.

    Called by the evaluation harness, ``tools/replay.py`` and
    ``tools/bench_live.py``: the pipeline must use the null SOS service and no
    background service may be running in this process.
    """
    from sentrix.sos import NullSosService
    if pipeline is not None and not isinstance(getattr(pipeline, "sos", None),
                                               NullSosService):
        raise MeasurementUnsafe("measurement pipeline must use NullSosService")
    running = registry.running()
    if running:
        raise MeasurementUnsafe(f"background services running during measurement: "
                                f"{', '.join(running)}")


__all__ = ["Service", "ServiceContext", "ServiceRegistry", "registry",
           "BUILTIN_FACTORIES", "health_fields", "assert_measurement_safe",
           "MeasurementUnsafe"]
