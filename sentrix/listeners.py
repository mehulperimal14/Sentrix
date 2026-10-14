"""In-process change listeners: how services hear about writes they do not own.

The pipeline, the database, the evidence vault, the event log and the runtime
mode each announce what they just did; the link client (mirror events for the
Portal), the SOS engine (a verdict that cancelled a request) and the channel
router subscribe. Nothing registers in tests, replays or tools unless a test
does so itself (``tests/conftest.py`` clears the registry after every test).

Kinds and their payloads (docs/7 §6.3):

``incident``  after ``database.save_incident`` and for rows ``close_interrupted`` ends
``dispatch``  after ``save_dispatch`` / ``update_dispatch_status`` (``get_dispatch`` dict)
``verdict``   after ``set_verdict`` (incident_id, verdict, effect, request_id, ...)
``evidence``  after ``vault.save`` sealed a bundle (metadata only, no image)
``snapshot``  after the pipeline saved an incident snapshot (name, incident_id)
``audit``     after ``EventLog.audit`` wrote a row (gallery changes, mode_set, ...)
``mode``      after ``sentrix.mode.set_armed`` (armed, by, at)

Callbacks run after the write committed, on the writer's thread (usually the
pipeline's FIFO worker or a web request thread): enqueue and return. A failing
callback is logged and never breaks the write. Stdlib only.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

log = logging.getLogger("sentrix.listeners")

KINDS = ("incident", "dispatch", "verdict", "evidence", "snapshot", "audit", "mode")

_LISTENERS: dict[str, list[Callable[[dict], None]]] = {}
_LOCK = threading.Lock()


def add_listener(kind: str, callback: Callable[[dict], None]) -> None:
    if kind not in KINDS:
        raise ValueError(f"unknown listener kind {kind!r}")
    with _LOCK:
        _LISTENERS.setdefault(kind, []).append(callback)


def remove_listener(kind: str, callback: Callable[[dict], None]) -> None:
    with _LOCK:
        if callback in _LISTENERS.get(kind, []):
            _LISTENERS[kind].remove(callback)


def has_listeners(kind: str) -> bool:
    with _LOCK:
        return bool(_LISTENERS.get(kind))


def emit(kind: str, payload: Callable[[], dict] | dict) -> None:
    """Call every listener of ``kind``. ``payload`` may be a callable so the
    dict is only built when someone listens."""
    with _LOCK:
        callbacks = list(_LISTENERS.get(kind, ()))
    if not callbacks:
        return
    try:
        data: Any = payload() if callable(payload) else payload
    except Exception as exc:                  # building the payload must not break a write
        log.error("%s listener payload failed: %s", kind, exc)
        return
    for callback in callbacks:
        try:
            callback(data)
        except Exception as exc:              # a listener never breaks a write
            log.error("%s listener failed: %s", kind, exc)


def clear() -> None:
    """Forget every listener (tests; the end of the app lifespan)."""
    with _LOCK:
        _LISTENERS.clear()


__all__ = ["KINDS", "add_listener", "remove_listener", "has_listeners", "emit", "clear"]
