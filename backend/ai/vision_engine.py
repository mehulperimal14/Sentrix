# ai/vision_engine.py
#
# ARCHITECTURE: Wraps YOLOv8 for person/object detection.
# detect() returns (score, detections) where score is the highest confidence
# among detected persons. Detections are dicts with bbox and label.
# motion_score() uses frame differencing to estimate motion intensity.
# Falls back gracefully if YOLO model fails to load (returns zeros).

import os
import cv2
import numpy as np
from typing import Tuple, List
from pathlib import Path
from core.paths import MODELS_DIR

try:
    from ultralytics import YOLO
    _YOLO_AVAILABLE = True
except ImportError:
    _YOLO_AVAILABLE = False

# Use model from backend/models/ directory
_MODEL_PATH = str(MODELS_DIR / "yolov8n.pt")


class VisionEngine:
    """YOLOv8-based person detection with motion scoring."""

    def __init__(self):
        self._model = None
        self._prev_gray = None
        self._available = False
        try:
            if _YOLO_AVAILABLE:
                self._model = YOLO(_MODEL_PATH)
                self._available = True
                print(f"[VisionEngine] YOLO loaded: {_MODEL_PATH}")
            else:
                print("[VisionEngine] ultralytics not installed. Running without YOLO.")
        except Exception as e:
            print(f"[VisionEngine] YOLO load failed: {e}. Returning zero scores.")

    def detect(self, frame) -> Tuple[float, List[dict]]:
        """
        Run person detection on frame.
        Returns (highest_confidence, list_of_detection_dicts).
        Each detection dict: {label, conf, bbox: [x1,y1,x2,y2]}
        """
        import time
        start_time = time.time()
        if self._model is None or frame is None:
            from core.instrumentation import log_instrumentation
            log_instrumentation("VisionEngine", "missing_output", {"reason": "model or frame is None"})
            return 0.0, []
        try:
            results = self._model(frame, verbose=False, classes=[0])[0]  # class 0 = person
            detections = []
            highest_conf = 0.0
            for box in results.boxes:
                conf = float(box.conf[0])
                if conf < 0.35:
                    continue
                x1, y1, x2, y2 = [int(v) for v in box.xyxy[0]]
                detections.append({
                    "label": "person",
                    "conf": conf,
                    "bbox": [x1, y1, x2, y2],
                })
                if conf > highest_conf:
                    highest_conf = conf
            latency = time.time() - start_time
            from core.instrumentation import log_instrumentation
            log_instrumentation("VisionEngine", "inference", {"confidence": highest_conf, "latency": latency, "num_detections": len(detections)})
            return highest_conf, detections
        except Exception as e:
            print(f"[VisionEngine] detect error: {e}")
            return 0.0, []

    def motion_score(self, frame) -> float:
        """Frame differencing motion score between 0.0 and 1.0."""
        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (21, 21), 0)
            if self._prev_gray is None:
                self._prev_gray = gray
                return 0.0
            diff  = cv2.absdiff(self._prev_gray, gray)
            score = float(np.mean(diff)) / 255.0
            self._prev_gray = gray
            return min(score * 10.0, 1.0)  # amplify and clamp
        except Exception:
            return 0.0