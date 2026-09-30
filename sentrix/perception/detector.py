"""Unified YOLOv8 detector: people, weapons, fire and smoke in one component.

Why this module exists
----------------------
The previous build spread YOLO across two engines and loaded four models to do
three jobs: ``VisionEngine`` loaded ``yolov8n.pt`` for people, and
``CloudThreatEngine`` loaded ``yolov8n.pt`` *again* as a COCO fallback plus the
two custom detectors. Two copies of identical weights sat in memory, and the
person pass and the weapon pass ran on separate schedules, so a weapon could be
attributed to a frame in which the person had already moved.

YOLOv8 covers most of what this project needs from vision:

===========================  ==========================================
Task                         How
===========================  ==========================================
Person localisation          ``yolov8n.pt``, class 0
Weapon localisation          fine-tuned 3-class head (knife/long_gun/pistol)
Fire & smoke localisation    fine-tuned 2-class head
Multi-object tracking        ``model.track()`` with ByteTrack, built in
Pose / keypoints             ``yolov8n-pose.pt`` — see ``PoseEstimator``
===========================  ==========================================

The tracking column is the one worth dwelling on. Ultralytics ships ByteTrack
and BoT-SORT, and ``track(persist=True)`` maintains identities across calls.
That removes the ``deep-sort-realtime`` dependency entirely — which mattered,
because it was never installed, so the previous tracker silently degraded to
assigning ``id = index + 1`` per frame. Track ids were therefore reused by
different people from one frame to the next, which quietly corrupted every
downstream kinematic feature: loitering and running were computed over
trajectories that mixed several people together.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from sentrix.config import settings
from sentrix.paths import model_path
from sentrix.utils.logging import get_logger, telemetry, timed

log = get_logger("detector")

try:
    from ultralytics import YOLO
    _YOLO_AVAILABLE = True
except ImportError:                                    # pragma: no cover
    YOLO = None                                        # type: ignore[assignment]
    _YOLO_AVAILABLE = False


@dataclass
class Detection:
    """One localised object."""

    label: str
    confidence: float
    bbox: tuple[int, int, int, int]         # x1, y1, x2, y2
    #: Per-camera tracker id from ByteTrack. Stable within one source.
    track_id: int | None = None
    #: Cross-camera identity from ``ReIDEngine``. Declared here rather than
    #: attached ad hoc at runtime, so it survives ``to_dict()`` and reaches the
    #: dashboard and the evidence sidecar.
    global_id: int | None = None

    @property
    def centroid(self) -> tuple[float, float]:
        x1, y1, x2, y2 = self.bbox
        return (x1 + x2) / 2.0, (y1 + y2) / 2.0

    @property
    def width(self) -> int:
        return max(self.bbox[2] - self.bbox[0], 1)

    @property
    def height(self) -> int:
        return max(self.bbox[3] - self.bbox[1], 1)

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "conf": round(self.confidence, 4),
                "bbox": list(self.bbox), "track_id": self.track_id,
                "global_id": self.global_id}


@dataclass
class DetectionResult:
    """Everything vision knows about a single frame."""

    persons: list[Detection] = field(default_factory=list)
    weapons: list[Detection] = field(default_factory=list)
    fires: list[Detection] = field(default_factory=list)
    #: False when the person pass did not run (no model, or it failed), so
    #: "no persons" means "unknown", not "nobody there".
    person_ok: bool = True

    @property
    def person_score(self) -> float:
        return max((d.confidence for d in self.persons), default=0.0)

    @property
    def weapon_score(self) -> float:
        return max((d.confidence for d in self.weapons), default=0.0)

    @property
    def fire_score(self) -> float:
        return max((d.confidence for d in self.fires), default=0.0)

    @property
    def all(self) -> list[Detection]:
        return self.persons + self.weapons + self.fires

    def to_dict(self) -> dict[str, Any]:
        return {
            "persons": [d.to_dict() for d in self.persons],
            "weapons": [d.to_dict() for d in self.weapons],
            "fires": [d.to_dict() for d in self.fires],
            # Flat view used by the evidence vault, which records labels only.
            "all": [d.to_dict() for d in self.all],
        }


# Weight files, in preference order. First one present wins, so a V3 promotion
# is a file copy rather than a code change.
_PERSON_WEIGHTS = ("yolov8n.pt",)
_WEAPON_WEIGHTS = ("v4_custom/weapon_detector_custom_v4.pt",
                   "v4_finetuned/weapon_detector_ft_v4.pt",
                   "v3_real/weapon_detector_real_v3.pt", "weapon_detector.pt")
_FIRE_WEIGHTS = ("v4_custom/fire_detector_custom_v4.pt",
                 "v4_finetuned/fire_smoke_detector_ft_v4.pt",
                 "v3_real/fire_smoke_detector_real_v3.pt", "fire_smoke_detector.pt")


def _resolve(candidates: tuple[str, ...]):
    for name in candidates:
        path = model_path(*name.split("/"))
        if path.exists():
            return path
    return None


class Detector:
    """Loads each distinct weight exactly once and runs all vision detection."""

    def __init__(self) -> None:
        self.cfg = settings.detection
        self._person = self._load(_PERSON_WEIGHTS, "person")
        self._weapon = self._load(_WEAPON_WEIGHTS, "weapon")
        self._fire = self._load(_FIRE_WEIGHTS, "fire/smoke")
        self._weapon_names: dict[int, str] = self._class_names(self._weapon)
        self._fire_names: dict[int, str] = self._class_names(self._fire)

    # ── loading ───────────────────────────────────────────────────────────────
    @staticmethod
    def _class_names(model) -> dict[int, str]:
        if model is None:
            return {}
        names = getattr(model, "names", None) or {}
        if isinstance(names, dict):
            return {int(k): str(v) for k, v in names.items()}
        return {i: str(n) for i, n in enumerate(names)}

    def _load(self, candidates: tuple[str, ...], role: str):
        if not _YOLO_AVAILABLE:
            log.warning("ultralytics not installed — %s detection disabled", role)
            return None
        path = _resolve(candidates)
        if path is None:
            log.warning("no weights found for %s (looked for %s)", role,
                        ", ".join(candidates))
            return None
        try:
            model = YOLO(str(path))
            log.info("%s detector loaded: %s", role, path.name)
            return model
        except Exception as exc:
            log.error("failed to load %s detector from %s: %s", role, path, exc)
            return None

    @property
    def available(self) -> bool:
        return self._person is not None

    # ── geometry sanity ───────────────────────────────────────────────────────
    def _plausible(self, bbox: tuple[int, int, int, int], shape) -> bool:
        """Reject boxes that are implausibly large or small for the frame."""
        h, w = shape[:2]
        area = max(bbox[2] - bbox[0], 0) * max(bbox[3] - bbox[1], 0)
        frac = area / float(max(h * w, 1))
        return self.cfg.min_box_area_fraction <= frac <= self.cfg.max_box_area_fraction

    def _collect(self, result, names, conf_floor, shape,
                 fixed_label: str | None = None) -> list[Detection]:
        out: list[Detection] = []
        boxes = getattr(result, "boxes", None)
        if boxes is None:
            return out
        ids = getattr(boxes, "id", None)
        for i, box in enumerate(boxes):
            conf = float(box.conf[0])
            if conf < conf_floor:
                continue
            xyxy = [int(v) for v in box.xyxy[0]]
            bbox = (xyxy[0], xyxy[1], xyxy[2], xyxy[3])
            if not self._plausible(bbox, shape):
                continue
            cls_id = int(box.cls[0])
            label = fixed_label or names.get(cls_id, f"class_{cls_id}")
            track_id = None
            if ids is not None:
                try:
                    track_id = int(ids[i].item())
                except (IndexError, RuntimeError, ValueError):
                    track_id = None
            out.append(Detection(label=label, confidence=conf, bbox=bbox,
                                 track_id=track_id))
        return out

    # ── inference ─────────────────────────────────────────────────────────────
    def detect(self, frame: np.ndarray) -> DetectionResult:
        """Run every available head on one frame.

        Never raises: a failing head yields no detections for that modality so a
        single bad model cannot take the pipeline down.
        """
        result = DetectionResult()
        if frame is None or getattr(frame, "size", 0) == 0:
            return result

        shape = frame.shape

        if self._person is not None:
            try:
                with timed("detector.person"):
                    # persist=True keeps ByteTrack state between calls, which is
                    # what makes track ids stable across frames.
                    raw = self._person.track(
                        frame, persist=True, verbose=False, classes=[0],
                        conf=self.cfg.person_conf, iou=self.cfg.iou,
                        imgsz=self.cfg.imgsz, tracker=self.cfg.tracker,
                    )[0]
                result.persons = self._collect(
                    raw, {0: "person"}, self.cfg.person_conf, shape,
                    fixed_label="person")
            except Exception as exc:
                log.debug("person pass failed, falling back to predict: %s", exc)
                try:
                    raw = self._person(frame, verbose=False, classes=[0],
                                       conf=self.cfg.person_conf,
                                       imgsz=self.cfg.imgsz)[0]
                    result.persons = self._collect(
                        raw, {0: "person"}, self.cfg.person_conf, shape,
                        fixed_label="person")
                except Exception as exc2:
                    log.error("person detection failed: %s", exc2)
                    result.person_ok = False
        else:
            result.person_ok = False

        if self._weapon is not None:
            try:
                with timed("detector.weapon"):
                    raw = self._weapon(frame, verbose=False,
                                       conf=self.cfg.weapon_conf,
                                       iou=self.cfg.iou, imgsz=self.cfg.imgsz)[0]
                result.weapons = self._collect(raw, self._weapon_names,
                                               self.cfg.weapon_conf, shape)
            except Exception as exc:
                log.error("weapon detection failed: %s", exc)

        if self._fire is not None:
            try:
                with timed("detector.fire"):
                    raw = self._fire(frame, verbose=False,
                                     conf=self.cfg.fire_conf,
                                     iou=self.cfg.iou, imgsz=self.cfg.imgsz)[0]
                result.fires = self._collect(raw, self._fire_names,
                                             self.cfg.fire_conf, shape)
            except Exception as exc:
                log.error("fire detection failed: %s", exc)

        telemetry.record("detector", "frame", {
            "persons": len(result.persons),
            "weapons": len(result.weapons),
            "fires": len(result.fires),
        })
        return result

    def reset_tracks(self) -> None:
        """Drop tracker state, e.g. when switching video source."""
        if self._person is not None and hasattr(self._person, "predictor"):
            try:
                self._person.predictor.trackers[0].reset()
            except Exception:
                pass


class PoseEstimator:
    """Optional ``yolov8n-pose`` wrapper — 17 COCO keypoints per person.

    Not wired into the pipeline yet, and deliberately so: it is the recommended
    next upgrade for behaviour classification but needs evaluation first.

    Keypoints beat bounding-box kinematics for aggression detection because the
    signal lives in limb velocity and body configuration, not in how fast a box
    centroid drifts. A raised arm, a lunge and a fall are all obvious in
    keypoint space and nearly invisible in centroid space. It also fixes the
    crawl heuristic, which currently infers "crawling" from box aspect ratio and
    so fires on anyone the detector clips at the frame edge.

    Weights are not shipped; ultralytics downloads ``yolov8n-pose.pt`` on first
    use, which is why this is opt-in rather than default.
    """

    def __init__(self, weights: str = "yolov8n-pose.pt") -> None:
        self._model = None
        if not _YOLO_AVAILABLE:
            return
        local = model_path(weights)
        try:
            self._model = YOLO(str(local) if local.exists() else weights)
            log.info("pose estimator loaded: %s", weights)
        except Exception as exc:
            log.warning("pose estimator unavailable: %s", exc)

    @property
    def available(self) -> bool:
        return self._model is not None

    def keypoints(self, frame: np.ndarray) -> list[np.ndarray]:
        """Return one ``(17, 3)`` array of ``(x, y, confidence)`` per person."""
        if self._model is None or frame is None:
            return []
        try:
            raw = self._model(frame, verbose=False,
                              conf=settings.detection.person_conf)[0]
            kps = getattr(raw, "keypoints", None)
            if kps is None or kps.data is None:
                return []
            return [k.cpu().numpy() for k in kps.data]
        except Exception as exc:
            log.debug("pose inference failed: %s", exc)
            return []


__all__ = ["Detection", "DetectionResult", "Detector", "PoseEstimator"]
