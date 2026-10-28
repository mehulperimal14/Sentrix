"""/api/recordings and /media/recording/* - training recordings (D20).

The models fail in the deployment room because none of their training footage
came from it (CLAUDE.md §5, M6). The dashboard's "Record video" card
records the camera's raw frames (and the microphone, when one is connected)
through ``Pipeline.start_recording``; the device admin downloads the files on
the local network and deletes them.

* ``GET /api/recordings`` - the recorder's status (with the microphone), the
  finished recordings (newest first), free space, whether ffmpeg is installed
  and how long recordings are kept; ``via`` ("lan" or "portal"),
  ``can_delete``, ``download_here`` and, when known, the device's address on
  its network and the console's URL there (``lan_url``).
* ``POST /api/recordings/start`` - JSON ``{"label"?}``. 409 ``conflict`` while
  recording or saving, before the camera's first frame, short of disk or over
  the recordings' quota; 400 ``invalid_args`` for a bad body.
* ``POST /api/recordings/stop`` - 409 when nothing is recording. Never rate
  limited: stopping is what ends the filming of the room, and a stop when
  idle changes nothing. Starts are limited to :data:`CONTROL_PER_MINUTE` per
  caller (429 ``rate_limited``).
* ``GET``/``HEAD /media/recording/{file}`` - a finished ``.avi``, ``.wav`` or
  ``.json`` as an attachment (streamed from disk, ``Range`` supported); 404
  for anything else, a ``.part`` file or a path included. Every download is
  audited (``recording_downloaded``) unless the request can only be a later
  part of one (:func:`_starts_a_download`).
* ``DELETE /api/recordings/{name}`` - 404 when there is none, 409 for the
  recording in progress.

**Never onto the device's own disk.** A download lands on the computer whose
browser asked for it. On a Raspberry Pi a browser on the device itself (the
desktop dashboard on ``127.0.0.1``, or the Pi opening its own network address)
would save gigabytes of video onto the same 128 GB SD card the alarm's
evidence lives on, so such downloads are refused (409) and the card names the
address to open on a laptop instead (``SENTRIX_RECORD_DOWNLOAD_ON_DEVICE``,
:func:`downloads_allowed`).

**Who.** The device admin on the LAN does everything. Since 2026-10-28 the
owner may also start, stop and list recordings through the Portal
(``require_lc("device_admin", "owner")``; a start also needs a step-up within
:data:`~sentrix.link.policy.STEP_UP_SECONDS`, which the link's path table
checks first and the route checks again; a stop never does: a step-up lasts
5 min, a recording up to 10, and stopping the filming is never blocked). Downloads and deletes stay the
device admin's on the LAN (``require_lc("device_admin")``; the path table
refuses them to every tunnelled role as ``lc_only`` before any handler runs,
``sentrix/link/policy.py``): continuous video stays on the device (D12) and a
recording is far over the tunnel's 16 MiB response cap. A tunnelled listing
therefore says ``download_here`` and ``can_delete`` false and names the
console's address on the home network instead. Members and the Desk are
refused all of it.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse

from sentrix.io.recorder import RecorderError
from sentrix.link.policy import STEP_UP_SECONDS
from sentrix.utils.logging import get_logger
from sentrix.utils.redact import redact_tree
from sentrix.web.auth import LcError, LcPrincipal, rate_key, require_lc
from sentrix.web.security import RateLimiter

log = get_logger("recording_routes")
router = APIRouter()

#: Start requests per caller per minute (refused ones count; stops never do).
CONTROL_PER_MINUTE = 10
control_limiter = RateLimiter(max_attempts=CONTROL_PER_MINUTE, window_seconds=60.0)
#: The listing also deletes recordings past ``keep_days``, at most this often
#: (s); the recorder's own hourly housekeeping does it without any request.
PRUNE_EVERY_SECONDS = 3600.0
#: A single ``bytes=N-[M]`` range: with N > 0, a later part of a download.
_LATER_PART = re.compile(r"\s*bytes\s*=\s*(\d+)\s*-\s*\d*\s*", re.IGNORECASE)
#: The start body: a JSON object with an optional label.
MAX_BODY_BYTES = 4096
MAX_LABEL_CHARS = 200
_MEDIA_TYPES = {".avi": "video/x-msvideo", ".wav": "audio/wav", ".json": "application/json"}
_NO_STORE = {"Cache-Control": "no-store"}

_pruned_at: float | None = None
#: The device's own network address, for the "download on your laptop" hint:
#: (monotonic time, address), refreshed at most every ADDRESS_TTL_SECONDS.
ADDRESS_TTL_SECONDS = 60.0
_address_cache: tuple[float, str | None] | None = None

#: Downloads and deletes: the device admin on the LAN only.
device_admin = require_lc("device_admin")
#: The status and list: the device admin, or the owner through the Portal.
owner_or_admin = require_lc("device_admin", "owner")
#: Start: the same, the owner with a fresh step-up (the path table's ``record``
#: row, restated so a request past the table is refused all the same). Stop
#: uses :data:`owner_or_admin`: it is never blocked.
controller = require_lc("device_admin", "owner", step_up_seconds=STEP_UP_SECONDS)


def _pipeline() -> Any:
    from sentrix.pipeline.engine import get_pipeline
    return get_pipeline()


def _error(exc: RecorderError) -> LcError:
    """The recorder's refusal as the console's JSON error, same code."""
    error = LcError(exc.code, exc.message)
    if 400 <= exc.status < 600:
        error.status_code = exc.status
    return error


def _limit(request: Request) -> None:
    if not control_limiter.allow(rate_key(request)):
        raise LcError("rate_limited", f"at most {CONTROL_PER_MINUTE} starts a minute",
                      headers={"Retry-After": "60"})


def _ip(value: Any) -> Any:
    """An address as an ``ipaddress`` object (IPv4-mapped IPv6 unwrapped), or
    None for anything that is not one (e.g. the test client's "testclient")."""
    try:
        address = ipaddress.ip_address(str(value).split("%")[0])
    except ValueError:
        return None
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped or address


def same_machine(request: Request) -> bool:
    """Did a browser on the device itself send this request? Its source is a
    loopback address, or the connection's two ends are the same address (the
    device opening its own network address)."""
    client = _ip(request.client.host) if request.client else None
    if client is None:
        return False
    if client.is_loopback:
        return True
    server = (request.scope.get("server") or (None,))[0]
    return client == _ip(server)


def downloads_allowed(request: Request, cfg: Any) -> bool:
    """May this browser download recordings? Always from another computer;
    from the device itself only where its own disk can take it (see the
    module docstring)."""
    mode = str(getattr(cfg, "download_on_device", "auto") or "auto").strip().lower()
    if mode in ("1", "true", "yes", "on") or not same_machine(request):
        return True
    if mode in ("0", "false", "no", "off"):
        return False
    from sentrix.io import sensors
    return not sensors.is_raspberry_pi()


def _device_address() -> str | None:
    """The device's address on its network (nmcli), cached; None off Linux."""
    global _address_cache
    now = time.monotonic()
    if _address_cache is not None and now - _address_cache[0] < ADDRESS_TTL_SECONDS:
        return _address_cache[1]
    try:
        from sentrix.io import network
        address = network.status().get("ip")
    except Exception as exc:
        log.debug("network status failed: %s", exc)
        address = None
    _address_cache = (now, address)
    return address


def lan_url(address: str | None) -> str | None:
    """The console's URL on the home network (``SENTRIX_PORT``, default 8000),
    or None when the address is unknown. A page served through the Portal
    cannot work it out: its own port is the Portal's."""
    if not address:
        return None
    port = os.getenv("SENTRIX_PORT", "").strip() or "8000"
    host = f"[{address}]" if ":" in address else address
    return f"http://{host}:{port}"


def _starts_a_download(request: Request) -> bool:
    """Does this GET send the file from its first byte, as far as the
    headers tell before it is served? Only a single ``bytes=N-`` range with
    N > 0 and no ``If-Range`` is surely a later part of a download. Anything
    else counts as a download: no range, ``bytes=0-`` in any spelling, a
    suffix range (``bytes=-N`` can cover the whole file), several ranges
    (merged, they can too), or an ``If-Range`` the file may not match (then
    the whole file is sent). An extra audit row is harmless; a missing one
    would let footage of the household leave the device unrecorded."""
    header = request.headers.get("range")
    if not header or request.headers.get("if-range") is not None:
        return True
    match = _LATER_PART.fullmatch(header)
    return match is None or int(match.group(1)) == 0


def _audit(pipeline: Any, event: str, **detail: Any) -> None:
    audit = getattr(getattr(pipeline, "events", None), "audit", None)
    if callable(audit):
        try:
            audit(event, **detail)
        except Exception as exc:
            log.debug("audit %s failed: %s", event, exc)


def _maybe_prune(recorder: Any) -> None:
    global _pruned_at
    now = time.monotonic()
    if _pruned_at is not None and now - _pruned_at < PRUNE_EVERY_SECONDS:
        return
    _pruned_at = now
    try:
        recorder.prune()
    except Exception as exc:
        log.warning("recordings prune failed: %s", exc)


def recordings_payload(pipeline: Any) -> dict[str, Any]:
    """The ``GET /api/recordings`` body (blocking: run it in a thread)."""
    recorder = pipeline.get_recorder()
    _maybe_prune(recorder)
    status = pipeline.recording_status()
    items = recorder.list()
    cfg = recorder.cfg
    return {"status": status, "items": items,
            "free_bytes": status.get("free_bytes"), "ffmpeg": status.get("ffmpeg"),
            "microphone": status.get("microphone"),
            "keep_days": float(cfg.keep_days),
            "total_bytes": sum(int(item.get("bytes") or 0) for item in items),
            "max_total_bytes": int(cfg.max_total_mb) * 1024 * 1024}


async def _label(request: Request) -> str:
    """The optional label of a start request; anything malformed is 400."""
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise LcError("invalid_args", "the request body is too large")
    if not raw.strip():
        return ""
    try:
        body = json.loads(raw)
    except ValueError:
        raise LcError("invalid_args", "the body must be JSON") from None
    if not isinstance(body, dict):
        raise LcError("invalid_args", 'the body must be a JSON object such as {"label": "..."}')
    label = body.get("label")
    if label is None:
        return ""
    if not isinstance(label, str) or len(label) > MAX_LABEL_CHARS:
        raise LcError("invalid_args", f"label must be text of at most {MAX_LABEL_CHARS} "
                                      "characters")
    return label


@router.get("/api/recordings")
async def api_recordings(request: Request,
                         principal: LcPrincipal = Depends(owner_or_admin)) -> JSONResponse:
    pipeline = await asyncio.to_thread(_pipeline)
    payload = await asyncio.to_thread(recordings_payload, pipeline)
    recorder = await asyncio.to_thread(pipeline.get_recorder)
    # Through the Portal the files are out of reach (D12, the 16 MiB cap): the
    # card hides Download and Delete and names the console on the home network.
    remote = principal.kind == "tunnel"
    payload["via"] = "portal" if remote else "lan"
    payload["can_delete"] = not remote
    payload["download_here"] = not remote and downloads_allowed(request, recorder.cfg)
    address = None if payload["download_here"] else await asyncio.to_thread(_device_address)
    payload["device_address"] = address
    payload["lan_url"] = lan_url(address)
    return JSONResponse(redact_tree(payload), headers=_NO_STORE)


@router.post("/api/recordings/start")
async def api_recording_start(request: Request,
                              principal: LcPrincipal = Depends(controller)) -> JSONResponse:
    _limit(request)
    label = await _label(request)
    pipeline = await asyncio.to_thread(_pipeline)
    try:
        status = await asyncio.to_thread(pipeline.start_recording, label=label,
                                         by=principal.actor)
    except RecorderError as exc:
        raise _error(exc) from None
    return JSONResponse(redact_tree(status), headers=_NO_STORE)


@router.post("/api/recordings/stop")
async def api_recording_stop(principal: LcPrincipal = Depends(owner_or_admin)) -> JSONResponse:
    # Neither rate limited nor behind a step-up: a stop must never be refused
    # while the room is being filmed (a step-up lasts 5 min, a recording up to
    # 10), and when nothing records it is a 409 that changes nothing.
    pipeline = await asyncio.to_thread(_pipeline)
    try:
        status = await asyncio.to_thread(pipeline.stop_recording, by=principal.actor)
    except RecorderError as exc:
        raise _error(exc) from None
    return JSONResponse(redact_tree(status), headers=_NO_STORE)


@router.delete("/api/recordings/{name}")
async def api_recording_delete(name: str,
                               principal: LcPrincipal = Depends(device_admin)) -> JSONResponse:
    pipeline = await asyncio.to_thread(_pipeline)
    recorder = await asyncio.to_thread(pipeline.get_recorder)
    try:
        found = await asyncio.to_thread(recorder.delete, name, by=principal.actor)
    except RecorderError as exc:
        raise _error(exc) from None
    if not found:
        raise LcError("not_found", "no such recording")
    return JSONResponse({"status": "ok", "name": name})


@router.api_route("/media/recording/{filename}", methods=["GET", "HEAD"])
async def media_recording(request: Request, filename: str,
                          principal: LcPrincipal = Depends(device_admin)) -> FileResponse:
    """A finished recording's file, streamed from disk as an attachment."""
    pipeline = await asyncio.to_thread(_pipeline)
    recorder = await asyncio.to_thread(pipeline.get_recorder)
    if not downloads_allowed(request, recorder.cfg):
        raise LcError("conflict", "Download it on your laptop or another computer on the same "
                                  "network: in this browser, on the device itself, the video "
                                  "would be saved onto the device's own SD card.")
    path = await asyncio.to_thread(recorder.path_for, filename)
    if path is None:
        raise LcError("not_found", "no such recording file")
    # One audit row per download, not per byte range a resumed download asks for.
    if request.method == "GET" and _starts_a_download(request):
        await asyncio.to_thread(_audit, pipeline, "recording_downloaded", file=path.name,
                                by=principal.actor)
    return FileResponse(path, filename=path.name,
                        media_type=_MEDIA_TYPES.get(path.suffix.lower(),
                                                    "application/octet-stream"),
                        headers=_NO_STORE)


__all__ = ["router", "recordings_payload", "control_limiter", "CONTROL_PER_MINUTE",
           "same_machine", "downloads_allowed", "lan_url"]
