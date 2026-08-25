# ai/behaviour_engine.py
#
# ARCHITECTURE: Multimodal behaviour & threat classifier.
# Combines trained PyTorch deep vision violence classifier (ResNet18)
# with bounding-box trajectory kinematics (speed, crawling aspect ratio, loitering).
#
# Output: (score, label) representing highest-threat behaviour across all tracks.

import os
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

from core.paths import MODELS_DIR

# Heuristic constants
SPEED_RUN_THRESHOLD    = 45.0   # pixels per frame (raised to prevent jitter from triggering)
SPEED_WALK_THRESHOLD   = 12.0   # pixels per frame
CRAWL_ASPECT_THRESHOLD = 0.2    # h/w < 0.2 -> very wide/low -> crawling
LOITER_TIME_THRESHOLD  = 30.0   # seconds in same zone
LOITER_RADIUS          = 100    # pixels


@dataclass
class BehaviourResult:
    score: float
    label: str   # fighting | running | crawling | loitering | walking | normal


def _dist(a: tuple, b: tuple) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2)


class BehaviourEngine:
    """
    Hybrid behaviour classifier:
    1. Deep PyTorch violence classifier (ResNet18) on person crops
    2. Centroid trajectory velocity & temporal loitering heuristics
    """

    def __init__(self):
        # Per-track history: track_id -> list of (centroid, timestamp)
        self.history: Dict[int, List[Tuple[tuple, float]]] = {}
        self._violence_model = None
        self._violence_tf = None
        self._device = None
        self._load_violence_model()

    def _load_violence_model(self):
        """Attempts to load trained PyTorch violence classifier."""
        model_path = MODELS_DIR / "violence_classifier.pt"
        if not model_path.exists():
            return
        try:
            import torch
            import torch.nn as nn
            from torchvision import models, transforms

            self._device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
            base = models.resnet18(weights=None)
            num_ftrs = base.fc.in_features
            base.fc = nn.Sequential(
                nn.Dropout(0.3),
                nn.Linear(num_ftrs, 2)
            )
            checkpoint = torch.load(str(model_path), map_location=self._device)
            if "model_state_dict" in checkpoint:
                base.load_state_dict(checkpoint["model_state_dict"])
            else:
                base.load_state_dict(checkpoint)

            base.to(self._device)
            base.eval()
            self._violence_model = base
            self._violence_tf = transforms.Compose([
                transforms.ToPILImage(),
                transforms.Resize((224, 224)),
                transforms.ToTensor(),
                transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
            ])
            print(f"[BehaviourEngine] ✅ Loaded deep violence classifier: {model_path.name}")
        except Exception as e:
            print(f"[BehaviourEngine] Violence model load fallback to heuristics: {e}")

    def classify_frame_deep(self, frame) -> Tuple[float, str]:
        """Runs full-frame or person crop through deep violence model."""
        if self._violence_model is None or frame is None or frame.size == 0:
            return 0.0, "normal"
        try:
            import torch
            import cv2
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            inp = self._violence_tf(rgb).unsqueeze(0).to(self._device)
            with torch.no_grad():
                out = self._violence_model(inp)
                prob = torch.softmax(out, dim=1)[0, 1].item()
            if prob > 0.65:
                return prob, "fighting"
            return prob * 0.3, "normal"
        except Exception:
            return 0.0, "normal"

    def classify(self, tracks: list, detections: list, frame=None) -> Tuple[float, str]:
        """
        Analyse active tracks + full frame and return dominant (score, label).
        """
        now = time.time()
        results: List[BehaviourResult] = []

        # 1. Deep Violence Check
        if frame is not None and self._violence_model is not None:
            v_score, v_label = self.classify_frame_deep(frame)
            if v_score > 0.50:
                results.append(BehaviourResult(score=v_score, label=v_label))

        # 2. Trajectory Kinematics
        if tracks:
            for track in tracks:
                try:
                    if hasattr(track, "track_id") and hasattr(track, "to_ltrb"):
                        track_id = track.track_id
                        x1, y1, x2, y2 = [int(v) for v in track.to_ltrb()]
                    elif isinstance(track, dict):
                        track_id = track.get("id", id(track))
                        x1, y1, x2, y2 = track.get("bbox", [0, 0, 50, 100])
                    else:
                        continue

                    cx = (x1 + x2) / 2.0
                    cy = (y1 + y2) / 2.0
                    w  = max(x2 - x1, 1)
                    h  = max(y2 - y1, 1)
                    centroid = (cx, cy)

                    if track_id not in self.history:
                        self.history[track_id] = []

                    self.history[track_id].append((centroid, now))
                    self.history[track_id] = self.history[track_id][-60:]

                    past = self.history[track_id]
                    speed = _dist(centroid, past[-2][0]) if len(past) >= 2 else 0.0
                    aspect = h / w

                    old_entries = [p for p in past if (now - p[1]) >= LOITER_TIME_THRESHOLD]
                    loitering = False
                    if old_entries and _dist(centroid, old_entries[0][0]) < LOITER_RADIUS:
                        loitering = True

                    if speed > SPEED_RUN_THRESHOLD:
                        results.append(BehaviourResult(score=0.65, label="running"))
                    elif aspect < CRAWL_ASPECT_THRESHOLD:
                        results.append(BehaviourResult(score=0.70, label="crawling"))
                    elif loitering:
                        results.append(BehaviourResult(score=0.55, label="loitering"))
                    elif speed > SPEED_WALK_THRESHOLD:
                        results.append(BehaviourResult(score=0.20, label="walking"))
                    else:
                        results.append(BehaviourResult(score=0.10, label="normal"))

                except Exception:
                    results.append(BehaviourResult(score=0.10, label="normal"))

        if not results:
            from core.instrumentation import log_instrumentation
            log_instrumentation("BehaviourEngine", "inference", {"score": 0.10, "label": "normal", "latency": time.time() - now})
            return 0.10, "normal"

        dominant = max(results, key=lambda r: r.score)
        from core.instrumentation import log_instrumentation
        log_instrumentation("BehaviourEngine", "inference", {"score": dominant.score, "label": dominant.label, "latency": time.time() - now})
        return dominant.score, dominant.label

    def _prune_old_tracks(self, active_ids: set):
        """Remove history for inactive tracks."""
        for tid in list(self.history.keys()):
            if tid not in active_ids:
                del self.history[tid]
