# ai/violence_engine.py
#
# ARCHITECTURE: ResNet18 + LSTM violence classifier wrapper.
# Accepts a buffer of recent frames (up to seq_len), runs the trained
# model from Phase 3 training, and returns a score in [0.0, 1.0].
# Falls back gracefully to 0.0 if the model file is not yet present
# or if torch is not available.
#
# Designed for the SENTRIX pipeline — called every frame with the
# latest frame appended to an internal rolling buffer.

import os
import time
from collections import deque
from typing import Optional

import numpy as np

_MODELS_DIR   = os.path.join(os.path.dirname(os.path.dirname(__file__)), "models")
_MODEL_PATH   = os.path.join(_MODELS_DIR, "violence_classifier.pt")
_SEQ_LEN      = 10   # must match training (frames_per_clip)
_IMG_SIZE     = 224


class ViolenceEngine:
    """
    Per-frame violence scorer using ResNet18+LSTM trained on Phase 3 data.
    Maintains an internal rolling frame buffer of length _SEQ_LEN.
    Returns score=0.0 until the buffer is full (first _SEQ_LEN frames).
    """

    def __init__(self):
        self._model    = None
        self._device   = None
        self._buffer   = deque(maxlen=_SEQ_LEN)
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
                print(f"[ViolenceEngine] Model not found at {_MODEL_PATH}. Returning zero scores.")
                return

            # Rebuild model architecture (must match train_violence_classifier.py)
            class ResNetLSTM(nn.Module):
                def __init__(self):
                    super().__init__()
                    resnet = tv_models.resnet18(weights=None)
                    self.feature_extractor = nn.Sequential(*list(resnet.children())[:-1])
                    self.lstm = nn.LSTM(512, 256, 2, batch_first=True)
                    self.fc   = nn.Sequential(
                        nn.Linear(256, 128), nn.ReLU(), nn.Linear(128, 2)
                    )
                def forward(self, x):
                    bs, sl, c, h, w = x.size()
                    x = x.view(bs * sl, c, h, w)
                    feats = self.feature_extractor(x).view(bs, sl, -1)
                    out, _ = self.lstm(feats)
                    return self.fc(out[:, -1, :])

            model = ResNetLSTM()
            model.load_state_dict(torch.load(_MODEL_PATH, map_location=self._device))
            model.to(self._device).eval()
            self._model = model
            self._available = True
            print(f"[ViolenceEngine] Loaded: {_MODEL_PATH}")

        except Exception as e:
            print(f"[ViolenceEngine] Init error: {e}. Returning zero scores.")

    def score(self, frame) -> float:
        """
        Accept a BGR numpy frame, update buffer, return violence probability.
        Returns 0.0 until the buffer is full or if the model is unavailable.
        """
        start = time.time()
        if not self._available or frame is None:
            return 0.0

        try:
            import torch
            import torch.nn.functional as F
            import cv2

            # Convert BGR → RGB for torchvision
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            tensor = self._transform(rgb)
            self._buffer.append(tensor)

            if len(self._buffer) < _SEQ_LEN:
                return 0.0  # Buffer not full yet

            clip = torch.stack(list(self._buffer)).unsqueeze(0).to(self._device)  # (1, T, C, H, W)
            with torch.no_grad():
                logits = self._model(clip)
                prob   = float(F.softmax(logits, dim=1)[0, 1].cpu())

            from core.instrumentation import log_instrumentation
            log_instrumentation("ViolenceEngine", "inference", {
                "score": prob, "latency": time.time() - start
            })
            return prob

        except Exception as e:
            print(f"[ViolenceEngine] Inference error: {e}")
            return 0.0
