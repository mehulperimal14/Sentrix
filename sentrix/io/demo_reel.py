"""A demo "reel": video files used as the camera, switched by the operator.

The teachers' demo (docs/7 §15, design §6) plays validated trigger clips into
the real pipeline instead of a webcam. A plain looping ``FileSource`` cannot
do that: a trigger clip looping on its own fires every 14 s, and the
operator has to choose *when* a scene starts. This source plays an **idle**
segment of normal footage in a loop and, when ``tools/demo_run.py scene
<name>`` writes the control file, plays that scene once and goes back to idle.

``CAMERA_SOURCES=demo-reel:<path to reel.json>``; the file (written by
``tools/demo_run.py``) is::

    {"control": "<file the CLI writes>",
     "idle":   {"file": "...", "start": 0, "end": 60},
     "scenes": {"gun": {"file": "...", "start": 0, "end": null}, ...}}

Rules:

* Only video files. Never a webcam or a stream, and **never the test video**
  (``wa02``: refused by file name and by the dataset's split table), so a
  demo can never be used to tune or to show the held-out result.
* Every frame is resized to the idle clip's size, so the motion engine never
  compares frames of different shapes when a scene starts.
* A control token present at start is ignored (a stale request from a
  previous run never replays a scene by itself).
* Nothing here sends anything or changes a threshold: a scene is just frames.
"""

from __future__ import annotations

import csv
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from sentrix.io.sources import FrameSource, _placeholder
from sentrix.paths import TRAINING_DIR
from sentrix.utils.logging import get_logger

log = get_logger("demo_reel")

try:
    import cv2
except ImportError:                                    # pragma: no cover
    cv2 = None                                         # type: ignore[assignment]

SPEC_PREFIX = "demo-reel:"
#: The custom dataset's held-out test video (CLAUDE.md: never tune on wa02,
#: never show it). Also read from the split table below, so a renamed copy
#: listed there is refused too.
TEST_VIDEO_FILES = frozenset({"WhatsApp Video 2026-09-19 at 7.04.16 PM (2).mp4"})
DATASET_DIR = TRAINING_DIR / "datasets" / "sentrix_custom"
#: How often the control file is polled (seconds).
CONTROL_POLL_SECONDS = 0.25


def test_split_files(dataset_dir: Path = DATASET_DIR) -> frozenset[str]:
    """File names of every video the dataset marks ``test`` (plus the known one)."""
    names = set(TEST_VIDEO_FILES)
    try:
        with open(dataset_dir / "video_split.csv", newline="", encoding="utf-8") as handle:
            test_ids = {row["video_id"] for row in csv.DictReader(handle)
                        if row.get("split", "").strip() == "test"}
        with open(dataset_dir / "videos.csv", newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row.get("video_id") in test_ids or row.get("split", "").strip() == "test":
                    names.add(row["file"])
        names.update(f"{video_id}.mp4" for video_id in test_ids)
    except (OSError, KeyError) as exc:
        log.debug("split table unavailable (%s): only the known test file is refused", exc)
    return frozenset(names)


def refuse_test_video(path: str | Path) -> None:
    """Raise ``ValueError`` when ``path`` is the held-out test video."""
    name = Path(path).name
    if name in test_split_files() or name.lower().startswith("wa02"):
        raise ValueError(f"{name} is the held-out test video (wa02): never used in a demo")


@dataclass(frozen=True)
class Segment:
    name: str
    file: Path
    start: float = 0.0
    end: float | None = None                          # None: to the end of the file

    @classmethod
    def parse(cls, name: str, raw: dict[str, Any], base: Path) -> "Segment":
        path = Path(str(raw["file"]))
        if not path.is_absolute():
            path = base / path
        refuse_test_video(path)
        start = max(0.0, float(raw.get("start") or 0.0))
        end = raw.get("end")
        end = None if end in (None, "") else float(end)
        if end is not None and end <= start:
            raise ValueError(f"scene {name}: end {end} must be after start {start}")
        return cls(name=name, file=path, start=start, end=end)


def load_reel(path: str | Path) -> tuple[Segment, dict[str, Segment], Path]:
    """(idle, scenes, control file) from a reel JSON file."""
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    base = path.parent
    idle = Segment.parse("idle", raw["idle"], base)
    scenes = {str(name): Segment.parse(str(name), spec, base)
              for name, spec in (raw.get("scenes") or {}).items()}
    if "idle" in scenes:
        raise ValueError("'idle' is reserved")
    control = Path(str(raw.get("control") or (base / "scene.txt")))
    if not control.is_absolute():
        control = base / control
    return idle, scenes, control


def read_control(control: Path) -> tuple[str, str] | None:
    """``(token, scene)`` from the control file, or None. The CLI writes
    ``<token> <scene>``; a new token starts the scene again."""
    try:
        text = control.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    parts = text.split()
    if len(parts) != 2:
        return None
    return parts[0], parts[1]


def write_control(control: Path, scene: str) -> str:
    """What ``tools/demo_run.py scene <name>`` does; returns the token."""
    token = f"{time.time_ns():x}"
    control.parent.mkdir(parents=True, exist_ok=True)
    temporary = control.with_suffix(".tmp")
    temporary.write_text(f"{token} {scene}\n", encoding="utf-8")
    temporary.replace(control)
    return token


class DemoReelSource(FrameSource):
    """Idle footage in a loop; a named scene once, on request."""

    def __init__(self, reel_path: str | Path) -> None:
        self.reel_path = Path(reel_path)
        self.idle, self.scenes, self.control = load_reel(self.reel_path)
        self._lock = threading.Lock()
        self._cap = None
        self._segment = self.idle
        self._end_frame: int | None = None
        self._size: tuple[int, int] | None = None     # (width, height) of the idle clip
        self._next_poll = 0.0
        current = read_control(self.control)
        self._token = current[0] if current else ""    # stale requests are ignored
        self.scene_started_at: float | None = None
        self.plays: list[tuple[str, float]] = []       # (scene, wall time) history
        self._open(self.idle)
        self.name = "demo-reel:idle"

    # ── segments ─────────────────────────────────────────────────────────────
    def _open(self, segment: Segment) -> bool:
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        if cv2 is None or not segment.file.exists():
            log.error("demo reel: cannot open %s", segment.file)
            return False
        cap = cv2.VideoCapture(str(segment.file))
        if not cap.isOpened():
            log.error("demo reel: cannot open %s", segment.file)
            return False
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0) or 25.0
        if segment.start:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(round(segment.start * fps)))
        self._end_frame = (int(round(segment.end * fps)) if segment.end is not None else None)
        self._cap = cap
        self._segment = segment
        self.name = f"demo-reel:{segment.name}"
        return True

    def _poll_control(self) -> None:
        now = time.monotonic()
        if now < self._next_poll:
            return
        self._next_poll = now + CONTROL_POLL_SECONDS
        current = read_control(self.control)
        if current is None or current[0] == self._token:
            return
        self._token, scene = current
        if scene == "idle":
            self._open(self.idle)
            return
        segment = self.scenes.get(scene)
        if segment is None:
            log.warning("demo reel: unknown scene %r (known: %s)", scene, ", ".join(self.scenes))
            return
        if self._open(segment):
            self.scene_started_at = time.time()
            self.plays.append((scene, self.scene_started_at))
            log.info("demo reel: scene %s (%s) started", scene, segment.file.name)

    def _fit(self, frame: np.ndarray) -> np.ndarray:
        height, width = frame.shape[:2]
        if self._size is None:
            self._size = (width, height)
        if (width, height) != self._size and cv2 is not None:
            frame = cv2.resize(frame, self._size)
        return frame

    @property
    def scene(self) -> str:
        return self._segment.name

    # ── FrameSource ───────────────────────────────────────────────────────────
    def read(self) -> tuple[bool, np.ndarray | None]:
        with self._lock:
            self._poll_control()
            for _ in range(2):                        # at most one segment change per read
                if self._cap is None:
                    return True, self._fit(_placeholder("Demo reel: clip missing"))
                position = int(self._cap.get(cv2.CAP_PROP_POS_FRAMES) or 0)
                ok, frame = (False, None)
                if self._end_frame is None or position < self._end_frame:
                    ok, frame = self._cap.read()
                if ok and frame is not None:
                    return True, self._fit(frame)
                if self._segment is self.idle:
                    self._open(self.idle)              # loop the idle footage
                else:
                    log.info("demo reel: scene %s ended, back to idle", self._segment.name)
                    self._open(self.idle)
            return False, None

    @property
    def online(self) -> bool:
        return self._cap is not None

    def release(self) -> None:
        with self._lock:
            if self._cap is not None:
                self._cap.release()
                self._cap = None


def build(spec: str) -> DemoReelSource:
    """``demo-reel:<path>`` -> the source (``sources.build_source``)."""
    path = spec[len(SPEC_PREFIX):].strip()
    if not path:
        raise ValueError("demo-reel: needs the path of a reel JSON file")
    return DemoReelSource(path)


__all__ = ["DemoReelSource", "Segment", "SPEC_PREFIX", "TEST_VIDEO_FILES", "build",
           "load_reel", "read_control", "refuse_test_video", "test_split_files",
           "write_control"]
