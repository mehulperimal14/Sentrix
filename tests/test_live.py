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
        assert pipeline.latest_jpeg() is pipeline._jpeg


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
