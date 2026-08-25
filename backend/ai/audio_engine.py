# ai/audio_engine.py
#
# ARCHITECTURE: Real-time background audio anomaly detector.
# Captures 1-second 16kHz PCM audio buffers in a non-blocking daemon loop.
# Uses trained PyTorch Audio Spectrogram CNN for multi-class acoustic classification
# with calibrated RMS energy gate to eliminate ambient noise false alarms.

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

from core.paths import MODELS_DIR

try:
    import numpy as np
    import sounddevice as sd
    _AUDIO_DEPS = True
except ImportError:
    _AUDIO_DEPS = False

SAMPLE_RATE     = 16000
RECORD_SECONDS  = 1
CHANNELS        = 1
RMS_NOISE_FLOOR = 0.020  # Below this threshold, room is quiet (ambient)
RMS_HIGH_ENERGY = 0.080  # Above this threshold, loud acoustic event


@dataclass
class AudioResult:
    score: float
    label: str


class AudioEngine:
    """Acoustic threat detector with deep PyTorch CNN + calibrated energy gate."""

    def __init__(self):
        self.available = False
        self.result    = AudioResult(score=0.02, label="normal_ambient")
        self._lock     = threading.Lock()
        self._stop     = threading.Event()
        self._model    = None
        self._device   = None
        self._classes  = ["normal_ambient", "gunshot_like", "scream_like", "explosion_like", "siren_like"]

        self._load_classifier_model()

        if not _AUDIO_DEPS:
            self.result = AudioResult(score=0.0, label="mic_unavailable")
            print("[AudioEngine] sounddevice/numpy not available. Audio disabled.")
            return

        try:
            # Query default input device
            dev_info = sd.query_devices(kind="input")
            self.available = True
            print(f"[AudioEngine] Microphone detected: {dev_info.get('name', 'Default Mic')}. Starting acoustic loop.")
        except Exception as e:
            print(f"[AudioEngine] No microphone found ({e}). Audio disabled.")
            self.result = AudioResult(score=0.0, label="mic_unavailable")
            return

        t = threading.Thread(target=self._record_loop, daemon=True, name="sentrix-audio")
        t.start()

    def _load_classifier_model(self):
        """Loads trained PyTorch audio threat CNN if present."""
        model_path = MODELS_DIR / "audio_classifier.pt"
        if not model_path.exists():
            return
        try:
            import torch
            import torch.nn as nn
            self._device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")

            class AudioThreatCNN(nn.Module):
                def __init__(self, num_classes=5):
                    super().__init__()
                    self.conv = nn.Sequential(
                        nn.Conv2d(1, 16, kernel_size=3, padding=1),
                        nn.BatchNorm2d(16),
                        nn.ReLU(),
                        nn.MaxPool2d(2, 2),
                        nn.Conv2d(16, 32, kernel_size=3, padding=1),
                        nn.BatchNorm2d(32),
                        nn.ReLU(),
                        nn.MaxPool2d(2, 2),
                        nn.Conv2d(32, 64, kernel_size=3, padding=1),
                        nn.BatchNorm2d(64),
                        nn.ReLU(),
                        nn.AdaptiveAvgPool2d((4, 4))
                    )
                    self.fc = nn.Sequential(
                        nn.Dropout(0.3),
                        nn.Linear(64 * 4 * 4, 64),
                        nn.ReLU(),
                        nn.Linear(64, num_classes)
                    )
                def forward(self, x):
                    feat = self.conv(x)
                    feat = feat.view(feat.size(0), -1)
                    return self.fc(feat)

            ckpt = torch.load(str(model_path), map_location=self._device)
            net = AudioThreatCNN(num_classes=len(self._classes))
            if "model_state_dict" in ckpt:
                net.load_state_dict(ckpt["model_state_dict"])
            else:
                net.load_state_dict(ckpt)
            net.to(self._device)
            net.eval()
            self._model = net
            print(f"[AudioEngine] ✅ Loaded deep acoustic classifier: {model_path.name}")
        except Exception as e:
            print(f"[AudioEngine] Audio model load fallback: {e}")

    def stop(self):
        """Signal background thread to stop."""
        self._stop.set()

    def _infer_deep(self, samples: np.ndarray, rms: float) -> Tuple[float, str]:
        """Runs Mel-spectrogram tensor through CNN model."""
        if self._model is None or rms < RMS_NOISE_FLOOR:
            return 0.02, "normal_ambient"
        try:
            import torch
            import scipy.signal
            import cv2

            f, t, Sxx = scipy.signal.spectrogram(samples, fs=SAMPLE_RATE, nperseg=512, noverlap=256)
            Sxx = np.log(Sxx + 1e-6)
            Sxx_resized = cv2.resize(Sxx, (64, 64)).astype(np.float32)
            tensor = torch.tensor(Sxx_resized).unsqueeze(0).unsqueeze(0).to(self._device)
            
            with torch.no_grad():
                out = self._model(tensor)
                probs = torch.softmax(out, dim=1)[0].cpu().numpy()
                top_idx = int(np.argmax(probs))
                top_prob = float(probs[top_idx])

            label = self._classes[top_idx]
            
            # If highest probability is normal_ambient, return low threat score
            if label == "normal_ambient" or top_prob < 0.70:
                return 0.02, "normal_ambient"

            threat_weights = {
                "gunshot_like": 0.90,
                "scream_like": 0.75,
                "explosion_like": 0.95,
                "siren_like": 0.60
            }
            score = threat_weights.get(label, 0.10) * top_prob
            return float(score), label
        except Exception:
            return 0.02, "normal_ambient"

    def _record_loop(self):
        while not self._stop.is_set():
            if not self.available:
                time.sleep(2)
                continue
            try:
                audio_data = sd.rec(
                    int(SAMPLE_RATE * RECORD_SECONDS),
                    samplerate=SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="float32",
                )
                sd.wait()
                samples = np.nan_to_num(audio_data.flatten().astype(np.float64))
                rms = float(np.sqrt(np.mean(samples ** 2)))

                # 1. Energy gating: If ambient room is quiet, classify immediately as normal
                if rms < RMS_NOISE_FLOOR:
                    with self._lock:
                        self.result = AudioResult(score=0.02, label="normal_ambient")
                    continue

                # 2. If sound energy is present, run deep CNN
                deep_score, deep_label = self._infer_deep(samples, rms)
                with self._lock:
                    self.result = AudioResult(score=deep_score, label=deep_label)

            except Exception as e:
                time.sleep(1)

    def get_result(self) -> AudioResult:
        """Non-blocking result retrieval."""
        with self._lock:
            return AudioResult(score=self.result.score, label=self.result.label)

    def detect_safe(self) -> Tuple[float, str]:
        """Tuple format used by system engine."""
        res = self.get_result()
        return res.score, res.label

    # Legacy alias
    def process_safe(self) -> AudioResult:
        return self.get_result()