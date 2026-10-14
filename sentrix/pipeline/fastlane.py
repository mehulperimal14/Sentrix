"""The fast lane: a small YOLO pass between clips (HARDWARE.md T1).

A clip decision comes every hop (2 s) and needs two positive keyframes, so a
pistol drawn just after a clip boundary reaches L3 only 2.5-4.5 s later. The
fast lane closes that gap: the detector heads at 320 px on every
``fast_lane_every_n``-th buffered frame (about 4 Hz at 15 fps), and **two
consecutive hits on one track** raise L3 at once - snapshot and evidence. The
clip that follows confirms, types and, with corroboration, escalates.

The rules are the evidence layer's, applied to frames instead of keyframes:

* a hit is a box of an alarm label (``ThreatConfig.detector_weapon_labels`` /
  ``detector_fire_labels``) at or above the detection floor
  (``DetectionConfig.weapon_conf`` / ``fire_conf``); stick and smoke never
  count (D1);
* "one track" is the same object on consecutive fast-lane frames
  (:func:`~sentrix.runtime.tracking.same_region`): a box that jumps across
  the room is a new track and starts again from one hit;
* a weapon hit needs a person seen in the last ``person_memory_seconds``
  (by the fast lane's own person head, or the clip engine);
* the fast lane is one detector source, so it can never pass the L3 band on
  its own (one confirmed source <= ``single_source_cap``, D3).

The heads are the shipped detectors' own weights, loaded a second time on the
configured runtime (``SENTRIX_RUNTIME``): the clip engine runs the torch heads
on another thread, and ultralytics models are not safe to share between
threads.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from sentrix.config import settings
from sentrix.fusion.evidence import CLASS_ORDER, EvidenceAccumulator, KeyframeVote
from sentrix.runtime.detector import PERSON_CLASS, plausible
from sentrix.runtime.tracking import iou, same_region
from sentrix.utils.logging import get_logger, telemetry

log = get_logger("fastlane")

#: The fast lane counts as a detector for ``ThreatConfig.sources``.
SOURCE = "fastlane"


@dataclass(frozen=True)
class FastLaneHit:
    """A confirmed fast-lane raise: ``hits`` consecutive frames on one track."""

    cls: str
    label: str
    value: float
    threshold: float
    bbox: tuple[int, int, int, int]
    track: int
    hits: int
    first_at: float
    at: float

    def to_dict(self) -> dict:
        return {"cls": self.cls, "label": self.label, "conf": round(self.value, 4),
                "threshold": round(self.threshold, 4), "bbox": list(self.bbox),
                "track": self.track, "hits": self.hits,
                "span_s": round(self.at - self.first_at, 3)}


@dataclass
class _Track:
    tid: int
    box: tuple
    label: str
    conf: float
    first_at: float
    last_n: int
    hits: int = 1


@dataclass
class FastLaneFrame:
    """What one fast-lane pass saw (for the decision's features and the HUD)."""

    at: float
    persons: list = field(default_factory=list)
    boxes: dict = field(default_factory=dict)
    hits: list = field(default_factory=list)
    person_recent: bool | None = None
    ms: float = 0.0


class FastLane:
    """Detector heads at ``fast_lane_imgsz`` on every Nth frame."""

    def __init__(self, heads: dict, cfg=None) -> None:
        self.cfg = cfg or settings.clip
        self.threat = settings.threat
        self.detection = settings.detection
        self.heads = {role: head for role, head in heads.items() if head is not None}
        self.every_n = max(1, int(self.cfg.fast_lane_every_n))
        self.hits_needed = max(1, int(self.cfg.fast_lane_hits))
        self._lock = threading.Lock()
        self._n = 0
        self._tracks: dict[str, list[_Track]] = {"weapon": [], "fire": []}
        self._next_id = 1
        self._last_person_at: float | None = None
        self.calls = 0
        self.raises = 0
        self._ms: deque[float] = deque(maxlen=500)

    @classmethod
    def build(cls, cfg=None, backend: str | None = None,
              threads: int | None = None) -> "FastLane | None":
        """Load the person/weapon/fire weights at the fast-lane size; None if
        neither alarm head can run."""
        from sentrix.perception.detector import (_FIRE_WEIGHTS, _PERSON_WEIGHTS,
                                                 _WEAPON_WEIGHTS, _resolve)
        from sentrix.runtime import build_head

        cfg = cfg or settings.clip
        size = int(cfg.fast_lane_imgsz)
        heads = {role: build_head(_resolve(weights), size, backend, threads)
                 for role, weights in (("person", _PERSON_WEIGHTS),
                                       ("weapon", _WEAPON_WEIGHTS),
                                       ("fire", _FIRE_WEIGHTS))}
        if heads["weapon"] is None and heads["fire"] is None:
            log.error("fast lane disabled: no weapon or fire head could be loaded")
            return None
        lane = cls(heads, cfg)
        log.info("fast lane ready: %s at %d px, every %d frames, %d hits",
                 {r: getattr(h, "backend", None) for r, h in lane.heads.items()}, size,
                 lane.every_n, lane.hits_needed)
        return lane

    @property
    def available(self) -> bool:
        return "weapon" in self.heads or "fire" in self.heads

    def due(self, seq: int) -> bool:
        """Whether buffered frame ``seq`` is one the fast lane analyses."""
        return seq % self.every_n == 0

    # ── analysis ──────────────────────────────────────────────────────────────
    def _alarm_boxes(self, role: str, frame: np.ndarray):
        head = self.heads.get(role)
        if head is None:
            return []
        if role == "weapon":
            labels, floor = self.threat.detector_weapon_labels, self.detection.weapon_conf
        else:
            labels, floor = self.threat.detector_fire_labels, self.detection.fire_conf
        boxes = head.predict(frame, conf=floor, iou=self.detection.iou,
                             max_det=self.detection.max_detections)
        return [(b, floor) for b in boxes
                if b.label in labels and b.conf >= floor
                and plausible(b.bbox, frame.shape, self.detection)]

    def analyse(self, frame: np.ndarray, at: float,
                person_seen_at: float | None = None) -> FastLaneFrame:
        """One pass. ``person_seen_at`` is the clip engine's latest person time."""
        started = time.perf_counter()
        out = FastLaneFrame(at=at)
        with self._lock:
            n = self._n
            self._n += 1
            if "person" in self.heads:
                try:
                    out.persons = [b for b in self.heads["person"].predict(
                        frame, conf=self.detection.person_conf, iou=self.detection.iou,
                        classes=[PERSON_CLASS], max_det=self.detection.max_detections)
                        if plausible(b.bbox, frame.shape, self.detection)]
                except Exception as exc:
                    log.error("fast-lane person pass failed: %s", exc)
            if out.persons:
                self._last_person_at = at
            seen = [t for t in (self._last_person_at, person_seen_at) if t is not None]
            if "person" not in self.heads and person_seen_at is None:
                out.person_recent = None            # no way to know: gate off
            else:
                out.person_recent = any(
                    0.0 <= at - t <= self.threat.person_memory_seconds for t in seen)
            for cls in ("weapon", "fire"):
                try:
                    boxes = self._alarm_boxes(cls, frame)
                except Exception as exc:
                    log.error("fast-lane %s pass failed: %s", cls, exc)
                    boxes = []
                out.boxes[cls] = [b for b, _ in boxes]
                if cls == "weapon" and out.person_recent is False:
                    boxes = []                       # a weapon needs a person
                out.hits.extend(self._associate(cls, boxes, n, at))
            self.calls += 1
            self.raises += bool(out.hits)
            out.ms = (time.perf_counter() - started) * 1000.0
            self._ms.append(out.ms)
        telemetry.record("fastlane", "frame", None, out.ms / 1000.0)
        return out

    def _associate(self, cls: str, boxes, n: int, at: float) -> list[FastLaneHit]:
        """Extend the tracks hit on the previous pass; drop the rest."""
        previous = [t for t in self._tracks[cls] if t.last_n == n - 1]
        tracks: list[_Track] = []
        for box, floor in sorted(boxes, key=lambda b: -b[0].conf):
            match, best = None, -1.0
            for track in previous:
                if not same_region(box.bbox, track.box):
                    continue
                score = iou(box.bbox, track.box)
                if score > best:
                    match, best = track, score
            if match is not None:
                previous.remove(match)
                match.hits += 1
                match.box, match.label, match.conf, match.last_n = box.bbox, box.label, box.conf, n
                tracks.append(match)
            else:
                tracks.append(_Track(self._next_id, box.bbox, box.label, box.conf, at, n))
                self._next_id += 1
        self._tracks[cls] = tracks
        threshold = (self.detection.weapon_conf if cls == "weapon"
                     else self.detection.fire_conf)
        return [FastLaneHit(cls, t.label, t.conf, threshold, tuple(t.box), t.tid, t.hits,
                            t.first_at, at)
                for t in tracks if t.hits >= self.hits_needed]

    # ── reporting ─────────────────────────────────────────────────────────────
    def stats(self) -> dict:
        with self._lock:
            values = sorted(self._ms)
            calls, raises = self.calls, self.raises
        def pct(q):
            return round(values[min(len(values) - 1, int(len(values) * q))], 1) if values else None
        return {"calls": calls, "raises": raises, "every_n": self.every_n,
                "imgsz": int(self.cfg.fast_lane_imgsz), "ms_p50": pct(0.5), "ms_p95": pct(0.95),
                "backends": {r: getattr(h, "backend", None) for r, h in self.heads.items()}}

    def reset(self) -> None:
        with self._lock:
            self._n = 0
            self._tracks = {"weapon": [], "fire": []}
            self._last_person_at = None


def fast_lane_evidence(hits: list[FastLaneHit], cfg=None) -> dict:
    """Per-class evidence for a fast-lane raise, on the evidence layer's scale.

    One confirmed detector source: ``confirmed_floor`` at the threshold up to
    ``single_source_cap`` - the L3 band, never L4 (D3).
    """
    scorer = EvidenceAccumulator(cfg)
    out = {}
    for cls in CLASS_ORDER:
        mine = [h for h in hits if h.cls == cls]
        votes = []
        if mine and scorer._allowed("detector", cls):
            best = max(mine, key=lambda h: h.value)
            votes.append(KeyframeVote(SOURCE, best.value, best.threshold, hits=best.hits,
                                      seen=best.hits, confirmed=True, fresh=True,
                                      span=best.at - best.first_at,
                                      group=("track", best.track), unit="frames"))
        out[cls] = scorer._score(cls, votes)
    return out


__all__ = ["FastLane", "FastLaneFrame", "FastLaneHit", "SOURCE", "fast_lane_evidence"]
