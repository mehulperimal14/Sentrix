"""Download the third-party model weights SENTRIX does not train itself.

Today that is one file: the PANNs CNN10 AudioSet tagger (25 MB, Zenodo record
3987831, Kong et al. 2020) used as the general sound model (D15). It is
tracked in git under ``models/audio/``, so a fresh clone needs this only if
the file was removed or is corrupt. The download is checked against its
SHA-256 before it replaces anything.

The tagger runs on onnxruntime with its own threads (it must never change
torch's process-wide thread count), from ``models/audio/
panns_cnn10_audioset.onnx``: ``--export`` regenerates it from the weights
(parity-checked against torch) and records the weights' SHA-256 next to it.

Usage::

    python tools/fetch_models.py            # fetch what is missing or corrupt
    python tools/fetch_models.py --check    # verify only, never download
    python tools/fetch_models.py --export   # (re)build the ONNX tagger

This tool reaches the network (zenodo.org); nothing else in SENTRIX does at
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
from sentrix.perception.sound_tagger import (ONNX_NAME, WEIGHTS_NAME,  # noqa: E402
                                             WEIGHTS_SHA256, WEIGHTS_URL, export_onnx)

FILES = [(MODELS_DIR / "audio" / WEIGHTS_NAME, WEIGHTS_URL, WEIGHTS_SHA256)]


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
                        help="rebuild models/audio/" + ONNX_NAME + " from the weights")
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
    import json
    weights = MODELS_DIR / "audio" / WEIGHTS_NAME
    target = MODELS_DIR / "audio" / ONNX_NAME
    meta_path = target.with_suffix(".json")
    if export:
        diff = export_onnx(weights, target)
        meta = {"source": WEIGHTS_NAME, "source_sha256": sha256(weights),
                "onnx_sha256": sha256(target), "max_abs_diff_vs_torch": diff,
                "opset": 17, "input": "wave [batch, samples] float32 at 32 kHz",
                "output": "scores [batch, 527] (AudioSet sigmoid)"}
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        print(f"exported {target.relative_to(MODELS_DIR)} (max |onnx - torch| {diff:.2e})")
        return 0 if diff < 1e-3 else 1
    if not target.exists() or not meta_path.exists():
        print(f"MISSING  {target.relative_to(MODELS_DIR)} (python tools/fetch_models.py --export;"
              " the tagger falls back to torch)")
        return 1 if check else 0
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if meta.get("source_sha256") != sha256(weights) or meta.get("onnx_sha256") != sha256(target):
        print(f"STALE    {target.relative_to(MODELS_DIR)}: re-export with --export")
        return 1
    print(f"ok       {target.relative_to(MODELS_DIR)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
