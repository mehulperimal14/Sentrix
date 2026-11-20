"""ONNX vs NCNN (ncnn's default fp16 math, and fp32) for the Pi's three 320 px heads.

Run on the Pi from stdin: ``python - parity`` (service may run) or
``python - timing`` (stop the service first). Train + val videos only; wa02 is
never read. Nothing is written.
"""
import os
import statistics
import sys
import time

os.environ.setdefault("SENTRIX_FIRE_WEIGHTS", "models/v4_custom/fire_detector_custom_v4.pt")
sys.path.insert(0, os.getcwd())

import cv2
import numpy as np

from sentrix.evaluation.custom_dataset import videos_for
from sentrix.perception.detector import _FIRE_WEIGHTS, _PERSON_WEIGHTS, _WEAPON_WEIGHTS, _resolve
from sentrix.runtime import build_head
from sentrix.runtime.yolo import decode, letterbox

MODE = sys.argv[1] if len(sys.argv) > 1 else "parity"
VIDEOS = ("gun", "vid04", "mmgun", "mmknife", "vid07", "nvroom2", "vid06")
PER_VIDEO = 15 if MODE == "parity" else 5
BARS = (0.25, 0.35, 0.5)


def frames():
    out = []
    for name in VIDEOS:
        assert name != "wa02"
        cap = cv2.VideoCapture(str(videos_for(name)[0].path()))
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
        for i in np.linspace(0, total - 1, PER_VIDEO).astype(int):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
            ok, frame = cap.read()
            if ok:
                out.append(cv2.resize(frame, (1280, 720)))
        cap.release()
    return out


def fp32(head):
    import ncnn
    net = ncnn.Net()
    net.opt.num_threads = head._net.opt.num_threads
    net.opt.use_vulkan_compute = False
    net.opt.use_fp16_packed = False
    net.opt.use_fp16_storage = False
    net.opt.use_fp16_arithmetic = False
    net.load_param(str(head.path / "model.ncnn.param"))
    net.load_model(str(head.path / "model.ncnn.bin"))
    head._net = net
    return head


images = frames()
print(f"mode {MODE}: {len(images)} frames from {', '.join(VIDEOS)}", flush=True)
for role, cands in (("person", _PERSON_WEIGHTS), ("weapon", _WEAPON_WEIGHTS), ("fire", _FIRE_WEIGHTS)):
    weights = _resolve(cands)
    heads = {"onnx": build_head(weights, 320, "onnx", 4),
             "ncnn16": build_head(weights, 320, "ncnn", 4),
             "ncnn32": fp32(build_head(weights, 320, "ncnn", 4))}
    if MODE == "timing":
        line = []
        for key, head in heads.items():
            for image in images[:3]:
                head.predict(image, conf=0.25)
            times = []
            for image in images:
                start = time.perf_counter()
                head.predict(image, conf=0.25)
                times.append((time.perf_counter() - start) * 1000)
            line.append(f"{key} {statistics.median(times):.1f} ms")
        print(f"{role:6s} {weights.name:32s} " + "  ".join(line), flush=True)
        continue
    stats = {k: {"raw": [], "top": 0.0, "flips": {b: 0 for b in BARS}, "boxes": 0} for k in ("ncnn16", "ncnn32")}
    ref_boxes = 0
    for image in images:
        blob, ratio, pad = letterbox(image, 320)
        outs = {k: h.raw(blob) for k, h in heads.items()}
        names = heads["onnx"].names

        def labels(out, bar):
            return decode(out, conf=bar, iou=0.45, ratio=ratio, pad=pad, shape=image.shape,
                          names=names, classes=None, max_det=50)

        ref = {bar: labels(outs["onnx"], bar) for bar in BARS}
        ref_boxes += len(ref[0.25])
        for key in ("ncnn16", "ncnn32"):
            a, b = outs["onnx"][0][4:], outs[key][0][4:]
            live = np.maximum(a, b) >= 0.1
            if live.any():
                stats[key]["raw"].append(float(np.abs(a - b)[live].max()))
            for bar in BARS:
                got = labels(outs[key], bar)
                stats[key]["flips"][bar] += {x.label for x in got} != {x.label for x in ref[bar]}
                if bar == 0.25:
                    stats[key]["boxes"] += len(got)
                    for label in {x.label for x in got} & {x.label for x in ref[bar]}:
                        sa = max(x.conf for x in ref[bar] if x.label == label)
                        sb = max(x.conf for x in got if x.label == label)
                        stats[key]["top"] = max(stats[key]["top"], abs(sa - sb))
    for key, s in stats.items():
        raw = sorted(s["raw"]) or [0.0]
        print(f"{role:6s} {key:6s} score err (anchors >= 0.1) median {statistics.median(raw):.4f} "
              f"max {raw[-1]:.4f}  top-score err {s['top']:.4f}  boxes@0.25 onnx {ref_boxes} {key} {s['boxes']}  "
              + "  ".join(f"frames differing@{b} {s['flips'][b]}" for b in BARS), flush=True)
