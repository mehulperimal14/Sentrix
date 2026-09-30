"""Acoustic threat classification over a sliding window.

Checkpoint-adaptive by design
-----------------------------
The repository contains two mutually incompatible audio checkpoints:

===================  ==========================  ==========================
                     ``audio_classifier.pt``     ``..._real_v3.pt``
===================  ==========================  ==========================
Parameter tensors    25                          52
Module names         ``conv.*`` / ``fc.*``       ``features.*`` / ``classifier.*``
Classes              5 (``normal_ambient`` …)    4 (``speech_normal`` …)
Front end            linear spectrogram, 64x64   log-mel, 64x251, 4 s
===================  ==========================  ==========================

The previous engine hardcoded the 5-class V2 architecture and its front end.
Following the documented upgrade procedure — copying the V3 file over the
deployed name — therefore raised a ``load_state_dict`` error that was swallowed
by a bare ``except``, leaving ``self._model = None``. Audio then reported
``0.02 / normal_ambient`` forever, silently.

This module inspects the state dict, builds the matching architecture, and
selects the front end that checkpoint was trained with. Both files load, and
promoting V3 really is just a file copy.

Threat weighting is keyed by *class name*, not index, so adding or removing a
class (dropping ``explosion``, adding ``glass_break`` or a
``nuisance_impulsive`` hard-negative class) needs no code change here.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import numpy as np

from sentrix.config import settings
from sentrix.paths import model_path
from sentrix.utils.logging import get_logger, timed

log = get_logger("audio")

try:
    import sounddevice as sd
    _SD = True
except Exception:                                      # pragma: no cover
    sd = None                                          # type: ignore[assignment]
    _SD = False

_WEIGHTS = ("v3_real/audio_classifier_real_v3.pt", "audio_classifier.pt")

#: How dangerous each acoustic class is, by name. Unknown classes score 0.
#: Sirens sit low on purpose: a siren means help is already coming, and in
#: Indian urban soundscapes it is also the class most often confused with a
#: vehicle pressure horn.
THREAT_WEIGHTS: dict[str, float] = {
    "normal_ambient": 0.0,
    "speech_normal": 0.0,
    "ambient": 0.0,
    "nuisance_impulsive": 0.05,
    "siren": 0.45,
    "siren_like": 0.45,
    "alarm_siren": 0.55,
    "fire": 0.75,
    "scream": 0.80,
    "scream_like": 0.80,
    "glass_break": 0.80,
    "gunshot": 0.95,
    "gunshot_like": 0.95,
    "explosion_like": 0.95,
}

_QUIET = ("normal_ambient", "speech_normal", "ambient")


@dataclass
class AudioResult:
    score: float = 0.0
    label: str = "normal_ambient"
    confidence: float = 0.0


def _pick_device():
    import torch
    if torch.cuda.is_available():
        return torch.device("cuda")
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def _build_v3(num_classes: int):
    """Four VGG-style blocks -> global pool -> 2-layer head. 52 tensors."""
    import torch.nn as nn

    def block(cin, cout):
        return nn.Sequential(
            nn.Conv2d(cin, cout, 3, padding=1, bias=False), nn.BatchNorm2d(cout), nn.ReLU(inplace=True),
            nn.Conv2d(cout, cout, 3, padding=1, bias=False), nn.BatchNorm2d(cout), nn.ReLU(inplace=True),
            nn.MaxPool2d(2, 2),
        )

    class SentrixAudioCNN(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.features = nn.Sequential(block(1, 32), block(32, 64),
                                          block(64, 128), block(128, 256))
            self.classifier = nn.Sequential(
                nn.AdaptiveAvgPool2d((1, 1)), nn.Flatten(),
                nn.Linear(256, 128), nn.ReLU(inplace=True),
                nn.Dropout(0.3), nn.Linear(128, num_classes),
            )

        def forward(self, x):
            return self.classifier(self.features(x))

    return SentrixAudioCNN()


def _build_v2(num_classes: int):
    """Legacy 3-conv net with a 4x4 adaptive pool. 25 tensors."""
    import torch.nn as nn

    class AudioThreatCNN(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.conv = nn.Sequential(
                nn.Conv2d(1, 16, 3, padding=1), nn.BatchNorm2d(16), nn.ReLU(), nn.MaxPool2d(2, 2),
                nn.Conv2d(16, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(), nn.MaxPool2d(2, 2),
                nn.Conv2d(32, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(),
                nn.AdaptiveAvgPool2d((4, 4)),
            )
            self.fc = nn.Sequential(nn.Dropout(0.3), nn.Linear(64 * 16, 64),
                                    nn.ReLU(), nn.Linear(64, num_classes))

        def forward(self, x):
            return self.fc(self.conv(x).flatten(1))

    return AudioThreatCNN()


class AudioEngine:
    """Background microphone capture plus windowed acoustic classification."""

    def __init__(self) -> None:
        self.cfg = settings.windows
        self.available = False
        self._result = AudioResult()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._model = None
        self._device = None
        self._classes: list[str] = []
        self._front_end = "logmel"
        self._meta: dict = {}
        self._ring = np.zeros(0, dtype=np.float32)

        self._load()
        self._start_capture()

    # ── loading ───────────────────────────────────────────────────────────────
    def _load(self) -> None:
        path = None
        for name in _WEIGHTS:
            candidate = model_path(*name.split("/"))
            if candidate.exists():
                path = candidate
                break
        if path is None:
            log.warning("no audio weights found — acoustic engine disabled")
            return
        try:
            import torch

            self._device = _pick_device()
            checkpoint = torch.load(str(path), map_location=self._device,
                                    weights_only=False)
            state = checkpoint.get("model_state_dict", checkpoint)
            self._meta = {k: v for k, v in checkpoint.items()
                          if not hasattr(v, "shape") and k != "model_state_dict"}

            classes = checkpoint.get("classes")
            if not classes:
                log.error("checkpoint %s has no class list — refusing to guess",
                          path.name)
                return
            self._classes = [str(c) for c in classes]

            # Architecture is chosen from the parameter names, not from a
            # version string, so an unexpected checkpoint fails loudly here
            # rather than silently disabling audio at runtime.
            keys = list(state.keys())
            if any(k.startswith("features.") for k in keys):
                net = _build_v3(len(self._classes))
                self._front_end = "logmel"
            elif any(k.startswith("conv.") for k in keys):
                net = _build_v2(len(self._classes))
                self._front_end = "spectrogram"
            else:
                log.error("unrecognised audio architecture in %s", path.name)
                return

            net.load_state_dict(state)
            net.to(self._device).eval()
            self._model = net
            log.info("audio classifier loaded: %s (%s front end, classes=%s)",
                     path.name, self._front_end, self._classes)

            unknown = [c for c in self._classes if c not in THREAT_WEIGHTS]
            if unknown:
                log.warning("classes with no threat weight, scored 0.0: %s", unknown)
        except Exception as exc:
            log.error("audio classifier failed to load: %s", exc)
            self._model = None

    # ── capture ───────────────────────────────────────────────────────────────
    def _start_capture(self) -> None:
        if not _SD:
            log.warning("sounddevice not installed — microphone disabled")
            return
        try:
            info = sd.query_devices(kind="input")
            log.info("microphone: %s", info.get("name", "default"))
        except Exception as exc:
            log.warning("no input device (%s) — microphone disabled", exc)
            return
        self.available = True
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="sentrix-audio")
        self._thread.start()

    def _loop(self) -> None:
        sr = self.cfg.audio_sample_rate
        hop = self.cfg.audio_hop_seconds
        window_samples = int(sr * self.cfg.audio_window_seconds)
        hop_samples = int(sr * hop)

        while not self._stop.is_set():
            try:
                chunk = sd.rec(hop_samples, samplerate=sr, channels=1,
                               dtype="float32")
                sd.wait()
                if self._stop.is_set():
                    break
                samples = np.nan_to_num(chunk.flatten().astype(np.float32))

                # Ring buffer holds exactly one analysis window.
                self._ring = np.concatenate([self._ring, samples])[-window_samples:]
                if len(self._ring) < window_samples:
                    continue

                rms = float(np.sqrt(np.mean(self._ring ** 2)))
                if rms < self.cfg.audio_rms_floor:
                    # Energy gate. Skipping inference on a quiet room is both a
                    # large compute saving and the single most effective
                    # acoustic false-alarm suppressor available.
                    self._publish(AudioResult(0.0, "normal_ambient", 1.0))
                    continue

                self._publish(self._classify(self._ring, rms))
            except Exception as exc:
                log.debug("audio loop error: %s", exc)
                time.sleep(0.5)

    def _publish(self, result: AudioResult) -> None:
        with self._lock:
            self._result = result

    # ── front ends ────────────────────────────────────────────────────────────
    def _logmel(self, samples: np.ndarray):
        """Log-mel spectrogram matching PRODUCTION_PREPROCESSING_CONTRACT.json."""
        import torch

        meta = self._meta
        n_mels = int(meta.get("n_mels", self.cfg.audio_n_mels))
        n_fft = int(meta.get("n_fft", self.cfg.audio_n_fft))
        hop = int(meta.get("hop_length", self.cfg.audio_hop_length))
        fmin = float(meta.get("f_min", self.cfg.audio_fmin))
        fmax = float(meta.get("f_max", self.cfg.audio_fmax))
        top_db = float(meta.get("top_db", self.cfg.audio_top_db))
        sr = int(meta.get("sample_rate", self.cfg.audio_sample_rate))

        try:
            import torchaudio
            wav = torch.from_numpy(samples).unsqueeze(0)
            mel = torchaudio.transforms.MelSpectrogram(
                sample_rate=sr, n_fft=n_fft, hop_length=hop, f_min=fmin,
                f_max=fmax, n_mels=n_mels, power=2.0)(wav)
            spec = torchaudio.transforms.AmplitudeToDB(stype="power",
                                                       top_db=top_db)(mel)
        except ImportError:
            import librosa
            mel = librosa.feature.melspectrogram(
                y=samples, sr=sr, n_fft=n_fft, hop_length=hop, n_mels=n_mels,
                fmin=fmin, fmax=fmax, power=2.0)
            spec = torch.from_numpy(
                librosa.power_to_db(mel, top_db=top_db)).unsqueeze(0)

        spec = (spec - spec.mean()) / (spec.std() + 1e-5)   # per-sample norm
        return spec.unsqueeze(0)

    def _legacy_spectrogram(self, samples: np.ndarray):
        """Linear spectrogram resized to 64x64, as the V2 checkpoint expects."""
        import cv2
        import scipy.signal
        import torch

        _, _, sxx = scipy.signal.spectrogram(
            samples.astype(np.float64), fs=self.cfg.audio_sample_rate,
            nperseg=512, noverlap=256)
        sxx = np.log(sxx + 1e-6)
        resized = cv2.resize(sxx, (64, 64)).astype(np.float32)
        return torch.from_numpy(resized).unsqueeze(0).unsqueeze(0)

    # ── inference ─────────────────────────────────────────────────────────────
    def _classify(self, samples: np.ndarray, rms: float) -> AudioResult:
        if self._model is None:
            return AudioResult(0.0, "normal_ambient", 0.0)
        try:
            import torch
            with timed("audio", "inference", {"rms": round(rms, 4)}):
                if self._front_end == "logmel":
                    tensor = self._logmel(samples)
                else:
                    tensor = self._legacy_spectrogram(samples)
                tensor = tensor.to(self._device)
                with torch.no_grad():
                    probs = torch.softmax(self._model(tensor), dim=1)[0]
                    probs = probs.detach().cpu().numpy()

            idx = int(np.argmax(probs))
            label = self._classes[idx]
            confidence = float(probs[idx])

            if label in _QUIET:
                return AudioResult(0.0, label, confidence)

            # Threat score is weight * confidence, so a hesitant "maybe a
            # scream" contributes proportionally less than a confident one.
            score = THREAT_WEIGHTS.get(label, 0.0) * confidence
            return AudioResult(float(score), label, confidence)
        except Exception as exc:
            log.debug("audio inference failed: %s", exc)
            return AudioResult(0.0, "normal_ambient", 0.0)

    # ── public API ────────────────────────────────────────────────────────────
    def result(self) -> AudioResult:
        with self._lock:
            return AudioResult(self._result.score, self._result.label,
                               self._result.confidence)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


__all__ = ["AudioEngine", "AudioResult", "THREAT_WEIGHTS"]
