# ai/cloud_engines.py
#
# ARCHITECTURE: Queries Roboflow Inference SDK for weapon/fire/theft/harmful
# object detection. Runs only every FRAME_SKIP frames to avoid blocking the
# pipeline. Caches the last result between frames. Returns zeros on any failure.
# process_safe() is always synchronous and never raises. Falls back to demo mode
# (zero scores, cloud_online=False) when ROBOFLOW_API_KEY is not set.

import os
import cv2
from dataclasses import dataclass, field
from typing import Dict

from dotenv import load_dotenv

load_dotenv()

FRAME_SKIP           = 10
MAX_CONSECUTIVE_FAIL = 5
CONF_THRESHOLD       = 0.50

WEAPON_MODEL_ID  = "weapon-detection-m7qso/1"
FIRE_MODEL_ID    = "fire-smoke-detection-lk8z9/1"
THEFT_MODEL_ID   = "theft-detection-xfw3e/1"
HARMFUL_MODEL_ID = "harmful-object-detection/1"


@dataclass
class CloudThreatResult:
    weapon_score:  float = 0.0
    fire_score:    float = 0.0
    theft_score:   float = 0.0
    harmful_score: float = 0.0
    predictions:   Dict  = field(default_factory=dict)
    cloud_online:  bool  = False


class CloudThreatEngine:
    """
    Roboflow-backed cloud threat detector.
    Falls back silently to zero scores when API key is missing or calls fail.
    """

    def __init__(self):
        self.api_key       = os.getenv("ROBOFLOW_API_KEY", "").strip()
        self.demo_mode     = os.getenv("DEMO_MODE", "False").lower() == "true"
        self.frame_count   = 0
        self.cached_result = CloudThreatResult()
        self.fail_count    = 0
        self._requests     = None

        if self.demo_mode:
            print("[CloudThreatEngine] DEMO_MODE enabled. Cloud inference bypassed.")
        elif self.api_key:
            self._init_client()
        else:
            print("[CloudThreatEngine] No ROBOFLOW_API_KEY — running in demo mode (zero scores).")

    def _init_client(self):
        import requests
        self._requests = requests
        print("[CloudThreatEngine] Using requests with strict 1.5s timeout.")

    def _infer_model(self, jpg_bytes: bytes, model_id: str) -> float:
        """Run inference for a single model. Returns max confidence or 0.0."""
        if not self.api_key:
            return 0.0
        try:
            # Split model_id (e.g. "weapon-detection-m7qso/1")
            parts = model_id.split("/")
            dataset = parts[0]
            version = parts[1] if len(parts) > 1 else "1"
            
            url = f"https://detect.roboflow.com/{dataset}/{version}?api_key={self.api_key}"
            response = self._requests.post(
                url, 
                data=jpg_bytes, 
                headers={"Content-Type": "application/x-www-form-urlencoded"}, 
                timeout=1.5
            )
            response.raise_for_status()
            preds = response.json().get("predictions", [])
            scores = [p["confidence"] for p in preds if p.get("confidence", 0) >= CONF_THRESHOLD]
            return max(scores) if scores else 0.0
        except self._requests.exceptions.Timeout:
            if self.fail_count == 0:
                print(f"[WARN] Cloud Timeout for {model_id}. Suppressing further warnings.")
            return 0.0
        except Exception as e:
            if self.fail_count == 0:
                print(f"[CloudThreatEngine] Inference error ({model_id}): {e}")
            return 0.0

        import time
        start_time = time.time()
        if os.getenv("SENTRIX_EVAL_MODE") == "1":
            from core.instrumentation import log_instrumentation
            log_instrumentation("CloudThreatEngine", "inference", {"weapon_score": 0.0, "fire_score": 0.0, "latency": time.time() - start_time, "reason": "eval_mock"})
            return CloudThreatResult(cloud_online=False)

        if self.demo_mode or not hasattr(self, '_requests') or not self.api_key:
            from core.instrumentation import log_instrumentation
            log_instrumentation("CloudThreatEngine", "inference", {"weapon_score": 0.0, "fire_score": 0.0, "latency": time.time() - start_time, "reason": "demo_or_no_api"})
            return CloudThreatResult(cloud_online=False)

        try:
            _, buffer  = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            jpg_bytes  = buffer.tobytes()
            result     = CloudThreatResult(cloud_online=True)

            result.weapon_score  = self._infer_model(jpg_bytes, WEAPON_MODEL_ID)
            result.fire_score    = self._infer_model(jpg_bytes, FIRE_MODEL_ID)
            result.theft_score   = self._infer_model(jpg_bytes, THEFT_MODEL_ID)
            result.harmful_score = self._infer_model(jpg_bytes, HARMFUL_MODEL_ID)
            self.fail_count      = 0
            from core.instrumentation import log_instrumentation
            log_instrumentation("CloudThreatEngine", "inference", {"weapon_score": result.weapon_score, "fire_score": result.fire_score, "latency": time.time() - start_time})
            return result

        except Exception as e:
            print(f"[CloudThreatEngine] Inference failed: {e}")
            self.fail_count += 1
            from core.instrumentation import log_instrumentation
            log_instrumentation("CloudThreatEngine", "exception", {"error": str(e), "latency": time.time() - start_time})
            return CloudThreatResult(cloud_online=False)

    def process_safe(self, frame) -> CloudThreatResult:
        """Non-blocking cached cloud inference. Always returns a result."""
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
    """Legacy wrapper — returns zero scores without API key."""

    def __init__(self):
        self._engine = CloudThreatEngine()

    def detect(self, frame) -> float:
        result = self._engine.process_safe(frame)
        return result.weapon_score