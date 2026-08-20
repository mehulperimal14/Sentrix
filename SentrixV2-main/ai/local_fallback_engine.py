# ai/local_fallback_engine.py
#
# ARCHITECTURE: Pure-OpenCV heuristics used when cloud threat engine is
# unavailable or has exceeded MAX_CONSECUTIVE_FAIL.
# Tries, in order:
#   1. Local ultralytics YOLO model (models/weapon.pt)
#   2. Local ONNX model (models/weapon.onnx)
#   3. Simple elongated-contour OpenCV heuristic
# Always returns a float score in [0.0, 1.0]; never raises.

import os
import cv2
import numpy as np


class LocalFallbackEngine:
    """
    OpenCV-based weapon heuristic detector with optional local model support.
    Tries, in order: local ultralytics model (`models/weapon.pt`), ONNX model
    (`models/weapon.onnx`), otherwise falls back to simple OpenCV heuristic.
    """

    def __init__(self):
        self.model = None
        self.model_type = None

        # Check for ultralytics model first
        try:
            from ultralytics import YOLO
            pt_path = os.path.join(
                os.path.dirname(os.path.dirname(__file__)), "models", "weapon.pt"
            )
            if os.path.exists(pt_path):
                try:
                    self.model = YOLO(pt_path)
                    self.model_type = "ultralytics"
                    print("[LocalFallbackEngine] Loaded local weapon model (ultralytics).")
                except Exception as e:
                    print(f"[LocalFallbackEngine] Failed to load ultralytics model: {e}")
        except Exception:
            pass

        # If no ultralytics model, try ONNXRuntime
        if self.model is None:
            try:
                import onnxruntime as ort
                onnx_path = os.path.join(
                    os.path.dirname(os.path.dirname(__file__)), "models", "weapon.onnx"
                )
                if os.path.exists(onnx_path):
                    try:
                        self.model = ort.InferenceSession(onnx_path)
                        self.model_type = "onnx"
                        print("[LocalFallbackEngine] Loaded local weapon model (ONNXRuntime).")
                    except Exception as e:
                        print(f"[LocalFallbackEngine] Failed to load ONNX model: {e}")
            except Exception:
                pass

    def _heuristic_detect(self, frame) -> float:
        """Simple OpenCV heuristic: look for elongated contours in the hand-zone ROI."""
        try:
            gray    = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            blurred = cv2.GaussianBlur(gray, (5, 5), 0)
            edges   = cv2.Canny(blurred, 50, 150)

            h, w = frame.shape[:2]
            roi  = edges[h // 3: 2 * h // 3, w // 4: 3 * w // 4]

            contours, _ = cv2.findContours(
                roi, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )

            for contour in contours:
                area = cv2.contourArea(contour)
                if area < 500:
                    continue
                _, _, cw, ch = cv2.boundingRect(contour)
                aspect = cw / max(ch, 1)
                if aspect > 4.5 or aspect < 0.2:   # very elongated → weapon-like
                    return 0.35

            return 0.0
        except Exception:
            return 0.0

    def detect_weapon_heuristic(self, frame) -> float:
        """
        Primary API: return a weapon score in [0.0, 1.0].
        If a local model is loaded it is used; otherwise falls back to heuristic.
        """
        import time
        start_time = time.time()
        score = 0.0
        method = "heuristic"

        try:
            if self.model_type == "ultralytics" and self.model is not None:
                try:
                    results = self.model(frame)
                    r = results[0]
                    confidences = []
                    if hasattr(r, "boxes"):
                        for b in getattr(r, "boxes"):
                            try:
                                conf = float(b.conf[0]) if hasattr(b, "conf") else float(b.conf)
                                confidences.append(conf)
                            except Exception:
                                pass
                    if confidences:
                        score = float(max(confidences))
                        method = "ultralytics"
                except Exception as e:
                    print(f"[LocalFallbackEngine] ultralytics inference error: {e}")

            elif self.model_type == "onnx" and self.model is not None:
                try:
                    img = cv2.resize(frame, (640, 640))
                    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype("float32") / 255.0
                    img = np.transpose(img, (2, 0, 1))[None, :]
                    ort_inputs = {self.model.get_inputs()[0].name: img}
                    out = self.model.run(None, ort_inputs)
                    confidences = []
                    for o in out:
                        try:
                            arr = np.array(o).ravel()
                            if arr.size:
                                confidences.append(float(arr.max()))
                        except Exception:
                            continue
                    if confidences:
                        score = max(confidences)
                        method = "onnx"
                except Exception as e:
                    print(f"[LocalFallbackEngine] ONNX inference error: {e}")

            if method == "heuristic":
                score = self._heuristic_detect(frame)

        except Exception:
            score = 0.0

        from core.instrumentation import log_instrumentation
        log_instrumentation("LocalFallbackEngine", "inference", {
            "score": score, "method": method, "latency": time.time() - start_time
        })
        return score

    # Legacy alias used by system_engine
    def detect_weapon(self, frame) -> float:
        return self.detect_weapon_heuristic(frame)