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
The classifier reports per-class probabilities and nothing else. It does not
map them onto fusion scales or raise overrides: the evidence layer
(:mod:`sentrix.fusion.evidence`) decides what a probability is worth, after
k-of-n confirmation, the person gate and corroboration caps. On its own the
classifier can reach at most the L3 band (CLAUDE.md D3) - the first live run
showed why: in a room it had never seen, it sat at its decision boundary.

Thresholds
----------
The checkpoint's thresholds were tuned for macro-F1 on data that is 41 %
threat; live, that put the weapon threshold at the 92nd percentile of *normal*
ticks. Effective thresholds are therefore, in order of precedence: a
deployment calibration (``var/calibration/scene.json`` or
``SENTRIX_SCENE_THRESHOLDS`` as JSON, e.g. ``{"weapon": 0.55}``), else the
checkpoint's value raised to ``ThreatConfig.scene_threshold_floor``.

Temporal behaviour
------------------
Every processed frame is scored once; probabilities are averaged over the last
``SceneConfig.window_ticks`` observations (counted, not timed).

Checkpoints are self-describing (architecture, classes, preprocessing,
temperature, thresholds), so promoting a new model is a file copy.

Runtimes
--------
``SENTRIX_RUNTIME=onnx`` (or ``ncnn``; ``SENTRIX_SCENE_RUNTIME`` overrides it
for this model alone) serves an exported twin of the checkpoint instead of
PyTorch: ``<name>.onnx`` / ``<name>_ncnn_model`` next to the checkpoint (the
slim models' format, docs/7 §2.4) or in ``models/exported/`` (written by
``tools/export_models.py``), described by ``<name>_metadata.json``. Neither
torch nor timm is imported then, which is what a Pi wants. A missing export or
metadata falls back to PyTorch with a warning.

A checkpoint may be a *delta*: when most of a backbone was frozen during
training, only the tensors training changed are stored, together with a
SHA-256 fingerprint of the frozen remainder. Loading rebuilds the public
pretrained weights (downloaded once by timm, then cached), refuses to continue
unless they hash to exactly what the classifier was trained on, and overlays
the delta. A 171 MB DINOv2 checkpoint becomes a 57 MB file with no loss.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np

from sentrix.config import settings
from sentrix.paths import CALIBRATION_DIR, model_path
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


def scene_runtime(runtime: str | None = None) -> str:
    """torch | onnx | ncnn for the scene model."""
    from sentrix.runtime import resolve_backend
    return resolve_backend(runtime or os.getenv("SENTRIX_SCENE_RUNTIME", "").strip() or None)


def exported_scene(path: Path, runtime: str) -> tuple[Path | None, Path | None]:
    """``(export, metadata)`` for a checkpoint on ``runtime``: the export next to
    the checkpoint first (slim models ship one), then ``models/exported/``."""
    from sentrix.runtime import EXPORT_DIR, exported_path
    candidates = [exported_path(path, None, runtime, path.parent),
                  exported_path(path, None, runtime, EXPORT_DIR)]
    export = next((c for c in candidates if c.exists()), None)
    meta_name = f"{path.stem}_metadata.json"
    metas = [path.parent / meta_name] + ([export.parent / meta_name] if export else [])
    metadata = next((m for m in metas if m.exists()), None)
    return export, metadata


@dataclass
class SceneResult:
    """Window-averaged class probabilities and the thresholds that apply."""

    probs: dict[str, float] = field(default_factory=dict)
    #: Display label: the first class in :data:`PRIORITY` over its threshold.
    #: Informational only - the evidence layer decides what raises an alarm.
    label: str = "normal"
    thresholds: dict[str, float] = field(default_factory=dict)
    #: False when this is the previous result returned again (no new inference).
    fresh: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label,
                "probs": {k: round(v, 4) for k, v in self.probs.items()},
                "thresholds": {k: round(v, 4) for k, v in self.thresholds.items()},
                "fresh": self.fresh}


def _valid_thresholds(values: dict, origin: str) -> dict[str, float]:
    """Keep only known classes with a threshold strictly inside (0, 1).

    A threshold of 0 or >= 1 (e.g. ``55`` meant as a percentage) would never be
    crossed and would silently disable the class, so it is rejected loudly and
    the checkpoint's floored value stays in force.
    """
    kept: dict[str, float] = {}
    for key, value in (values or {}).items():
        if key not in PRIORITY:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = float("nan")
        if 0.0 < number < 1.0:
            kept[key] = number
        else:
            log.error("ignoring %s threshold %r for %s: must be in (0, 1)",
                      origin, value, key)
    return kept


def deployment_thresholds() -> dict[str, float]:
    """Per-deployment threshold overrides, file first then environment."""
    overrides: dict[str, float] = {}
    path = CALIBRATION_DIR / "scene.json"
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            overrides.update(_valid_thresholds(data.get("thresholds", data), str(path)))
    except Exception as exc:
        log.error("ignoring unreadable scene calibration %s: %s", path, exc)
    raw = os.getenv("SENTRIX_SCENE_THRESHOLDS", "").strip()
    if raw:
        try:
            overrides.update(_valid_thresholds(json.loads(raw), "SENTRIX_SCENE_THRESHOLDS"))
        except Exception as exc:
            log.error("ignoring malformed SENTRIX_SCENE_THRESHOLDS: %s", exc)
    return overrides


class SceneEngine:
    """Per-tick scene classification with a short probability window."""

    def __init__(self, weights: str | Path | None = None,
                 runtime: str | None = None) -> None:
        self.cfg = settings.scene
        self._model = None
        self._device = None
        self._transform = None
        #: An exported model (onnx/ncnn) serving instead of the torch network.
        self._backend = None
        self._size = 224
        self._norm: dict = {}
        self.runtime = "torch"
        self.weights_name: str | None = None
        self.classes: list[str] = list(SCENE_CLASSES)
        self.temperature = 1.0
        self.thresholds: dict[str, float] = {c: 0.5 for c in PRIORITY}
        self.threshold_source = "default"
        self._buffer: deque[tuple[float, np.ndarray]] = deque(maxlen=64)
        self._last: SceneResult = SceneResult()
        if self.cfg.enabled:
            self._load(Path(weights) if weights else _resolve(), runtime)

    # ── loading ───────────────────────────────────────────────────────────────
    def _load(self, path: Path | None, runtime: str | None = None) -> None:
        if path is None or not path.exists():
            log.info("no scene classifier weights — scene engine disabled")
            return
        backend = scene_runtime(runtime)
        if backend != "torch" and self._load_exported(path, backend):
            return
        self._load_torch(path)

    def _load_exported(self, path: Path, backend: str) -> bool:
        """Serve the checkpoint's exported twin; False to fall back to torch."""
        export, metadata = exported_scene(path, backend)
        if export is None or metadata is None:
            log.warning("scene runtime %s: no export or metadata for %s (run "
                        "tools/export_models.py); using torch", backend, path.name)
            return False
        try:
            from sentrix.runtime.classifier import NcnnClassifier, OnnxClassifier
            meta = json.loads(metadata.read_text(encoding="utf-8"))
            classes = [str(c) for c in meta.get("classes", SCENE_CLASSES)]
            if set(PRIORITY) - set(classes):
                raise ValueError(f"metadata classes {classes} lack {PRIORITY}")
            threads = settings.clip.threads
            model = (OnnxClassifier(export, threads) if backend == "onnx"
                     else NcnnClassifier(export, threads))
            self.classes = classes
            self._size = int(meta.get("image_size", 224))
            self._norm = meta.get("normalization") or {}
            self.temperature = float(meta.get("temperature", 1.0) or 1.0)
            self.thresholds.update({k: float(v) for k, v in
                                    (meta.get("thresholds") or {}).items() if k in PRIORITY})
            self._apply_threshold_policy()
            self._backend = self._model = model
            self._device = f"{backend}:cpu"
            self.runtime = backend
            self.weights_name = path.name
            log.info("scene classifier on %s: %s (%dpx, T=%.2f, thr=%s)", backend,
                     export.name, self._size, self.temperature,
                     {k: round(v, 2) for k, v in self.thresholds.items()})
            return True
        except Exception as exc:
            log.error("scene export %s failed to load (%s); using torch", export, exc)
            self._backend = self._model = None
            return False

    def _load_torch(self, path: Path) -> None:
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
            if delta:
                # First run only: timm downloads the public base weights
                # (~330 MB) into the Hugging Face cache with no progress output.
                log.info("loading the scene model's public base weights (%s); "
                         "the first run downloads ~330 MB, which takes a few "
                         "minutes", ckpt["architecture"])
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
            self._apply_threshold_policy()
            self._model = net
            self.weights_name = path.name
            log.info("scene classifier loaded: %s (%s, %dpx, T=%.2f, thr=%s)",
                     path.name, ckpt["architecture"], size, self.temperature,
                     {k: round(v, 2) for k, v in self.thresholds.items()})
        except Exception as exc:
            log.error("scene classifier failed to load from %s: %s", path, exc)
            self._model = None

    def _apply_threshold_policy(self) -> None:
        # Floors first, then any class a deployment calibration names. A
        # calibration of one class must not drop the floors of the others (a
        # weapon-only override used to put fire back at the checkpoint's 0.35,
        # the value behind the live false fire alarms).
        floors = settings.threat.scene_threshold_floor
        self.thresholds = {c: max(t, float(floors.get(c, 0.0)))
                           for c, t in self.thresholds.items()}
        self.threshold_source = "checkpoint raised to floor"
        overrides = deployment_thresholds()
        if overrides:
            self.thresholds.update(overrides)
            self.threshold_source += f"; deployment calibration ({', '.join(sorted(overrides))})"

    @property
    def available(self) -> bool:
        return self._model is not None

    @property
    def device(self):
        """Where the classifier runs (``cuda`` when a CUDA build of torch is installed)."""
        return self._device

    # ── inference ─────────────────────────────────────────────────────────────
    def score_frames(self, frames: list[np.ndarray]) -> np.ndarray:
        """Class probabilities for BGR frames, shape ``(N, len(classes))``."""
        backend = getattr(self, "_backend", None)
        if backend is not None:
            from sentrix.runtime.classifier import (IMAGENET_MEAN, IMAGENET_STD, preprocess,
                                                    softmax)
            batch = preprocess(frames, self._size, self._norm.get("mean", IMAGENET_MEAN),
                               self._norm.get("std", IMAGENET_STD))
            return softmax(backend.logits(batch), self.temperature)

        import torch

        batch = [self._transform(cv2.cvtColor(f, cv2.COLOR_BGR2RGB)) for f in frames]
        with torch.no_grad():
            logits = self._model(torch.stack(batch).to(self._device)).float()
            probs = torch.softmax(logits / self.temperature, dim=1)
        return probs.cpu().numpy()

    def decide(self, probs: np.ndarray) -> SceneResult:
        """Probabilities, display label and the thresholds that apply."""
        p = {c: float(probs[i]) for i, c in enumerate(self.classes)}
        return SceneResult(p, decide_label(p, self.thresholds), dict(self.thresholds))

    def observe(self, frame: np.ndarray, now: float | None = None) -> SceneResult:
        """Score one frame and return the window-averaged verdict."""
        if self._model is None or frame is None or getattr(frame, "size", 0) == 0:
            return self.last
        now = time.monotonic() if now is None else now
        try:
            with timed("scene", "inference"):
                probs = self.score_frames([frame])[0]
        except Exception as exc:
            log.debug("scene inference failed: %s", exc)
            return self.last
        self._buffer.append((now, probs))
        horizon = now - self.cfg.max_age_seconds
        while len(self._buffer) > 1 and self._buffer[0][0] < horizon:
            self._buffer.popleft()
        recent = list(self._buffer)[-max(1, self.cfg.window_ticks):]
        mean = np.mean([p for _, p in recent], axis=0)
        self._last = self.decide(mean)
        return self._last

    @property
    def last(self) -> SceneResult:
        """The previous verdict, marked as not fresh (no new inference)."""
        return replace(self._last, fresh=False)

    def reset(self) -> None:
        self._buffer.clear()
        self._last = SceneResult()


__all__ = ["SCENE_CLASSES", "PRIORITY", "SceneEngine", "SceneResult",
           "base_fingerprint", "build_scene_model", "decide_label",
           "deployment_thresholds", "exported_scene", "merge_delta", "scene_runtime"]
