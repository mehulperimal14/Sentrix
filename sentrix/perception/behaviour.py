"""Kinematic behaviour classification from tracked person trajectories.

Everything here is resolution- and frame-rate independent. The previous version
was neither: ``SPEED_RUN_THRESHOLD = 45.0`` pixels *per frame* meant something
different at 5 FPS than at 30 FPS and different again on a 1080p camera than on
a 480p webcam, and ``LOITER_RADIUS = 100`` pixels had the same problem.

Speeds are now expressed in frame diagonals per second, and dwell times in
seconds, so a tuned threshold transfers across cameras and hardware.

Track history is also pruned. ``_prune_old_tracks`` existed before but was never
called, so ``self.history`` grew without bound for the lifetime of the process.

Upgrade path: bounding-box centroids are a weak proxy for what a person is
doing. ``PoseEstimator`` in ``detector.py`` gives 17 keypoints per person, from
which limb velocity and body configuration are directly observable. In
particular the crawl heuristic below infers "crawling" from box aspect ratio,
which also fires on anyone the detector clips at the frame edge; keypoints
resolve that unambiguously.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict, deque

from sentrix.config import settings
from sentrix.perception.detector import Detection
from sentrix.utils.logging import get_logger

log = get_logger("behaviour")


class BehaviourEngine:
    """Per-track kinematics over a time-based history window."""

    def __init__(self) -> None:
        self.cfg = settings.behaviour
        self._history: dict[int, deque] = defaultdict(lambda: deque(maxlen=256))
        self._last_seen: dict[int, float] = {}

    def _prune(self, now: float) -> None:
        stale = [tid for tid, seen in self._last_seen.items()
                 if now - seen > self.cfg.track_memory_seconds]
        for tid in stale:
            self._history.pop(tid, None)
            self._last_seen.pop(tid, None)

    def classify(self, persons: list[Detection], frame_shape,
                 violence: tuple[float, str] | None = None,
                 now: float | None = None) -> tuple[float, str]:
        """Return the dominant ``(score, label)`` across all tracked people.

        ``violence`` is the windowed classifier output, passed in rather than
        computed here so this module stays free of model dependencies.
        """
        now = time.monotonic() if now is None else now
        self._prune(now)

        candidates: list[tuple[float, str]] = []

        # The temporal violence classifier outranks any kinematic heuristic.
        if violence is not None:
            score, label = violence
            if label == "fighting":
                candidates.append((max(score, self.cfg.scores["fighting"]),
                                   "fighting"))

        height, width = frame_shape[:2]
        diagonal = math.hypot(width, height) or 1.0

        for person in persons:
            tid = person.track_id
            if tid is None:
                continue
            centroid = person.centroid
            history = self._history[tid]
            history.append((now, centroid))
            self._last_seen[tid] = now

            aspect = person.height / float(person.width)

            # Speed over the two most recent observations, normalised.
            speed = 0.0
            if len(history) >= 2:
                (t0, c0), (t1, c1) = history[-2], history[-1]
                dt = max(t1 - t0, 1e-3)
                distance = math.dist(c0, c1) / diagonal
                speed = distance / dt

            # Loitering: present for the dwell period and still nearby.
            loitering = False
            dwell = self.cfg.loiter_dwell_seconds
            oldest = next((entry for entry in history if now - entry[0] >= dwell),
                          None)
            if oldest is not None:
                drift = math.dist(oldest[1], centroid) / diagonal
                loitering = drift < self.cfg.loiter_radius

            if speed > self.cfg.run_speed:
                candidates.append((self.cfg.scores["running"], "running"))
            elif aspect < self.cfg.crawl_aspect_ratio:
                candidates.append((self.cfg.scores["crawling"], "crawling"))
            elif loitering:
                candidates.append((self.cfg.scores["loitering"], "loitering"))
            elif speed > self.cfg.walk_speed:
                candidates.append((self.cfg.scores["walking"], "walking"))
            else:
                candidates.append((self.cfg.scores["normal"], "normal"))

        if not candidates:
            return self.cfg.scores["normal"], "normal"
        return max(candidates, key=lambda c: c[0])

    def reset(self) -> None:
        self._history.clear()
        self._last_seen.clear()


#: Labels that earn the contextual TCI booster. ``fighting`` is included here;
#: the previous fusion engine boosted only running/crawling/loitering, so a
#: detected fight received no boost at all.
SUSPICIOUS_LABELS = frozenset({"fighting", "running", "crawling", "loitering"})

__all__ = ["BehaviourEngine", "SUSPICIOUS_LABELS"]
