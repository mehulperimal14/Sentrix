"""YOLO detection heads behind one interface, on torch, onnxruntime or ncnn.

Why a runtime layer
-------------------
On a Raspberry Pi 5 the same YOLO11n is about five times faster in NCNN than
in PyTorch (docs/8_RASPBERRY_PI5_INTEGRATION.md §2), so PyTorch is for development only there. The
heads below take a BGR frame and return boxes in frame pixels whatever runs
the network:

* :class:`TorchHead` - ultralytics ``predict`` on the ``.pt`` weights (the
  reference; what the per-frame pipeline runs today);
* :class:`OnnxHead` - an exported ``.onnx`` on onnxruntime, with numpy pre-
  and post-processing, so it needs neither torch nor ultralytics at runtime;
* :class:`NcnnHead` - an exported ``*_ncnn_model`` folder on the ``ncnn``
  Python package (Tencent's runtime; the Pi's fastest CPU path).

The exported heads reproduce ultralytics' detection head exactly: a square
letterbox (grey 114 border), one ``(1, 4 + classes, anchors)`` output of
centre-xywh boxes and per-class sigmoid scores, the best class per anchor,
class-aware NMS, boxes scaled back to the frame. ``tools/export_models.py``
checks that parity against PyTorch on real frames.

onnxruntime sessions never spin-wait: with the default busy-wait the
InsightFace sessions once held ~20 of 24 cores after every call and made the
next YOLO pass 10-30x slower (CLAUDE.md §5, M2).
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import numpy as np

from sentrix.utils.logging import get_logger

log = get_logger("runtime")

try:
    import cv2
except ImportError:                                    # pragma: no cover
    cv2 = None                                         # type: ignore[assignment]

#: ultralytics' letterbox fill and its class offset for batched NMS.
PAD_VALUE = 114
_NMS_CLASS_OFFSET = 7680.0


@dataclass(frozen=True)
class Box:
    """One detection in frame pixels."""

    label: str
    conf: float
    bbox: tuple[int, int, int, int]
    cls: int


@runtime_checkable
class DetectorHead(Protocol):
    """A detection network on some runtime."""

    name: str
    backend: str
    imgsz: int
    names: dict[int, str]

    def predict(self, frame: np.ndarray, conf: float, iou: float = 0.45,
                classes: list[int] | None = None, max_det: int = 50) -> list[Box]: ...


# ── pre- and post-processing (shared by every exported head) ──────────────────
def letterbox(frame: np.ndarray, size: int) -> tuple[np.ndarray, float, tuple[float, float]]:
    """BGR frame -> ``(1, 3, size, size)`` float32 RGB in [0, 1], scale, padding.

    The square letterbox of an exported model (ultralytics pads to the full
    size for a fixed-shape export): aspect kept, ``PAD_VALUE`` border split
    evenly, bilinear resize.
    """
    height, width = frame.shape[:2]
    ratio = min(size / height, size / width)
    new_w, new_h = int(round(width * ratio)), int(round(height * ratio))
    pad_w, pad_h = (size - new_w) / 2.0, (size - new_h) / 2.0
    if (new_w, new_h) != (width, height):
        frame = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    top, bottom = int(round(pad_h - 0.1)), int(round(pad_h + 0.1))
    left, right = int(round(pad_w - 0.1)), int(round(pad_w + 0.1))
    frame = cv2.copyMakeBorder(frame, top, bottom, left, right, cv2.BORDER_CONSTANT,
                               value=(PAD_VALUE, PAD_VALUE, PAD_VALUE))
    blob = frame[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
    return np.ascontiguousarray(blob), ratio, (float(left), float(top))


def nms(boxes: np.ndarray, scores: np.ndarray, iou: float) -> list[int]:
    """Greedy non-maximum suppression on ``(n, 4)`` xyxy boxes."""
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes.T
    areas = np.maximum(x2 - x1, 0) * np.maximum(y2 - y1, 0)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size:
        i = int(order[0])
        keep.append(i)
        rest = order[1:]
        w = np.maximum(0.0, np.minimum(x2[i], x2[rest]) - np.maximum(x1[i], x1[rest]))
        h = np.maximum(0.0, np.minimum(y2[i], y2[rest]) - np.maximum(y1[i], y1[rest]))
        inter = w * h
        overlap = inter / np.maximum(areas[i] + areas[rest] - inter, 1e-9)
        order = rest[overlap <= iou]
    return keep


def decode(output: np.ndarray, *, conf: float, iou: float, ratio: float,
           pad: tuple[float, float], shape: tuple[int, ...], names: dict[int, str],
           classes: list[int] | None = None, max_det: int = 50) -> list[Box]:
    """Raw head output ``(1, 4 + nc, anchors)`` -> boxes in frame pixels."""
    pred = np.asarray(output, dtype=np.float32)
    if pred.ndim == 3:
        pred = pred[0]
    pred = pred.T                                     # (anchors, 4 + nc)
    if pred.shape[1] <= 4:
        return []
    scores_all = pred[:, 4:]
    cls_ids = scores_all.argmax(axis=1)
    scores = scores_all[np.arange(len(pred)), cls_ids]
    mask = scores > conf                              # strictly, as ultralytics' NMS
    if classes is not None:
        mask &= np.isin(cls_ids, np.asarray(classes))
    if not mask.any():
        return []
    pred, scores, cls_ids = pred[mask], scores[mask], cls_ids[mask]
    cx, cy, w, h = pred[:, 0], pred[:, 1], pred[:, 2], pred[:, 3]
    xyxy = np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], axis=1)
    keep = nms(xyxy + (cls_ids[:, None] * _NMS_CLASS_OFFSET), scores, iou)[:max_det]
    height, width = shape[:2]
    out: list[Box] = []
    for i in keep:
        x1, y1, x2, y2 = xyxy[i]
        x1 = min(max((x1 - pad[0]) / ratio, 0.0), width)
        x2 = min(max((x2 - pad[0]) / ratio, 0.0), width)
        y1 = min(max((y1 - pad[1]) / ratio, 0.0), height)
        y2 = min(max((y2 - pad[1]) / ratio, 0.0), height)
        cls = int(cls_ids[i])
        out.append(Box(names.get(cls, f"class_{cls}"), float(scores[i]),
                       (int(x1), int(y1), int(x2), int(y2)), cls))
    return out


def parse_names(raw) -> dict[int, str]:
    """Class names from ultralytics export metadata (a dict, or its repr)."""
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            raw = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            try:
                raw = json.loads(raw)
            except ValueError:
                return {}
    if isinstance(raw, dict):
        return {int(k): str(v) for k, v in raw.items()}
    if isinstance(raw, (list, tuple)):
        return {i: str(v) for i, v in enumerate(raw)}
    return {}


# ── heads ─────────────────────────────────────────────────────────────────────
class TorchHead:
    """ultralytics ``predict`` on ``.pt`` weights (the reference)."""

    backend = "torch"

    def __init__(self, weights: str | Path, imgsz: int) -> None:
        from ultralytics import YOLO
        self.path = Path(weights)
        self.name = self.path.name
        self.imgsz = int(imgsz)
        self._model = YOLO(str(self.path))
        raw = getattr(self._model, "names", None) or {}
        self.names = parse_names(raw)

    def predict(self, frame: np.ndarray, conf: float, iou: float = 0.45,
                classes: list[int] | None = None, max_det: int = 50) -> list[Box]:
        result = self._model.predict(frame, imgsz=self.imgsz, conf=conf, iou=iou,
                                     classes=classes, max_det=max_det, verbose=False)[0]
        boxes = getattr(result, "boxes", None)
        out: list[Box] = []
        if boxes is None:
            return out
        for box in boxes:
            cls = int(box.cls[0])
            xyxy = [int(v) for v in box.xyxy[0]]
            out.append(Box(self.names.get(cls, f"class_{cls}"), float(box.conf[0]),
                           (xyxy[0], xyxy[1], xyxy[2], xyxy[3]), cls))
        return out


def ort_session(path: str | Path, threads: int):
    """An onnxruntime CPU session that never spin-waits."""
    import onnxruntime as ort
    options = ort.SessionOptions()
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    options.add_session_config_entry("session.inter_op.allow_spinning", "0")
    options.intra_op_num_threads = max(1, int(threads))
    options.inter_op_num_threads = 1
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    return ort.InferenceSession(str(path), sess_options=options,
                                providers=["CPUExecutionProvider"])


class OnnxHead:
    """An exported YOLO head on onnxruntime; numpy pre/post-processing."""

    backend = "onnx"

    def __init__(self, path: str | Path, threads: int = 4,
                 names: dict[int, str] | None = None, imgsz: int | None = None) -> None:
        self.path = Path(path)
        self.name = self.path.name
        self.session = ort_session(self.path, threads)
        inp = self.session.get_inputs()[0]
        self._input = inp.name
        height = inp.shape[2] if len(inp.shape) == 4 else None
        self.imgsz = int(height) if isinstance(height, int) else int(imgsz or 640)
        meta = self.session.get_modelmeta().custom_metadata_map or {}
        self.names = names or parse_names(meta.get("names"))

    def raw(self, blob: np.ndarray) -> np.ndarray:
        return self.session.run(None, {self._input: blob})[0]

    def predict(self, frame: np.ndarray, conf: float, iou: float = 0.45,
                classes: list[int] | None = None, max_det: int = 50) -> list[Box]:
        blob, ratio, pad = letterbox(frame, self.imgsz)
        return decode(self.raw(blob), conf=conf, iou=iou, ratio=ratio, pad=pad,
                      shape=frame.shape, names=self.names, classes=classes,
                      max_det=max_det)


class NcnnHead:
    """An exported ``*_ncnn_model`` folder on the ``ncnn`` Python package."""

    backend = "ncnn"

    def __init__(self, folder: str | Path, threads: int = 4,
                 names: dict[int, str] | None = None, imgsz: int | None = None,
                 ncnn_module=None) -> None:
        ncnn = ncnn_module
        if ncnn is None:
            import ncnn                                # noqa: F811
        self.path = Path(folder)
        self.name = self.path.name
        meta = _ncnn_metadata(self.path)
        self.names = names or parse_names(meta.get("names"))
        size = meta.get("imgsz")
        if isinstance(size, (list, tuple)) and size:
            size = size[0]
        self.imgsz = int(size or imgsz or 640)
        self._ncnn = ncnn
        self._net = ncnn.Net()
        self._net.opt.num_threads = max(1, int(threads))
        self._net.opt.use_vulkan_compute = False
        self._net.load_param(str(self.path / "model.ncnn.param"))
        self._net.load_model(str(self.path / "model.ncnn.bin"))

    def raw(self, blob: np.ndarray) -> np.ndarray:
        extractor = self._net.create_extractor()
        extractor.input("in0", self._ncnn.Mat(np.ascontiguousarray(blob[0])))
        _, out = extractor.extract("out0")
        return np.asarray(out)[None]

    def predict(self, frame: np.ndarray, conf: float, iou: float = 0.45,
                classes: list[int] | None = None, max_det: int = 50) -> list[Box]:
        blob, ratio, pad = letterbox(frame, self.imgsz)
        return decode(self.raw(blob), conf=conf, iou=iou, ratio=ratio, pad=pad,
                      shape=frame.shape, names=self.names, classes=classes,
                      max_det=max_det)


def _ncnn_metadata(folder: Path) -> dict:
    """``metadata.yaml`` written by ultralytics next to an NCNN export."""
    path = folder / "metadata.yaml"
    if not path.exists():
        return {}
    try:
        import yaml
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        log.warning("unreadable NCNN metadata %s: %s", path, exc)
        return {}


__all__ = ["Box", "DetectorHead", "NcnnHead", "OnnxHead", "PAD_VALUE", "TorchHead",
           "decode", "letterbox", "nms", "ort_session", "parse_names"]
