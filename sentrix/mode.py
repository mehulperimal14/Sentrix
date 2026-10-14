"""Runtime HOME / ARMED mode (decision D2, remote toggle in docs/7 §5.6).

``SENTRIX_ARMED`` (``settings.threat.armed``) is the mode at first start. The
Portal's ``mode.set`` command, the LC toggle and the SOS engine's disarm (SOS
PIN, or an authorised face during the armed-intrusion entry delay) change it
at runtime through :func:`set_armed`; the TCI engine reads
:func:`armed_override` on every tick.

The choice survives a restart: :func:`set_armed` writes ``var/mode.json``
(atomic replace) and the app lifespan calls :func:`load` at start-up, so a
power cut cannot silently disarm the house. Only the live app loads it:
replays, tools and tests that never call :func:`load` keep the configured
value, so their results do not depend on what someone last chose on the LC.

Every change publishes the ``state.mode`` block, emits the ``mode`` listener
event (the link client turns it into ``mode.changed``) and writes a
``mode_set`` audit row to the live pipeline's event log.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from sentrix.paths import VAR_DIR

#: Where the runtime choice is kept (tests point it at a temporary file).
MODE_PATH: Path = VAR_DIR / "mode.json"

_lock = threading.Lock()
_override: bool | None = None
_changed_by: str | None = None
_changed_at: str | None = None


def armed_override() -> bool | None:
    """The runtime choice, or None while the configured value applies."""
    return _override


def is_armed() -> bool:
    if _override is not None:
        return _override
    from sentrix.config import settings
    return settings.threat.armed


def _persist(armed: bool, by: str | None, at: str) -> None:
    path = Path(MODE_PATH)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"armed": armed, "by": by, "at": at}),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        from sentrix.utils.logging import get_logger
        get_logger("mode").error("could not persist the mode to %s: %s", path, exc)


def _audit(block: dict) -> None:
    """``mode_set`` row in the live pipeline's event log, if one is running.
    Never builds a pipeline (and so never loads a model) just to log."""
    engine = sys.modules.get("sentrix.pipeline.engine")
    pipeline = getattr(engine, "_pipeline", None) if engine else None
    if pipeline is not None:
        try:
            pipeline.events.audit("mode_set", armed=block["armed"],
                                  by=block["changed_by"])
        except Exception:
            pass


def set_armed(armed: bool, by: str | None = None, *, persist: bool = True) -> dict:
    """Switch mode at runtime. Callers check PINs and roles first (disarming
    needs the SOS PIN; ``sos.set_mode``). ``by`` is an ``Actor.ref``
    (``face:<name>`` for a disarm by an authorised face)."""
    global _override, _changed_by, _changed_at
    with _lock:
        _override = bool(armed)
        _changed_by = by
        _changed_at = datetime.now(timezone.utc).isoformat()
        block = snapshot()
        if persist:
            _persist(_override, by, _changed_at)
    from sentrix import listeners
    from sentrix.pipeline.state import state
    state.update(mode=block)
    listeners.emit("mode", {"armed": block["armed"], "by": by, "at": _changed_at,
                            "source": block["source"]})
    _audit(block)
    return block


def load() -> dict:
    """Restore the persisted choice (app start-up only). A missing or
    unreadable file keeps the configured value."""
    global _override, _changed_by, _changed_at
    try:
        data = json.loads(Path(MODE_PATH).read_text(encoding="utf-8"))
        armed = data["armed"]
        if not isinstance(armed, bool):
            raise ValueError("armed must be a boolean")
    except FileNotFoundError:
        return snapshot()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        from sentrix.utils.logging import get_logger
        get_logger("mode").warning("ignoring %s: %s", MODE_PATH, exc)
        return snapshot()
    with _lock:
        _override = armed
        _changed_by = data.get("by")
        _changed_at = data.get("at")
        block = snapshot()
    from sentrix.pipeline.state import state
    state.update(mode=block)
    return block


def clear_override(*, forget: bool = False) -> None:
    """Back to the configured mode (tests, factory reset). ``forget`` also
    deletes the persisted choice."""
    global _override, _changed_by, _changed_at
    with _lock:
        _override, _changed_by, _changed_at = None, None, None
        if forget:
            try:
                Path(MODE_PATH).unlink()
            except FileNotFoundError:
                pass


def snapshot() -> dict:
    return {"armed": is_armed(),
            "source": "config" if _override is None else "runtime",
            "changed_by": _changed_by, "changed_at": _changed_at}


__all__ = ["armed_override", "is_armed", "set_armed", "load", "clear_override",
           "snapshot", "MODE_PATH"]
