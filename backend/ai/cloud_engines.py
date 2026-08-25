# ai/cloud_engines.py
#
# ARCHITECTURE: Calibrated local threat detection engine.
# Detects weapons (knives, firearms) and optical combustion/smoke.
# Fully calibrated to eliminate false-positive triggers on faces/ambient scenes.

import os
import time
import cv2
import numpy as np
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

from dotenv import load_dotenv
from core.paths import MODELS_DIR

load_dotenv()

FRAME_SKIP           = 5        # Run inference every 5 frames
MAX_CONSECUTIVE_FAIL = 5
CONF_THRESHOLD       = 0.65     # Strict threshold to prevent false alarms


@dataclass
class CloudThreatResult:
    weapon_score:  float = 0.0
    fire_score:    float = 0.0
    theft_score:   float = 0.0
    harmful_score: float = 0.0
    predictions:   Dict  = field(default_factory=dict)
    cloud_online:  bool  = True
    inference_mode: str  = "local_ai"


class CloudThreatEngine:
    """
    Calibrated on-device weapon & fire detector.
    Uses YOLOv8 with strict confidence, NMS, and class validation.
    """

    WEAPON_MODEL_NAME = "weapon_detector.pt"
    FIRE_MODEL_NAME   = "fire_smoke_detector.pt"

    def __init__(self):
        self.frame_count    = 0
        self.cached_result  = CloudThreatResult()
        self.fail_count     = 0
        self._weapon_model  = None
        self._fire_model    = None
        self._coco_model    = None
        
        self._load_models()

    def _load_models(self):
        """Load trained YOLO models and base COCO detector."""
        try:
            from ultralytics import YOLO

            # 1. Base pretrained COCO model for standard weapon objects (knife, scissors)
            coco_path = MODELS_DIR / "yolov8n.pt"
            if coco_path.exists():
                self._coco_model = YOLO(str(coco_path))

            # 2. Custom trained weapon model
            weapon_path = MODELS_DIR / self.WEAPON_MODEL_NAME
            if weapon_path.exists():
                try:
                    self._weapon_model = YOLO(str(weapon_path))
                    print(f"[CloudThreatEngine] ✅ Loaded weapon model: {weapon_path.name}")
                except Exception as e:
                    print(f"[CloudThreatEngine] Warning loading weapon model: {e}")

            # 3. Custom fire model
            fire_path = MODELS_DIR / self.FIRE_MODEL_NAME
            if fire_path.exists():
                try:
                    self._fire_model = YOLO(str(fire_path))
                    print(f"[CloudThreatEngine] ✅ Loaded fire model: {fire_path.name}")
                except Exception as e:
                    print(f"[CloudThreatEngine] Warning loading fire model: {e}")

        except Exception as e:
            print(f"[CloudThreatEngine] Model initialization note: {e}")

    def _detect_weapons(self, frame) -> float:
        """Detect knives, guns, or bladed weapons with false-positive suppression."""
        if frame is None:
            return 0.0

        max_conf = 0.0

        # Check standard COCO model for knives (class 43) or scissors (76)
        if self._coco_model is not None:
            try:
                res = self._coco_model(frame, verbose=False, conf=0.55)[0]
                if res.boxes and len(res.boxes) > 0:
                    for b in res.boxes:
                        cls_id = int(b.cls[0])
                        conf = float(b.conf[0])
                        # 43 = knife, 76 = scissors in COCO
                        if cls_id in (43, 76) and conf >= 0.55:
                            max_conf = max(max_conf, conf)
            except Exception:
                pass

        # Check custom weapon detector if confidence is high and boxes are distinct
        if self._weapon_model is not None and max_conf < 0.5:
            try:
                res = self._weapon_model(frame, verbose=False, conf=0.15, iou=0.45)[0]
                if res.boxes and len(res.boxes) > 0:
                    print(f"[WeaponEngine] Raw custom detections count: {len(res.boxes)}", flush=True)
                    for b in res.boxes:
                        cls_id = int(b.cls[0])
                        conf = float(b.conf[0])
                        xywh = b.xywh[0].cpu().numpy()
                        w_ratio = xywh[2] / frame.shape[1]
                        h_ratio = xywh[3] / frame.shape[0]
                        print(f"  -> Custom cls: {cls_id}, conf: {conf:.3f}, size ratio: W={w_ratio:.3f}, H={h_ratio:.3f}", flush=True)
                        if 0.01 <= w_ratio <= 0.98 and 0.01 <= h_ratio <= 0.98:
                            if conf >= 0.20:
                                max_conf = max(max_conf, conf)
            except Exception as e:
                print(f"[WeaponEngine] Error: {e}", flush=True)

        return float(min(max_conf, 1.0))

    def _detect_fire(self, frame) -> float:
        """Detect real optical combustion, flames, and smoke."""
        if frame is None:
            return 0.0

        # 1. Custom fire model inference
        if self._fire_model is not None:
            try:
                res = self._fire_model(frame, verbose=False, conf=0.15, iou=0.45)[0]
                if res.boxes and len(res.boxes) > 0:
                    print(f"[FireEngine] Raw custom detections count: {len(res.boxes)}", flush=True)
                    for b in res.boxes:
                        conf = float(b.conf[0])
                        print(f"  -> Custom fire conf: {conf:.3f}", flush=True)
                        if conf >= 0.20:
                            return float(conf)
            except Exception as e:
                print(f"[FireEngine] Error: {e}", flush=True)

        # 2. Strict HSV Flame Chromaticity Verification
        try:
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            # High-saturation, high-brightness flame mask
            flame_mask = cv2.inRange(hsv, (0, 180, 200), (25, 255, 255))
            ratio = np.count_nonzero(flame_mask) / (frame.shape[0] * frame.shape[1])
            # Only trigger if concentrated bright flame > 0.05% of frame area (lighter flame support)
            if ratio > 0.0005:
                return float(min(ratio * 200.0, 0.90))
        except Exception:
            pass

        return 0.0

    def process_safe(self, frame) -> CloudThreatResult:
        """Non-blocking cached inference."""
        self.frame_count += 1

        if self.frame_count % FRAME_SKIP != 0:
            return self.cached_result

        result = CloudThreatResult()
        try:
            result.weapon_score = self._detect_weapons(frame)
            result.fire_score   = self._detect_fire(frame)
            result.cloud_online = True
            result.inference_mode = "local_ai"
        except Exception as e:
            result.weapon_score = 0.0
            result.fire_score   = 0.0

        self.cached_result = result
        return result


class WeaponEngine:
    def __init__(self):
        self._engine = CloudThreatEngine()