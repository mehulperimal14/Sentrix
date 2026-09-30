"""Motion-only detector — the baseline the whole project is measured against.

The approved objectives promise a **60% or greater reduction in false alarms
versus a motion-only baseline**, and the abstract opens by attacking a 94-98%
false-alarm rate. Neither claim can be made without this file: you cannot state
a reduction without measuring the thing you reduced from.

This is deliberately the naive detector the abstract describes — Gaussian-blurred
frame differencing with a fixed threshold and a debounce, which is what a PIR
sensor or a cheap CCTV motion trigger does. It is a fair strawman precisely
because it is what the market actually ships.

Usage::

    python tools/baseline.py --clips data/clips
    python tools/baseline.py --clips data/clips --threshold 0.02
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

VIDEO_SUFFIXES = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".m4v"}
THREAT_WORDS = {"threat", "fight", "weapon", "fire", "intrusion", "violence"}


class MotionOnlyDetector:
    """Frame differencing plus a fixed threshold and a debounce."""

    def __init__(self, threshold: float = 0.015, debounce_frames: int = 3) -> None:
        self.threshold = threshold
        self.debounce = debounce_frames
        self._prev = None
        self._above = 0

    def reset(self) -> None:
        self._prev, self._above = None, 0

    def update(self, frame) -> bool:
        import cv2

        gray = cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (21, 21), 0)
        if self._prev is None or self._prev.shape != gray.shape:
            self._prev = gray
            return False
        score = float(np.mean(cv2.absdiff(self._prev, gray))) / 255.0
        self._prev = gray
        self._above = self._above + 1 if score > self.threshold else 0
        return self._above >= self.debounce


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--clips", type=Path, required=True)
    parser.add_argument("--threshold", type=float, default=0.015)
    parser.add_argument("--debounce", type=int, default=3)
    parser.add_argument("--fps", type=float, default=6.0,
                        help="assumed inference rate, for alarms-per-hour")
    args = parser.parse_args(argv)

    from sentrix.io.sources import FileSource

    clips = [p for p in sorted(args.clips.rglob("*"))
             if p.suffix.lower() in VIDEO_SUFFIXES]
    if not clips:
        raise SystemExit(f"no videos under {args.clips}")

    detector = MotionOnlyDetector(args.threshold, args.debounce)
    stats = {"normal": [0, 0, 0], "threat": [0, 0, 0]}   # clips, alarmed, frames

    print(f"motion-only baseline  threshold={args.threshold} "
          f"debounce={args.debounce}\n")
    for path in clips:
        label = ("threat" if path.parent.name.lower() in THREAT_WORDS
                 else "normal")
        source = FileSource(path)
        detector.reset()
        alarmed, frames = False, 0
        while True:
            ok, frame = source.read()
            if not ok or frame is None:
                break
            frames += 1
            if detector.update(frame):
                alarmed = True
        source.release()

        stats[label][0] += 1
        stats[label][1] += int(alarmed)
        stats[label][2] += frames
        print(f"  {path.name[:48]:<50} {label:<7} "
              f"{'ALARM' if alarmed else '  -  '}  {frames:>5} frames")

    print("\n" + "=" * 62)
    normal_clips, normal_alarms, normal_frames = stats["normal"]
    threat_clips, threat_alarms, _ = stats["threat"]

    if normal_clips:
        hours = normal_frames / args.fps / 3600.0
        print(f"normal clips     : {normal_clips}")
        print(f"false alarms     : {normal_alarms} "
              f"({100 * normal_alarms / normal_clips:.1f}% of clips)")
        if hours > 0:
            print(f"footage duration : {hours:.2f} h "
                  f"(at {args.fps} Hz)")
            print(f"FALSE ALARMS/HOUR: {normal_alarms / hours:.2f}   <-- baseline")
    if threat_clips:
        print(f"\nthreat clips     : {threat_clips}")
        print(f"detected         : {threat_alarms} "
              f"({100 * threat_alarms / threat_clips:.1f}% recall)")

    print("\nCompare against SENTRIX on the SAME footage:")
    print("  python tools/replay.py --clips", args.clips)
    print("  then notebooks/03_tci_thresholds.ipynb")
    return 0


if __name__ == "__main__":
    sys.exit(main())
