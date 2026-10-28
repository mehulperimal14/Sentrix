"""Live loop: the video must never wait for the AI (CLAUDE.md M2).

In the first live test the preview was drawn only after inference, so the
video froze at the AI rate (0.3 Hz). These tests drive ``Pipeline.run_live``
with a fake 30 fps camera and a deliberately slow ``process``, without loading
a single model or opening a device.
"""

from __future__ import annotations

import threading
import time
from collections import deque

import numpy as np

from sentrix.config import settings
from sentrix.io.sources import FrameSource
from sentrix.pipeline.engine import Overlay, Pipeline, _rate


class FakeCamera(FrameSource):
    """Pushes a numbered frame every ``period`` seconds, like a reader thread."""

    name = "fake"

    def __init__(self, period: float = 1 / 30) -> None:
        self.period = period
        self.started = time.monotonic()

    def read(self):
        return True, self._frame(self._seq_now())

    def _seq_now(self) -> int:
        return int((time.monotonic() - self.started) / self.period) + 1

    @staticmethod
    def _frame(seq: int) -> np.ndarray:
        frame = np.zeros((48, 64, 3), np.uint8)
        frame[0, 0, 0] = seq % 256
        frame[0, 0, 1] = seq // 256
        return frame

    def latest(self, after_seq: int = -1):
        seq = self._seq_now()
        if seq == after_seq:
            return None, seq, 0.0
        return self._frame(seq), seq, self.started + seq * self.period


def live_pipeline(process_seconds: float):
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.cfg = settings
    pipeline.source = FakeCamera()
    pipeline._init_live()
    seen: list[int] = []

    def process(frame, now=None, label=None):
        seen.append(int(frame[0, 0, 0]) + 256 * int(frame[0, 0, 1]))
        time.sleep(process_seconds)
        pipeline._overlay = Overlay(tci=0.5, level=3, status="ELEVATED",
                                    behaviour_label="normal", weapon=0.5, fire=0.0,
                                    authorized=False, identity_degraded=False,
                                    detections={}, at=now)
        return {}

    pipeline.process = process
    return pipeline, seen


def run_for(pipeline, seconds: float) -> None:
    stop = threading.Event()
    thread = threading.Thread(target=pipeline.run_live, args=(stop,), daemon=True)
    thread.start()
    time.sleep(seconds)
    stop.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()


class TestDecoupledLoop:
    def test_preview_keeps_the_camera_rate_while_inference_is_slow(self):
        """A 400 ms inference used to freeze the video at 2.5 fps."""
        pipeline, seen = live_pipeline(process_seconds=0.4)
        run_for(pipeline, 1.5)
        assert pipeline.preview_seq >= 30, f"only {pipeline.preview_seq} preview frames in 1.5 s"
        assert 2 <= len(seen) <= 5

    def test_inference_takes_the_newest_frame_and_drops_the_rest(self):
        pipeline, seen = live_pipeline(process_seconds=0.3)
        run_for(pipeline, 1.5)
        gaps = np.diff(seen)
        assert (gaps >= 5).all(), f"inference fell behind a queue: {seen}"

    def test_inference_is_capped_at_the_configured_rate(self):
        pipeline, seen = live_pipeline(process_seconds=0.0)
        run_for(pipeline, 1.0)
        assert len(seen) <= settings.pipeline.inference_fps + 2

    def test_preview_is_encoded_once_per_frame(self):
        pipeline, _ = live_pipeline(process_seconds=0.05)
        run_for(pipeline, 0.3)
        seq, data = pipeline.latest_jpeg()
        assert data is not None and data[:2] == b"\xff\xd8"
        assert pipeline.latest_jpeg() is pipeline._jpegs[(0, 70)]


def preview_pipeline(width: int, height: int):
    """A pipeline holding one published preview frame (no camera, no models)."""
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.cfg = settings
    pipeline._init_live()
    pipeline._publish(np.tile(np.arange(width, dtype=np.uint8)[None, :, None], (height, 1, 3)))
    return pipeline


def decoded_shape(encoded) -> tuple[int, int]:
    import cv2
    return cv2.imdecode(np.frombuffer(encoded[1], np.uint8), cv2.IMREAD_COLOR).shape[:2]


class TestRemotePreview:
    """The Portal's remote video is a scaled copy of the preview (full-size
    1280x720 frames are suspected of lagging over the Pi's Wi-Fi; not yet
    measured); the LAN MJPEG stays full size."""

    def test_scaled_copy_keeps_the_aspect_with_even_sides(self):
        assert decoded_shape(preview_pipeline(1280, 720).latest_jpeg(70, 640)) == (360, 640)
        assert decoded_shape(preview_pipeline(1000, 563).latest_jpeg(70, 641)) == (360, 640)

    def test_full_size_by_default_and_for_a_narrower_preview(self):
        assert decoded_shape(preview_pipeline(1280, 720).latest_jpeg()) == (720, 1280)
        assert decoded_shape(preview_pipeline(1280, 720).latest_jpeg(70, 0)) == (720, 1280)
        laptop = preview_pipeline(640, 480)
        assert laptop.latest_jpeg(70, 640) is laptop.latest_jpeg(70), \
            "a preview no wider than max_width shares the full-size JPEG"

    def test_each_size_and_quality_is_encoded_once_per_frame(self, monkeypatch):
        import cv2
        from sentrix.pipeline import engine
        encoded: list[tuple] = []
        real = cv2.imencode
        monkeypatch.setattr(engine.cv2, "imencode",
                            lambda ext, img, params: encoded.append(img.shape) or
                            real(ext, img, params))
        pipeline = preview_pipeline(1280, 720)
        for _ in range(3):                       # three LAN viewers and three remote ones
            full = pipeline.latest_jpeg(70)
            small = pipeline.latest_jpeg(70, 640)
        assert encoded == [(720, 1280, 3), (360, 640, 3)]
        assert full[0] == small[0] and len(small[1]) < len(full[1])
        pipeline.latest_jpeg(90, 640)
        assert len(encoded) == 3, "another quality is another JPEG"
        pipeline._publish(np.zeros((720, 1280, 3), np.uint8))
        for _ in range(2):
            pipeline.latest_jpeg(70)
            pipeline.latest_jpeg(70, 640)
        assert len(encoded) == 5, "a new frame: each size encoded once more"
        assert {seq for seq, _ in pipeline._jpegs.values()} == {pipeline.preview_seq}, \
            "older frames' JPEGs are dropped"


class TestOverlay:
    def test_stale_boxes_are_not_drawn(self):
        pipeline, _ = live_pipeline(process_seconds=0.0)
        box = {"persons": [{"bbox": [5, 100, 60, 200], "label": "person", "conf": 0.9}]}
        overlay = Overlay(tci=0.1, level=1, status="SECURE", behaviour_label="normal",
                          weapon=0.0, fire=0.0, authorized=False,
                          identity_degraded=False, detections=box, at=0.0)
        frame = np.zeros((240, 320, 3), np.uint8)
        fresh = pipeline._draw(frame.copy(), overlay, now=1.0)
        stale = pipeline._draw(frame.copy(), overlay,
                               now=settings.pipeline.stale_overlay_seconds + 1.0)
        region = (slice(100, 201), slice(0, 70))      # the box, below the banner
        assert fresh[region].any() and not stale[region].any()


class TestRates:
    def test_rate_over_window(self):
        times = deque([0.0, 0.5, 1.0, 1.5, 2.0])
        assert _rate(times, now=2.0) == 2.0
        assert _rate(deque([1.0]), now=1.0) == 0.0
        assert _rate(times, now=10.0) == 0.0, "old events do not count"


class TestPulledSourcePacing:
    def test_file_as_camera_plays_at_the_capture_rate(self):
        class Counter(FrameSource):
            reads = 0

            def read(self):
                Counter.reads += 1
                return True, np.zeros((4, 4, 3), np.uint8)

        source = Counter()
        end = time.monotonic() + 0.5
        while time.monotonic() < end:
            source.latest()
            time.sleep(0.002)
        assert Counter.reads <= settings.pipeline.capture_fps * 0.5 + 2
