"""Model runtime backends: torch (development), onnxruntime, ncnn (HARDWARE.md §9).

``settings.clip.runtime`` (``SENTRIX_RUNTIME``) picks the backend for every
exported model; ``settings.clip.threads`` (``SENTRIX_CLIP_THREADS``) is the
CPU budget, four on a Pi 5. Exports live in ``models/exported/`` (written by
``tools/export_models.py``, never committed) and are named after the weights
they came from and their input size::

    models/exported/weapon_detector_custom_v4_320.onnx
    models/exported/weapon_detector_custom_v4_320_ncnn_model/
    models/exported/scene_classifier_custom_v4.onnx

A missing export, or a runtime that is not installed, falls back to torch
with a warning: the unit keeps detecting, only slower.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from sentrix.config import settings
from sentrix.paths import MODELS_DIR
from sentrix.utils.logging import get_logger

log = get_logger("runtime")

BACKENDS: tuple[str, ...] = ("torch", "onnx", "ncnn")
EXPORT_DIR = MODELS_DIR / "exported"


def installed(backend: str) -> bool:
    """Whether a backend's Python package is importable (no import done)."""
    module = {"torch": "ultralytics", "onnx": "onnxruntime", "ncnn": "ncnn"}.get(backend)
    return module is not None and importlib.util.find_spec(module) is not None


def available() -> dict[str, bool]:
    return {name: installed(name) for name in BACKENDS}


def resolve_backend(backend: str | None = None) -> str:
    """The configured backend, normalised; unknown names fall back to torch."""
    name = (backend or settings.clip.runtime or "torch").strip().lower()
    if name not in BACKENDS:
        log.error("unknown runtime %r (use one of %s); using torch", name, BACKENDS)
        return "torch"
    return name


def configure_threads(threads: int | None = None) -> dict:
    """Hold torch and OpenCV to the clip engine's CPU budget.

    Process-wide: called when clip mode starts and by the clip tools.
    onnxruntime and ncnn sessions take the same number per session.
    """
    n = max(1, int(threads or settings.clip.threads))
    report = {"threads": n}
    try:
        import torch
        torch.set_num_threads(n)
        report["torch"] = torch.get_num_threads()
    except Exception as exc:                           # pragma: no cover - torch optional
        report["torch"] = f"unavailable ({exc})"
    try:
        import cv2
        cv2.setNumThreads(n)
        report["opencv"] = cv2.getNumThreads()
    except Exception as exc:                           # pragma: no cover
        report["opencv"] = f"unavailable ({exc})"
    return report


def export_stem(weights: Path, imgsz: int | None = None) -> str:
    return f"{Path(weights).stem}_{int(imgsz)}" if imgsz else Path(weights).stem


def exported_path(weights: Path, imgsz: int | None, backend: str,
                  directory: Path | None = None) -> Path:
    """Where ``tools/export_models.py`` writes ``weights`` for ``backend``."""
    directory = directory or EXPORT_DIR
    stem = export_stem(weights, imgsz)
    if backend == "ncnn":
        return directory / f"{stem}_ncnn_model"
    return directory / f"{stem}.onnx"


def build_head(weights: Path | None, imgsz: int, backend: str | None = None,
               threads: int | None = None, directory: Path | None = None):
    """A detection head for ``weights`` at ``imgsz`` on the configured runtime.

    Returns None when there are no weights or nothing can run them.
    """
    from sentrix.runtime import yolo

    if weights is None:
        return None
    backend = resolve_backend(backend)
    threads = max(1, int(threads or settings.clip.threads))
    if backend != "torch":
        path = exported_path(weights, imgsz, backend, directory)
        if not installed(backend):
            log.warning("runtime %s is not installed; %s runs on torch", backend,
                        Path(weights).name)
        elif not path.exists():
            log.warning("no %s export of %s at %d px (run tools/export_models.py); "
                        "using torch", backend, Path(weights).name, imgsz)
        else:
            try:
                head = (yolo.OnnxHead(path, threads=threads) if backend == "onnx"
                        else yolo.NcnnHead(path, threads=threads))
                log.info("%s head loaded: %s (%d px)", backend, path.name, head.imgsz)
                return head
            except Exception as exc:
                log.error("cannot load %s export %s (%s); using torch", backend, path, exc)
    if not installed("torch"):
        log.error("no runtime can run %s", Path(weights).name)
        return None
    try:
        return yolo.TorchHead(weights, imgsz)
    except Exception as exc:
        log.error("cannot load %s on torch: %s", weights, exc)
        return None


__all__ = ["BACKENDS", "EXPORT_DIR", "available", "build_head", "configure_threads",
           "export_stem", "exported_path", "installed", "resolve_backend"]
