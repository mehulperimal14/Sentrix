"""SOS assistance requests: the device-side interface (docs/7 §7).

Two kinds of callers use the SOS service:

* **The pipeline** never decides anything about SOS. At a committed L3+ it
  asks :meth:`SosService.wants` whether this incident type is an SOS class,
  and if so hands over a :class:`SosTrigger` once per incident; on every
  committed level change it reports a :class:`LevelChange`.
* **People and the cloud**, through thin wrappers: the LC routes
  (``sentrix/web/sos_routes.py`` for ``/api/sos``; the verdict, dispatch-send
  and ``/api/mode`` routes in ``sentrix/web``) and the link's command handlers
  (``sentrix/link/commands.py``: ``sos.respond``, ``sos.update``,
  ``sos.sync``, ``verdict.set``, ``mode.set``, ``demo.*``). They parse the
  input, build the :class:`~sentrix.actor.Actor`, call one method below and
  map the :class:`SosResult` to a status. They never touch the request store.

Everything else (rendering, prompts, timers, the window, dispatch, PINs)
belongs to the implementation, which runs in the web process (decision: one
process owns web app, SOS service, link client, outbox and channel router).

:class:`NullSosService` is the default on every ``Pipeline`` and whenever
``SENTRIX_SOS_ENABLED`` is off. Evaluation, replay and benchmark pipelines
must keep it (``sentrix.services.assert_measurement_safe``); only the app
lifespan swaps in the real service. With SOS off the console still works: the
null service does the pre-portal thing for verdicts, HOME/ARMED and "Send"
on a package, and refuses the SOS-only calls with ``unsupported``.

Contract for implementations (track T-SOS, ``sentrix/sos/service.py``):

* ``wants`` is called on the inference thread at L3+ until it accepts:
  constant time, no I/O, no locks held for long.
* ``trigger`` runs on the pipeline's FIFO worker *after* the incident's
  snapshot and first evidence capture (so their paths are set), or
  synchronously when that queue is full: it must only enqueue (SQLite insert
  or an in-memory queue) and must be idempotent per ``incident_id``.
* ``on_level`` is called on the inference thread: enqueue only.
* The other methods are called from web request threads (``asyncio.
  to_thread``) and the link client's worker; they may block briefly on SQLite
  and must serialise request writes with ``storage.requests.REQUEST_LOCK``.
* Never raise into a caller: every method returns a :class:`SosResult`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, TypedDict, runtime_checkable

from sentrix.actor import Actor

#: Buttons the LC / Portal may show for the open request (``state.sos.actions``,
#: owner view). ``call_112`` is a tel: link (the Desk number in test mode).
SOS_ACTIONS = ("approve", "call_112", "checking", "decline", "safe", "help_now")
#: ``respond`` actions (``protocol.SOS_RESPOND_ACTIONS``).
RESPOND_ACTIONS = ("approve", "decline", "checking", "safe", "help_now")
#: ``current(view=...)`` projections. ``owner`` applies ``states.owner_view``
#: (duress hidden); ``desk``/``contact`` see the duress banner; ``full`` is
#: device-internal.
VIEWS = ("owner", "desk", "contact", "full")


class SosResult(TypedDict):
    """What every SOS call returns. ``code`` is "ok" or a docs/7 §13 code;
    ``status`` its HTTP-like status; ``request`` the request after the call
    in the caller's view (owner view for owners and members), or None."""

    ok: bool
    code: str
    status: int
    message: str
    request: dict | None


def sos_result(code: str = "ok", message: str = "",
               request: dict | None = None) -> SosResult:
    """Build a :class:`SosResult`; the status comes from ``protocol.ERROR_CODES``."""
    from sentrix.link.protocol import ERROR_CODES
    status = 200 if code == "ok" else ERROR_CODES.get(code, 400)
    return SosResult(ok=code == "ok", code=code, status=status, message=message,
                     request=request)


@dataclass(frozen=True)
class SosTrigger:
    """A committed L3+ tick of an incident the SOS service said it wants."""

    incident_id: str
    level: int
    incident_type: str                 # weapon | fire | intrusion
    evidence_state: str                # none | unconfirmed | confirmed | corroborated
    tci: float
    reason: str
    session_id: str
    camera_id: str = "CAM_0"
    armed: bool = False
    at_wall: float = 0.0               # time.time() of the tick
    at_mono: float = 0.0               # pipeline (capture) time of the tick
    snapshot_path: str | None = None   # file name in var/snapshots
    evidence_bundle: str | None = None
    evidence_hash: str | None = None
    #: Per-class evidence votes at the tick (``features.extras["evidence"]``).
    votes: dict = field(default_factory=dict)
    #: True when produced by the test-mode scripted trigger (disclosed demo).
    simulated: bool = False


@dataclass(frozen=True)
class LevelChange:
    """A committed level change (either direction)."""

    level: int
    previous_level: int
    incident_id: str | None
    incident_type: str
    evidence_state: str
    tci: float
    at_wall: float
    at_mono: float


@runtime_checkable
class SosService(Protocol):
    name: str

    # ── pipeline hook ─────────────────────────────────────────────────────────
    def wants(self, incident_type: str, level: int) -> bool: ...

    def trigger(self, trigger: SosTrigger) -> None: ...

    def on_level(self, change: LevelChange) -> None: ...

    # ── people and the cloud ──────────────────────────────────────────────────
    def respond(self, request_id: str, action: str, *, actor: Actor, channel: str,
                authority: str | None = None, pin: str | None = None) -> SosResult:
        """An answer to the prompt (``RESPOND_ACTIONS``). ``decline`` and
        ``safe`` need the SOS PIN (5 tries / 10 min, ``sos/pins.py``); the
        duress PIN answers ``approve`` with ``duress`` while the result shows
        "Marked as false alarm". A contact may only ``approve``, and only when
        the request's frozen policy has safety defaults on and that contact is
        CONFIRMED in it. ``help_now`` on EXPIRED/DECLINED/CANCELLED opens a new
        linked request (returned in ``request``)."""
        ...

    def cloud_event(self, request_id: str, event: str, *, actor: Actor,
                    detail: dict | None = None) -> SosResult:
        """A cloud-side fact (``protocol.SOS_UPDATE_EVENTS``: Desk acknowledge
        and resolve, delivered, ...), applied through ``states.next_state``."""
        ...

    def sync(self, request_id: str, *, cloud_state: str, cloud_version: int,
             authority_side: str, events: list[dict]) -> SosResult:
        """``sos.sync`` after a reconnect (docs/7 §7.6): import the cloud-origin
        events (``requests.import_event``), apply ``states.reconcile``, write
        a ``conflict`` event when the state changed. An unknown ``request_id``
        is a request the cloud opened while the device was offline: create it
        with ``create_request(..., origin="cloud")``."""
        ...

    def false_verdict(self, incident_id: str, *, pin: str | None, actor: Actor,
                      channel: str, note: str | None = None) -> SosResult:
        """The Events page's "False alarm". With an open request the SOS PIN
        is required; the duress PIN becomes approve + duress *before*
        ``database.set_verdict`` is called (the label still reads FALSE)."""
        ...

    def set_mode(self, armed: bool, *, pin: str | None, actor: Actor) -> SosResult:
        """HOME/ARMED. Arming needs no PIN; disarming needs the SOS PIN. The
        duress PIN disarms visibly and opens a silent request (duress)."""
        ...

    def approve_package(self, package_id: str, authority: str, *, actor: Actor,
                        channel: str) -> SosResult:
        """"Send now" on a dispatch package. A package linked to a request
        approves that request with ``authority``; 409 ``conflict`` for SENT,
        CANCELLED, EXPIRED (and a DECLINED request)."""
        ...

    def demo_trigger(self, incident_type: str, *, actor: Actor) -> SosResult:
        """Test mode only (``test_mode_only`` otherwise): a disclosed scripted
        L3 marked ``simulated`` ("SIMULATED DETECTION")."""
        ...

    def demo_close_incident(self, *, actor: Actor) -> SosResult:
        """Test mode only: ``Pipeline.request_close_incident("demo")`` so the
        next scene opens a new request."""
        ...

    def current(self, view: str = "owner") -> dict | None:
        """The open request projected for ``view`` (``VIEWS``), or None."""
        ...

    # ── service ───────────────────────────────────────────────────────────────
    def status(self) -> dict[str, Any]:
        """The ``state.sos`` block (owner view, base-relative paths)."""
        ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def health(self) -> dict[str, Any]: ...


_UNSUPPORTED = "the SOS service is off (SENTRIX_SOS_ENABLED=0)"


class NullSosService:
    """No requests, no prompts. The default, and the only service measurement
    may use. Console calls keep their pre-portal behaviour."""

    name = "sos"
    enabled = False

    # ── pipeline hook ─────────────────────────────────────────────────────────
    def wants(self, incident_type: str, level: int) -> bool:
        return False

    def trigger(self, trigger: SosTrigger) -> None:
        return None

    def on_level(self, change: LevelChange) -> None:
        return None

    # ── people and the cloud ──────────────────────────────────────────────────
    def respond(self, request_id: str, action: str, *, actor: Actor, channel: str,
                authority: str | None = None, pin: str | None = None) -> SosResult:
        return sos_result("unsupported", _UNSUPPORTED)

    def cloud_event(self, request_id: str, event: str, *, actor: Actor,
                    detail: dict | None = None) -> SosResult:
        return sos_result("unsupported", _UNSUPPORTED)

    def sync(self, request_id: str, *, cloud_state: str, cloud_version: int,
             authority_side: str, events: list[dict]) -> SosResult:
        return sos_result("unsupported", _UNSUPPORTED)

    def false_verdict(self, incident_id: str, *, pin: str | None, actor: Actor,
                      channel: str, note: str | None = None) -> SosResult:
        """Without the SOS service nothing prompts, so the pre-portal rule
        applies (``database.set_verdict``: the incident's PENDING packages are
        cancelled)."""
        from sentrix.storage import database
        ok = database.set_verdict(incident_id, "FALSE", note, by=actor.ref,
                                  channel=channel, actor_type=actor.event_actor_type)
        return sos_result() if ok else sos_result("not_found", "unknown incident")

    def set_mode(self, armed: bool, *, pin: str | None, actor: Actor) -> SosResult:
        """Arming is always allowed. Without the SOS service there is no PIN
        to check, so only the device admin (LC password) may disarm."""
        from sentrix import mode
        if not armed and actor.type != "device_admin":
            return sos_result("unsupported", "disarming needs the SOS PIN, which "
                                             "needs the SOS service")
        mode.set_armed(armed, by=actor.ref)
        return sos_result()

    def approve_package(self, package_id: str, authority: str, *, actor: Actor,
                        channel: str) -> SosResult:
        """The legacy operator-confirmed send (guarded; a dry run by default)."""
        from sentrix.alerting.dispatch import dispatch_service
        outcome = dispatch_service.send(package_id, authority, by=actor.ref)
        return sos_result(outcome.code, outcome.message, None)

    def demo_trigger(self, incident_type: str, *, actor: Actor) -> SosResult:
        return sos_result("unsupported", _UNSUPPORTED)

    def demo_close_incident(self, *, actor: Actor) -> SosResult:
        return sos_result("unsupported", _UNSUPPORTED)

    def current(self, view: str = "owner") -> dict | None:
        return None

    # ── service ───────────────────────────────────────────────────────────────
    def status(self) -> dict[str, Any]:
        from sentrix.pipeline.state import SOS_IDLE
        return dict(SOS_IDLE)

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def health(self) -> dict[str, Any]:
        return {"enabled": False, "service": "null"}


_NULL = NullSosService()


def current_service() -> Any:
    """The running SOS service, else the shared null one. Routes and command
    handlers call this instead of building the pipeline."""
    from sentrix import services
    service = services.registry.get("sos")
    return service if service is not None else _NULL


__all__ = ["SosTrigger", "LevelChange", "SosService", "NullSosService", "SosResult",
           "sos_result", "current_service", "SOS_ACTIONS", "RESPOND_ACTIONS", "VIEWS"]
