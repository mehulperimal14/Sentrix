"""Threat level bands and the hysteresis that stops them flapping.

EMA smoothing alone cannot suppress false alarms. A single large spike still
drags the smoothed value across a threshold; it just takes slightly longer. What
actually works — and what alarm engineering has used for decades — is a dwell
requirement: a level change is only committed once the candidate level has
persisted for a minimum period.

The dwell is deliberately asymmetric:

* **Escalate fast** (0.6 s default). Response latency is a product requirement.
* **De-escalate slowly** (5 s default). An intruder stepping behind a pillar,
  or a detector dropping one frame, must not clear an active alarm.

The previous build used ``statistics.mode`` over the last five levels, which is
symmetric, frame-rate dependent, and undefined behaviour on ties — Python's
``mode`` raised ``StatisticsError`` on a multimodal window, which the caller
caught by falling back to the raw unsmoothed level, defeating the filter exactly
when the signal was noisiest.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sentrix.config import settings
from sentrix.utils.logging import get_logger

log = get_logger("policy")


def level_for(tci: float, thresholds: tuple = None) -> int:
    """Map a TCI value to a level in 1..5 using the configured bands."""
    thresholds = thresholds or settings.policy.thresholds
    level = 1
    for threshold in thresholds:
        if tci >= threshold:
            level += 1
    return min(level, 5)


@dataclass
class LevelState:
    level: int
    status: str
    changed: bool
    since: float


class LevelPolicy:
    """Hysteretic mapping from a TCI stream to a committed threat level."""

    def __init__(self) -> None:
        self.cfg = settings.policy
        self._level = 1
        self._since = time.monotonic()
        self._candidate = 1
        self._candidate_since = time.monotonic()

    @property
    def level(self) -> int:
        return self._level

    @property
    def status(self) -> str:
        return self.cfg.names.get(self._level, "UNKNOWN")

    def reset(self) -> None:
        now = time.monotonic()
        self._level, self._candidate = 1, 1
        self._since = self._candidate_since = now

    def update(self, tci: float, now: float | None = None) -> LevelState:
        now = time.monotonic() if now is None else now
        instant = level_for(tci, self.cfg.thresholds)

        if instant != self._candidate:
            self._candidate = instant
            self._candidate_since = now

        if instant == self._level:
            return LevelState(self._level, self.status, False, self._since)

        dwell = now - self._candidate_since
        required = (self.cfg.escalate_dwell_seconds if instant > self._level
                    else self.cfg.deescalate_dwell_seconds)

        if dwell < required:
            # Candidate has not held long enough — stay put.
            return LevelState(self._level, self.status, False, self._since)

        previous = self._level
        self._level, self._since = instant, now
        log.info("level %d -> %d (tci=%.3f, held %.1fs)", previous, instant,
                 tci, dwell)
        return LevelState(self._level, self.status, True, self._since)


__all__ = ["LevelPolicy", "LevelState", "level_for"]
