"""``GET /api/level-history`` and the Wi-Fi setup helpers (docs/7 §9.2).

Owner: track T-WEB. Included by ``sentrix.web.extensions``.

* ``/api/level-history`` is the dashboard's Event Log history: the last 50
  committed level changes, so the log survives a reload.
* Wi-Fi setup mode (``SENTRIX_SETUP_MODE=1``, the unit's button): the device
  app's ``/setup/wifi`` screen calls ``/api/setup/wifi*``
  (``sentrix.web.api_routes``), which uses the helpers here: whether setup mode
  is on, T-HW's Wi-Fi control (``sentrix.io.network``: ``scan``, ``connect``,
  ``status``), the label's one-time setup code (checked against
  ``setup_code_digest`` in the device identity file) and its limiter, 5 tries
  per 10 minutes. Setup mode exists only on the LAN: the tunnel never carries
  ``/api/setup/*`` and the hardening refuses it for tunnel identities.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import re
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from sentrix.config import settings
from sentrix.pipeline.state import state
from sentrix.utils.logging import get_logger
from sentrix.web import auth
from sentrix.web.routes import redacted
from sentrix.web.security import RateLimiter

log = get_logger("setup_routes")
router = APIRouter()

#: Setup-code attempts per client (the code is short and printed on a label).
setup_limiter = RateLimiter(max_attempts=5, window_seconds=600.0)


def setup_mode_enabled() -> bool:
    """``SENTRIX_SETUP_MODE`` (a function so tests can switch it)."""
    return bool(settings.web.setup_mode)


@router.get("/api/level-history", dependencies=[Depends(auth.require_lc())])
async def api_level_history() -> JSONResponse:
    """The last 50 committed level changes, oldest first (docs/7 §9.2)."""
    return JSONResponse(redacted(state.level_history()))


# ── Wi-Fi setup helpers ───────────────────────────────────────────────────────
def _network():
    """T-HW's Wi-Fi control (``sentrix.io.network``), or None on this build."""
    try:
        return importlib.import_module("sentrix.io.network")
    except ImportError:
        return None


def _setup_digest() -> str | None:
    """``setup_code_digest`` from the device identity file, or None."""
    try:
        from sentrix.link.profile import identity_dir
        data = json.loads((identity_dir() / "identity.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("setup code check unavailable: %s", exc)
        return None
    digest = str(data.get("setup_code_digest") or "").strip().lower()
    return digest if re.fullmatch(r"[0-9a-f]{64}", digest) else None


def code_matches(code: str, digest: str) -> bool:
    """The typed code against the label's SHA-256 digest. Spaces, hyphens and
    letter case are forgiven (``kt rw-bqxl`` = ``KTRW-BQXL``)."""
    typed = (code or "").strip()
    if not typed:
        return False
    compact = re.sub(r"[\s-]", "", typed).upper()
    grouped = f"{compact[:4]}-{compact[4:]}" if len(compact) == 8 else compact
    matched = False
    for candidate in {typed, typed.upper(), compact, grouped}:
        actual = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        matched |= hmac.compare_digest(actual, digest)
    return matched


def _network_status(network) -> dict[str, Any]:
    try:
        return dict(network.status() or {})
    except Exception as exc:
        log.warning("network status failed: %s", exc)
        return {"connected": False, "error": "status unavailable"}


__all__ = ["router", "setup_mode_enabled", "code_matches", "setup_limiter"]
