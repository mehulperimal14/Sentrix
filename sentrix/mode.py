"""Runtime HOME / ARMED mode (decision D2, remote toggle in docs/7 §5.6).

``SENTRIX_ARMED`` (``settings.threat.armed``) is the mode at start-up. The
Portal's ``mode.set`` command, the LC toggle and the SOS engine's disarm (SOS
PIN or an authorised face during the entry delay) change it at runtime through
:func:`set_armed`; the TCI engine reads :func:`armed_override` on every tick.
Until something overrides it, the configured value applies unchanged, so
replays and tests behave exactly as before.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

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


def set_armed(armed: bool, by: str | None = None) -> dict:
    """Switch mode at runtime; publishes the ``mode`` state block. Callers
    check PINs and roles first (disarming needs the SOS PIN)."""
    global _override, _changed_by, _changed_at
    with _lock:
        _override = bool(armed)
        _changed_by = by
        _changed_at = datetime.now(timezone.utc).isoformat()
        block = snapshot()
    from sentrix.pipeline.state import state
    state.update(mode=block)
    return block


def clear_override() -> None:
    """Back to the configured mode (tests, factory reset)."""
    global _override, _changed_by, _changed_at
    with _lock:
        _override, _changed_by, _changed_at = None, None, None


def snapshot() -> dict:
    return {"armed": is_armed(),
            "source": "config" if _override is None else "runtime",
            "changed_by": _changed_by, "changed_at": _changed_at}


__all__ = ["armed_override", "is_armed", "set_armed", "clear_override", "snapshot"]
