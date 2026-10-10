"""Replay labelled video clips through the real pipeline.

This is how evaluation numbers get produced, and how the fusion training set
that the V3 notebook could not find gets built. Every clip traverses exactly the
same ``Pipeline.process`` the live camera uses — no parallel evaluation path, so
published numbers come from the shipped code.

Expected layout::

    data/clips/
        labels.csv          clip,label      (label: threat | normal)
        normal/room_01.mp4
        threat/knife_03.mp4

If ``labels.csv`` is absent, the parent directory name is used as the label,
which makes ``normal/`` and ``threat/`` subfolders sufficient.

Output: one JSONL row per scored frame in ``var/events/``, carrying the full
feature vector, TCI, committed level and the ground-truth label. Feed that to
``notebooks/02_tci_calibration.ipynb`` and ``03_tci_thresholds.ipynb``.

Usage::

    python tools/replay.py --clips data/clips
    python tools/replay.py --clips data/clips --limit 20

Side effects are disabled: no camera is opened, no snapshot, evidence,
incident row, SMS, call, siren or dispatch is produced, whatever ``.env``
contains. The microphone stays off unless ``--audio`` is given.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

VIDEO_SUFFIXES = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".m4v"}
THREAT_WORDS = {"threat", "fight", "weapon", "fire", "intrusion", "violence",
                "anomaly", "true", "1"}


def discover(clips_dir: Path) -> list[tuple[Path, str]]:
    """Find clips and their labels, preferring labels.csv over folder names."""
    labels: dict[str, str] = {}
    manifest = clips_dir / "labels.csv"
    if manifest.exists():
        with open(manifest, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                key = (row.get("clip") or row.get("file") or row.get("path") or "").strip()
                value = (row.get("label") or row.get("class") or "").strip().lower()
                if key:
                    labels[Path(key).name] = value
        print(f"labels.csv: {len(labels)} entries")

    found: list[tuple[Path, str]] = []
    for path in sorted(clips_dir.rglob("*")):
        if path.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        label = labels.get(path.name) or path.parent.name.lower()
        found.append((path, "threat" if label in THREAT_WORDS else "normal"))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--clips", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0,
                        help="process at most N clips")
    parser.add_argument("--max-frames", type=int, default=0,
                        help="cap frames per clip")
    parser.add_argument("--audio", action="store_true",
                        help="also feed the live microphone (off by default: the "
                             "room does not hear the clip)")
    parser.add_argument("--no-audio", action="store_true",
                        help="kept for compatibility: the microphone is off "
                             "unless --audio is given")
    args = parser.parse_args(argv)

    if not args.clips.is_dir():
        raise SystemExit(f"not a directory: {args.clips}")

    clips = discover(args.clips)
    if not clips:
        raise SystemExit(f"no video files found under {args.clips}")
    if args.limit:
        clips = clips[: args.limit]

    counts = Counter(label for _, label in clips)
    print(f"{len(clips)} clip(s): " +
          ", ".join(f"{v} {k}" for k, v in counts.items()))
    if len(counts) < 2:
        print("WARNING: only one class present. False-alarm rate needs normal "
              "footage; detection rate needs threat footage.")

    from sentrix.evaluation.harness import _NullSource, _noop
    from sentrix.io.sources import FileSource
    from sentrix.pipeline.engine import Pipeline

    # Audio is off unless asked for: during replay the microphone hears the
    # room, not the clip, so live audio would inject an uncorrelated signal
    # into every feature vector. No camera is opened (null source) and
    # every side effect is a no-op: replaying a weapon clip must never text
    # the phone in .env or sound the siren.
    pipeline = Pipeline(source=_NullSource(), enable_audio=args.audio and not args.no_audio,
                        event_source="replay")
    pipeline._act = _noop
    pipeline._submit = _noop
    pipeline._save_open_incident = _noop
    print(f"event log: {pipeline.events.path}\n")

    started = time.time()
    total_frames = 0
    try:
        for index, (path, label) in enumerate(clips, start=1):
            source = FileSource(path)
            rows = pipeline.replay(source, label=label,
                                   max_frames=args.max_frames or None)
            source.release()
            total_frames += len(rows)
            peak = max((r["level"] for r in rows), default=1)
            peak_tci = max((r["tci"] for r in rows), default=0.0)
            print(f"[{index:>3}/{len(clips)}] {path.name[:44]:<46} "
                  f"{label:<7} {len(rows):>5} frames  "
                  f"peak L{peak} tci={peak_tci:.3f}")
    finally:
        pipeline.shutdown()

    elapsed = time.time() - started
    print(f"\n{total_frames:,} frames from {len(clips)} clip(s) in {elapsed:.1f}s")
    print(f"event log written: {pipeline.events.path}")
    print("\nNext: notebooks/02_tci_calibration.ipynb")
    return 0


if __name__ == "__main__":
    sys.exit(main())
