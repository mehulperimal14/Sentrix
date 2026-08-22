# ai/audio_cnn_engine.py
#
# ARCHITECTURE: AudioCNN-based threat audio classifier (Phase 3).
# Replaces the heuristic RMS/ZCR approach in audio_engine.py with a
# trained Log-Mel CNN that classifies: explosion, fire, scream, siren,
# speech_normal.
#
# Threat scores: explosion→0.85, scream→0.70, fire→0.60, siren→0.50
# The main AudioEngine in audio_engine.py still handles recording;
# this module only replaces the classification step when the model exists.

import os
import time

_MODELS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "models")
_MODEL_PATH = os.path.join(_MODELS_DIR, "audio_classifier.pt")

CLASSES      = ['explosion', 'fire', 'scream', 'siren', 'speech_normal']
THREAT_SCORE = {
    'explosion':    0.85,
    'fire':         0.60,
    'scream':       0.70,
    'siren':        0.50,
    'speech_normal': 0.05,
}

N_MELS     = 128
FIXED_LEN  = 128
SAMPLE_RATE = 22050
N_FFT      = 2048
HOP_LENGTH = 512


class AudioCNNEngine:
    """
    Trained CNN classifier for audio threat detection.
    Call classify_samples(samples) with a float32 numpy array (1-D, 22050 Hz)
    to get a (score, label) tuple.
    """

    def __init__(self):
        self._model     = None
        self._device    = None
        self._available = False

        try:
            import torch
            import torch.nn as nn

            self._device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

            if not os.path.exists(_MODEL_PATH):
                print(f"[AudioCNNEngine] Model not found at {_MODEL_PATH}. Heuristic fallback will be used.")
                return

            # Mirror the architecture from train_audio_classifier.py
            class AudioCNN(nn.Module):
                def __init__(self, num_classes=5):
                    super().__init__()
                    self.features = nn.Sequential(
                        nn.Conv2d(1, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(True), nn.MaxPool2d(2),
                        nn.Conv2d(32, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(True), nn.MaxPool2d(2),
                        nn.Conv2d(64, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(True), nn.MaxPool2d(2),
                        nn.AdaptiveAvgPool2d((4, 4)),
                    )
                    self.classifier = nn.Sequential(
                        nn.Dropout(0.4), nn.Linear(128 * 4 * 4, 256), nn.ReLU(True),
                        nn.Dropout(0.3), nn.Linear(256, num_classes),
                    )
                def forward(self, x):
                    x = self.features(x)
                    return self.classifier(x.view(x.size(0), -1))

            model = AudioCNN(num_classes=len(CLASSES))
            model.load_state_dict(torch.load(_MODEL_PATH, map_location=self._device))
            model.to(self._device).eval()
            self._model = model
            self._available = True
            print(f"[AudioCNNEngine] Loaded: {_MODEL_PATH}")

        except Exception as e:
            print(f"[AudioCNNEngine] Init error: {e}. Heuristic fallback will be used.")

    def is_available(self) -> bool:
        return self._available

    def classify_samples(self, samples) -> tuple:
        """
        samples: float32 numpy array (1-D, recorded at SAMPLE_RATE Hz).
        Returns (threat_score, label) where threat_score ∈ [0.0, 1.0].
        """
        start = time.time()
        if not self._available:
            return 0.0, "cnn_unavailable"

        try:
            import torch
            import torch.nn.functional as F
            import numpy as np
            import librosa

            mel = librosa.feature.melspectrogram(
                y=samples, sr=SAMPLE_RATE, n_fft=N_FFT,
                hop_length=HOP_LENGTH, n_mels=N_MELS,
            )
            log_mel = librosa.power_to_db(mel, ref=np.max)
            log_mel = (log_mel + 80.0) / 80.0 * 2.0 - 1.0

            T = log_mel.shape[1]
            if T < FIXED_LEN:
                pad = np.zeros((N_MELS, FIXED_LEN - T), dtype=np.float32)
                log_mel = np.concatenate([log_mel, pad], axis=1)
            else:
                log_mel = log_mel[:, :FIXED_LEN]

            tensor = torch.FloatTensor(log_mel).unsqueeze(0).unsqueeze(0).to(self._device)
            with torch.no_grad():
                logits = self._model(tensor)
                probs  = F.softmax(logits, dim=1)[0].cpu().numpy()

            pred_idx   = int(probs.argmax())
            pred_label = CLASSES[pred_idx]
            score      = float(THREAT_SCORE.get(pred_label, 0.05))

            from core.instrumentation import log_instrumentation
            log_instrumentation("AudioCNNEngine", "inference", {
                "label": pred_label, "score": score,
                "confidence": float(probs[pred_idx]),
                "latency": time.time() - start,
            })
            return score, pred_label

        except Exception as e:
            print(f"[AudioCNNEngine] Inference error: {e}")
            return 0.0, "cnn_error"
