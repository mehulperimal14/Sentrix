"""Download the third-party model weights SENTRIX does not train itself.

Today that is one file: the PANNs CNN10 AudioSet tagger (25 MB, Zenodo record
3987831, Kong et al. 2020) used as the general sound model (D15). It is
tracked in git under ``models/audio/``, so a fresh clone needs this only if
the file was removed or is corrupt. The download is checked against its
SHA-256 before it replaces anything.

Usage::

    python tools/fetch_models.py            # fetch what is missing or corrupt
    python tools/fetch_models.py --check    # verify only, never download

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
from sentrix.perception.sound_tagger import (WEIGHTS_NAME, WEIGHTS_SHA256,  # noqa: E402
                                             WEIGHTS_URL)

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
    return status


if __name__ == "__main__":
    raise SystemExit(main())
