"""Benchmark the live configuration: what the dashboard would actually show.

The published latency used to come from the evaluation harness, which stubs
identity and audio; it overstated the live rate about 10x (CLAUDE.md section 5).
This drives the real ``Pipeline.run_live`` - capture thread, inference thread,
preview - with the real identity engine and the enrolled gallery, and a video
file standing in for the webcam at its native frame rate. It never opens a
camera, never records audio, and every side effect is stubbed: no evidence,
incident rows, SMS, siren or dispatch, and the event log goes to a temporary
folder.

Reports the preview rate (what the operator sees), the AI rate, frame age
(capture to decision) and per-stage p50/p95.

Usage::

    python tools/bench_live.py                       # 7.mov from $SENTRIX_CUSTOM_VIDEOS
    python tools/bench_live.py --video clip.mp4 --seconds 60 --json bench.json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from sentrix.io.sources import FrameSource  # noqa: E402


class VideoCamera(FrameSource):
    """A video file played like a webcam: a reader thread at the file's rate."""

    def __init__(self, path: Path, fps: float | None = None) -> None:
        import cv2

        self.name = f"bench:{path.name}"
        self._cap = cv2.VideoCapture(str(path))
        if not self._cap.isOpened():
            raise SystemExit(f"cannot open {path}")
        self.fps = fps or float(self._cap.get(cv2.CAP_PROP_FPS) or 30.0)
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self._seq, self._stamp = 0, 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        import cv2

        period = 1.0 / self.fps
        next_at = time.monotonic()
        while not self._stop.is_set():
            ok, frame = self._cap.read()
            if not ok:
                self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                continue
            if frame.shape[1] > 960:           # a webcam delivers 640x480
                scale = 640 / frame.shape[1]
                frame = cv2.resize(frame, (640, int(frame.shape[0] * scale)))
            next_at += period
            delay = next_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            with self._lock:
                self._frame, self._seq, self._stamp = frame, self._seq + 1, time.monotonic()

    def read(self):
        with self._lock:
            return (self._frame is not None), (None if self._frame is None
                                               else self._frame.copy())

    def latest(self, after_seq: int = -1):
        with self._lock:
            if self._frame is None or self._seq == after_seq:
                return None, self._seq, self._stamp
            return self._frame.copy(), self._seq, self._stamp

    @property
    def online(self) -> bool:
        return True

    def release(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._cap.release()


def percentile(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    videos = Path(os.getenv("SENTRIX_CUSTOM_VIDEOS", r"C:\Users\PC\OneDrive\Desktop\Videos"))
    parser.add_argument("--video", type=Path, default=videos / "7.mov")
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--warmup", type=float, default=5.0,
                        help="seconds discarded while models warm up")
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    from sentrix.config import settings
    from sentrix.pipeline.engine import Pipeline
    from sentrix.pipeline.events import EventLog
    from sentrix.pipeline.state import state
    from sentrix.utils.logging import telemetry

    def noop(*_a, **_k):
        return None

    camera = VideoCamera(args.video)
    pipeline = Pipeline(source=camera, enable_audio=False, event_source="bench")
    pipeline._act = noop                 # snapshots, evidence, SMS, siren, dispatch
    pipeline._submit = noop              # incident rows and other background writes
    scratch = Path(tempfile.mkdtemp(prefix="sentrix_bench_"))
    pipeline.events = EventLog(directory=scratch, source="bench")

    ticks: list[float] = []
    ages: list[float] = []
    process = pipeline.process

    def timed_process(frame, now=None, label=None):
        started = time.perf_counter()
        out = process(frame, now=now, label=label)
        ticks.append(time.perf_counter() - started)
        ages.append(time.monotonic() - now)
        return out

    pipeline.process = timed_process
    stop = threading.Event()
    loop = threading.Thread(target=pipeline.run_live, args=(stop,), daemon=True)
    loop.start()

    time.sleep(args.warmup)
    ticks.clear()
    ages.clear()
    for samples in telemetry._latencies.values():
        samples.clear()
    seq0, t0 = pipeline.preview_seq, time.monotonic()
    time.sleep(args.seconds)
    seq1, t1 = pipeline.preview_seq, time.monotonic()
    stop.set()
    loop.join(timeout=10.0)
    camera.release()
    pipeline.shutdown()

    elapsed = t1 - t0
    stages = {}
    for component, samples in telemetry._latencies.items():
        values = [s * 1000 for s in samples]
        if values and component != "pipeline":
            stages[component] = {"p50_ms": round(statistics.median(values), 1),
                                 "p95_ms": round(percentile(values, 0.95), 1),
                                 "n": len(values)}
    live = state.get()
    report = {
        "video": str(args.video), "source_fps": round(camera.fps, 1),
        "seconds": round(elapsed, 1),
        "inference_fps_cap": settings.pipeline.inference_fps,
        "preview_fps": round((seq1 - seq0) / elapsed, 1),
        "ai_hz": round(len(ticks) / elapsed, 2),
        "tick_ms": {"p50": round(1000 * statistics.median(ticks), 1) if ticks else None,
                    "p95": round(1000 * percentile(ticks, 0.95), 1) if ticks else None},
        "frame_age_ms": {"p50": round(1000 * statistics.median(ages), 1) if ages else None,
                         "p95": round(1000 * percentile(ages, 0.95), 1) if ages else None},
        "faces_last_tick": live.get("person_count"),
        "identity_backend": pipeline.identity.backend,
        "scene_model": pipeline.scene.weights_name,
        "stages": dict(sorted(stages.items(), key=lambda kv: -kv[1]["p50_ms"])),
    }

    print(f"\n{args.video.name} at {report['source_fps']} fps for {report['seconds']} s "
          f"(identity {report['identity_backend']}, scene {report['scene_model']})")
    print(f"  preview   {report['preview_fps']:6.1f} fps   (what the operator sees)")
    print(f"  AI        {report['ai_hz']:6.2f} Hz    (cap {settings.pipeline.inference_fps:g})")
    print(f"  tick      p50 {report['tick_ms']['p50']} ms   p95 {report['tick_ms']['p95']} ms")
    print(f"  frame age p50 {report['frame_age_ms']['p50']} ms   p95 {report['frame_age_ms']['p95']} ms")
    print("  stages (p50 / p95 ms):")
    for name, s in report["stages"].items():
        print(f"    {name:<18} {s['p50_ms']:8.1f} {s['p95_ms']:8.1f}   n={s['n']}")
    if args.json:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
