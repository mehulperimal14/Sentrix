# ai/cloud_engines.py
#
# ARCHITECTURE: Local YOLO models for weapon/fire detection (Phase 3).
# When trained models exist at models/weapon_detector.pt and
# models/fire_smoke_detector.pt, they are used directly without any
# Roboflow API calls. Falls back to the Roboflow REST API only if
# local models are absent AND an API key is configured.
#
# process_safe() is always synchronous and never raises.
# Returns zeros on any failure.

import os
import cv2
from dataclasses import dataclass, field
from typing import Dict, Optional

from dotenv import load_dotenv

load_dotenv()

FRAME_SKIP           = 10
MAX_CONSECUTIVE_FAIL = 5
CONF_THRESHOLD       = 0.50

# Roboflow model IDs (only used when no local model is present)
WEAPON_MODEL_ID  = "weapon-detection-m7qso/1"
FIRE_MODEL_ID    = "fire-smoke-detection-lk8z9/1"
THEFT_MODEL_ID   = "theft-detection-xfw3e/1"
HARMFUL_MODEL_ID = "harmful-object-detection/1"

# Local model paths (relative to package root)
_MODELS_DIR   = os.path.join(os.path.dirname(os.path.dirname(__file__)), "models")
WEAPON_PT     = os.path.join(_MODELS_DIR, "weapon_detector.pt")
FIRE_SMOKE_PT = os.path.join(_MODELS_DIR, "fire_smoke_detector.pt")


@dataclass
class CloudThreatResult:
    weapon_score:  float = 0.0
    fire_score:    float = 0.0
    theft_score:   float = 0.0
    harmful_score: float = 0.0
    predictions:   Dict  = field(default_factory=dict)
    cloud_online:  bool  = False


def _load_yolo(path: str):
    """Load a local YOLO model. Returns None if unavailable."""
    try:
        from ultralytics import YOLO
        if os.path.exists(path):
            model = YOLO(path)
            print(f"[CloudThreatEngine] Loaded local model: {os.path.basename(path)}")
            return model
    except Exception as e:
        print(f"[CloudThreatEngine] Could not load {path}: {e}")
    return None


def _yolo_max_conf(model, frame, conf_thresh: float = CONF_THRESHOLD) -> float:
    """Run YOLO inference and return max confidence above threshold."""
    try:
        results = model(frame, verbose=False)[0]
        scores = [
            float(b.conf[0])
            for b in results.boxes
            if float(b.conf[0]) >= conf_thresh
        ]
        return max(scores) if scores else 0.0
    except Exception:
        return 0.0


class CloudThreatEngine:
    """
    Threat detector: prefers local YOLO models, falls back to Roboflow API.
    Falls back silently to zero scores when neither is available.
    """

    def __init__(self):
        self.api_key       = os.getenv("ROBOFLOW_API_KEY", "").strip()
        self.demo_mode     = os.getenv("DEMO_MODE", "False").lower() == "true"
        self.frame_count   = 0
        self.cached_result = CloudThreatResult()
        self.fail_count    = 0
        self._requests     = None

        # --- Load local models (Phase 3 trained weights) ---
        self._weapon_model    = _load_yolo(WEAPON_PT)
        self._fire_smoke_model = _load_yolo(FIRE_SMOKE_PT)

        if self._weapon_model and self._fire_smoke_model:
            print("[CloudThreatEngine] Running fully local (no cloud calls needed).")
        elif self.demo_mode:
            print("[CloudThreatEngine] DEMO_MODE enabled. Cloud inference bypassed.")
        elif self.api_key:
            self._init_client()
        else:
            print("[CloudThreatEngine] No local models + no ROBOFLOW_API_KEY — zero scores.")

    def _init_client(self):
        import requests
        self._requests = requests
        print("[CloudThreatEngine] Using Roboflow REST API with 1.5s timeout.")

    def _infer_model_api(self, jpg_bytes: bytes, model_id: str) -> float:
        """Run Roboflow API inference. Returns max confidence or 0.0."""
        if not self.api_key or self._requests is None:
            return 0.0
        try:
            parts   = model_id.split("/")
            dataset = parts[0]
            version = parts[1] if len(parts) > 1 else "1"
            url = f"https://detect.roboflow.com/{dataset}/{version}?api_key={self.api_key}"
            response = self._requests.post(
                url,
                data=jpg_bytes,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=1.5,
            )
            response.raise_for_status()
            preds  = response.json().get("predictions", [])
            scores = [p["confidence"] for p in preds if p.get("confidence", 0) >= CONF_THRESHOLD]
            return max(scores) if scores else 0.0
        except Exception as e:
            if self.fail_count == 0:
                print(f"[CloudThreatEngine] API error ({model_id}): {e}")
            return 0.0

    def _run_inference(self, frame) -> CloudThreatResult:
        import time
        start_time = time.time()

        if os.getenv("SENTRIX_EVAL_MODE") == "1":
            from core.instrumentation import log_instrumentation
            log_instrumentation("CloudThreatEngine", "inference", {
                "weapon_score": 0.0, "fire_score": 0.0,
                "latency": time.time() - start_time, "reason": "eval_mock"
            })
            return CloudThreatResult(cloud_online=False)

        result = CloudThreatResult()

        # --- Path A: Local YOLO models (fast, no network) ---
        if self._weapon_model is not None:
            result.weapon_score = _yolo_max_conf(self._weapon_model, frame)
            result.cloud_online = True

        if self._fire_smoke_model is not None:
            result.fire_score   = _yolo_max_conf(self._fire_smoke_model, frame)
            result.cloud_online = True

        # --- Path B: Roboflow API (fallback when no local models) ---
        if self._weapon_model is None or self._fire_smoke_model is None:
            if self.demo_mode or not self.api_key:
                from core.instrumentation import log_instrumentation
                log_instrumentation("CloudThreatEngine", "inference", {
                    "weapon_score": 0.0, "fire_score": 0.0,
                    "latency": time.time() - start_time, "reason": "demo_or_no_api"
                })
                return result  # partial result (local scores already filled)

            try:
                _, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                jpg_bytes  = buffer.tobytes()

                if self._weapon_model is None:
                    result.weapon_score  = self._infer_model_api(jpg_bytes, WEAPON_MODEL_ID)
                if self._fire_smoke_model is None:
                    result.fire_score    = self._infer_model_api(jpg_bytes, FIRE_MODEL_ID)
                result.theft_score   = self._infer_model_api(jpg_bytes, THEFT_MODEL_ID)
                result.harmful_score = self._infer_model_api(jpg_bytes, HARMFUL_MODEL_ID)
                result.cloud_online  = True
                self.fail_count      = 0
            except Exception as e:
                print(f"[CloudThreatEngine] Inference failed: {e}")
                self.fail_count += 1

        from core.instrumentation import log_instrumentation
        log_instrumentation("CloudThreatEngine", "inference", {
            "weapon_score": result.weapon_score,
            "fire_score":   result.fire_score,
            "latency":      time.time() - start_time,
            "local_weapon": self._weapon_model is not None,
            "local_fire":   self._fire_smoke_model is not None,
        })
        return result

    def process_safe(self, frame) -> CloudThreatResult:
        """Non-blocking cached cloud/local inference. Always returns a result."""
        self.frame_count += 1

        if self.frame_count % FRAME_SKIP != 0:
            return self.cached_result

        if self.fail_count >= MAX_CONSECUTIVE_FAIL:
            return CloudThreatResult(cloud_online=False)

        result = self._run_inference(frame)
        self.cached_result = result
        return result


# Legacy WeaponEngine alias (keeps backward compat with old system_engine.py)
class WeaponEngine:
    """Legacy wrapper — prefers local model, returns zero if unavailable."""

    def __init__(self):
        self._engine = CloudThreatEngine()

    def detect(self, frame) -> float:
        result = self._engine.process_safe(frame)
        return result.weapon_score