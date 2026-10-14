"""Image classifiers (the scene model) on onnxruntime or ncnn.

The scene classifier is trained and served in PyTorch with torchvision's
``Resize((s, s))`` on a PIL image, ``ToTensor`` and ``Normalize`` (the
"squashed resize" of its metadata). :func:`preprocess` reproduces that chain
with Pillow and numpy, so an exported model scores the same pixels without
torch installed; ``tools/export_models.py`` checks the probabilities match.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from sentrix.runtime.yolo import ort_session

try:
    import cv2
except ImportError:                                    # pragma: no cover
    cv2 = None                                         # type: ignore[assignment]

IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)


def preprocess(frames: list[np.ndarray], size: int, mean=IMAGENET_MEAN,
               std=IMAGENET_STD) -> np.ndarray:
    """BGR frames -> ``(n, 3, size, size)`` float32, as the torch transform.

    Pillow's bilinear resize (antialiased when shrinking) is what
    ``torchvision.transforms.Resize`` runs on a PIL image; OpenCV's bilinear
    is not antialiased and drifts by a few grey levels on a 4x shrink.
    """
    from PIL import Image

    mean_arr = np.asarray(mean, dtype=np.float32).reshape(3, 1, 1)
    std_arr = np.asarray(std, dtype=np.float32).reshape(3, 1, 1)
    batch = []
    for frame in frames:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(rgb).resize((size, size), Image.BILINEAR)
        tensor = np.asarray(image, dtype=np.float32).transpose(2, 0, 1) / 255.0
        batch.append((tensor - mean_arr) / std_arr)
    return np.ascontiguousarray(np.stack(batch).astype(np.float32))


class OnnxClassifier:
    """Logits from an exported classifier (input ``images``, output ``logits``)."""

    backend = "onnx"

    def __init__(self, path: str | Path, threads: int = 4) -> None:
        self.path = Path(path)
        self.session = ort_session(self.path, threads)
        inp = self.session.get_inputs()[0]
        self._input = inp.name
        self._fixed_batch = inp.shape[0] if isinstance(inp.shape[0], int) else None

    def logits(self, batch: np.ndarray) -> np.ndarray:
        if self._fixed_batch == 1 and len(batch) > 1:
            return np.concatenate([self.session.run(None, {self._input: batch[i:i + 1]})[0]
                                   for i in range(len(batch))])
        return self.session.run(None, {self._input: batch})[0]


class NcnnClassifier:
    """Logits from an NCNN export (``model.ncnn.param`` / ``.bin``; blobs
    ``in0`` / ``out0``, one image per call)."""

    backend = "ncnn"

    def __init__(self, folder: str | Path, threads: int = 4, ncnn_module=None) -> None:
        ncnn = ncnn_module
        if ncnn is None:
            import ncnn                                # noqa: F811
        self.path = Path(folder)
        self._ncnn = ncnn
        self._net = ncnn.Net()
        self._net.opt.num_threads = max(1, int(threads))
        self._net.opt.use_vulkan_compute = False
        self._net.load_param(str(self.path / "model.ncnn.param"))
        self._net.load_model(str(self.path / "model.ncnn.bin"))

    def logits(self, batch: np.ndarray) -> np.ndarray:
        rows = []
        for image in batch:
            extractor = self._net.create_extractor()
            extractor.input("in0", self._ncnn.Mat(np.ascontiguousarray(image)))
            _, out = extractor.extract("out0")
            rows.append(np.asarray(out, dtype=np.float32).reshape(-1))
        return np.stack(rows)


def softmax(logits: np.ndarray, temperature: float = 1.0) -> np.ndarray:
    z = np.asarray(logits, dtype=np.float64) / max(float(temperature), 1e-6)
    z -= z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return (e / e.sum(axis=1, keepdims=True)).astype(np.float32)


__all__ = ["IMAGENET_MEAN", "IMAGENET_STD", "NcnnClassifier", "OnnxClassifier",
           "preprocess", "softmax"]
