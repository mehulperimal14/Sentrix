"""LC pages /sos and /node, GET /api/level-history, and /setup/wifi (only while
SENTRIX_SETUP_MODE=1, docs/7 §9.2).

Owner: track T-WEB. Included by ``sentrix.web.extensions``.

* ``/sos`` is the assistance-request page and ``/node`` the device page; both
  are views over other tracks' JSON APIs (T-SOS ``/api/sos/*``, T-HW
  ``/api/node``, T-LINK ``/api/link``, T-CH ``/api/channels``), which the
  page scripts call. A missing API shows "not available on this build".
* ``/api/level-history`` is the dashboard's Event Log history: the last 50
  committed level changes, so the log survives a reload.
* ``/setup/wifi`` exists only in physical setup mode (the unit's button puts
  it there) and only on the LAN: the tunnel never carries ``/setup/*`` and the
  hardening refuses it for tunnel identities. It needs no console session
  (the household has none yet) but the one-time setup code printed on the
  label, checked against ``setup_code_digest`` in the device identity file,
  5 tries per 10 minutes. Wi-Fi control itself is T-HW's
  ``sentrix.io.network`` (``scan``, ``connect``, ``status``); without it the
  page says so.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib
import json
import re
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from sentrix.config import settings
from sentrix.pipeline.state import state
from sentrix.sos import current_service as sos_service
from sentrix.utils.logging import get_logger
from sentrix.web import auth
from sentrix.web.routes import redacted, render, require_auth, templates
from sentrix.web.security import RateLimiter

log = get_logger("setup_routes")
router = APIRouter()

#: Ids a page may be asked to select (a request id in ``/sos?request=``).
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
#: Setup-code attempts per client (the code is short and printed on a label).
setup_limiter = RateLimiter(max_attempts=5, window_seconds=600.0)


def setup_mode_enabled() -> bool:
    """``SENTRIX_SETUP_MODE`` (a function so tests can switch it)."""
    return bool(settings.web.setup_mode)


# ── pages ─────────────────────────────────────────────────────────────────────
@router.get("/sos", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def sos_page(request: Request) -> HTMLResponse:
    """Open request with its countdown and buttons, history, per-request
    audit trail and the SOS preview (data from ``/api/sos/*``).
    ``?request=<id>`` opens that request's audit trail."""
    selected = request.query_params.get("request") or ""
    service = sos_service()
    return render(request, "sos.html", "sos",
                  selected=selected if _ID.match(selected) else "",
                  sos_enabled=getattr(service, "enabled", True) is not False)


@router.get("/node", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def node_page(request: Request) -> HTMLResponse:
    """Device health: node telemetry, link, channels and modem, mode, the
    confirmed 1 s siren test, and the live configuration (read-only)."""
    live = state.get()
    return render(request, "node.html", "node", node=live.get("node") or {},
                  link=live.get("link") or {}, channels=live.get("channels") or {},
                  mode=live.get("mode") or {}, profile_name=settings.node.profile)


@router.get("/api/level-history", dependencies=[Depends(auth.require_lc())])
async def api_level_history() -> JSONResponse:
    """The last 50 committed level changes, oldest first (docs/7 §9.2)."""
    return JSONResponse(redacted(state.level_history()))


# ── /setup/wifi ───────────────────────────────────────────────────────────────
def _require_setup_mode(request: Request) -> None:
    """Outside setup mode, or through the tunnel, these routes do not exist."""
    if not setup_mode_enabled() or auth.is_tunnelled(request):
        raise HTTPException(status_code=404, detail="Not Found")


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


def _check_code(request: Request, code: str | None) -> None:
    client = request.client.host if request.client else "unknown"
    if not setup_limiter.allow(client):
        raise auth.LcError("rate_limited", "Too many wrong codes; wait 10 minutes",
                           headers={"Retry-After": "600"})
    digest = _setup_digest()
    if digest is None:
        raise auth.LcError("unsupported", "This unit has no setup code (it was not "
                                          "provisioned with tools/provision_device.py)")
    if not code_matches(code or "", digest):
        raise auth.LcError("pin_invalid", "That setup code is not the one on the label")
    setup_limiter.reset(client)


def _network_status(network) -> dict[str, Any]:
    try:
        return dict(network.status() or {})
    except Exception as exc:
        log.warning("network status failed: %s", exc)
        return {"connected": False, "error": "status unavailable"}


@router.get("/setup/wifi", response_class=HTMLResponse)
async def setup_wifi_page(request: Request) -> HTMLResponse:
    _require_setup_mode(request)
    return templates.TemplateResponse(
        request=request, name="setup_wifi.html",
        context={"request": request, "network_available": _network() is not None,
                 "test_mode": settings.test_mode})


@router.post("/setup/wifi/scan")
async def setup_wifi_scan(request: Request,
                          code: str | None = Body(None, embed=True)) -> JSONResponse:
    """Networks in range (needs the setup code)."""
    _require_setup_mode(request)
    _check_code(request, code)
    network = _network()
    if network is None:
        raise auth.LcError("unsupported", "Wi-Fi control is not available on this build")
    try:
        networks = await asyncio.to_thread(network.scan)
    except Exception as exc:
        log.warning("wifi scan failed: %s", exc)
        raise auth.LcError("internal", "Could not scan for networks")
    rows = []
    for item in networks or []:
        if not isinstance(item, dict) or not item.get("ssid"):
            continue
        rows.append({"ssid": str(item["ssid"])[:32], "signal": item.get("signal"),
                     "security": str(item.get("security") or "")[:32]})
    return JSONResponse({"networks": rows,
                         "status": await asyncio.to_thread(_network_status, network)})


@router.post("/setup/wifi")
async def setup_wifi_connect(request: Request,
                             code: str | None = Body(None, embed=True),
                             ssid: str | None = Body(None, embed=True),
                             psk: str | None = Body(None, embed=True)) -> JSONResponse:
    """Join a network. The passphrase is passed through and never logged."""
    _require_setup_mode(request)
    _check_code(request, code)
    ssid = (ssid or "").strip()
    psk = psk or ""
    if not ssid or len(ssid.encode("utf-8")) > 32 or any(ord(c) < 32 for c in ssid):
        raise auth.LcError("invalid_args", "Choose a network name (at most 32 bytes)")
    if psk and not 8 <= len(psk) <= 63:
        raise auth.LcError("invalid_args", "A Wi-Fi password has 8 to 63 characters")
    network = _network()
    if network is None:
        raise auth.LcError("unsupported", "Wi-Fi control is not available on this build")
    log.info("setup: joining Wi-Fi network %r", ssid)
    try:
        result = await asyncio.to_thread(network.connect, ssid, psk)
    except Exception as exc:
        log.warning("wifi connect failed: %s", exc)
        result = {"ok": False, "error": "could not start the connection"}
    result = dict(result or {})
    return JSONResponse({"ok": bool(result.get("ok")), "error": result.get("error"),
                         "status": await asyncio.to_thread(_network_status, network)},
                        status_code=200 if result.get("ok") else 502)


__all__ = ["router", "setup_mode_enabled", "code_matches", "setup_limiter"]
