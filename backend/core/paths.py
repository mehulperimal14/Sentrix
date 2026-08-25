# core/paths.py
#
# Single source of truth for all runtime file paths.
# Resolves relative to the backend/ directory so the system works regardless
# of the current working directory — on macOS, Linux, and Windows.

from pathlib import Path

# backend/ directory — absolute, resolved
BASE_DIR = Path(__file__).parent.parent.resolve()

# Runtime directories (generated at runtime, not committed to git)
RUNTIME_DIR       = BASE_DIR / "runtime"
STATIC_DIR        = RUNTIME_DIR / "static"
ALERTS_DIR        = STATIC_DIR / "alerts"
EVIDENCE_DIR      = STATIC_DIR / "alerts" / "evidence"
AUTH_FACES_DIR    = STATIC_DIR / "authorized_faces"
TEMPLATES_DIR     = RUNTIME_DIR / "templates"
EVIDENCE_KEY_DIR  = RUNTIME_DIR / "evidence"
MODELS_DIR        = BASE_DIR / "models"

# String versions — for libraries that don't accept Path objects (cv2, etc.)
ALERTS_DIR_STR      = str(ALERTS_DIR)
EVIDENCE_DIR_STR    = str(EVIDENCE_DIR)
AUTH_FACES_DIR_STR  = str(AUTH_FACES_DIR)
MODELS_DIR_STR      = str(MODELS_DIR)


def ensure_runtime_dirs():
    """Create all runtime directories if they don't exist. Call once at startup."""
    for d in [ALERTS_DIR, EVIDENCE_DIR, AUTH_FACES_DIR, EVIDENCE_KEY_DIR]:
        d.mkdir(parents=True, exist_ok=True)
