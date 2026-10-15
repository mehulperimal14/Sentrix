"""The person/weapon/fire detector on any runtime, as a drop-in for ``Detector``.

``sentrix.perception.detector.Detector`` runs the three YOLO heads in
PyTorch through ultralytics (``model.track`` for people). On a Pi that is the
slowest way to run them (docs/8_RASPBERRY_PI5_INTEGRATION.md §2), so the clip engine builds this
class instead when ``SENTRIX_RUNTIME`` is ``onnx`` or ``ncnn``: the same
weights (resolved in the same order), the same confidence floors and box
plausibility rules, the same :class:`~sentrix.perception.detector.DetectionResult`,
with person track ids from :class:`~sentrix.runtime.tracking.PersonTracker`.
"""

from __future__ import annotations

import numpy as np

from sentrix.config import settings
from sentrix.perception.detector import (_FIRE_WEIGHTS, _PERSON_WEIGHTS, _WEAPON_WEIGHTS,
                                         Detection, DetectionResult, _resolve)
from sentrix.runtime import build_head, resolve_backend
from sentrix.runtime.tracking import PersonTracker
from sentrix.utils.logging import get_logger, telemetry, timed

log = get_logger("runtime")

#: COCO class id of "person" in the person head.
PERSON_CLASS = 0


def plausible(bbox, shape, cfg=None) -> bool:
    """The detector's box sanity rule: neither filling the frame nor a speck."""
    cfg = cfg or settings.detection
    h, w = shape[:2]
    area = max(bbox[2] - bbox[0], 0) * max(bbox[3] - bbox[1], 0)
    frac = area / float(max(h * w, 1))
    return cfg.min_box_area_fraction <= frac <= cfg.max_box_area_fraction


class RuntimeDetector:
    """``Detector.detect`` on exported heads, tracking people with ByteTrack."""

    def __init__(self, person=None, weapon=None, fire=None, cfg=None,
                 track: bool = True) -> None:
        self.cfg = cfg or settings.detection
        self._person, self._weapon, self._fire = person, weapon, fire
        self._tracker = PersonTracker() if track else None

    @classmethod
    def build(cls, imgsz: int | None = None, backend: str | None = None,
              threads: int | None = None, track: bool = True) -> "RuntimeDetector":
        size = int(imgsz or settings.detection.imgsz)
        backend = resolve_backend(backend)
        heads = {role: build_head(_resolve(weights), size, backend, threads)
                 for role, weights in (("person", _PERSON_WEIGHTS),
                                       ("weapon", _WEAPON_WEIGHTS),
                                       ("fire", _FIRE_WEIGHTS))}
        return cls(heads["person"], heads["weapon"], heads["fire"], track=track)

    @property
    def available(self) -> bool:
        return self._person is not None

    @property
    def backends(self) -> dict[str, str | None]:
        return {role: getattr(head, "backend", None) for role, head in
                (("person", self._person), ("weapon", self._weapon), ("fire", self._fire))}

    def _boxes(self, head, frame, conf, role, classes=None) -> list[Detection]:
        out: list[Detection] = []
        with timed(f"detector.{role}"):
            boxes = head.predict(frame, conf=conf, iou=self.cfg.iou, classes=classes,
                                 max_det=self.cfg.max_detections)
        for box in boxes:
            if box.conf < conf or not plausible(box.bbox, frame.shape, self.cfg):
                continue
            out.append(Detection(label="person" if role == "person" else box.label,
                                 confidence=box.conf, bbox=box.bbox))
        return out

    def detect(self, frame: np.ndarray) -> DetectionResult:
        """Every available head on one frame; never raises."""
        result = DetectionResult()
        if frame is None or getattr(frame, "size", 0) == 0:
            return result
        if self._person is not None:
            try:
                persons = self._boxes(self._person, frame, self.cfg.person_conf, "person",
                                      classes=[PERSON_CLASS])
                if self._tracker is not None:
                    ids = self._tracker.update([p.bbox for p in persons],
                                               [p.confidence for p in persons], frame)
                    for person, track_id in zip(persons, ids):
                        person.track_id = track_id
                result.persons = persons
            except Exception as exc:
                log.error("person detection failed: %s", exc)
                result.person_ok = False
        else:
            result.person_ok = False
        for role, head, conf in (("weapon", self._weapon, self.cfg.weapon_conf),
                                 ("fire", self._fire, self.cfg.fire_conf)):
            if head is None:
                continue
            try:
                boxes = self._boxes(head, frame, conf, role)
            except Exception as exc:
                log.error("%s detection failed: %s", role, exc)
                continue
            if role == "weapon":
                result.weapons = boxes
            else:
                result.fires = boxes
        telemetry.record("detector", "frame", {"persons": len(result.persons),
                                               "weapons": len(result.weapons),
                                               "fires": len(result.fires)})
        return result

    def reset_tracks(self) -> None:
        if self._tracker is not None:
            self._tracker.reset()


__all__ = ["PERSON_CLASS", "RuntimeDetector", "plausible"]
