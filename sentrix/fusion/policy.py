"""Threat level bands and the hysteresis that stops them flapping.

Single-frame false positives are rejected upstream: the TCI only leaves the L1
band on evidence confirmed over several observations (``fusion/evidence.py``).
The policy adds what alarm engineering has used for decades — a dwell before
the costly levels and a slow step-down.

How the dwell works (per threshold, not per band)
-------------------------------------------------
For every band threshold the policy tracks two facts: since when the TCI has
been *continuously at or above* it (with how many observations), and when it
was *last* at or above it.

* **Escalate** to the highest level whose threshold has been held continuously
  for ``escalate_dwell_seconds`` **and** ``min_escalate_observations`` ticks
  (both per threshold: immediate for L2/L3, 0.6 s and 2 ticks for L4/L5).
  A rising threat that moves from one band into the next keeps its clock —
  the previous version restarted the clock on every band change, so a ramp
  0.64 → 0.76 never committed (the live "L2 at TCI 0.758", CLAUDE.md §5).
  The observation count stops one frame at 0.5 Hz from committing anything.
* **De-escalate** below a threshold only after the TCI has not touched it for
  ``deescalate_dwell_seconds``. A value flapping between two lower bands no
  longer resets the timer (the live 20.9 s stuck L3).

Escalate fast, de-escalate slowly: an intruder stepping behind a pillar, or a
detector dropping one frame, must not clear an active alarm.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sentrix.config import settings
from sentrix.utils.logging import get_logger

log = get_logger("policy")


def _per_threshold(value, n: int) -> tuple:
    """A scalar setting applies to every threshold; a tuple is per threshold."""
    if isinstance(value, (tuple, list)):
        values = tuple(value)
        if len(values) != n:
            raise ValueError(f"expected {n} per-threshold values, got {values}")
        return values
    return (value,) * n


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
    #: Seconds until the committed level may step down, if the TCI is below it.
    deescalate_in: float | None = None


class LevelPolicy:
    """Hysteretic mapping from a TCI stream to a committed threat level."""

    def __init__(self) -> None:
        self.cfg = settings.policy
        n = len(self.cfg.thresholds)
        self._dwell = _per_threshold(self.cfg.escalate_dwell_seconds, n)
        self._min_obs = _per_threshold(self.cfg.min_escalate_observations, n)
        self.reset()

    @property
    def level(self) -> int:
        return self._level

    @property
    def status(self) -> str:
        return self.cfg.names.get(self._level, "UNKNOWN")

    def reset(self) -> None:
        n = len(self.cfg.thresholds)
        self._level = 1
        self._since = time.monotonic()
        self._above_since: list[float | None] = [None] * n
        self._above_count = [0] * n
        self._last_above: list[float | None] = [None] * n

    def update(self, tci: float, now: float | None = None) -> LevelState:
        now = time.monotonic() if now is None else now
        cfg = self.cfg
        for i, threshold in enumerate(cfg.thresholds):
            if tci >= threshold:
                if self._above_since[i] is None:
                    self._above_since[i], self._above_count[i] = now, 0
                self._above_count[i] += 1
                self._last_above[i] = now
            else:
                self._above_since[i], self._above_count[i] = None, 0

        confirmed = 1
        held = 1
        for i in range(len(cfg.thresholds)):
            since = self._above_since[i]
            if (since is not None and now - since >= self._dwell[i]
                    and self._above_count[i] >= self._min_obs[i]):
                confirmed = i + 2
            last = self._last_above[i]
            if last is not None and now - last < cfg.deescalate_dwell_seconds:
                held = i + 2

        new = max(confirmed, min(self._level, held))
        changed = new != self._level
        if changed:
            log.info("level %d -> %d (tci=%.3f)", self._level, new, tci)
            self._level, self._since = new, now

        deescalate_in = None
        if self._level > 1 and tci < cfg.thresholds[self._level - 2]:
            last = self._last_above[self._level - 2]
            if last is not None:
                deescalate_in = max(0.0, cfg.deescalate_dwell_seconds - (now - last))
        return LevelState(self._level, self.status, changed, self._since, deescalate_in)


__all__ = ["LevelPolicy", "LevelState", "level_for"]
