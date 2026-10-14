"""Benchmark clip mode under the Pi 5's CPU budget (HARDWARE.md T1, Gate A).

Drives the real live loop - ``Pipeline.run_live`` with the clip strategy:
capture thread, preview, the identity worker, the clip engine on the
inference thread and (``--fast-lane``) the fast lane on its own - with a
video file standing in for the camera. The process is held to a 4-core
budget: torch, OpenCV and onnxruntime get ``--threads`` threads each, and
(with psutil) the process is pinned to that many logical CPUs, one per
physical core where the machine has SMT.

It never opens a camera or a microphone, every side effect is stubbed (no
evidence, incident rows, SMS, siren or dispatch), the event log goes to a
temporary folder, and it refuses to run with any background service or a
real SOS service (``services.assert_measurement_safe``).

Reports clip compute p50/p95 against the budget (``budget_fraction`` of the
hop: 1.6 s), clips dropped by the newest-clip-wins rule, per-stage p50/p95,
keyframes and detector calls per clip, the gate's open share, fast-lane pass
time and rate, the preview rate and frame age (clip end to decision).

Usage::

    python tools/bench_clip.py                                # vid07, 60 s, 4 threads
    python tools/bench_clip.py --fast-lane --json bench.json
    python tools/bench_clip.py --video clip.mp4 --seconds 120 --runtime onnx
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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def pick_cpus(count: int) -> list[int] | None:
    """``count`` logical CPUs, one per physical core when SMT doubles them."""
    try:
        import psutil
    except ImportError:
        return None
    logical = psutil.cpu_count(logical=True) or 0
    physical = psutil.cpu_count(logical=False) or logical
    step = 2 if logical >= 2 * count and logical > physical else 1
    cpus = list(range(0, logical, step))[:count]
    return cpus if len(cpus) == count else None


def pin(cpus: list[int] | None) -> list[int] | None:
    if not cpus:
        return None
    try:
        import psutil
        psutil.Process().cpu_affinity(cpus)
        return psutil.Process().cpu_affinity()
    except Exception as exc:
        print(f"  cpu affinity not set: {exc}")
        return None


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(len(ordered) * q))], 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--video", type=Path, default=None,
                        help="video used as the camera (default: the bundled 7.mov)")
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--warmup", type=float, default=8.0,
                        help="seconds discarded while models warm up")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--no-affinity", action="store_true",
                        help="do not pin the process to --threads CPUs")
    parser.add_argument("--fast-lane", action="store_true")
    parser.add_argument("--runtime", choices=("torch", "onnx", "ncnn"), default=None,
                        help="model runtime (default: SENTRIX_RUNTIME)")
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    # Read once at config import, so set before anything imports sentrix.
    os.environ["SENTRIX_PIPELINE_MODE"] = "clip"
    os.environ["SENTRIX_CLIP_THREADS"] = str(args.threads)
    os.environ.setdefault("SENTRIX_FACE_ORT_THREADS", str(args.threads))
    if args.runtime:
        os.environ["SENTRIX_RUNTIME"] = args.runtime
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("YOLO_OFFLINE", "1")
    os.environ.setdefault("YOLO_AUTOINSTALL", "False")
    affinity = None if args.no_affinity else pin(pick_cpus(args.threads))

    from sentrix.config import settings
    from sentrix.evaluation.custom_dataset import videos_dir
    from sentrix.pipeline.clip import build_strategy
    from sentrix.pipeline.engine import Pipeline
    from sentrix.pipeline.events import EventLog
    from sentrix.services import assert_measurement_safe
    from tools.bench_live import VideoCamera

    def noop(*_a, **_k):
        return None

    video = args.video or videos_dir() / "7.mov"
    camera = VideoCamera(video)
    pipeline = Pipeline(source=camera, enable_audio=False, event_source="bench")
    pipeline._act = noop                 # snapshots, evidence, SMS, siren, dispatch
    pipeline._submit = noop              # incident rows, clip bundles, other writes
    pipeline._save_open_incident = noop  # and the synchronous save at shutdown
    assert_measurement_safe(pipeline)
    pipeline.events = EventLog(directory=Path(tempfile.mkdtemp(prefix="sentrix_bench_")),
                               source="bench")
    strategy = build_strategy(pipeline)
    runner = strategy.runner
    if args.fast_lane and not runner.enable_fast_lane(True):
        raise SystemExit("--fast-lane: no fast-lane head could be loaded")

    clips: list = []
    lane_ms: list[float] = []
    ages: list[float] = []
    recording = threading.Event()
    run_clip, run_fast = runner.run_clip, runner.run_fast_lane

    def timed_clip(end, label=None):
        out = run_clip(end, label)
        if recording.is_set():
            clips.append(out["clip"])
            ages.append((time.monotonic() - end) * 1000.0)
        return out

    def timed_fast(kept, label=None):
        began = time.perf_counter()
        out = run_fast(kept, label)
        if recording.is_set():
            lane_ms.append((time.perf_counter() - began) * 1000.0)
        return out

    runner.run_clip, runner.run_fast_lane = timed_clip, timed_fast
    stop = threading.Event()
    loop = threading.Thread(target=pipeline.run_live, args=(stop, strategy), daemon=True)
    loop.start()
    time.sleep(args.warmup)
    dropped0, claimed0 = runner.scheduler.dropped, runner.scheduler.clips
    seq0, t0 = pipeline.preview_seq, time.monotonic()
    recording.set()
    time.sleep(args.seconds)
    recording.clear()
    dropped1, claimed1 = runner.scheduler.dropped, runner.scheduler.clips
    seq1, t1 = pipeline.preview_seq, time.monotonic()
    stop.set()
    loop.join(timeout=15.0)
    camera.release()
    pipeline.shutdown()

    elapsed = t1 - t0
    budget_ms = 1000.0 * settings.clip.hop_seconds * settings.clip.budget_fraction
    compute = [c.compute_ms for c in clips]
    stages: dict[str, dict] = {}
    for name in sorted({n for c in clips for n in c.stages}):
        ran = [c.stages[name]["ms"] for c in clips if c.stages.get(name, {}).get("ran")]
        calls = [c.stages[name].get("calls", 0) for c in clips if name in c.stages]
        stages[name] = {"p50_ms": pct(ran, 0.5), "p95_ms": pct(ran, 0.95), "ran": len(ran),
                        "calls_per_clip": round(sum(calls) / max(len(clips), 1), 2)}
    dropped = dropped1 - dropped0
    analysed = claimed1 - claimed0
    p95 = pct(compute, 0.95)
    report = {
        "video": str(video), "source_fps": round(camera.fps, 1), "seconds": round(elapsed, 1),
        "threads": args.threads, "cpu_affinity": affinity,
        "runtime": settings.clip.runtime, "fast_lane": runner.fast_lane is not None,
        "window_s": settings.clip.window_seconds, "hop_s": settings.clip.hop_seconds,
        "preview_fps": round((seq1 - seq0) / elapsed, 1),
        "clips": len(clips), "clips_claimed": analysed, "clips_dropped": dropped,
        "dropped_fraction": round(dropped / max(analysed + dropped, 1), 4),
        "compute_ms": {"p50": pct(compute, 0.5), "p95": p95, "max": pct(compute, 1.0)},
        "budget_ms": round(budget_ms, 1),
        "within_budget": None if p95 is None else p95 <= budget_ms,
        "frame_age_ms": {"p50": pct(ages, 0.5), "p95": pct(ages, 0.95)},
        "gate_open_fraction": round(sum(bool(c.gate.get("open")) for c in clips)
                                    / max(len(clips), 1), 3),
        "keyframes_per_clip": round(statistics.mean(len(c.keyframes) for c in clips), 2)
        if clips else None,
        "persons_max": max((c.persons for c in clips), default=0),
        "stages": stages,
        "fast_lane_ms": {"p50": pct(lane_ms, 0.5), "p95": pct(lane_ms, 0.95),
                         "passes_per_s": round(len(lane_ms) / elapsed, 2)}
        if runner.fast_lane is not None else None,
        "identity": {"backend": pipeline.identity.backend,
                     "model": getattr(pipeline.identity, "model_name", None),
                     "det_size": settings.identity.det_size},
        "scene": {"model": pipeline.scene.weights_name,
                  "runtime": getattr(pipeline.scene, "runtime", "torch")},
        "detector": getattr(runner.engine.detector, "backends", "torch (ultralytics)"),
    }
    print(f"\n{video.name} at {report['source_fps']} fps for {report['seconds']} s, "
          f"{args.threads} threads, CPUs {affinity}, runtime {report['runtime']}")
    print(f"  preview   {report['preview_fps']:6.1f} fps")
    print(f"  clips     {len(clips)} analysed, {dropped} dropped "
          f"({100 * report['dropped_fraction']:.1f} %)")
    print(f"  compute   p50 {report['compute_ms']['p50']} ms  p95 {p95} ms  "
          f"(budget {budget_ms:.0f} ms: {'OK' if report['within_budget'] else 'OVER'})")
    print(f"  frame age p50 {report['frame_age_ms']['p50']} ms  "
          f"p95 {report['frame_age_ms']['p95']} ms")
    print(f"  gate open {report['gate_open_fraction']:.2f}, "
          f"{report['keyframes_per_clip']} keyframes/clip")
    for name, s in stages.items():
        print(f"    {name:12} p50 {s['p50_ms']!s:>7} ms  p95 {s['p95_ms']!s:>7} ms  "
              f"calls/clip {s['calls_per_clip']}")
    if report["fast_lane_ms"]:
        f = report["fast_lane_ms"]
        print(f"  fast lane p50 {f['p50']} ms  p95 {f['p95']} ms  {f['passes_per_s']} passes/s")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"written: {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
