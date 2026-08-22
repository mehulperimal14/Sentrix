# ai/anomaly_engine.py
#
# ARCHITECTURE: MobileNetV2 anomaly classifier wrapper (Model F).
# Accepts a single frame, runs binary classification (normal vs anomalous),
# and returns a score in [0.0, 1.0].
# Falls back gracefully to 0.0 if the model file is not yet present.

import os
import time

_MODELS_DIR  = os.path.join(os.path.dirname(os.path.dirname(__file__)), "models")
_MODEL_PATH  = os.path.join(_MODELS_DIR, "anomaly_classifier.pt")
_IMG_SIZE    = 224


class AnomalyEngine:
    """
    Per-frame anomaly scorer using MobileNetV2 trained on Phase 3 data.
    Binary: 0 = normal, 1 = anomalous.
    """

    def __init__(self):
        self._model     = None
        self._device    = None
        self._transform = None
        self._available = False

        try:
            import torch
            import torch.nn as nn
            import torchvision.models as tv_models
            from torchvision import transforms

            self._device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            self._transform = transforms.Compose([
                transforms.ToPILImage(),
                transforms.Resize((_IMG_SIZE, _IMG_SIZE)),
                transforms.ToTensor(),
                transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                     std=[0.229, 0.224, 0.225]),
            ])

            if not os.path.exists(_MODEL_PATH):
                print(f"[AnomalyEngine] Model not found at {_MODEL_PATH}. Returning zero scores.")
                return

            model = tv_models.mobilenet_v2(weights=None)
            model.classifier[1] = nn.Linear(model.last_channel, 2)
            model.load_state_dict(torch.load(_MODEL_PATH, map_location=self._device))
            model.to(self._device).eval()
            self._model = model
            self._available = True
            print(f"[AnomalyEngine] Loaded: {_MODEL_PATH}")

        except Exception as e:
            print(f"[AnomalyEngine] Init error: {e}. Returning zero scores.")

    def score(self, frame) -> float:
        """Accept a BGR numpy frame, return anomaly probability [0.0, 1.0]."""
        start = time.time()
        if not self._available or frame is None:
            return 0.0

        try:
            import torch
            import torch.nn.functional as F
            import cv2

            rgb    = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            tensor = self._transform(rgb).unsqueeze(0).to(self._device)

            with torch.no_grad():
                logits = self._model(tensor)
                prob   = float(F.softmax(logits, dim=1)[0, 1].cpu())

            from core.instrumentation import log_instrumentation
            log_instrumentation("AnomalyEngine", "inference", {
                "score": prob, "latency": time.time() - start
            })
            return prob

        except Exception as e:
            print(f"[AnomalyEngine] Inference error: {e}")
            return 0.0
