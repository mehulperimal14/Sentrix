"""Download the third-party model weights SENTRIX does not train itself.

Two files: the PANNs CNN10 AudioSet tagger (25 MB, Zenodo record 3987831,
Kong et al. 2020) used as the general sound model (D15), and OSNet-AIN x1_0
trained on MSMT17 (17.3 MB, torchreid's model zoo on Google Drive; Zhou et al.
2021), the body re-id model (D22; IntrusionSentry's OSNet family). Both are tracked in git
under ``models/audio/`` and ``models/reid/``, so a fresh clone needs this only
if a file was removed or is corrupt. A download is checked against its SHA-256
before it replaces anything, and the weights are only ever read with torch's
weights-only unpickler.

Both run on onnxruntime with their own threads (they must never change
torch's process-wide thread count), from an ONNX file next to the weights:
``--export`` regenerates them (parity-checked against torch) and records the
weights' SHA-256 next to each.

Usage::

    python tools/fetch_models.py            # fetch what is missing or corrupt
    python tools/fetch_models.py --check    # verify only, never download
    python tools/fetch_models.py --export   # (re)build the ONNX models

This tool reaches the network (zenodo.org, Google Drive); nothing else in SENTRIX does at
runtime, and the tests never call it.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sentrix.paths import MODELS_DIR  # noqa: E402
from sentrix.perception import osnet  # noqa: E402
from sentrix.perception.sound_tagger import (ONNX_NAME, WEIGHTS_NAME,  # noqa: E402
                                             WEIGHTS_SHA256, WEIGHTS_URL, export_onnx)

FILES = [(MODELS_DIR / "audio" / WEIGHTS_NAME, WEIGHTS_URL, WEIGHTS_SHA256),
         (MODELS_DIR / "reid" / osnet.WEIGHTS_NAME, osnet.WEIGHTS_URL, osnet.WEIGHTS_SHA256)]

#: (weights, ONNX file, exporter, input, output) of each ONNX model.
EXPORTS = [
    (MODELS_DIR / "audio" / WEIGHTS_NAME, MODELS_DIR / "audio" / ONNX_NAME, export_onnx,
     "wave [batch, samples] float32 at 32 kHz", "scores [batch, 527] (AudioSet sigmoid)"),
    (MODELS_DIR / "reid" / osnet.WEIGHTS_NAME, MODELS_DIR / "reid" / osnet.ONNX_NAME,
     osnet.export_onnx, "image [batch, 3, 256, 128] float32, RGB, ImageNet mean/std",
     "feature [batch, 512] (OSNet fc output, not yet L2-normalised)"),
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true", help="verify only")
    parser.add_argument("--export", action="store_true",
                        help="rebuild the ONNX models from their weights")
    args = parser.parse_args(argv)
    status = 0
    for path, url, expected in FILES:
        if path.exists() and sha256(path) == expected:
            print(f"ok       {path.relative_to(MODELS_DIR)}")
            continue
        if args.check:
            print(f"MISSING  {path.relative_to(MODELS_DIR)}" if not path.exists()
                  else f"CORRUPT  {path.relative_to(MODELS_DIR)}")
            status = 1
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        print(f"fetching {url}")
        with tempfile.TemporaryDirectory() as scratch:
            target = Path(scratch) / "download"
            urllib.request.urlretrieve(url, target)
            got = sha256(target)
            if got != expected:
                print(f"FAILED   checksum {got} != {expected}; nothing replaced")
                status = 1
                continue
            shutil.move(str(target), str(path))
        print(f"ok       {path.relative_to(MODELS_DIR)}")
    status = max(status, onnx_step(args.export, args.check))
    return status


def onnx_step(export: bool, check: bool) -> int:
    return max(export_one(*entry, export=export, check=check) for entry in EXPORTS)


def export_one(weights: Path, target: Path, exporter, inputs: str, outputs: str, *,
               export: bool, check: bool) -> int:
    import json
    meta_path = target.with_suffix(".json")
    name = target.relative_to(MODELS_DIR).as_posix()
    if export:
        diff = exporter(weights, target)
        meta = {"source": weights.name, "source_sha256": sha256(weights),
                "onnx_sha256": sha256(target), "max_abs_diff_vs_torch": diff,
                "opset": 17, "input": inputs, "output": outputs}
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        print(f"exported {name} (max |onnx - torch| {diff:.2e})")
        return 0 if diff < 1e-3 else 1
    if not target.exists() or not meta_path.exists():
        print(f"MISSING  {name} (python tools/fetch_models.py --export;"
              " the model falls back to torch)")
        return 1 if check else 0
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if meta.get("source_sha256") != sha256(weights) or meta.get("onnx_sha256") != sha256(target):
        print(f"STALE    {name}: re-export with --export")
        return 1
    print(f"ok       {name}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
