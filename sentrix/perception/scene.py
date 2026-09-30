"""Five-class scene classifier: normal, fire, weapon, assault, robbery.

Why this exists
---------------
On the team's own footage (``docs/6_CUSTOM_DATASET_EVALUATION.md``) the v3
detectors are near chance: the weapon head separates weapon frames from normal
ones at AUROC 0.54, the fire head finds none of the flame jets, and the RWF-2000
violence model calls an empty CCTV corridor a fight. Those models answer "is
there a box of class X" and "is this RWF-2000 fight footage"; neither was ever
shown this environment. This module answers the question the dataset actually
labels — *what is happening in this frame* — with a classifier trained on it.

How its output reaches the TCI
------------------------------
The classifier does not get a private path to the alarm. Its probabilities are
mapped onto the scales the existing fusion already acts on, so every override,
incident type, hysteresis rule and side-effect gate applies unchanged:

==========  =====================================================================
class       becomes
==========  =====================================================================
weapon      a ``weapon`` score, fused with the detector's (``SceneConfig.fusion``)
fire        a ``fire`` score, fused likewise
assault     the violence score: label ``fighting`` above its threshold
robbery     a ``robbery`` behaviour label above its threshold
==========  =====================================================================

The mapping is threshold-aligned (:func:`align`): a probability at the class's
tuned threshold lands exactly on the fusion override for that modality
(weapon 0.55, fire 0.70), and anything below it stays under the 0.3 bar at
which the TCI engine starts typing an incident. The classifier's decision
threshold and the pipeline's action threshold are therefore the same point.

Temporal behaviour
------------------
Every processed frame is scored — one forward pass per tick — and class
probabilities are averaged over ``SceneConfig.window_seconds``. A short window
stops a single ambiguous frame flipping the incident type tick to tick without
adding the latency of the 2 s violence window; the TCI's own EMA and
hysteresis still apply on top.

Checkpoints are self-describing (architecture, classes, preprocessing,
temperature, thresholds), so promoting a new model is a file copy.

A checkpoint may be a *delta*: when most of a backbone was frozen during
training, only the tensors training changed are stored, together with a
SHA-256 fingerprint of the frozen remainder. Loading rebuilds the public
pretrained weights (downloaded once by timm, then cached), refuses to continue
unless they hash to exactly what the classifier was trained on, and overlays
the delta. A 171 MB DINOv2 checkpoint becomes a 57 MB file with no loss.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from sentrix.config import settings
from sentrix.paths import model_path
from sentrix.utils.logging import get_logger, timed

log = get_logger("scene")

try:
    import cv2
except ImportError:                                    # pragma: no cover
    cv2 = None                                         # type: ignore[assignment]

SCENE_CLASSES: tuple[str, ...] = ("normal", "fire", "weapon", "assault", "robbery")

#: Decision priority when more than one class clears its threshold. Mirrors the
#: TCI override order: fire, then weapon, then behaviour.
PRIORITY: tuple[str, ...] = ("fire", "weapon", "assault", "robbery")

#: Weight files, in preference order; first present wins, so promotion is a file
#: copy. ``SENTRIX_SCENE_WEIGHTS`` overrides. v4_custom is trained on the custom
#: dataset from generic pretrained weights; v4_finetuned is the v3 violence
#: ResNet-18 fine-tuned on it (docs/6).
_WEIGHTS = ("v4_custom/scene_classifier_custom_v4.pt",
            "v4_finetuned/scene_classifier_ft_v4.pt")

#: Below the class threshold a signal is scaled into [0, SUB_THRESHOLD_CEILING),
#: i.e. under the 0.3 bar at which ``TCIEngine`` starts typing an incident.
SUB_THRESHOLD_CEILING = 0.3


def align(p: float, threshold: float, anchor: float) -> float:
    """Map a class probability onto a fusion score.

    ``threshold`` maps to ``anchor`` (the fusion override for that modality) and
    1.0 maps to 1.0, linearly. Below the threshold the score stays under
    ``SUB_THRESHOLD_CEILING`` so it can nudge the TCI but never type an incident.
    """
    p = float(min(max(p, 0.0), 1.0))
    threshold = float(min(max(threshold, 1e-3), 1.0 - 1e-3))
    if p >= threshold:
        return anchor + (1.0 - anchor) * (p - threshold) / (1.0 - threshold)
    return SUB_THRESHOLD_CEILING * p / threshold


def decide_label(probs: dict[str, float], thresholds: dict[str, float]) -> str:
    """The first class in :data:`PRIORITY` that clears its threshold, else normal.

    Shared by the runtime and the training scripts, so the rule a checkpoint's
    thresholds were tuned under is the rule it is served under.
    """
    return next((c for c in PRIORITY if probs.get(c, 0.0) >= thresholds.get(c, 0.5)),
                "normal")


def base_fingerprint(state: dict, exclude: set[str] | frozenset[str]) -> str:
    """SHA-256 over every tensor not in ``exclude``, in name order, as float32."""
    digest = hashlib.sha256()
    for name in sorted(state):
        if name in exclude:
            continue
        tensor = state[name].detach().float().cpu().contiguous()
        digest.update(name.encode())
        digest.update(tensor.numpy().tobytes())
    return digest.hexdigest()


def merge_delta(net, delta: dict, expected_fingerprint: str) -> None:
    """Overlay a delta checkpoint onto ``net``'s pretrained weights, or raise."""
    actual = base_fingerprint(net.state_dict(), frozenset(delta))
    if actual != expected_fingerprint:
        raise ValueError("pretrained base weights differ from the ones this "
                         "classifier was trained on (fingerprint mismatch)")
    missing, unexpected = net.load_state_dict(delta, strict=False)
    if unexpected:
        raise ValueError(f"delta has tensors the model lacks: {unexpected[:5]}")


def build_scene_model(architecture: str, num_classes: int, pretrained: bool = False,
                      image_size: int | None = None):
    """Construct the network named in a checkpoint.

    ``resnet18`` is the torchvision layout of the v3 violence classifier (a
    dropout + linear head), so its weights load directly for fine-tuning.
    ``timm:<name>`` is any timm model; ``timm`` is only imported when needed.
    Vision transformers are built for ``image_size`` (DINOv2 defaults to 518
    px, and a ViT's position grid is fixed at construction).
    """
    import torch.nn as nn

    if architecture == "resnet18":
        from torchvision import models
        weights = models.ResNet18_Weights.DEFAULT if pretrained else None
        net = models.resnet18(weights=weights)
        net.fc = nn.Sequential(nn.Dropout(0.3), nn.Linear(net.fc.in_features, num_classes))
        return net
    if architecture.startswith("timm:"):
        import timm
        name = architecture.split(":", 1)[1]
        kwargs = {"num_classes": num_classes, "drop_rate": 0.3}
        if image_size and name.startswith(("vit_", "eva", "deit")):
            kwargs["img_size"] = image_size
        return timm.create_model(name, pretrained=pretrained, **kwargs)
    raise ValueError(f"unknown scene architecture {architecture!r}")


def _resolve() -> Path | None:
    override = os.getenv("SENTRIX_SCENE_WEIGHTS", "").strip()
    if override:
        path = Path(override)
        return path if path.exists() else None
    for name in _WEIGHTS:
        path = model_path(*name.split("/"))
        if path.exists():
            return path
    return None


def _pick_device():
    import torch
    if torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


@dataclass
class SceneResult:
    """One aggregated verdict, already mapped onto fusion scales."""

    probs: dict[str, float] = field(default_factory=dict)
    label: str = "normal"
    #: ``weapon`` and ``fire`` on the fusion score scale (see :func:`align`).
    signals: dict[str, float] = field(default_factory=dict)
    violence: tuple[float, str] = (0.0, "normal")
    robbery: tuple[float, str] = (0.0, "normal")

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label,
                "probs": {k: round(v, 4) for k, v in self.probs.items()},
                "signals": {k: round(v, 4) for k, v in self.signals.items()}}


class SceneEngine:
    """Per-tick scene classification with a short probability window."""

    def __init__(self, weights: str | Path | None = None) -> None:
        self.cfg = settings.scene
        self._model = None
        self._device = None
        self._transform = None
        self.weights_name: str | None = None
        self.classes: list[str] = list(SCENE_CLASSES)
        self.temperature = 1.0
        self.thresholds: dict[str, float] = {c: 0.5 for c in PRIORITY}
        self._buffer: deque[tuple[float, np.ndarray]] = deque(maxlen=64)
        self._last: SceneResult = SceneResult()
        if self.cfg.enabled:
            self._load(Path(weights) if weights else _resolve())

    # ── loading ───────────────────────────────────────────────────────────────
    def _load(self, path: Path | None) -> None:
        if path is None or not path.exists():
            log.info("no scene classifier weights — scene engine disabled")
            return
        try:
            import torch
            from torchvision import transforms

            self._device = _pick_device()
            ckpt = torch.load(str(path), map_location="cpu", weights_only=False)
            classes = [str(c) for c in ckpt.get("classes", SCENE_CLASSES)]
            if set(PRIORITY) - set(classes):
                raise ValueError(f"checkpoint classes {classes} lack {PRIORITY}")
            state = {k: v.float() for k, v in ckpt["model_state_dict"].items()}
            delta = ckpt.get("storage") == "delta"
            net = build_scene_model(ckpt["architecture"], len(classes), pretrained=delta,
                                    image_size=int(ckpt.get("image_size", 224)))
            if delta:
                merge_delta(net, state, ckpt["base_fingerprint"])
            else:
                net.load_state_dict(state)
            net.to(self._device).eval()

            norm = ckpt.get("normalization") or {}
            size = int(ckpt.get("image_size", 224))
            self._transform = transforms.Compose([
                transforms.ToPILImage(),
                transforms.Resize((size, size)),
                transforms.ToTensor(),
                transforms.Normalize(mean=norm.get("mean", [0.485, 0.456, 0.406]),
                                     std=norm.get("std", [0.229, 0.224, 0.225])),
            ])
            self.classes = classes
            self.temperature = float(ckpt.get("temperature", 1.0) or 1.0)
            self.thresholds.update({k: float(v) for k, v in
                                    (ckpt.get("thresholds") or {}).items()
                                    if k in PRIORITY})
            self._model = net
            self.weights_name = path.name
            log.info("scene classifier loaded: %s (%s, %dpx, T=%.2f, thr=%s)",
                     path.name, ckpt["architecture"], size, self.temperature,
                     {k: round(v, 2) for k, v in self.thresholds.items()})
        except Exception as exc:
            log.error("scene classifier failed to load from %s: %s", path, exc)
            self._model = None

    @property
    def available(self) -> bool:
        return self._model is not None

    # ── inference ─────────────────────────────────────────────────────────────
    def score_frames(self, frames: list[np.ndarray]) -> np.ndarray:
        """Class probabilities for BGR frames, shape ``(N, len(classes))``."""
        import torch

        batch = [self._transform(cv2.cvtColor(f, cv2.COLOR_BGR2RGB)) for f in frames]
        with torch.no_grad():
            logits = self._model(torch.stack(batch).to(self._device)).float()
            probs = torch.softmax(logits / self.temperature, dim=1)
        return probs.cpu().numpy()

    def decide(self, probs: np.ndarray) -> SceneResult:
        """Threshold, prioritise and map one probability vector."""
        p = {c: float(probs[i]) for i, c in enumerate(self.classes)}
        label = decide_label(p, self.thresholds)
        fusion = settings.fusion
        signals = {
            "weapon": align(p["weapon"], self.thresholds["weapon"],
                            fusion.weapon_override_high),
            "fire": align(p["fire"], self.thresholds["fire"], fusion.fire_override),
        }
        violence = (p["assault"],
                    "fighting" if p["assault"] >= self.thresholds["assault"] else "normal")
        robbery = (p["robbery"],
                   "robbery" if p["robbery"] >= self.thresholds["robbery"] else "normal")
        return SceneResult(p, label, signals, violence, robbery)

    def observe(self, frame: np.ndarray, now: float | None = None) -> SceneResult:
        """Score one frame and return the window-averaged verdict."""
        if self._model is None or frame is None or getattr(frame, "size", 0) == 0:
            return self._last
        now = time.monotonic() if now is None else now
        try:
            with timed("scene", "inference"):
                probs = self.score_frames([frame])[0]
        except Exception as exc:
            log.debug("scene inference failed: %s", exc)
            return self._last
        self._buffer.append((now, probs))
        horizon = now - self.cfg.window_seconds
        while len(self._buffer) > 1 and self._buffer[0][0] < horizon:
            self._buffer.popleft()
        mean = np.mean([p for _, p in self._buffer], axis=0)
        self._last = self.decide(mean)
        return self._last

    def reset(self) -> None:
        self._buffer.clear()
        self._last = SceneResult()


__all__ = ["SCENE_CLASSES", "PRIORITY", "SceneEngine", "SceneResult", "align",
           "base_fingerprint", "build_scene_model", "decide_label", "merge_delta"]
