"""Time each detection head on each installed runtime (onnx, ncnn, torch).

The clip engine runs three YOLO heads per keyframe; on the Pi their runtime
decides how old a decision is (docs/8 T1). This loads the heads the product
would load (``sentrix.runtime.build_head`` on the resolved weights and their
exports in ``models/exported``) and times ``predict`` on frames of a custom
video, after a warm-up. Nothing else runs: no camera, no pipeline.

Usage::

    python tools/bench_heads.py [--size 320] [--threads 4] [--runs 30]

Prints a table and writes ``--json`` if given.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def frames(count: int):
    import cv2
    from sentrix.evaluation.custom_dataset import videos_for
    cap = cv2.VideoCapture(str(videos_for("gun")[0].path()))
    out = []
    while len(out) < count:
        ok, frame = cap.read()
        if not ok:
            break
        out.append(cv2.resize(frame, (640, 480)))
    cap.release()
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--size", type=int, default=320)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--backends", default="onnx,ncnn")
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    from sentrix.perception.detector import (_FIRE_WEIGHTS, _PERSON_WEIGHTS, _WEAPON_WEIGHTS,
                                             _resolve)
    from sentrix.runtime import build_head, installed
    images = frames(args.runs)
    report = {"size": args.size, "threads": args.threads, "runs": len(images), "heads": {}}
    for role, cands in (("person", _PERSON_WEIGHTS), ("weapon", _WEAPON_WEIGHTS),
                        ("fire", _FIRE_WEIGHTS)):
        weights = _resolve(cands)
        report["heads"][role] = {"weights": weights.name if weights else None}
        for backend in args.backends.split(","):
            if not installed(backend):
                report["heads"][role][backend] = "not installed"
                continue
            head = build_head(weights, args.size, backend, args.threads)
            kind = type(head).__name__ if head is not None else None
            if head is None or (backend != "torch" and kind == "TorchHead"):
                report["heads"][role][backend] = "no export"
                continue
            for image in images[:3]:
                head.predict(image, conf=0.25)
            times = []
            for image in images:
                start = time.perf_counter()
                head.predict(image, conf=0.25)
                times.append((time.perf_counter() - start) * 1000)
            report["heads"][role][backend] = {"p50_ms": round(statistics.median(times), 1),
                                              "p95_ms": round(sorted(times)[int(0.95 * (len(times) - 1))], 1)}
    for role, d in report["heads"].items():
        print(f"{role:7s} {d.pop('weights')!s:34s} " +
              "  ".join(f"{b}: {v if isinstance(v, str) else str(v['p50_ms']) + ' ms'}"
                        for b, v in d.items()))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
