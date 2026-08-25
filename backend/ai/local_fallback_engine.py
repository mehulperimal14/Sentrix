# ai/local_fallback_engine.py
#
# ARCHITECTURE: Local threat detection fallback engine.
# Provides calibrated weapon score verification.

import os
import cv2
import numpy as np
from pathlib import Path
from core.paths import MODELS_DIR


class LocalFallbackEngine:
    """Local threat detection fallback engine."""

    def __init__(self):
        self.model = None

    def detect_weapon_heuristic(self, frame) -> float:
        """Returns 0.0 unless clear elongated bladed contour detected in hand region."""
        if frame is None:
            return 0.0
        try:
            gray    = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            blurred = cv2.GaussianBlur(gray, (5, 5), 0)
            edges   = cv2.Canny(blurred, 80, 200)

            h, w = frame.shape[:2]
            roi  = edges[h // 3: 2 * h // 3, w // 4: 3 * w // 4]

            contours, _ = cv2.findContours(
                roi, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )

            for contour in contours:
                area = cv2.contourArea(contour)
                if area < 1500 or area > (h * w * 0.3):
                    continue
                _, _, cw, ch = cv2.boundingRect(contour)
                aspect = cw / max(ch, 1)
                # Extremely thin and long weapon blade ratio
                if aspect > 6.0 or aspect < 0.15:
                    return 0.25

            return 0.0
        except Exception:
            return 0.0

    def detect_weapon(self, frame) -> float:
        return self.detect_weapon_heuristic(frame)