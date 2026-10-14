"""Picamera2 source (T-HW): two streams per frame, request hygiene, timestamps,
the Pi 4 YUV fallback, reconnects, ``build_source`` specs and ``run_live``.

``picamera2`` is a fake module here; no camera is opened.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from sentrix.io import picamera
from sentrix.io.picamera import PiCameraSource, parse_spec
from sentrix.io.sources import build_source


class FakeRequest:
    def __init__(self, camera: "FakePicamera2", seq: int) -> None:
        self.camera, self.seq = camera, seq

    def make_array(self, name: str) -> np.ndarray:
        stream = self.camera.config[name]
        width, height = stream["size"]
        if stream["format"] == "YUV420":
            plane = np.full((height * 3 // 2, width), 128, np.uint8)
            plane[:height] = self.seq % 256                     # luma
            return plane
        frame = np.zeros((height, width, 3), np.uint8)
        frame[0, 0] = (self.seq % 256, 1 if name == "main" else 2, 0)
        return frame

    def get_metadata(self) -> dict:
        stamp = time.monotonic_ns() if self.camera.good_timestamps else 12345
        return {"SensorTimestamp": stamp, "ExposureTime": 10000, "Lux": 87.5}

    def release(self) -> None:
        self.camera.released += 1


class FakePicamera2:
    cameras: list = [{"Model": "imx708_wide", "Num": 0}]
    instances: list = []
    pi4 = False
    fail_after: int | None = None
    good_timestamps = True

    @staticmethod
    def global_camera_info():
        return FakePicamera2.cameras

    def __init__(self, camera_num: int = 0) -> None:
        self.camera_num = camera_num
        self.config = None
        self.started = self.closed = False
        self.captured = self.released = 0
        self.configure_calls: list = []
        self.good_timestamps = FakePicamera2.good_timestamps
        FakePicamera2.instances.append(self)

    def create_video_configuration(self, main, lores, controls):
        return {"main": dict(main), "lores": dict(lores), "controls": dict(controls)}

    def configure(self, config) -> None:
        self.configure_calls.append(config)
        if FakePicamera2.pi4 and config["lores"]["format"] != "YUV420":
            raise RuntimeError("lores stream must be YUV on this platform")
        self.config = config

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        self.closed = True

    def capture_request(self) -> FakeRequest:
        time.sleep(0.005)
        if not self.started or self.closed:
            raise RuntimeError("camera not running")
        self.captured += 1
        if FakePicamera2.fail_after is not None and self.captured > FakePicamera2.fail_after:
            FakePicamera2.fail_after = None            # one failure, then recover
            raise RuntimeError("Device timeout detected, attempting a restart!!!")
        return FakeRequest(self, self.captured)


@pytest.fixture
def fake_module():
    FakePicamera2.cameras = [{"Model": "imx708_wide", "Num": 0}]
    FakePicamera2.instances = []
    FakePicamera2.pi4 = False
    FakePicamera2.fail_after = None
    FakePicamera2.good_timestamps = True
    return SimpleNamespace(Picamera2=FakePicamera2)


def wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _source(module, **kwargs) -> PiCameraSource:
    kwargs.setdefault("fps", 100.0)
    return PiCameraSource(picamera2_module=module, reconnect_seconds=0.05, **kwargs)


class TestStreams:
    def test_main_and_lores_frames(self, fake_module):
        source = _source(fake_module)
        try:
            assert wait_for(lambda: source.latest()[0] is not None)
            frame, seq, at = source.latest()
            lores, lseq, lat = source.latest_lores()
            assert frame.shape == (720, 1280, 3) and frame.dtype == np.uint8
            assert lores.shape == (480, 640, 3)
            assert source.online and source.name == "picamera:0"
            camera = FakePicamera2.instances[0]
            config = camera.config
            assert config["main"] == {"size": (1280, 720), "format": "RGB888"}
            assert config["lores"] == {"size": (640, 480), "format": "RGB888"}
            assert config["controls"]["FrameDurationLimits"] == (10000, 10000)
            ok, copy = source.read()
            assert ok and copy.shape == frame.shape
            status = source.status()
            assert status["lores_format"] == "RGB888" and status["lux"] == 87.5
        finally:
            source.release()
        assert camera.closed and camera.released == camera.captured, \
            "every request goes back to libcamera"

    def test_latest_honours_the_sequence(self, fake_module):
        source = _source(fake_module, start=False)        # captures driven by hand
        try:
            assert source.latest() == (None, 0, 0.0)
            assert source._open()
            source._capture_one()
            frame, seq, at = source.latest()
            assert frame is not None and seq == 1
            assert source.latest(1)[0] is None and source.latest_lores(1)[0] is None
            source._capture_one()
            assert source.latest(1)[1] == 2 and source.latest_lores(1)[1] == 2
        finally:
            source.release()

    def test_pair_comes_from_the_same_request(self, fake_module):
        source = _source(fake_module)
        try:
            assert wait_for(lambda: source.frames >= 3)
            with source._lock:
                main_seq = int(source._frame[0, 0, 0])
                lores_seq = int(source._lores[0, 0, 0])
            assert main_seq == lores_seq
        finally:
            source.release()

    def test_lores_never_larger_than_main(self, fake_module):
        source = _source(fake_module, main_size=(640, 360), lores_size=(1280, 720), start=False)
        assert source.lores_size == (640, 360)


class TestTimestamps:
    def test_sensor_timestamp_is_the_capture_time(self, fake_module):
        source = _source(fake_module)
        try:
            assert wait_for(lambda: source.latest()[0] is not None)
            _, _, at = source.latest()
            assert abs(at - time.monotonic()) < 1.0
        finally:
            source.release()

    def test_implausible_timestamp_falls_back_to_arrival(self):
        assert PiCameraSource._capture_time({"SensorTimestamp": 12345}, 500.0) == 500.0
        assert PiCameraSource._capture_time({}, 7.0) == 7.0
        assert PiCameraSource._capture_time({"SensorTimestamp": 501_000_000_000}, 500.0) == 501.0


class TestFallbacksAndFailures:
    def test_pi4_gets_a_yuv420_lores_stream_converted_to_bgr(self, fake_module):
        FakePicamera2.pi4 = True
        source = _source(fake_module)
        try:
            assert wait_for(lambda: source.latest_lores()[0] is not None)
            lores, _, _ = source.latest_lores()
            assert source.lores_format == "YUV420" and lores.shape == (480, 640, 3)
            camera = FakePicamera2.instances[0]
            assert [c["lores"]["format"] for c in camera.configure_calls] == ["RGB888", "YUV420"]
        finally:
            source.release()

    def test_capture_failure_reopens_the_camera(self, fake_module):
        FakePicamera2.fail_after = 3
        source = _source(fake_module)
        try:
            assert wait_for(lambda: len(FakePicamera2.instances) >= 2 and source.frames >= 5)
            assert source.errors == 1 and source.last_error is None, "recovered"
            assert FakePicamera2.instances[0].closed
        finally:
            source.release()

    def test_no_camera_detected(self, fake_module):
        FakePicamera2.cameras = []
        source = _source(fake_module)
        try:
            assert wait_for(lambda: source.errors >= 1)
            assert not source.online and source.latest()[0] is None
            assert "ribbon" in source.last_error and FakePicamera2.instances == []
        finally:
            source.release()

    def test_picamera2_not_installed(self, monkeypatch):
        def missing():
            raise ImportError("No module named 'picamera2'")

        monkeypatch.setattr(picamera, "_import_picamera2", missing)
        source = PiCameraSource(reconnect_seconds=0.05)
        try:
            assert wait_for(lambda: source.errors >= 1)
            assert "python3-picamera2" in source.last_error
            assert "--system-site-packages" in source.last_error
            assert not source.online and source.read() == (False, None)
        finally:
            source.release()


class TestSpecs:
    @pytest.mark.parametrize("spec,expected", [
        ("picamera", {"camera_num": 0}),
        ("picamera:1", {"camera_num": 1}),
        ("PiCamera:0?fps=15", {"camera_num": 0, "fps": 15.0}),
        ("picamera:0?main=1920x1080&lores=320x240",
         {"camera_num": 0, "main_size": (1920, 1080), "lores_size": (320, 240)}),
    ])
    def test_parse(self, spec, expected):
        assert parse_spec(spec) == expected

    @pytest.mark.parametrize("spec", ["picamera:x", "picamera:0?fps=500", "picamera?main=big",
                                      "webcam:0"])
    def test_bad_specs(self, spec):
        with pytest.raises(ValueError):
            parse_spec(spec)

    def test_build_source(self, fake_module, monkeypatch):
        monkeypatch.setattr(picamera, "_import_picamera2", lambda: fake_module)
        source = build_source("picamera:1?fps=15&main=1920x1080")
        try:
            assert isinstance(source, PiCameraSource) and source.name == "picamera:1"
            assert source.fps == 15.0 and source.main_size == (1920, 1080)
        finally:
            source.release()

    def test_default_sizes_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("SENTRIX_PICAMERA_MAIN", "1536x864")
        monkeypatch.setenv("SENTRIX_PICAMERA_LORES", "bogus")
        source = PiCameraSource(start=False)
        assert source.main_size == (1536, 864) and source.lores_size == (640, 480)


class TestRunLive:
    def test_the_live_loop_streams_the_pi_camera(self, fake_module):
        """``Pipeline.run_live`` needs nothing but ``latest``/``online``."""
        from sentrix.config import settings
        from sentrix.pipeline.engine import Pipeline

        pipeline = Pipeline.__new__(Pipeline)
        pipeline.cfg = settings
        pipeline.source = _source(fake_module, fps=30.0, main_size=(320, 240),
                                  lores_size=(160, 120))
        pipeline._init_live()
        seen = []
        pipeline.process = lambda frame, now=None, label=None: seen.append(frame.shape) or {}
        stop = threading.Event()
        thread = threading.Thread(target=pipeline.run_live, args=(stop,), daemon=True)
        thread.start()
        try:
            assert wait_for(lambda: pipeline.preview_seq >= 5 and seen)
        finally:
            stop.set()
            thread.join(timeout=5.0)
            pipeline.source.release()
        assert not thread.is_alive()
        assert seen[0] == (240, 320, 3)
        seq, jpeg = pipeline.latest_jpeg()
        assert jpeg[:2] == b"\xff\xd8"
