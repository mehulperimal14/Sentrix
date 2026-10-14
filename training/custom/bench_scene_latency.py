"""CPU latency of scene classifiers at a Raspberry Pi 5's thread budget.

HARDWARE.md §2 measured the laptop "limited to 4 threads to mimic the Pi 5's
4 cores". This does the same for scene checkpoints, in both runtimes the Pi
can use from Python:

* **PyTorch** (``torch.set_num_threads``), as ``SceneEngine`` runs today;
* **ONNX Runtime** (``intra_op_num_threads``, one inter-op thread, spinning off
  as the app configures its face models), from a temporary export.

Batch 1 at the checkpoint's image size, model forward only; the engine's
preprocessing (BGR 640x480 frame -> normalised tensor) is timed separately.
Each model's graph is built from its checkpoint; weights do not change the
cost, so a delta checkpoint is timed without downloading its public base.

Other work on the machine moves CPU timings, so the models are timed in
interleaved rounds - a busy minute slows every model, not just one - and the
load before the run is recorded. Median and p95 are reported.

Run in the app environment (CPU torch + onnxruntime)::

    .venv/Scripts/python training/custom/bench_scene_latency.py \\
        models/v5_slim/*.pt models/v4_custom/scene_classifier_custom_v4.pt \\
        --threads 4 --out training/runs/custom/slim/latency.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sentrix.perception.scene import build_scene_model  # noqa: E402


def cpu_name() -> str:
    """The processor's marketing name, where the OS exposes it."""
    try:
        if sys.platform == "win32":
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                 r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith(("model name", "hardware", "model")):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor() or "unknown"


def on_mains() -> bool | None:
    """True on mains power, False on battery, None when unknown.

    A laptop on battery caps its CPU (and GPU) power, which can double CPU
    latencies, so the power source is part of every measurement.
    """
    try:
        if sys.platform == "win32":
            import ctypes

            class PowerStatus(ctypes.Structure):
                _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                            ("BatteryLifePercent", ctypes.c_ubyte),
                            ("SystemStatusFlag", ctypes.c_ubyte),
                            ("BatteryLifeTime", ctypes.c_ulong),
                            ("BatteryFullLifeTime", ctypes.c_ulong)]
            status = PowerStatus()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
                return {0: False, 1: True}.get(status.ACLineStatus)
            return None
        supplies = list(Path("/sys/class/power_supply").glob("*/online"))
        if supplies:
            return any(p.read_text().strip() == "1" for p in supplies)
    except Exception:
        pass
    return None


def summarise(samples_ms: list[float]) -> dict:
    a = np.asarray(samples_ms, dtype=np.float64)
    return {"n": int(a.size), "median_ms": round(float(np.median(a)), 2),
            "p95_ms": round(float(np.percentile(a, 95)), 2),
            "mean_ms": round(float(a.mean()), 2), "min_ms": round(float(a.min()), 2)}


def interleaved(timers: dict[str, Callable[[], None]], rounds: int,
                per_round: int, warmup: int) -> dict[str, list[float]]:
    """Time every callable ``rounds`` x ``per_round`` times, alternating rounds."""
    for fn in timers.values():
        for _ in range(warmup):
            fn()
    samples: dict[str, list[float]] = {name: [] for name in timers}
    for _ in range(rounds):
        for name, fn in timers.items():
            for _ in range(per_round):
                t0 = time.perf_counter()
                fn()
                samples[name].append((time.perf_counter() - t0) * 1000.0)
    return samples


def gflops(net: torch.nn.Module, size: int) -> float:
    """Forward FLOPs at batch 1 (2 x multiply-accumulates), in GFLOPs."""
    from torch.utils.flop_counter import FlopCounterMode
    counter = FlopCounterMode(display=False)
    with counter, torch.inference_mode():
        net(torch.zeros(1, 3, size, size))
    return round(counter.get_total_flops() / 1e9, 3)


def load_graph(path: Path) -> tuple[torch.nn.Module, dict]:
    ckpt = torch.load(str(path), map_location="cpu", weights_only=False)
    size = int(ckpt.get("image_size", 224))
    net = build_scene_model(ckpt["architecture"], len(ckpt["classes"]), pretrained=False,
                            image_size=size)
    if ckpt.get("storage") != "delta":
        net.load_state_dict({k: v.float() for k, v in ckpt["model_state_dict"].items()})
    info = {"checkpoint": path.name, "architecture": ckpt["architecture"],
            "image_size": size, "storage": ckpt.get("storage", "full"),
            "params_m": round(sum(p.numel() for p in net.parameters()) / 1e6, 2),
            "checkpoint_mb": round(path.stat().st_size / 1e6, 1)}
    return net.eval(), info


def ort_session(net: torch.nn.Module, size: int, threads: int, workdir: Path):
    import onnxruntime as ort
    path = workdir / f"model_{id(net)}.onnx"
    torch.onnx.export(net, (torch.zeros(1, 3, size, size),), str(path), dynamo=False,
                      opset_version=17, input_names=["input"], output_names=["logits"],
                      do_constant_folding=True)
    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    options.add_session_config_entry("session.inter_op.allow_spinning", "0")
    return ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])


def preprocess_timer(size: int) -> Callable[[], None]:
    """``SceneEngine``'s per-frame preprocessing on a BGR 640x480 frame."""
    import cv2
    from torchvision import transforms
    tf = transforms.Compose([transforms.ToPILImage(), transforms.Resize((size, size)),
                             transforms.ToTensor(),
                             transforms.Normalize([0.485, 0.456, 0.406],
                                                  [0.229, 0.224, 0.225])])
    frame = np.random.default_rng(0).integers(0, 255, (480, 640, 3), dtype=np.uint8)
    return lambda: tf(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("checkpoints", nargs="+", type=Path)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--per-round", type=int, default=40)
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    torch.set_num_threads(args.threads)
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:                                   # already set
        pass
    try:
        import psutil
        load_before = psutil.cpu_percent(interval=2.0)
    except ImportError:                                    # pragma: no cover
        load_before = None
    import onnxruntime as ort

    models, timers = [], {}
    with tempfile.TemporaryDirectory() as tmp:
        for path in args.checkpoints:
            net, info = load_graph(path)
            size = info["image_size"]
            info["gflops"] = gflops(net, size)
            x = torch.zeros(1, 3, size, size)
            session = ort_session(net, size, args.threads, Path(tmp))
            feed = {"input": x.numpy()}

            def run_torch(net=net, x=x):
                with torch.inference_mode():
                    net(x)

            timers[f"{info['checkpoint']}|torch"] = run_torch
            timers[f"{info['checkpoint']}|onnxruntime"] = (
                lambda s=session, f=feed: s.run(["logits"], f))
            models.append(info)
        sizes = sorted({m["image_size"] for m in models})
        for size in sizes:
            timers[f"preprocess@{size}|cpu"] = preprocess_timer(size)
        samples = interleaved(timers, args.rounds, args.per_round, args.warmup)

    for info in models:
        info["torch"] = summarise(samples[f"{info['checkpoint']}|torch"])
        info["onnxruntime"] = summarise(samples[f"{info['checkpoint']}|onnxruntime"])
    report = {
        "measured": datetime.now().isoformat(timespec="seconds"),
        "machine": {"cpu": cpu_name(), "os": platform.platform(),
                    "logical_cpus": os.cpu_count(),
                    "cpu_load_before_percent": load_before, "on_mains_power": on_mains()},
        "threads": args.threads, "batch": 1,
        "runs_per_model": args.rounds * args.per_round, "warmup": args.warmup,
        "versions": {"python": platform.python_version(), "torch": torch.__version__,
                     "onnxruntime": ort.__version__},
        "models": models,
        "preprocess": {str(s): summarise(samples[f"preprocess@{s}|cpu"]) for s in sizes},
    }
    for info in models:
        print(f"{info['checkpoint']:42} {info['params_m']:6.1f} M {info['gflops']:7.2f} GFLOPs  "
              f"torch {info['torch']['median_ms']:7.2f} ms (p95 {info['torch']['p95_ms']:.2f})  "
              f"ORT {info['onnxruntime']['median_ms']:7.2f} ms "
              f"(p95 {info['onnxruntime']['p95_ms']:.2f})")
    for size, stats in report["preprocess"].items():
        print(f"preprocess @{size}: {stats['median_ms']:.2f} ms (p95 {stats['p95_ms']:.2f})")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"written {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
