"""Export a scene-classifier checkpoint to ONNX, and prove the export is the model.

The Raspberry Pi runs its models in ONNX Runtime or NCNN, not PyTorch
(HARDWARE.md §2: the same network is several times faster there). This writes
``<checkpoint stem>.onnx`` next to the checkpoint:

* input ``input``: float32 ``(N, 3, S, S)``, RGB, normalised with the
  checkpoint's mean/std after a squashed resize to ``S`` (``image_size``) -
  exactly what :class:`sentrix.perception.scene.SceneEngine` feeds PyTorch;
* output ``logits``: float32 ``(N, classes)``; probabilities are
  ``softmax(logits / temperature)``, as in the engine;
* the checkpoint's self-description (architecture, classes, image size,
  normalisation, temperature, thresholds and the SHA-256 of the source
  checkpoint) in the ONNX ``metadata_props``, so the file alone is enough.

Then it checks the export on real frames (train split, never ``wa02``): the
largest probability difference against PyTorch and the label agreement, and
how much a cv2 resize (a runtime without PIL) moves the probabilities. The
result goes into an ``onnx`` block of ``<stem>_metadata.json``.

Runs in the app environment (it needs ``onnx`` and ``onnxruntime``)::

    .venv/Scripts/python training/custom/export_scene_onnx.py \\
        models/v5_slim/scene_classifier_effb0_v5.pt
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sentrix.evaluation.custom_dataset import load_frames  # noqa: E402
from sentrix.perception.scene import (PRIORITY, build_scene_model,  # noqa: E402
                                      decide_label, merge_delta)

#: Opset 17 is supported by every ONNX Runtime since 1.11 and by NCNN's
#: pnnx/onnx2ncnn converters.
OPSET = 17
DEFAULT_MEAN, DEFAULT_STD = [0.485, 0.456, 0.406], [0.229, 0.224, 0.225]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_checkpoint(path: Path) -> tuple[torch.nn.Module, dict]:
    """The network and its description, loaded the way ``SceneEngine`` does."""
    ckpt = torch.load(str(path), map_location="cpu", weights_only=False)
    classes = [str(c) for c in ckpt["classes"]]
    state = {k: v.float() for k, v in ckpt["model_state_dict"].items()}
    delta = ckpt.get("storage") == "delta"
    net = build_scene_model(ckpt["architecture"], len(classes), pretrained=delta,
                            image_size=int(ckpt.get("image_size", 224)))
    if delta:
        merge_delta(net, state, ckpt["base_fingerprint"])
    else:
        net.load_state_dict(state)
    return net.eval(), ckpt


def describe(ckpt: dict, source: Path) -> dict[str, str]:
    """ONNX ``metadata_props``: string values, JSON where structured."""
    norm = ckpt.get("normalization") or {"mean": DEFAULT_MEAN, "std": DEFAULT_STD}
    return {
        "sentrix_model": "scene_classifier",
        "architecture": str(ckpt["architecture"]),
        "classes": json.dumps(list(ckpt["classes"])),
        "image_size": str(int(ckpt.get("image_size", 224))),
        "normalization": json.dumps(norm),
        "input_format": str(ckpt.get("input_format", "")),
        "output": "logits; probabilities = softmax(logits / temperature)",
        "temperature": repr(float(ckpt.get("temperature", 1.0) or 1.0)),
        "thresholds": json.dumps(ckpt.get("thresholds") or {}),
        "source_checkpoint": source.name,
        "source_sha256": sha256(source),
    }


def export(net: torch.nn.Module, ckpt: dict, source: Path, out: Path,
           opset: int = OPSET) -> None:
    import onnx

    size = int(ckpt.get("image_size", 224))
    dummy = torch.zeros(1, 3, size, size)
    # The TorchScript exporter: deterministic, no onnxscript dependency, and
    # its graphs convert to NCNN. dynamo=True would need onnxscript.
    torch.onnx.export(net, (dummy,), str(out), dynamo=False, opset_version=opset,
                      input_names=["input"], output_names=["logits"],
                      dynamic_axes={"input": {0: "batch"}, "logits": {0: "batch"}},
                      do_constant_folding=True)
    model = onnx.load(str(out))
    onnx.checker.check_model(model)
    del model.metadata_props[:]
    for key, value in describe(ckpt, source).items():
        entry = model.metadata_props.add()
        entry.key, entry.value = key, value
    onnx.save(model, str(out))


def preprocess(images: list[np.ndarray], ckpt: dict, resize: str = "pil") -> np.ndarray:
    """RGB uint8 images -> the model input batch.

    ``pil`` is the engine's path (torchvision ``Resize`` on a PIL image, i.e.
    antialiased bilinear); ``cv2`` is what a runtime without PIL would do
    (``INTER_AREA`` when shrinking), to measure how much that costs.
    """
    import cv2
    from PIL import Image

    size = int(ckpt.get("image_size", 224))
    norm = ckpt.get("normalization") or {}
    mean = np.asarray(norm.get("mean", DEFAULT_MEAN), dtype=np.float32)
    std = np.asarray(norm.get("std", DEFAULT_STD), dtype=np.float32)
    batch = []
    for rgb in images:
        if resize == "pil":
            small = np.asarray(Image.fromarray(rgb).resize((size, size), Image.BILINEAR))
        else:
            small = cv2.resize(rgb, (size, size), interpolation=cv2.INTER_AREA)
        x = (small.astype(np.float32) / 255.0 - mean) / std
        batch.append(x.transpose(2, 0, 1))
    return np.ascontiguousarray(np.stack(batch), dtype=np.float32)


def softmax(logits: np.ndarray, temperature: float) -> np.ndarray:
    z = logits / temperature
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def parity(net: torch.nn.Module, ckpt: dict, onnx_path: Path, n_frames: int,
           threads: int = 4) -> dict:
    """PyTorch vs ONNX Runtime on real train-split frames."""
    import onnxruntime as ort
    from PIL import Image

    frames = sorted(load_frames(("train",)), key=lambda f: f.path.name)
    picked = frames[:: max(1, len(frames) // n_frames)][:n_frames]
    images = [np.asarray(Image.open(f.path).convert("RGB")) for f in picked]
    x = preprocess(images, ckpt, "pil")
    temperature = float(ckpt.get("temperature", 1.0) or 1.0)
    classes = list(ckpt["classes"])
    thresholds = {k: float(v) for k, v in (ckpt.get("thresholds") or {}).items()
                  if k in PRIORITY}

    with torch.inference_mode():
        p_torch = softmax(net(torch.from_numpy(x)).numpy(), temperature)
    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    session = ort.InferenceSession(str(onnx_path), options,
                                   providers=["CPUExecutionProvider"])
    p_onnx = softmax(session.run(["logits"], {"input": x})[0], temperature)
    p_cv2 = softmax(session.run(["logits"], {"input": preprocess(images, ckpt, "cv2")})[0],
                    temperature)

    def labels(probs):
        return [decide_label(dict(zip(classes, map(float, p))), thresholds) for p in probs]

    return {"frames": len(images), "split": "train",
            "max_abs_prob_diff": round(float(np.abs(p_torch - p_onnx).max()), 7),
            "label_agreement": float(np.mean([a == b for a, b in
                                              zip(labels(p_torch), labels(p_onnx))])),
            "cv2_resize_max_abs_prob_diff": round(float(np.abs(p_onnx - p_cv2).max()), 4),
            "cv2_resize_label_agreement": round(float(np.mean(
                [a == b for a, b in zip(labels(p_onnx), labels(p_cv2))])), 4)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("--opset", type=int, default=OPSET)
    parser.add_argument("--frames", type=int, default=64,
                        help="train-split frames for the PyTorch/ONNX comparison")
    parser.add_argument("--no-metadata", action="store_true",
                        help="do not record the export in <stem>_metadata.json")
    args = parser.parse_args(argv)

    source = args.checkpoint.resolve()
    out = source.with_suffix(".onnx")
    net, ckpt = load_checkpoint(source)
    export(net, ckpt, source, out, args.opset)
    check = parity(net, ckpt, out, args.frames)
    record = {"file": out.name, "bytes": out.stat().st_size, "sha256": sha256(out),
              "opset": args.opset, "exporter": f"torch {torch.__version__} (TorchScript)",
              "input": "input: float32 (N, 3, S, S), normalised RGB",
              "output": "logits: float32 (N, classes)", "parity": check}
    print(f"{out.name}: {record['bytes'] / 1e6:.1f} MB, opset {args.opset}; "
          f"max |p_torch - p_onnx| = {check['max_abs_prob_diff']:.2e} on "
          f"{check['frames']} frames, labels agree {check['label_agreement']:.0%}; "
          f"cv2 resize moves p by up to {check['cv2_resize_max_abs_prob_diff']:.3f}")
    if check["max_abs_prob_diff"] > 1e-3 or check["label_agreement"] < 1.0:
        print("export does not reproduce the checkpoint", file=sys.stderr)
        return 1
    meta_path = source.with_name(source.stem + "_metadata.json")
    if not args.no_metadata and meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["onnx"] = record
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        print(f"recorded in {meta_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
