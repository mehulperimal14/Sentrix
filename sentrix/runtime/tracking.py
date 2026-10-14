"""Box association: same object across frames, without a full tracker.

Three users:

* the clip engine groups weapon and fire boxes into *regions*, so a class is
  confirmed only when it crosses on two keyframes of the same object (one
  track for a weapon held by a person, one image region for a fire);
* the fast lane needs "two consecutive hits on one track";
* :class:`PersonTracker` gives exported (onnx/ncnn) person heads the
  ByteTrack ids ``model.track(persist=True)`` gives the torch detector, and
  falls back to IoU association where ultralytics is not installed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import numpy as np

from sentrix.utils.logging import get_logger

log = get_logger("runtime")


def iou(a, b) -> float:
    """Intersection over union of two xyxy boxes."""
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def same_region(a, b, min_iou: float = 0.1, grow: float = 0.25) -> bool:
    """Whether two boxes plausibly show the same object a moment apart.

    Overlap, or one box's centre inside the other grown by ``grow`` of its
    size: a flickering flame or a waved pistol changes shape too much
    between keyframes a second apart for IoU alone.
    """
    if iou(a, b) >= min_iou:
        return True
    for inner, outer in ((a, b), (b, a)):
        cx, cy = (inner[0] + inner[2]) / 2.0, (inner[1] + inner[3]) / 2.0
        w, h = outer[2] - outer[0], outer[3] - outer[1]
        if (outer[0] - grow * w <= cx <= outer[2] + grow * w
                and outer[1] - grow * h <= cy <= outer[3] + grow * h):
            return True
    return False


def containing_track(box, persons) -> int | None:
    """The person track whose box holds ``box`` best (most of its area), if any.

    ``persons`` are ``(track_id, (x1, y1, x2, y2))``; a weapon in a hand often
    pokes out of the person box, so the person box is grown by 15 %.
    """
    best, best_cover = None, 0.0
    area = max(1.0, (box[2] - box[0]) * (box[3] - box[1]))
    for track_id, (x1, y1, x2, y2) in persons:
        if track_id is None:
            continue
        gw, gh = 0.15 * (x2 - x1), 0.15 * (y2 - y1)
        ix = max(0.0, min(box[2], x2 + gw) - max(box[0], x1 - gw))
        iy = max(0.0, min(box[3], y2 + gh) - max(box[1], y1 - gh))
        cover = ix * iy / area
        if cover > best_cover:
            best, best_cover = track_id, cover
    return best if best_cover >= 0.3 else None


@dataclass
class _Region:
    rid: int
    box: tuple
    seen_at: float


class RegionGrouper:
    """Persistent ids for boxes of one class across frames (by :func:`same_region`).

    Frames must arrive in time order; a region not seen for ``forget_seconds``
    is forgotten, so a fire that reappears elsewhere later is a new region.
    """

    def __init__(self, forget_seconds: float = 4.0) -> None:
        self.forget = forget_seconds
        self._regions: list[_Region] = []
        self._next = 1

    def assign(self, boxes, at: float) -> list[int]:
        self._regions = [r for r in self._regions if at - r.seen_at <= self.forget]
        ids: list[int] = []
        taken: set[int] = set()
        for box in boxes:
            match = None
            best = -1.0
            for region in self._regions:
                if region.rid in taken or not same_region(box, region.box):
                    continue
                score = iou(box, region.box)
                if score > best:
                    match, best = region, score
            if match is None:
                match = _Region(self._next, tuple(box), at)
                self._next += 1
                self._regions.append(match)
            match.box, match.seen_at = tuple(box), at
            taken.add(match.rid)
            ids.append(match.rid)
        return ids

    def reset(self) -> None:
        self._regions.clear()
        self._next = 1


@dataclass
class _Track:
    tid: int
    box: tuple
    seen_at: float
    misses: int = 0


@dataclass
class IouTracker:
    """Greedy IoU association of person boxes; the fallback when ByteTrack
    (ultralytics) is unavailable. Ids are stable while boxes keep overlapping."""

    min_iou: float = 0.3
    max_misses: int = 30
    _tracks: list = field(default_factory=list)
    _next: int = 1

    def update(self, boxes, at: float) -> list[int]:
        ids: list[int] = []
        free = list(self._tracks)
        for box in boxes:
            best, best_iou = None, self.min_iou
            for track in free:
                score = iou(box, track.box)
                if score >= best_iou:
                    best, best_iou = track, score
            if best is None:
                best = _Track(self._next, tuple(box), at)
                self._next += 1
                self._tracks.append(best)
            else:
                free.remove(best)
            best.box, best.seen_at, best.misses = tuple(box), at, 0
            ids.append(best.tid)
        for track in free:
            track.misses += 1
        self._tracks = [t for t in self._tracks if t.misses <= self.max_misses]
        return ids

    def reset(self) -> None:
        self._tracks.clear()
        self._next = 1


class _Detections:
    """The results-like object ultralytics' BYTETracker reads."""

    def __init__(self, xyxy: np.ndarray, conf: np.ndarray, cls: np.ndarray) -> None:
        self.xyxy, self.conf, self.cls = xyxy, conf, cls

    @property
    def xywh(self) -> np.ndarray:
        x1, y1, x2, y2 = self.xyxy.T
        return np.stack([(x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1], axis=1)

    def __len__(self) -> int:
        return len(self.conf)

    def __getitem__(self, index) -> "_Detections":
        return _Detections(self.xyxy[index], self.conf[index], self.cls[index])


class PersonTracker:
    """ByteTrack ids for person boxes from any runtime head.

    Uses ultralytics' ``BYTETracker`` with the ``bytetrack.yaml`` defaults
    (the tracker the torch detector runs); without ultralytics, :class:`IouTracker`.
    """

    BYTETRACK = {"tracker_type": "bytetrack", "track_high_thresh": 0.25,
                 "track_low_thresh": 0.1, "new_track_thresh": 0.25,
                 "track_buffer": 30, "match_thresh": 0.8, "fuse_score": True}

    def __init__(self) -> None:
        self._byte = None
        try:
            from ultralytics.trackers.byte_tracker import BYTETracker
            self._byte = BYTETracker(SimpleNamespace(**self.BYTETRACK))
        except Exception as exc:
            log.info("ByteTrack unavailable (%s); person ids by IoU association", exc)
        self._iou = IouTracker()

    @property
    def kind(self) -> str:
        return "bytetrack" if self._byte is not None else "iou"

    def update(self, boxes: list[tuple], confs: list[float], frame=None,
               at: float = 0.0) -> list[int | None]:
        """Track ids aligned with ``boxes`` (None for a box ByteTrack did not keep)."""
        if not boxes:
            if self._byte is not None:
                empty = np.zeros((0, 4), np.float32)
                self._byte.update(_Detections(empty, np.zeros(0, np.float32),
                                              np.zeros(0, np.float32)), frame)
            else:
                self._iou.update([], at)
            return []
        if self._byte is None:
            return list(self._iou.update(boxes, at))
        dets = _Detections(np.asarray(boxes, np.float32), np.asarray(confs, np.float32),
                           np.zeros(len(boxes), np.float32))
        out = self._byte.update(dets, frame)
        ids: list[int | None] = [None] * len(boxes)
        for row in np.asarray(out):
            # x1, y1, x2, y2, track_id, score, cls, idx
            index = int(row[-1])
            if 0 <= index < len(ids):
                ids[index] = int(row[4])
        return ids

    def reset(self) -> None:
        if self._byte is not None:
            self._byte.reset()
        self._iou.reset()


__all__ = ["IouTracker", "PersonTracker", "RegionGrouper", "containing_track", "iou",
           "same_region"]
