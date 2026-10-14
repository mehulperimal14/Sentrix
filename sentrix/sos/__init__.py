"""SOS assistance requests: the interface the pipeline calls (docs/7 §7).

The pipeline never decides anything about SOS. At a committed L3+ it asks the
service whether this incident type is an SOS class (:meth:`SosService.wants`),
and if so hands over a :class:`SosTrigger` once per incident; on every
committed level change it reports a :class:`LevelChange`. Everything else
(rendering, prompts, timers, the window, dispatch) belongs to the service,
which runs in the web process (decision: one process owns web app, SOS
service, link client, outbox and channel router).

:class:`NullSosService` is the default on every ``Pipeline``. Evaluation,
replay and benchmark pipelines must keep it (``sentrix.services.
assert_measurement_safe``); only the app lifespan swaps in the real service,
and only when ``SENTRIX_SOS_ENABLED=1``.

Contract for implementations (track T-SOS, ``sentrix/sos/service.py``):

* ``wants`` is called on the inference thread at L3+ until it accepts:
  constant time, no I/O, no locks held for long.
* ``trigger`` runs on the pipeline's FIFO worker *after* the incident's
  snapshot and first evidence capture (so their paths are set), or
  synchronously when that queue is full: it must only enqueue (SQLite insert
  or an in-memory queue) and must be idempotent per ``incident_id``.
* ``on_level`` is called on the inference thread: enqueue only.
* Never raise into the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


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

    def wants(self, incident_type: str, level: int) -> bool: ...

    def trigger(self, trigger: SosTrigger) -> None: ...

    def on_level(self, change: LevelChange) -> None: ...

    def status(self) -> dict[str, Any]: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def health(self) -> dict[str, Any]: ...


class NullSosService:
    """Does nothing. The default, and the only service measurement may use."""

    name = "sos"
    enabled = False

    def wants(self, incident_type: str, level: int) -> bool:
        return False

    def trigger(self, trigger: SosTrigger) -> None:
        return None

    def on_level(self, change: LevelChange) -> None:
        return None

    def status(self) -> dict[str, Any]:
        from sentrix.pipeline.state import SOS_IDLE
        return dict(SOS_IDLE)

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def health(self) -> dict[str, Any]:
        return {"enabled": False, "service": "null"}


__all__ = ["SosTrigger", "LevelChange", "SosService", "NullSosService"]
