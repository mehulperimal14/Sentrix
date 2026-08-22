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

try:
    from ultralytics import YOLO
    _YOLO_AVAILABLE = True
except ImportError:
    _YOLO_AVAILABLE = False

# Force the nano model for speed to prevent video lag
_MODEL_PATH = "yolov8n.pt"
_WEAPON_MODEL_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "models", "weapon_detector.pt")


class VisionEngine:
    """YOLOv8-based person detection with custom weapon model support."""

    def __init__(self):
        self._model = None
        self._weapon_model = None
        self._prev_gray = None
        self._available = False
        try:
            if _YOLO_AVAILABLE:
                self._model = YOLO(_MODEL_PATH)
                self._available = True
                print(f"[VisionEngine] YOLO loaded: {_MODEL_PATH}")
                
                if os.path.exists(_WEAPON_MODEL_PATH):
                    self._weapon_model = YOLO(_WEAPON_MODEL_PATH)
                    print(f"[VisionEngine] Custom weapon model loaded: {_WEAPON_MODEL_PATH}")
                else:
                    print(f"[VisionEngine] No custom weapon model found at {_WEAPON_MODEL_PATH}. Using heuristic fallback.")
            else:
                print("[VisionEngine] ultralytics not installed. Running without YOLO.")
        except Exception as e:
            print(f"[VisionEngine] YOLO load failed: {e}. Returning zero scores.")

    def detect(self, frame) -> Tuple[float, List[dict]]:
        """
        Run person + weapon detection on frame.
        Returns (highest_confidence, list_of_detection_dicts).
        Each detection dict: {label, conf, bbox: [x1,y1,x2,y2], is_weapon: bool}

        COCO weapon-adjacent classes detected:
          49 = knife, 76 = scissors, 38 = baseball bat
        Any detection of these with conf > 0.45 → weapon_score injected into scores.
        """
        import time
        start_time = time.time()
        if self._model is None or frame is None:
            from core.instrumentation import log_instrumentation
            log_instrumentation("VisionEngine", "missing_output", {"reason": "model or frame is None"})
            return 0.0, []
        try:
            # Run on ALL classes (no filter) to catch weapons + persons
            results = self._model(frame, verbose=False)[0]
            detections = []
            highest_conf = 0.0
            self._weapon_score = 0.0   # reset each frame

            # COCO weapon-class IDs
            WEAPON_CLASS_IDS = {49, 76, 38}   # knife, scissors, baseball bat
            WEAPON_CONF_THRESHOLD = 0.45

            for box in results.boxes:
                cls_id = int(box.cls[0])
                conf   = float(box.conf[0])
                x1, y1, x2, y2 = [int(v) for v in box.xyxy[0]]

                # Person detection (class 0)
                if cls_id == 0:
                    if conf < 0.35:
                        continue
                    detections.append({
                        "label": "person",
                        "conf": conf,
                        "bbox": [x1, y1, x2, y2],
                        "is_weapon": False,
                    })
                    if conf > highest_conf:
                        highest_conf = conf

                # Heuristic weapon fallback if custom model is not loaded
                elif self._weapon_model is None:
                    is_weapon = cls_id in WEAPON_CLASS_IDS
                    label_name = self._model.names.get(cls_id, str(cls_id))
                    if is_weapon and conf >= WEAPON_CONF_THRESHOLD:
                        self._weapon_score = max(self._weapon_score, 0.85)
                        detections.append({
                            "label": label_name,
                            "conf": conf,
                            "bbox": [x1, y1, x2, y2],
                            "is_weapon": True,
                        })
                        
            # Custom weapon model execution
            if self._weapon_model is not None:
                w_results = self._weapon_model(frame, verbose=False)[0]
                for box in w_results.boxes:
                    cls_id = int(box.cls[0])
                    conf   = float(box.conf[0])
                    x1, y1, x2, y2 = [int(v) for v in box.xyxy[0]]
                    
                    if conf >= 0.50:  # Weapon threshold
                        label_name = self._weapon_model.names.get(cls_id, "weapon")
                        # Map custom model's confidence to weapon score (scale up slightly)
                        self._weapon_score = max(self._weapon_score, min(1.0, conf * 1.2))
                        detections.append({
                            "label": label_name,
                            "conf": conf,
                            "bbox": [x1, y1, x2, y2],
                            "is_weapon": True,
                        })

            latency = time.time() - start_time
            from core.instrumentation import log_instrumentation
            log_instrumentation("VisionEngine", "inference", {
                "confidence": highest_conf,
                "latency": latency,
                "num_detections": len(detections),
                "weapon_score": self._weapon_score,
            })
            return highest_conf, detections
        except Exception as e:
            print(f"[VisionEngine] detect error: {e}")
            return 0.0, []

    def get_weapon_score(self) -> float:
        """Return the weapon score detected in the last detect() call."""
        return getattr(self, "_weapon_score", 0.0)


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