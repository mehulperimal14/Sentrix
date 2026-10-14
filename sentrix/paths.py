"""Filesystem layout.

Every path derives from the repository root, resolved from this file's location,
so the process works regardless of the current working directory.

Two rules that the previous layout broke:

1. **Runtime output never lives inside the package.** It goes to ``var/``, which
   is gitignored. Previously 642 MB of alert snapshots accumulated inside
   ``backend/runtime/static/`` and were committed to the repository.
2. **Nothing sensitive lives under a static mount.** ``var/`` is not mounted by
   the web layer. Evidence, snapshots and enrolled faces are served only through
   authenticated routes. Previously they sat under ``/static`` and were readable
   by anyone who could reach the port.
"""

from __future__ import annotations

from pathlib import Path

#: ``<repo>/sentrix/paths.py`` -> ``<repo>``
ROOT_DIR = Path(__file__).resolve().parent.parent

PACKAGE_DIR = ROOT_DIR / "sentrix"
MODELS_DIR = ROOT_DIR / "models"
TRAINING_DIR = ROOT_DIR / "training"
DOCS_DIR = ROOT_DIR / "docs"

# Web assets ship with the package.
WEB_DIR = PACKAGE_DIR / "web"
STATIC_DIR = WEB_DIR / "static"
TEMPLATES_DIR = WEB_DIR / "templates"

# Runtime output — gitignored, never web-mounted.
VAR_DIR = ROOT_DIR / "var"
EVIDENCE_DIR = VAR_DIR / "evidence"
SNAPSHOT_DIR = VAR_DIR / "snapshots"
EVENTLOG_DIR = VAR_DIR / "events"
FACES_DIR = VAR_DIR / "authorized_faces"
LOG_DIR = VAR_DIR / "logs"
#: Per-deployment calibration (thresholds fitted on this room's normal footage).
CALIBRATION_DIR = VAR_DIR / "calibration"
DB_PATH = VAR_DIR / "sentrix.db"
#: Development fallback for the device identity (docs/7 §5.2). On a Pi the
#: identity lives in /etc/sentrix so a factory reset (wipe var/) keeps it.
LINK_DIR = VAR_DIR / "link"
#: The Portal's development SQLite database and files (sentrix_portal).
PORTAL_VAR_DIR = VAR_DIR / "portal"

_RUNTIME_DIRS = (
    VAR_DIR, EVIDENCE_DIR, SNAPSHOT_DIR, EVENTLOG_DIR, FACES_DIR, LOG_DIR,
)


def ensure_runtime_dirs() -> None:
    """Create every runtime directory. Idempotent; call once at startup."""
    for directory in _RUNTIME_DIRS:
        directory.mkdir(parents=True, exist_ok=True)


def model_path(*parts: str) -> Path:
    """Resolve a path beneath ``models/``."""
    return MODELS_DIR.joinpath(*parts)


__all__ = [
    "ROOT_DIR", "PACKAGE_DIR", "MODELS_DIR", "TRAINING_DIR", "DOCS_DIR",
    "WEB_DIR", "STATIC_DIR", "TEMPLATES_DIR",
    "VAR_DIR", "EVIDENCE_DIR", "SNAPSHOT_DIR", "EVENTLOG_DIR", "FACES_DIR",
    "LOG_DIR", "CALIBRATION_DIR", "DB_PATH", "LINK_DIR", "PORTAL_VAR_DIR",
    "ensure_runtime_dirs", "model_path",
]
