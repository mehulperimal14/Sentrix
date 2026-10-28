"""The tunnel path table: who may reach which console path through the Portal.

The device does not trust the Portal (decisions: "device enforces"). Every
request the Portal forwards over the link, and every video or state
subscription, is checked here *before* the in-process call into the Local
Console. This module is the only place where tunnelled roles, consents,
step-up and rate limits are enforced (docs/7 §5.7); console routes enforce
only their business rules (the SOS PIN, 409 on a sent package).

Path classes (docs/7 §5.7):

=========================  ================  =====================================
Class                      Roles             Extra
=========================  ================  =====================================
pages, read APIs, static   owner, member
media, video relay         owner, member     consent ``remote_view``; faces also
                                             ``face_recognition``; <= 60/min/user
gallery writes             owner             consent ``face_recognition``;
                                             DELETE needs a step-up
verdict / SOS / dispatch   owner, member
HOME/ARMED (``/api/mode``) owner, member     the route checks the SOS PIN
siren test                 owner             step-up (the 1/min limit is
                                             ``actuators.siren_test``'s)
recording status           owner             ``GET /api/recordings`` (status and
                                             list; no step-up; D20)
recording start            owner             step-up; ``POST
                                             /api/recordings/start`` (D20)
recording stop             owner             ``POST /api/recordings/stop``; no
                                             step-up: stopping the filming of
                                             the room is never blocked (D20)
Desk view                  operator          only while a request of this device
                                             is on the help path *and* consent
                                             ``desk_live_view``
recording files            none (LC only)    ``DELETE /api/recordings/*`` and
                                             every other ``/api/recordings*``
                                             path, ``/media/recording/*``: the
                                             device admin on the LAN (D20)
=========================  ================  =====================================

Never tunnelled (refused even if the Portal forwarded them): ``/login``,
``/logout``, ``/setup/*``, ``/docs``, ``/redoc``, ``/openapi.json``, ``/video``
and ``/ws/*`` (the last two are relayed from frame and state messages). The
LC-only routes (``/api/link/reconnect``, ``/api/channels/test``, and every
``/api/recordings*`` and ``/media/recording/*`` path the two recording rows do
not name) belong to the device admin. The owner may start, stop and list
training recordings through the Portal, but the files stay on the LAN:
continuous video never leaves the device (D12) and a recording is far over the
tunnel's 16 MiB response cap (D20). Anything not in the table is refused.
``company_admin`` never reaches the device: it is not a tunnel role and
:class:`Actor` refuses it.

Consents come from the profile the Portal pushed (``sentrix.link.profile``);
a device without one (standalone) grants none of them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import unquote

from sentrix.actor import Actor
from sentrix.link.protocol import ERROR_CODES
from sentrix.web.security import RateLimiter

#: Step-up window the device accepts for owner-only sensitive actions (s).
STEP_UP_SECONDS = 300.0
#: Console pages an owner or member may open through the tunnel.
PAGES = frozenset({"/", "/dashboard", "/live", "/events", "/alerts", "/evidence",
                   "/dispatch", "/authorized", "/sos", "/node"})
#: What a Desk operator may read while a request is on the help path.
DESK_PATHS = frozenset({"/dashboard", "/live", "/api/health", "/api/metrics"})
#: Never served through the tunnel.
NEVER_EXACT = frozenset({"/login", "/logout", "/docs", "/redoc", "/openapi.json",
                         "/video", "/setup"})
NEVER_PREFIXES = ("/setup/", "/ws/", "/docs/", "/redoc/")
#: Routes only the device admin (LC password, on the LAN) may use.
LC_ONLY = frozenset({"/api/link/reconnect", "/api/channels/test"})
#: Path prefixes only the device admin may use, whatever the method: the
#: training recordings' files, their downloads and deletes (D20), except the
#: owner's status and start/stop (:data:`RECORD_STATUS_PATH`,
#: :data:`RECORD_CONTROL`), which :func:`classify` picks out first.
LC_ONLY_PREFIXES = ("/api/recordings", "/media/recording/")
#: The recorder's status and list (GET/HEAD): the owner through the Portal.
RECORD_STATUS_PATH = "/api/recordings"
#: Starting and stopping a training recording (POST): the owner; starting
#: needs a step-up, stopping never does (a stop only ends the filming).
RECORD_CONTROL = frozenset({"/api/recordings/start", "/api/recordings/stop"})
RECORD_STOP_PATH = "/api/recordings/stop"
READ_METHODS = frozenset({"GET", "HEAD"})
TUNNEL_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})

# Path classes.
NEVER, PAGE, READ_API, STATIC, MEDIA, FACES = "never", "page", "read_api", "static", "media", "faces"
GALLERY_WRITE, GALLERY_DELETE = "gallery_write", "gallery_delete"
SOS_WRITE, MODE, SIREN, LC_ADMIN, UNKNOWN = "sos_write", "mode", "siren_test", "lc_only", "unknown"
RECORD, RECORD_STOP, RECORD_STATUS = "record", "record_stop", "record_status"


@dataclass(frozen=True)
class Decision:
    """The verdict on one tunnelled request or subscription. ``code`` is "ok"
    or a docs/7 §13 code; ``path_class`` names the table row that applied."""

    allowed: bool
    code: str = "ok"
    reason: str = ""
    path_class: str = ""
    retry_after: float | None = None

    @property
    def status(self) -> int:
        return 200 if self.allowed else ERROR_CODES.get(self.code, 403)

    def __bool__(self) -> bool:
        return self.allowed


def _allow(path_class: str) -> Decision:
    return Decision(True, "ok", "", path_class)


def _refuse(code: str, reason: str, path_class: str = "",
            retry_after: float | None = None) -> Decision:
    return Decision(False, code, reason, path_class, retry_after)


def normalise_path(path: str) -> str | None:
    """The decoded path the console will route on, or None when it is not a
    plain absolute path (scheme, host, ``..``, backslash, NUL, ``//``)."""
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 2048:
        return None
    decoded = unquote(path)
    if ("\\" in decoded or "\x00" in decoded or "//" in decoded or "://" in decoded
            or any(part in ("..", ".") for part in decoded.split("/"))):
        return None
    return decoded


def classify(method: str, path: str) -> str:
    """The table row of ``method path`` (``path`` already normalised)."""
    method = method.upper()
    if path in NEVER_EXACT or path.startswith(NEVER_PREFIXES):
        return NEVER
    # Before the LC-only prefixes: the owner's two recording rows (D20).
    if method in READ_METHODS and path == RECORD_STATUS_PATH:
        return RECORD_STATUS
    if method == "POST" and path == RECORD_STOP_PATH:
        return RECORD_STOP
    if method == "POST" and path in RECORD_CONTROL:
        return RECORD
    if path in LC_ONLY or path.startswith(LC_ONLY_PREFIXES):
        return LC_ADMIN
    if method in READ_METHODS:
        if path.startswith("/static/"):
            return STATIC
        if path.startswith("/media/face/"):
            return FACES
        if path.startswith("/media/") or path == "/api/authorized/preview.jpg":
            return MEDIA
        if path in PAGES:
            return PAGE
        if path.startswith("/api/"):
            return READ_API
        return UNKNOWN
    if method == "POST" and path in ("/authorized/upload", "/api/authorized/capture"):
        return GALLERY_WRITE
    if method == "DELETE" and path.startswith("/api/authorized/"):
        return GALLERY_DELETE
    if method == "POST" and (
            (path.startswith("/api/incidents/") and path.endswith("/verdict"))
            or path.startswith("/api/sos/") or path.startswith("/api/dispatch/")):
        return SOS_WRITE
    if method == "POST" and path == "/api/mode":
        return MODE
    if method == "POST" and path == "/api/node/siren-test":
        return SIREN
    return UNKNOWN


def _help_path_open() -> bool:
    """Is a request of this device on the help path right now (Desk view)?"""
    try:
        from sentrix.sos import states
        from sentrix.storage import requests
        current = requests.open_request()
    except Exception:
        return False
    return bool(current) and current["state"] in (states.HELP_PATH - states.TERMINAL_STATES)


def _profile_consents() -> dict[str, Any]:
    from sentrix.link import profile
    try:
        return dict((profile.effective() or {}).get("consents") or {})
    except Exception:
        return {}


class TunnelPolicy:
    """Stateful only for the media rate limit (per user, per minute).

    ``consents`` and ``help_path_open`` are injectable for tests; by default
    they read the active profile and the device's own request store, never
    anything the Portal says in the request.
    """

    def __init__(self, *, media_per_minute: int | None = None,
                 consents: Callable[[], dict] | None = None,
                 help_path_open: Callable[[], bool] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        if media_per_minute is None:
            from sentrix.config import settings
            media_per_minute = settings.link.media_requests_per_minute
        self.media_per_minute = int(media_per_minute)
        self._media = RateLimiter(max_attempts=self.media_per_minute, window_seconds=60.0)
        self._consents = consents or _profile_consents
        self._help_path_open = help_path_open or _help_path_open
        self._clock = clock

    # ── helpers ───────────────────────────────────────────────────────────────
    def consent(self, purpose: str) -> bool:
        return bool(self._consents().get(purpose))

    def _media_budget(self, actor: Actor, path_class: str) -> Decision:
        if not self._media.allow(actor.ref):
            return _refuse("rate_limited",
                           f"more than {self.media_per_minute} media requests a minute",
                           path_class, retry_after=60.0)
        return _allow(path_class)

    def _desk(self, path_class: str) -> Decision:
        if not self._help_path_open():
            return _refuse("forbidden_role", "the Desk sees a home only while its request "
                           "is on the help path", path_class)
        if not self.consent("desk_live_view"):
            return _refuse("consent_required", "the household has not allowed the Desk "
                           "to view the camera", path_class)
        return _allow(path_class)

    # ── tunnelled HTTP ────────────────────────────────────────────────────────
    def check_request(self, method: str, path: str, actor: Actor) -> Decision:
        """May ``actor`` call ``method path`` through the tunnel?"""
        method = (method or "").upper()
        if method not in TUNNEL_METHODS:
            return _refuse("invalid_args", f"method {method} is not tunnelled")
        clean = normalise_path(path)
        if clean is None:
            return _refuse("invalid_args", "not a plain absolute path")
        if actor.type not in ("owner", "member", "operator"):
            return _refuse("forbidden_role", f"{actor.type} may not use the tunnel")
        path_class = classify(method, clean)
        if path_class == NEVER:
            return _refuse("not_found", "this path is never tunnelled", path_class)
        if path_class == LC_ADMIN:
            return _refuse("forbidden_role", "only the device admin on the LAN may use this",
                           path_class)
        if path_class == UNKNOWN:
            return _refuse("not_found" if method in READ_METHODS else "forbidden_role",
                           "not in the tunnel path table", path_class)

        if actor.type == "operator":
            if path_class == STATIC or (path_class in (PAGE, READ_API) and clean in DESK_PATHS):
                return self._desk(path_class)
            return _refuse("forbidden_role", "the Desk may only view the live console",
                           path_class)

        # owner and member from here on
        if path_class in (PAGE, READ_API, STATIC):
            return _allow(path_class)
        if path_class in (MEDIA, FACES):
            if not self.consent("remote_view"):
                return _refuse("consent_required", "remote viewing is not consented",
                               path_class)
            if path_class == FACES and not self.consent("face_recognition"):
                return _refuse("consent_required", "face recognition is not consented",
                               path_class)
            return self._media_budget(actor, path_class)
        if path_class in (GALLERY_WRITE, GALLERY_DELETE):
            if actor.type != "owner":
                return _refuse("forbidden_role", "only the owner may change the face gallery",
                               path_class)
            if not self.consent("face_recognition"):
                return _refuse("consent_required", "face recognition is not consented",
                               path_class)
            if path_class == GALLERY_DELETE and not actor.stepped_up_within(
                    STEP_UP_SECONDS, self._clock()):
                return _refuse("step_up_required", "re-enter your password first", path_class)
            return _allow(path_class)
        if path_class in (SOS_WRITE, MODE):
            return _allow(path_class)
        if path_class == SIREN:
            if actor.type != "owner":
                return _refuse("forbidden_role", "only the owner may test the siren", path_class)
            if not actor.stepped_up_within(STEP_UP_SECONDS, self._clock()):
                return _refuse("step_up_required", "re-enter your password first", path_class)
            return _allow(path_class)
        if path_class in (RECORD_STATUS, RECORD, RECORD_STOP):
            # The files never: deletes and downloads are lc_only above (D20).
            if actor.type != "owner":
                return _refuse("forbidden_role", "only the owner may record training videos",
                               path_class)
            if path_class == RECORD and not actor.stepped_up_within(
                    STEP_UP_SECONDS, self._clock()):
                return _refuse("step_up_required", "re-enter your password first", path_class)
            return _allow(path_class)
        return _refuse("forbidden_role", "not in the tunnel path table", path_class)

    # ── relays ────────────────────────────────────────────────────────────────
    def check_video(self, actor: Actor) -> Decision:
        """``video.subscribe``: owner/member with ``remote_view``; the Desk
        under the Desk rule."""
        if actor.type == "operator":
            return self._desk("video")
        if actor.type not in ("owner", "member"):
            return _refuse("forbidden_role", f"{actor.type} may not view the camera", "video")
        if not self.consent("remote_view"):
            return _refuse("consent_required", "remote viewing is not consented", "video")
        return _allow("video")

    def check_state(self, actor: Actor | None) -> Decision:
        """``state.subscribe``: the dashboard's live state. The Portal sends it
        for owner/member dashboards and, under the Desk rule, the Desk."""
        if actor is not None and actor.type == "operator":
            return self._desk("state")
        return _allow("state")


__all__ = ["Decision", "TunnelPolicy", "classify", "normalise_path", "STEP_UP_SECONDS",
           "PAGES", "DESK_PATHS", "LC_ONLY", "LC_ONLY_PREFIXES", "RECORD_STATUS_PATH",
           "RECORD_CONTROL", "RECORD_STOP_PATH", "NEVER_EXACT", "NEVER_PREFIXES",
           "TUNNEL_METHODS"]
