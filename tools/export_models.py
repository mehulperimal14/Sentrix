"""Export the shipped models for the Pi runtimes, and check they still agree.

On a Raspberry Pi 5 the same YOLO is about 5x faster in NCNN than in PyTorch
(docs/8_RASPBERRY_PI5_INTEGRATION.md §2), so the clip engine and the fast lane run exported heads
(``SENTRIX_RUNTIME=onnx`` / ``ncnn``). This tool writes them to
``models/exported/`` (never committed) under the names ``sentrix.runtime``
looks for, and checks each one against PyTorch on real frames:

* **raw parity** - the network's output on the same letterboxed tensor,
  PyTorch vs the export (scores and box coordinates, max absolute error);
* **decoded parity** - the runtime head's boxes against ultralytics'
  ``predict`` on the ``.pt`` weights (which letterboxes to a minimal
  rectangle, so a box may shift by a pixel; reported, not a gate);
* the scene classifier: logits on the same tensor, and probabilities through
  each runtime's own preprocessing.

Frames come from the custom dataset's train and val videos only (never the
test video ``wa02``). ``models/exported/manifest.json`` records every export:
source weights and their SHA-256, input size, format, file size and parity.

Exports run offline: ultralytics' auto-install and the Hugging Face hub are
switched off, so a missing package is reported instead of being downloaded.
NCNN needs the ``ncnn`` and ``pnnx`` packages (``pip install ncnn pnnx``);
without them the NCNN exports are skipped and say so.

Usage::

    python tools/export_models.py                     # YOLO heads -> ONNX @ 320, 640
    python tools/export_models.py --scene             # + the scene classifier -> ONNX
    python tools/export_models.py --ncnn              # + NCNN exports
    python tools/export_models.py --check-only        # parity of existing exports
    python tools/export_models.py --sizes 320 --heads weapon,fire
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: Raw-output tolerances: sigmoid scores, and box coordinates in input pixels.
SCORE_TOLERANCE = 2e-3
BOX_TOLERANCE = 0.5
PROB_TOLERANCE = 2e-3


def _offline() -> None:
    os.environ.setdefault("YOLO_OFFLINE", "1")
    os.environ.setdefault("YOLO_AUTOINSTALL", "False")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sample_frames(count: int) -> list:
    """Deterministic frames from the train and val videos (never wa02)."""
    import cv2

    from sentrix.evaluation.custom_dataset import load_frames
    frames = [f for f in load_frames(("train", "val")) if f.video != "wa02"]
    frames.sort(key=lambda f: (f.label, f.video, f.timestamp))
    step = max(1, len(frames) // max(count, 1))
    images = []
    for record in frames[::step][:count]:
        image = cv2.imread(str(record.path))
        if image is not None:
            images.append(image)
    return images


def head_weights(roles: list[str]) -> dict[str, Path]:
    from sentrix.perception.detector import (_FIRE_WEIGHTS, _PERSON_WEIGHTS,
                                             _WEAPON_WEIGHTS, _resolve)
    candidates = {"person": _PERSON_WEIGHTS, "weapon": _WEAPON_WEIGHTS, "fire": _FIRE_WEIGHTS}
    out = {}
    for role in roles:
        path = _resolve(candidates[role])
        if path is None:
            print(f"  {role}: no weights found - skipped")
        else:
            out[role] = path
    return out


# ── YOLO ──────────────────────────────────────────────────────────────────────
def export_yolo(weights: Path, size: int, fmt: str, directory: Path) -> Path:
    """Export through ultralytics in a scratch folder, then move into place."""
    from ultralytics import YOLO

    from sentrix.runtime import exported_path
    target = exported_path(weights, size, "ncnn" if fmt == "ncnn" else "onnx", directory)
    with tempfile.TemporaryDirectory(prefix="sentrix_export_") as scratch:
        local = Path(scratch) / weights.name
        shutil.copy2(weights, local)
        kwargs = {"format": fmt, "imgsz": size}
        if fmt == "onnx":
            kwargs.update(simplify=False, dynamic=False)
        produced = Path(YOLO(str(local)).export(**kwargs))
        if target.exists():
            shutil.rmtree(target) if target.is_dir() else target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(produced), str(target))
    return target


def torch_raw(weights: Path, blob):
    """The .pt network's raw head output on a letterboxed tensor."""
    import torch
    from ultralytics import YOLO

    net = YOLO(str(weights)).model.float().eval()
    with torch.no_grad():
        out = net(torch.from_numpy(blob))
    out = out[0] if isinstance(out, (list, tuple)) else out
    return out.cpu().numpy()


def match_boxes(a: list, b: list, min_iou: float = 0.5) -> tuple[int, float, float]:
    """Greedy same-class IoU matching: (matched, worst IoU, worst conf diff)."""
    from sentrix.runtime.tracking import iou
    free = list(b)
    matched, worst_iou, worst_conf = 0, 1.0, 0.0
    for box in sorted(a, key=lambda x: -x.conf):
        best, best_iou = None, min_iou
        for other in free:
            if other.cls != box.cls:
                continue
            score = iou(box.bbox, other.bbox)
            if score >= best_iou:
                best, best_iou = other, score
        if best is not None:
            free.remove(best)
            matched += 1
            worst_iou = min(worst_iou, best_iou)
            worst_conf = max(worst_conf, abs(best.conf - box.conf))
    return matched, worst_iou, worst_conf


def check_yolo(weights: Path, export: Path, backend: str, size: int, frames,
               threads: int) -> dict:
    import numpy as np

    from sentrix.runtime import yolo
    head = (yolo.OnnxHead(export, threads=threads) if backend == "onnx"
            else yolo.NcnnHead(export, threads=threads))
    reference = yolo.TorchHead(weights, size)
    score_err = box_err = 0.0
    boxes_ref = boxes_rt = matched = 0
    worst_iou, worst_conf = 1.0, 0.0
    for frame in frames:
        blob, _, _ = yolo.letterbox(frame, head.imgsz)
        ref, got = torch_raw(weights, blob), head.raw(blob)
        score_err = max(score_err, float(np.abs(ref[:, 4:] - got[:, 4:]).max()))
        box_err = max(box_err, float(np.abs(ref[:, :4] - got[:, :4]).max()))
        a = reference.predict(frame, conf=0.25)
        b = head.predict(frame, conf=0.25)
        m, wi, wc = match_boxes(a, b)
        boxes_ref, boxes_rt, matched = boxes_ref + len(a), boxes_rt + len(b), matched + m
        if m:
            worst_iou, worst_conf = min(worst_iou, wi), max(worst_conf, wc)
    ok = score_err <= SCORE_TOLERANCE and box_err <= BOX_TOLERANCE
    return {"frames": len(frames), "raw_score_max_abs": round(score_err, 6),
            "raw_box_max_abs_px": round(box_err, 4), "parity_ok": ok,
            "decoded": {"torch_boxes": boxes_ref, "runtime_boxes": boxes_rt,
                        "matched": matched, "worst_iou": round(worst_iou, 4),
                        "worst_conf_diff": round(worst_conf, 4),
                        "note": "ultralytics predict letterboxes .pt to a minimal "
                                "rectangle, exports to a square"}}


# ── scene ─────────────────────────────────────────────────────────────────────
def export_scene(directory: Path, frames, threads: int) -> dict:
    """The installed scene classifier -> ONNX with its metadata, and parity."""
    import numpy as np
    import torch

    from sentrix.perception.scene import SceneEngine, _resolve
    from sentrix.runtime.classifier import OnnxClassifier, preprocess, softmax

    weights = _resolve()
    if weights is None:
        return {"skipped": "no scene classifier weights"}
    engine = SceneEngine(weights, runtime="torch")
    if not engine.available:
        return {"skipped": f"scene classifier {weights.name} did not load"}
    ckpt = torch.load(str(weights), map_location="cpu", weights_only=False)
    size = int(ckpt.get("image_size", 224))
    target = directory / f"{weights.stem}.onnx"
    target.parent.mkdir(parents=True, exist_ok=True)
    net = engine._model.float().eval()
    dummy = torch.zeros(1, 3, size, size)
    started = time.time()
    torch.onnx.export(net, (dummy,), str(target), input_names=["images"],
                      output_names=["logits"], opset_version=17, dynamo=False,
                      dynamic_axes={"images": {0: "batch"}, "logits": {0: "batch"}})
    seconds = time.time() - started
    meta = {k: ckpt.get(k) for k in ("architecture", "classes", "image_size",
                                      "normalization", "temperature", "thresholds")}
    source_meta = weights.parent / f"{weights.stem}_metadata.json"
    if source_meta.exists():
        meta = {**json.loads(source_meta.read_text(encoding="utf-8")), **{
            k: v for k, v in meta.items() if v is not None}}
    meta.update(exported_from=weights.name, source_sha256=sha256(weights),
                input_format="RGB, float32, NCHW, ImageNet-normalised, squashed resize "
                             "(Pillow bilinear)")
    (directory / f"{weights.stem}_metadata.json").write_text(
        json.dumps(meta, indent=2, default=str), encoding="utf-8")

    model = OnnxClassifier(target, threads)
    norm = ckpt.get("normalization") or {}
    logit_err = prob_err = 0.0
    for frame in frames:
        batch = preprocess([frame], size, norm.get("mean", (0.485, 0.456, 0.406)),
                           norm.get("std", (0.229, 0.224, 0.225)))
        with torch.no_grad():
            ref = net(torch.from_numpy(batch)).numpy()
        got = model.logits(batch)
        logit_err = max(logit_err, float(np.abs(ref - got).max()))
        probs_torch = engine.score_frames([frame])
        probs_onnx = softmax(got, engine.temperature)
        prob_err = max(prob_err, float(np.abs(probs_torch - probs_onnx).max()))
    return {"role": "scene", "source": str(weights.relative_to(ROOT)),
            "source_sha256": sha256(weights), "imgsz": size, "format": "onnx",
            "file": str(target.relative_to(ROOT)), "bytes": target.stat().st_size,
            "export_seconds": round(seconds, 1),
            "parity": {"frames": len(frames), "logit_max_abs": round(logit_err, 6),
                       "prob_max_abs_end_to_end": round(prob_err, 6),
                       "parity_ok": prob_err <= PROB_TOLERANCE,
                       "note": "end to end: torchvision transform + torch vs Pillow "
                               "preprocessing + onnxruntime"}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--heads", default="person,weapon,fire")
    parser.add_argument("--sizes", default="320,640")
    parser.add_argument("--ncnn", action="store_true", help="also export NCNN")
    parser.add_argument("--scene", action="store_true", help="also export the scene model")
    parser.add_argument("--check-only", action="store_true",
                        help="parity of existing exports; export nothing")
    parser.add_argument("--frames", type=int, default=16, help="frames for the parity check")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--out", type=Path, default=None,
                        help="export folder (default: models/exported)")
    args = parser.parse_args(argv)
    _offline()

    from sentrix.runtime import EXPORT_DIR, configure_threads, exported_path
    directory = args.out or EXPORT_DIR
    configure_threads(args.threads)
    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    roles = [r.strip() for r in args.heads.split(",") if r.strip()]
    formats = ["onnx"] + (["ncnn"] if args.ncnn else [])
    frames = sample_frames(args.frames)
    print(f"parity frames: {len(frames)} (train/val videos)")

    manifest_path = directory / "manifest.json"
    manifest = {"exports": {}}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    skipped: list[str] = []
    failed = False
    for role, weights in head_weights(roles).items():
        for fmt in formats:
            if fmt == "ncnn" and not all(importlib.util.find_spec(m) for m in ("ncnn", "pnnx")):
                skipped.append(f"{role} ncnn: the ncnn and pnnx packages are not installed")
                continue
            for size in sizes:
                target = exported_path(weights, size, fmt, directory)
                started = time.time()
                if not args.check_only:
                    target = export_yolo(weights, size, fmt, directory)
                elif not target.exists():
                    skipped.append(f"{target.name}: not exported")
                    continue
                parity = check_yolo(weights, target, fmt, size, frames, args.threads)
                failed |= not parity["parity_ok"]
                size_bytes = (sum(p.stat().st_size for p in target.rglob("*") if p.is_file())
                              if target.is_dir() else target.stat().st_size)
                manifest["exports"][target.name] = {
                    "role": role, "source": str(weights.relative_to(ROOT)),
                    "source_sha256": sha256(weights), "imgsz": size, "format": fmt,
                    "file": str(target.relative_to(ROOT)), "bytes": size_bytes,
                    "seconds": round(time.time() - started, 1), "parity": parity}
                print(f"  {target.name:44} raw score err {parity['raw_score_max_abs']:.2e}  "
                      f"box err {parity['raw_box_max_abs_px']:.3f} px  decoded "
                      f"{parity['decoded']['matched']}/{parity['decoded']['torch_boxes']} "
                      f"{'OK' if parity['parity_ok'] else 'MISMATCH'}")
    if args.scene:
        if args.check_only:
            skipped.append("scene: --check-only re-exports nothing")
        else:
            entry = export_scene(directory, frames, args.threads)
            if "skipped" in entry:
                skipped.append(f"scene: {entry['skipped']}")
            else:
                failed |= not entry["parity"]["parity_ok"]
                manifest["exports"][Path(entry["file"]).name] = entry
                print(f"  {Path(entry['file']).name:44} logit err "
                      f"{entry['parity']['logit_max_abs']:.2e}  prob err "
                      f"{entry['parity']['prob_max_abs_end_to_end']:.2e} "
                      f"{'OK' if entry['parity']['parity_ok'] else 'MISMATCH'}")
    import onnx
    import onnxruntime
    import torch
    import ultralytics
    from tools.eval_custom_frames import git_describe
    manifest.update(updated_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                    commit=git_describe()[0], skipped=skipped,
                    versions={"ultralytics": ultralytics.__version__,
                              "torch": torch.__version__, "onnx": onnx.__version__,
                              "onnxruntime": onnxruntime.__version__},
                    tolerances={"score": SCORE_TOLERANCE, "box_px": BOX_TOLERANCE,
                                "prob": PROB_TOLERANCE})
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    for line in skipped:
        print(f"  skipped: {line}")
    print(f"written: {manifest_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
