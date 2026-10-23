"""A USB microphone plugged in after start-up, or unplugged and plugged back,
is picked up without restarting SENTRIX (demo: plug the mic into a running Pi).

No real audio device is touched: ``sounddevice`` is replaced by a fake whose
device list the test changes, the way a USB hot-plug changes PortAudio's list
after ``_terminate()`` / ``_initialize()``.
"""
from __future__ import annotations

import time
from dataclasses import replace

import numpy as np
import pytest

import sentrix.perception.audio as A
from sentrix.config import settings


class FakeStream:
    def __init__(self, sd, callback):
        self.sd, self.callback = sd, callback
        self.active = False
        self.closed = False

    def start(self):
        self.active = True
        self.sd.streams.append(self)

    def stop(self):
        self.active = False

    def close(self):
        self.closed = True

    def feed(self, seconds: float = 1.0, rate: int = 16000):
        frames = int(rate * seconds)
        info = type("T", (), {"inputBufferAdcTime": 0.0, "currentTime": 0.0})()
        self.callback(np.zeros((frames, 1), dtype=np.float32), frames, info, None)


class FakeSoundDevice:
    """PortAudio as seen by sounddevice: the device list only changes after a
    re-initialisation, as with a real USB hot-plug."""

    def __init__(self):
        self.plugged = False          # what is physically connected
        self.visible = False          # what PortAudio enumerated at its last init
        self.refreshes = 0
        self.streams: list[FakeStream] = []

    def _terminate(self):
        pass

    def _initialize(self):
        self.refreshes += 1
        self.visible = self.plugged

    def query_devices(self, kind=None):
        if not self.visible:
            raise RuntimeError("no input device")
        return {"name": "USB PnP Sound Device"}

    def InputStream(self, **kwargs):
        if not (self.visible and self.plugged):
            raise RuntimeError("device unavailable")
        return FakeStream(self, kwargs["callback"])


@pytest.fixture
def fake_sd(monkeypatch):
    sd = FakeSoundDevice()
    monkeypatch.setattr(A, "sd", sd)
    monkeypatch.setattr(A, "_SD", True)
    monkeypatch.setattr(A, "settings", replace(
        settings, audio=replace(settings.audio, model="off",
                                mic_retry_seconds=0.5, mic_stall_seconds=1.0)))
    return sd


def _wait(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _engine():
    engine = A.AudioEngine(capture=False)
    engine._start_capture()
    return engine


def test_microphone_plugged_in_after_start_is_picked_up(fake_sd):
    engine = _engine()
    try:
        assert engine.available is False            # nothing plugged at start
        fake_sd.plugged = True                      # the demo: plug the USB mic in
        assert _wait(lambda: engine.available), "the plugged microphone was never opened"
        assert engine.microphone == "USB PnP Sound Device"
        assert fake_sd.refreshes >= 1               # PortAudio re-scanned its devices
    finally:
        engine.stop()


def test_microphone_present_at_start_opens_at_once(fake_sd):
    fake_sd.plugged = fake_sd.visible = True
    engine = _engine()
    try:
        assert engine.available is True
        assert len(fake_sd.streams) == 1
    finally:
        engine.stop()


def test_unplugged_microphone_is_reconnected(fake_sd):
    fake_sd.plugged = fake_sd.visible = True
    engine = _engine()
    try:
        first = fake_sd.streams[0]
        first.feed()
        # unplug: the stream stops delivering and PortAudio loses the device
        fake_sd.plugged = False
        first.active = False
        assert _wait(lambda: not engine.available), "the lost microphone was not noticed"
        assert first.closed
        fake_sd.plugged = True                      # plug it back in
        assert _wait(lambda: engine.available), "the microphone was not reconnected"
        assert len(fake_sd.streams) == 2
    finally:
        engine.stop()


def test_silent_stream_counts_as_unplugged(fake_sd):
    fake_sd.plugged = fake_sd.visible = True
    engine = _engine()
    try:
        assert engine.available
        # the stream stays "active" but no data arrives (a stalled device)
        assert _wait(lambda: not engine.available, timeout=6.0)
    finally:
        engine.stop()


def test_stop_ends_the_watcher(fake_sd):
    engine = _engine()
    engine.stop()
    assert engine._watcher is not None
    assert not engine._watcher.is_alive()
