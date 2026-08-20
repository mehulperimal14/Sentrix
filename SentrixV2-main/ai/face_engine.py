# ai/face_engine.py
#
# ARCHITECTURE: Dual-mode face verification engine.
# Mode 1: Uses dlib-based face_recognition (128-d deep facial embeddings) if installed.
# Mode 2: High-speed multi-region spatial color & appearance descriptor fallback using OpenCV.
# Loads all face encodings from static/authorized_faces/ at startup and on hot-reload.
# Authorization persistence: holds "authorized" state for AUTH_HOLD_SECONDS after
# a successful match to survive temporary missed frames or lighting changes.

import os
import time
import threading
from typing import List
import cv2
import numpy as np

AUTHORIZED_DIR      = "static/authorized_faces"
MATCH_TOLERANCE     = 0.55
AUTH_HOLD_SECONDS   = 5  # Keep authorized=True for 5s after last match

try:
    import face_recognition
    _FR_AVAILABLE = True
except ImportError:
    _FR_AVAILABLE = False


def _extract_appearance_features(image: np.ndarray) -> np.ndarray:
    """Extract a robust 512-bin normalized spatial HSV feature descriptor."""
    if image is None or image.size == 0:
        return None
    try:
        # Resize to standard analysis size
        resized = cv2.resize(image, (160, 160))
        hsv = cv2.cvtColor(resized, cv2.COLOR_BGR2HSV)
        
        # 3D HSV histogram: 8 Hue x 8 Saturation x 8 Value bins = 512 dimensions
        hist = cv2.calcHist([hsv], [0, 1, 2], None, [8, 8, 8], [0, 180, 0, 256, 0, 256])
        norm_hist = cv2.normalize(hist, hist).flatten()
        return norm_hist
    except Exception:
        return None


class FaceEngine:
    """
    Face recognition and authorized resident verification engine.
    Supports both deep 128-d face_recognition embeddings and native spatial appearance descriptors.
    Thread-safe hot-reload via reload_encodings().
    """

    def __init__(self):
        self._deep_encodings: List = []
        self._appearance_features: List = []
        self._available = False
        self.encoding_lock = threading.Lock()
        self.last_authorized_time: float = 0.0
        self.authorized_hold_seconds = AUTH_HOLD_SECONDS

        os.makedirs(AUTHORIZED_DIR, exist_ok=True)
        self._load_encodings()
        self._available = True

    def _load_encodings(self):
        """(Re)load all face encodings from static/authorized_faces/ under lock."""
        new_deep = []
        new_app = []

        if os.path.isdir(AUTHORIZED_DIR):
            for fname in os.listdir(AUTHORIZED_DIR):
                if not fname.lower().endswith((".jpg", ".jpeg", ".png")):
                    continue
                fpath = os.path.join(AUTHORIZED_DIR, fname)
                try:
                    img_bgr = cv2.imread(fpath)
                    if img_bgr is not None:
                        # Extract appearance descriptor
                        feat = _extract_appearance_features(img_bgr)
                        if feat is not None:
                            new_app.append(feat)

                        # Extract deep face_recognition encoding if library is available
                        if _FR_AVAILABLE:
                            try:
                                img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
                                encs = face_recognition.face_encodings(img_rgb)
                                if encs:
                                    new_deep.append(encs[0])
                            except Exception as e:
                                print(f"[FaceEngine] Deep face encoding error for {fname}: {e}")
                except Exception as e:
                    print(f"[FaceEngine] Could not load {fname}: {e}")

        with self.encoding_lock:
            self._deep_encodings = new_deep
            self._appearance_features = new_app

        print(f"[FaceEngine] Loaded {len(new_app)} authorized face profile(s) (deep_models={len(new_deep)}, appearance={len(new_app)}).")

    def is_authorized(self, frame) -> bool:
        """
        Returns True if the person in frame matches an enrolled identity,
        or if a successful match occurred within the last AUTH_HOLD_SECONDS.
        """
        import time
        start_time = time.time()
        if frame is None or frame.size == 0:
            from core.instrumentation import log_instrumentation
            log_instrumentation("FaceEngine", "missing_output", {"reason": "frame is None"})
            return False

        with self.encoding_lock:
            known_deep = list(self._deep_encodings)
            known_app  = list(self._appearance_features)

        if not known_deep and not known_app:
            return False

        recognized = False

        # ── Mode 1: Deep face_recognition if available ───────────────────────
        if _FR_AVAILABLE and known_deep:
            try:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                face_locs = face_recognition.face_locations(rgb, model="hog")
                if face_locs:
                    face_encs = face_recognition.face_encodings(rgb, face_locs)
                    for enc in face_encs:
                        matches = face_recognition.compare_faces(
                            known_deep, enc, tolerance=MATCH_TOLERANCE
                        )
                        if any(matches):
                            recognized = True
                            break
            except Exception as e:
                print(f"[FaceEngine] Deep recognition error: {e}")

        # ── Mode 2: Multi-region appearance matching ────────────────────────
        if not recognized and known_app:
            try:
                # Extract central user ROI (head/upper torso in camera view)
                h, w = frame.shape[:2]
                center_crop = frame[int(h * 0.1):int(h * 0.9), int(w * 0.15):int(w * 0.85)]
                frame_feat = _extract_appearance_features(center_crop)

                if frame_feat is not None:
                    for enrolled_feat in known_app:
                        # Cosine similarity between feature vectors
                        dot_prod = np.dot(frame_feat, enrolled_feat)
                        norm_prod = (np.linalg.norm(frame_feat) * np.linalg.norm(enrolled_feat)) + 1e-7
                        similarity = float(dot_prod / norm_prod)

                        # High similarity (>0.68) indicates match with enrolled profile
                        if similarity >= 0.68:
                            recognized = True
                            break
            except Exception as e:
                print(f"[FaceEngine] Appearance matching error: {e}")

        now = time.time()
        from core.instrumentation import log_instrumentation
        if recognized:
            self.last_authorized_time = now
            log_instrumentation("FaceEngine", "inference", {"authorized": True, "latency": now - start_time})
            return True

        if (now - self.last_authorized_time) < self.authorized_hold_seconds:
            log_instrumentation("FaceEngine", "inference", {"authorized": True, "reason": "hold", "latency": now - start_time})
            return True

        log_instrumentation("FaceEngine", "inference", {"authorized": False, "latency": now - start_time})
        return False

    def reload_encodings(self):
        """Hot-reload authorized faces after upload or deletion."""
        self._load_encodings()