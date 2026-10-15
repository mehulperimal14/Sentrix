"""Frame sources — the hardware seam on the input side.

Everything upstream of the pipeline sits behind ``FrameSource``. Today that is a
laptop webcam; later it is an ESP32-S3 pushing RTSP or a Sony IMX291 on an
Orange Pi. Adding hardware becomes a new subclass and a config line, not a
refactor of the pipeline.

``FileSource`` is equally important: it is what makes evaluation possible.
Replaying a labelled clip must traverse *identical* code to a live camera,
otherwise every published number comes from a code path that is not the product.
The previous build had a separate ``process_eval_frame`` that duplicated the
whole pipeline, which is how evaluation and production drift apart.
"""

from __future__ import annotations

import sys
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np

from sentrix.config import settings
from sentrix.utils.logging import get_logger
from sentrix.utils.redact import redact_url

log = get_logger("sources")

try:
    import cv2
except ImportError:                                    # pragma: no cover
    cv2 = None                                         # type: ignore[assignment]


def _placeholder(text: str = "No Camera Signal") -> np.ndarray:
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    frame[:] = (18, 14, 24)
    if cv2 is not None:
        for y in range(0, 480, 40):
            cv2.line(frame, (0, y), (640, y), (30, 24, 40), 1)
        cv2.putText(frame, text, (60, 240), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                    (0, 210, 255), 2)
    return frame


class FrameSource(ABC):
    """A source of BGR frames."""

    name: str = "source"

    @abstractmethod
    def read(self) -> tuple[bool, np.ndarray | None]:
        """Return ``(ok, frame)``. ``ok=False`` means no frame is available."""

    def latest(self, after_seq: int = -1) -> tuple[np.ndarray | None, int, float]:
        """The newest frame as ``(frame, seq, captured_at)`` for the live loop.

        ``frame`` is None when there is nothing newer than ``after_seq``.
        ``captured_at`` is ``time.monotonic()`` at capture. This default paces
        a pull-based source (a video file used as a camera) at the capture
        rate; :class:`CaptureSource` overrides it with its reader thread's frame.
        """
        now = time.monotonic()
        seq = getattr(self, "_live_seq", 0)
        if now < getattr(self, "_live_next", 0.0):
            return None, seq, getattr(self, "_live_at", 0.0)
        ok, frame = self.read()
        if not ok or frame is None:
            return None, seq, getattr(self, "_live_at", 0.0)
        self._live_seq, self._live_at = seq + 1, now
        # Schedule from the previous deadline, not from now: on Windows each
        # wait overshoots, and adding the overshoot every frame played a 30 fps
        # file at ~21 fps.
        period = settings.pipeline.capture_period
        self._live_next = max(now, getattr(self, "_live_next", now) + period)
        return frame, self._live_seq, now

    @property
    def online(self) -> bool:
        return True

    @property
    def finite(self) -> bool:
        """True for sources that end (files), False for live cameras."""
        return False

    def release(self) -> None:
        pass


class CaptureSource(FrameSource):
    """Threaded OpenCV capture — webcam index or RTSP/HTTP URL.

    A dedicated reader thread keeps the buffer drained. Without it, OpenCV
    queues frames internally and ``read()`` returns progressively staler images
    while latency climbs; on an RTSP stream that drift is unbounded.
    """

    def __init__(self, source: int | str, reconnect_seconds: float = 2.0) -> None:
        self.source = source
        # Never the raw URL: rtsp://user:pw@host would reach /api/health, the
        # link heartbeat and the Portal (docs/7 §12).
        self.name = f"capture:{redact_url(str(source))}"
        self._reconnect = reconnect_seconds
        self._cap = None
        self._frame: np.ndarray | None = None
        self._seq = 0
        self._stamp = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._online = False
        self._last_attempt = 0.0
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name=f"sentrix-cam-{redact_url(str(source))}")
        self._thread.start()

    def _open(self) -> bool:
        if cv2 is None:
            return False
        now = time.monotonic()
        if now - self._last_attempt < self._reconnect:
            return False
        self._last_attempt = now
        try:
            if self._cap is not None:
                self._cap.release()
            # CAP_DSHOW avoids a multi-second open delay on Windows MSMF. The
            # constant exists in every OpenCV build, but DirectShow does not:
            # on Linux/macOS the default backend (V4L2/AVFoundation) is used.
            if isinstance(self.source, int) and sys.platform == "win32":
                cap = cv2.VideoCapture(self.source, cv2.CAP_DSHOW)
            else:
                cap = cv2.VideoCapture(self.source)
            if not cap.isOpened():
                cap.release()
                return False
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                if isinstance(self.source, int):
                    cfg = settings.pipeline
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.capture_width)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.capture_height)
                    cap.set(cv2.CAP_PROP_FPS, cfg.capture_fps)
            except Exception:
                pass
            self._cap = cap
            log.info("camera %s opened", redact_url(str(self.source)))
            return True
        except Exception as exc:
            log.debug("camera %s open failed: %s", self.source, exc)
            return False

    def _loop(self) -> None:
        # read() blocks until the device delivers the next frame, so the loop
        # runs at the camera's own rate. It used to sleep a full capture period
        # after each read as well, which halved the rate and aged every frame.
        # The floor only guards a backend that returns instantly.
        floor = 1.0 / 120.0
        while not self._stop.is_set():
            began = time.monotonic()
            if self._cap is None or not self._cap.isOpened():
                self._online = self._open()
                if not self._online:
                    time.sleep(self._reconnect)
                    continue
            try:
                ok, frame = self._cap.read()
            except Exception:
                ok, frame = False, None
            if not ok or frame is None:
                self._online = False
                if self._cap is not None:
                    self._cap.release()
                    self._cap = None
                continue
            with self._lock:
                self._frame = frame
                self._seq += 1
                self._stamp = time.monotonic()
                self._online = True
            spare = floor - (time.monotonic() - began)
            if spare > 0:
                time.sleep(spare)

    def read(self) -> tuple[bool, np.ndarray | None]:
        with self._lock:
            if self._frame is None:
                return False, None
            return True, self._frame.copy()

    def latest(self, after_seq: int = -1) -> tuple[np.ndarray | None, int, float]:
        with self._lock:
            if self._frame is None or self._seq == after_seq:
                return None, self._seq, self._stamp
            return self._frame.copy(), self._seq, self._stamp

    @property
    def online(self) -> bool:
        return self._online

    def release(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
        self._cap = None


class FileSource(FrameSource):
    """Deterministic playback of a video file, for evaluation and replay.

    Reads synchronously and does not drop frames, so a replay is reproducible.
    ``stride`` subsamples to the pipeline's inference rate without decoding
    every frame twice.
    """

    def __init__(self, path: str | Path, stride: int = 1, loop: bool = False) -> None:
        self.path = Path(path)
        self.name = f"file:{self.path.name}"
        self._stride = max(1, int(stride))
        self._loop = loop
        self._index = 0
        self._cap = None
        if cv2 is not None and self.path.exists():
            self._cap = cv2.VideoCapture(str(self.path))
            if not self._cap.isOpened():
                log.error("cannot open video %s", self.path)
                self._cap = None
        elif not self.path.exists():
            log.error("video not found: %s", self.path)

    @property
    def finite(self) -> bool:
        return True

    @property
    def fps(self) -> float:
        if self._cap is None:
            return 0.0
        return float(self._cap.get(cv2.CAP_PROP_FPS) or 0.0)

    @property
    def frame_count(self) -> int:
        if self._cap is None:
            return 0
        return int(self._cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

    def read(self) -> tuple[bool, np.ndarray | None]:
        if self._cap is None:
            return False, None
        frame = None
        for _ in range(self._stride):
            ok, frame = self._cap.read()
            self._index += 1
            if not ok:
                if self._loop:
                    self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    self._index = 0
                    ok, frame = self._cap.read()
                    if not ok:
                        return False, None
                else:
                    return False, None
        return True, frame

    @property
    def position(self) -> int:
        return self._index

    def release(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None


class MultiSource(FrameSource):
    """Several sources tiled into one mosaic for unified inference.

    Note the trade-off, which the previous docs did not state: tiling four 1080p
    feeds into one 640px inference frame quarters the effective resolution of
    each, so small-object detection (a knife at distance) degrades sharply. For
    more than two cameras, per-camera inference is the correct design; this is
    kept for demo parity.
    """

    def __init__(self, sources: list[FrameSource]) -> None:
        self.sources = sources
        self.name = "multi:" + ",".join(s.name for s in sources)

    def read(self) -> tuple[bool, np.ndarray | None]:
        frames = []
        for source in self.sources:
            ok, frame = source.read()
            if ok and frame is not None:
                frames.append(frame)
        if not frames:
            return False, None
        if len(frames) == 1 or cv2 is None:
            return True, frames[0]
        h, w = frames[0].shape[:2]
        tiles = [cv2.resize(f, (w, h)) for f in frames[:4]]
        if len(tiles) == 2:
            return True, cv2.hconcat(tiles)
        while len(tiles) < 4:
            tiles.append(np.zeros_like(tiles[0]))
        return True, cv2.vconcat([cv2.hconcat(tiles[:2]), cv2.hconcat(tiles[2:])])

    @property
    def online(self) -> bool:
        return any(s.online for s in self.sources)

    def release(self) -> None:
        for source in self.sources:
            source.release()


def build_source(spec: str | None = None) -> FrameSource:
    """Build a source from a ``CAMERA_SOURCES``-style spec.

    Accepts a comma-separated list of webcam indices, file paths and URLs::

        "0"                       laptop webcam
        "0,1"                     two webcams, tiled
        "rtsp://host:554/stream"  IP camera or ESP32 node
        "clips/test.mp4"          replay
        "picamera" / "picamera:0" Raspberry Pi CSI camera (Picamera2), e.g.
                                  "picamera:0?fps=15&main=1280x720&lores=640x480"
        "demo-reel:<reel.json>"   demo clips switched on request
                                  (sentrix/io/demo_reel.py, tools/demo_run.py)
    """
    import os

    spec = spec if spec is not None else os.getenv("CAMERA_SOURCES", "0")
    parts = [p.strip() for p in str(spec).split(",") if p.strip()]
    if not parts:
        parts = ["0"]

    sources: list[FrameSource] = []
    for part in parts:
        if part.lower().startswith("demo-reel:"):
            # Validated demo clips switched by tools/demo_run.py (never wa02).
            from sentrix.io.demo_reel import build as build_reel
            sources.append(build_reel(part))
        elif part.lower().split("?", 1)[0].split(":", 1)[0] in ("picamera", "picam"):
            # OpenCV cannot open the CSI camera on a Pi 5 (docs/8_RASPBERRY_PI5_INTEGRATION.md §3).
            from sentrix.io.picamera import PiCameraSource
            sources.append(PiCameraSource.from_spec(part))
        elif part.isdigit():
            sources.append(CaptureSource(int(part)))
        elif "://" in part:
            sources.append(CaptureSource(part))
        else:
            # A relative path may be given from the repository root (e.g. a
            # bundled clip); a video used as the camera loops instead of
            # freezing on its last frame.
            path = Path(part)
            if not path.exists() and not path.is_absolute():
                from sentrix.paths import ROOT_DIR
                path = ROOT_DIR / path
            sources.append(FileSource(path, loop=True) if path.exists()
                           else CaptureSource(part))
    return sources[0] if len(sources) == 1 else MultiSource(sources)


__all__ = ["FrameSource", "CaptureSource", "FileSource", "MultiSource",
           "build_source", "_placeholder"]
