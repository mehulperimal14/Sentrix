# ai/behaviour_engine.py
#
# ARCHITECTURE: Infers intent from bounding-box motion trajectories.
# No heavy pose model needed — uses centroid speed and aspect ratio heuristics.
# Maintains per-track history (dict keyed by track_id) for loitering detection.
# Returns a (score, label) tuple representing the highest-threat behaviour across
# all currently tracked persons.

import math
import time
from dataclasses import dataclass
from typing import Dict, List, Tuple


@dataclass
class BehaviourResult:
    score: float
    label: str   # running | crawling | loitering | walking | normal


# Heuristic constants
SPEED_RUN_THRESHOLD    = 45.0   # pixels per frame (raised to prevent jitter from triggering)
SPEED_WALK_THRESHOLD   = 12.0   # pixels per frame
CRAWL_ASPECT_THRESHOLD = 0.2    # h/w < 0.2 → very wide/low → crawling (lowered for webcam)
LOITER_TIME_THRESHOLD  = 30.0   # seconds in same zone
LOITER_RADIUS          = 100    # pixels
FIGHT_PROXIMITY_PX     = 120    # centroids closer than this → potential fight
FIGHT_MOTION_THRESHOLD = 0.35   # motion_score must also be elevated


def _dist(a: tuple, b: tuple) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2)


class BehaviourEngine:
    """Heuristic behaviour classifier based on centroid trajectories."""

    def __init__(self):
        # Per-track history: track_id → list of (centroid, timestamp)
        self.history: Dict[int, List[Tuple[tuple, float]]] = {}

    def classify(self, tracks: list, detections: list, scores: dict = None) -> Tuple[float, str]:
        """
        Analyse all active tracks and return dominant (score, label).
        tracks: list of objects with .track_id and .to_ltrb() OR dicts with bbox.
        detections: raw detection list (used as fallback if tracks is empty).
        scores: optional score dict used for motion_score in fight detection.
        """
        if not tracks:
            return 0.10, "normal"

        results: List[BehaviourResult] = []
        now = time.time()

        # --- FIGHTING DETECTION (multi-person proximity + motion) ---
        # Runs before per-track loop; returns immediately if confident.
        motion_score = float(scores.get("motion", 0.0)) if isinstance(scores, dict) else 0.0
        centroids = []
        for t in tracks:
            try:
                if hasattr(t, "to_ltrb"):
                    x1, y1, x2, y2 = [int(v) for v in t.to_ltrb()]
                elif isinstance(t, dict):
                    x1, y1, x2, y2 = t.get("bbox", [0, 0, 50, 100])
                else:
                    continue
                centroids.append(((x1 + x2) / 2.0, (y1 + y2) / 2.0))
            except Exception:
                pass

        if len(centroids) >= 2 and motion_score >= FIGHT_MOTION_THRESHOLD:
            min_dist = min(
                _dist(centroids[i], centroids[j])
                for i in range(len(centroids))
                for j in range(i + 1, len(centroids))
            )
            if min_dist < FIGHT_PROXIMITY_PX:
                from core.instrumentation import log_instrumentation
                log_instrumentation("BehaviourEngine", "inference", {
                    "score": 0.80, "label": "fighting",
                    "proximity_px": round(min_dist, 1), "latency": 0.0
                })
                return 0.80, "fighting"
        # --- END FIGHTING DETECTION ---

        for track in tracks:
            try:
                # Support both DeepSORT track objects and plain dicts
                if hasattr(track, "track_id") and hasattr(track, "to_ltrb"):
                    track_id = track.track_id
                    x1, y1, x2, y2 = [int(v) for v in track.to_ltrb()]
                elif isinstance(track, dict):
                    track_id = track.get("id", id(track))
                    x1, y1, x2, y2 = track.get("bbox", [0, 0, 50, 100])
                else:
                    continue

                cx = (x1 + x2) / 2.0
                cy = (y1 + y2) / 2.0
                w  = max(x2 - x1, 1)
                h  = max(y2 - y1, 1)
                centroid = (cx, cy)

                if track_id not in self.history:
                    self.history[track_id] = []

                self.history[track_id].append((centroid, now))
                self.history[track_id] = self.history[track_id][-60:]

                past = self.history[track_id]

                # Speed: distance from previous centroid
                if len(past) >= 2:
                    speed = _dist(centroid, past[-2][0])
                else:
                    speed = 0.0

                # Aspect ratio (h/w): low value → person appears wide/flat → crawling heuristic
                aspect = h / w

                # Loitering: stayed within LOITER_RADIUS for LOITER_TIME_THRESHOLD seconds
                old_entries = [p for p in past if (now - p[1]) >= LOITER_TIME_THRESHOLD]
                loitering = False
                if old_entries:
                    if _dist(centroid, old_entries[0][0]) < LOITER_RADIUS:
                        loitering = True

                # Classification priority: speed → crawl → loiter → walk → normal
                if speed > SPEED_RUN_THRESHOLD:
                    results.append(BehaviourResult(score=0.65, label="running"))
                elif aspect < CRAWL_ASPECT_THRESHOLD:
                    results.append(BehaviourResult(score=0.70, label="crawling"))
                elif loitering:
                    results.append(BehaviourResult(score=0.55, label="loitering"))
                elif speed > SPEED_WALK_THRESHOLD:
                    results.append(BehaviourResult(score=0.20, label="walking"))
                else:
                    results.append(BehaviourResult(score=0.10, label="normal"))

            except Exception as e:
                print(f"[BehaviourEngine] track error: {e}")
                results.append(BehaviourResult(score=0.10, label="normal"))

        if not results:
            from core.instrumentation import log_instrumentation
            log_instrumentation("BehaviourEngine", "inference", {"score": 0.10, "label": "normal", "latency": time.time() - now})
            return 0.10, "normal"

        dominant = max(results, key=lambda r: r.score)
        from core.instrumentation import log_instrumentation
        log_instrumentation("BehaviourEngine", "inference", {"score": dominant.score, "label": dominant.label, "latency": time.time() - now})
        return dominant.score, dominant.label

    def _prune_old_tracks(self, active_ids: set):
        """Remove history for tracks no longer active (call periodically)."""
        for tid in list(self.history.keys()):
            if tid not in active_ids:
                del self.history[tid]
