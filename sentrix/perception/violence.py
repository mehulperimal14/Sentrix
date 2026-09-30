"""Violence classification over a temporal sliding window.

Two problems are fixed here.

**The model was never called.** ``BehaviourEngine.classify(tracks, detections,
frame=None)`` only ran the ResNet-18 when ``frame is not None``, and both call
sites passed two arguments. A 42 MB checkpoint was loaded into memory at startup
and never given a single frame. The headline "violence classifier, macro-F1
0.83" contributed exactly nothing to the threat index.

**Single-frame classification is the wrong formulation.** A still frame of two
people standing close together is genuinely ambiguous — the discriminative
signal for a fight is motion, not appearance. The action-recognition literature
settled on clips of roughly 2-3 seconds sampled to a fixed frame count.

Retraining is out of scope right now, so we get the temporal benefit at
inference time instead: sample ``violence_frames`` frames spanning
``violence_window_seconds``, score each with the existing per-frame checkpoint,
and aggregate. Aggregation is the mean of the top quartile of per-frame
probabilities — ``max`` alone fires on one bad frame, ``mean`` alone dilutes a
short scuffle across seconds of calm.

The real fix is a temporal head trained end to end (the original proposal
specified ResNet-18 **+ LSTM**, and the mid-semester class diagram still shows
``frame_buffer: deque``). ``notebooks`` covers that path. This module is
structured so that swapping in a clip model changes ``_score_window`` only.
"""

from __future__ import annotations

import time
from collections import deque

import numpy as np

from sentrix.config import settings
from sentrix.paths import model_path
from sentrix.utils.logging import get_logger, timed

log = get_logger("violence")

try:
    import cv2
except ImportError:                                    # pragma: no cover
    cv2 = None                                         # type: ignore[assignment]

_WEIGHTS = ("v3_real/violence_classifier_real_v3.pt", "violence_classifier.pt")


def _resolve():
    for name in _WEIGHTS:
        path = model_path(*name.split("/"))
        if path.exists():
            return path
    return None


def _pick_device():
    import torch
    if torch.cuda.is_available():
        return torch.device("cuda")
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


class ViolenceEngine:
    """Sliding-window violence scorer built on the per-frame checkpoint."""

    def __init__(self) -> None:
        self.cfg = settings.windows
        self._model = None
        self._device = None
        self._transform = None
        self._classes: list[str] = ["nofight", "fight"]
        self._threshold = 0.5
        self._buffer: deque[tuple[float, np.ndarray]] = deque(maxlen=256)
        self._last_eval = 0.0
        self._score = 0.0
        self._label = "normal"
        self._load()

    # ── loading ───────────────────────────────────────────────────────────────
    def _load(self) -> None:
        path = _resolve()
        if path is None:
            log.warning("no violence weights found — engine disabled")
            return
        try:
            import torch
            import torch.nn as nn
            from torchvision import models, transforms

            self._device = _pick_device()
            checkpoint = torch.load(str(path), map_location=self._device,
                                    weights_only=False)

            # Read the checkpoint's own metadata rather than hardcoding it, so
            # promoting a V3 weight file never requires a code change.
            classes = checkpoint.get("classes") or self._classes
            self._classes = [str(c) for c in classes]
            self._threshold = float(checkpoint.get("best_threshold", 0.5))

            net = models.resnet18(weights=None)
            net.fc = nn.Sequential(nn.Dropout(0.3),
                                   nn.Linear(net.fc.in_features, len(self._classes)))
            state = checkpoint.get("model_state_dict", checkpoint)
            net.load_state_dict(state)
            net.to(self._device).eval()
            self._model = net

            norm = checkpoint.get("normalization") or {}
            mean = norm.get("mean", [0.485, 0.456, 0.406])
            std = norm.get("std", [0.229, 0.224, 0.225])
            size = int(checkpoint.get("image_size", self.cfg.violence_image_size))
            self._transform = transforms.Compose([
                transforms.ToPILImage(),
                transforms.Resize((size, size)),
                transforms.ToTensor(),
                transforms.Normalize(mean=mean, std=std),
            ])
            log.info("violence classifier loaded: %s (classes=%s, thr=%.3f)",
                     path.name, self._classes, self._threshold)
        except Exception as exc:
            log.error("violence classifier failed to load: %s", exc)
            self._model = None

    @property
    def available(self) -> bool:
        return self._model is not None

    # ── sliding window ────────────────────────────────────────────────────────
    def observe(self, frame: np.ndarray, now: float | None = None) -> None:
        """Push a frame into the window buffer. Cheap; call every tick."""
        if self._model is None or frame is None or frame.size == 0:
            return
        now = time.monotonic() if now is None else now
        # Store small: the model resizes to 224 anyway, and a full-resolution
        # ring buffer would dominate memory on a 1080p feed.
        try:
            small = cv2.resize(frame, (self.cfg.violence_image_size,
                                       self.cfg.violence_image_size))
        except Exception:
            small = frame
        self._buffer.append((now, small))
        self._trim(now)

    def _trim(self, now: float) -> None:
        horizon = now - self.cfg.violence_window_seconds
        while self._buffer and self._buffer[0][0] < horizon:
            self._buffer.popleft()

    def _sample(self) -> list[np.ndarray]:
        """Uniformly sample ``violence_frames`` frames across the window."""
        frames = [f for _, f in self._buffer]
        if not frames:
            return []
        want = self.cfg.violence_frames
        if len(frames) <= want:
            return frames
        idx = np.linspace(0, len(frames) - 1, want).astype(int)
        return [frames[i] for i in idx]

    def _score_window(self, frames: list[np.ndarray]) -> float:
        """Aggregate per-frame P(fight) across the sampled window."""
        import torch

        batch = []
        for frame in frames:
            try:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                batch.append(self._transform(rgb))
            except Exception:
                continue
        if not batch:
            return 0.0

        positive = self._classes.index("fight") if "fight" in self._classes else 1
        with torch.no_grad():
            tensor = torch.stack(batch).to(self._device)
            probs = torch.softmax(self._model(tensor), dim=1)[:, positive]
            values = probs.detach().cpu().numpy()

        # Top-quartile mean: robust to a single spurious frame, still sensitive
        # to a short burst of violence inside an otherwise calm window.
        k = max(1, len(values) // 4)
        return float(np.mean(np.sort(values)[-k:]))

    def result(self, now: float | None = None) -> tuple[float, str]:
        """Return the cached ``(score, label)``, re-scoring once per hop."""
        if self._model is None:
            return 0.0, "normal"
        now = time.monotonic() if now is None else now
        if now - self._last_eval < self.cfg.violence_hop_seconds:
            return self._score, self._label
        self._last_eval = now

        frames = self._sample()
        if len(frames) < 2:
            return self._score, self._label
        try:
            with timed("violence", "inference", {"frames": len(frames)}):
                score = self._score_window(frames)
        except Exception as exc:
            log.debug("violence inference failed: %s", exc)
            return self._score, self._label

        self._score = score
        self._label = "fighting" if score >= self._threshold else "normal"
        return self._score, self._label

    def reset(self) -> None:
        self._buffer.clear()
        self._score, self._label, self._last_eval = 0.0, "normal", 0.0


__all__ = ["ViolenceEngine"]
