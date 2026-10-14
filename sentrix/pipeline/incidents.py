"""Incident grouping — the fix for the alert storm.

The previous pipeline acted per *frame*. At level 2 it enqueued a snapshot, an
SMS and a database insert on every inference tick, and at level 3 it added an
encrypted evidence bundle and a siren activation. A single sustained false
positive therefore produced, in ``backend/runtime/``:

* 1,489 encrypted evidence bundles, 1,197 of them level 5
* 2,415 alert snapshots
* 642 MB on disk
* one SMS attempt per tick, throttled only by a global cooldown

Worse, evidence filenames had one-second resolution
(``evidence_%Y%m%d_%H%M%S_{level}``), so multiple writes inside the same second
silently overwrote each other. Evidence was being destroyed by the naming
scheme in a system claiming chain of custody.

The unit of response here is an **Incident**: a contiguous period above level 1.
Side effects bind to the incident, not the frame. A ten-minute event produces
one SMS, one dispatch package and a bounded number of evidence captures,
regardless of frame rate.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sentrix.config import settings
from sentrix.utils.logging import get_logger

log = get_logger("incidents")


@dataclass(frozen=True)
class PeakSnapshot:
    """The tick that defines an incident, every field taken from that one tick.

    The first live test's incident rows mixed ticks: the type was frozen at the
    first non-normal frame, the reason and TCI came from the peak, and the
    authorisation flag was hard-coded (CLAUDE.md section 5). A reviewer could
    not reconstruct what the system saw. Frozen, so a reader on another thread
    always sees one consistent tick.
    """

    tci: float = 0.0
    level: int = 1
    incident_type: str = "normal"
    reason: str = ""
    features: dict = field(default_factory=dict)
    authorized: bool = False
    person_present: bool = False
    #: The evidence behind the TCI at that tick (per-class votes).
    trigger: dict = field(default_factory=dict)
    at: float = 0.0
    wall: float = 0.0


@dataclass
class Incident:
    """One contiguous threat episode."""

    id: str
    started_at: float
    started_wall: float
    level: int
    peak_level: int
    last_active: float
    peak: PeakSnapshot = field(default_factory=PeakSnapshot)
    ended_at: float | None = None
    frames: int = 0
    evidence_count: int = 0
    last_evidence_at: float = 0.0
    #: Actions already performed, so each fires at most once per incident.
    actions: set[str] = field(default_factory=set)

    # Artifacts produced by the background worker. Populated after the fact,
    # then read once when the incident closes and is persisted. Previously the
    # worker's return values were dropped, so every incident row recorded a
    # null snapshot path and a null evidence bundle.
    snapshot_path: str | None = None
    evidence_bundle: str | None = None
    evidence_hash: str | None = None

    #: Monotonic time the row was last written, and the peak it wrote. The
    #: row is saved when the incident opens and again when the peak moves, so
    #: a hard kill keeps the incident (CLAUDE.md M3).
    saved_at: float | None = None
    saved_peak: PeakSnapshot | None = None
    saved_level: int = 0

    # The peak tick's fields, read by storage, evidence and tools.
    @property
    def peak_tci(self) -> float:
        return self.peak.tci

    @property
    def incident_type(self) -> str:
        return self.peak.incident_type

    @property
    def peak_reason(self) -> str:
        return self.peak.reason

    @property
    def peak_features(self) -> dict:
        return self.peak.features

    @property
    def duration(self) -> float:
        return (self.ended_at or time.monotonic()) - self.started_at

    @property
    def short_id(self) -> str:
        return self.id[:8]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "started": self.started_wall,
            "duration_s": round(self.duration, 2),
            "peak_level": self.peak_level,
            "peak_tci": round(self.peak_tci, 4),
            "incident_type": self.incident_type,
            "frames": self.frames,
            "evidence_count": self.evidence_count,
            "actions": sorted(self.actions),
            "snapshot_path": self.snapshot_path,
            "evidence_bundle": self.evidence_bundle,
        }


class IncidentManager:
    """Opens, extends and closes incidents from the committed level stream."""

    def __init__(self) -> None:
        self.cfg = settings.incident
        self._current: Incident | None = None
        self._closed: list[Incident] = []

    @property
    def current(self) -> Incident | None:
        return self._current

    @property
    def recent(self) -> list[Incident]:
        return list(self._closed[-50:])

    def update(self, level: int, tci: float, incident_type: str,
               now: float | None = None, features: dict | None = None,
               reason: str = "", authorized: bool = False,
               person_present: bool = False,
               trigger: dict | None = None) -> tuple[Incident | None, Incident | None]:
        """Advance incident state.

        Returns ``(active, just_closed)``. ``active`` is None when the scene is
        quiet; ``just_closed`` is set only on the tick an incident ends. The
        incident's type, reason, TCI, features and authorisation all come from
        its peak tick (highest TCI; the first such tick on a tie).
        """
        now = time.monotonic() if now is None else now
        closed: Incident | None = None

        if level <= 1:
            if self._current is not None:
                # Quiet ticks inside an open incident still belong to it.
                self._current.frames += 1
                self._current.level = level
                quiet = now - self._current.last_active
                if quiet >= self.cfg.close_after_quiet_seconds:
                    closed = self._close(now)
            return self._current, closed

        tick = PeakSnapshot(tci=tci, level=level, incident_type=incident_type,
                            reason=reason, features=dict(features or {}),
                            authorized=bool(authorized),
                            person_present=bool(person_present),
                            trigger=dict(trigger or {}), at=now, wall=time.time())
        if self._current is None:
            self._current = Incident(
                id=str(uuid.uuid4()), started_at=now, started_wall=time.time(),
                level=level, peak_level=level, last_active=now, peak=tick)
            log.info("incident %s opened (level %d, %s)",
                     self._current.short_id, level, incident_type)
        elif now - self._current.started_at > self.cfg.max_duration_seconds:
            # Cap runaway incidents so one continuous event does not become an
            # unbounded evidence bundle.
            closed = self._close(now)
            return (self.update(level, tci, incident_type, now, features, reason,
                                authorized, person_present, trigger)[0], closed)

        incident = self._current
        incident.frames += 1
        incident.level = level
        incident.last_active = now
        if level > incident.peak_level:
            incident.peak_level = level
        if tci > incident.peak.tci:
            incident.peak = tick
        return incident, closed

    def close_now(self, now: float | None = None) -> Incident | None:
        """Close the open incident at once (``Pipeline.request_close_incident``,
        the test-mode demo reset). Returns it, or None (nothing open, or too
        short and never saved)."""
        return self._close(time.monotonic() if now is None else now)

    def _close(self, now: float) -> Incident | None:
        incident = self._current
        self._current = None
        if incident is None:
            return None
        incident.ended_at = now
        if incident.duration < self.cfg.min_duration_seconds and incident.saved_at is None:
            log.debug("incident %s discarded (%.2fs, below minimum)",
                      incident.short_id, incident.duration)
            return None
        self._closed.append(incident)
        log.info("incident %s closed (peak L%d, %.1fs, %d frames)",
                 incident.short_id, incident.peak_level, incident.duration,
                 incident.frames)
        return incident

    def should_capture_evidence(self, incident: Incident,
                                now: float | None = None) -> bool:
        """Rate-limit evidence within a long-running incident."""
        now = time.monotonic() if now is None else now
        if incident.evidence_count == 0:
            return True
        return now - incident.last_evidence_at >= self.cfg.evidence_interval_seconds

    @staticmethod
    def claim(incident: Incident, action: str) -> bool:
        """Claim a once-per-incident action. True the first time only."""
        if action in incident.actions:
            return False
        incident.actions.add(action)
        return True


__all__ = ["Incident", "IncidentManager"]
