"""Training recordings in the live loop (D20).

The camera's own frame reaches the recorder on the capture thread before the
HUD is drawn on a copy, so the dashboard shows "REC mm:ss" and the file holds
only the camera's pixels; the microphone's blocks arrive through the audio
engine's sample listener; shutdown saves a recording in progress; and nothing
records until ``start_recording`` is called (replays, evaluations and benches
drive the same pipeline). A fake camera drives ``Pipeline.run_live`` as in
``tests/test_live.py``: no model, no device, files in a temporary directory.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from sentrix import paths
from sentrix.config import settings
from sentrix.io.recorder import RecorderError, open_mjpeg_writer
from sentrix.io.sources import FrameSource
from sentrix.pipeline.engine import Pipeline

GREY = 90
HEIGHT, WIDTH = 240, 320
FPS = 20.0
#: Where the REC badge goes on a 320 px preview (under the banner, right side).
BADGE = (slice(82, 102), slice(190, WIDTH))


class StampedCamera(FrameSource):
    """A 20 fps camera: a flat grey frame per period, stamped with its
    monotonic capture time, that declares its rate like Picamera2 does."""

    name = "capture:rtsp://admin:hunter2@10.0.0.9/stream"

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.released_while_recording: bool | None = None
        self.recorder = None

    def read(self):
        return True, self._frame()

    @staticmethod
    def _frame() -> np.ndarray:
        return np.full((HEIGHT, WIDTH, 3), GREY, np.uint8)

    def latest(self, after_seq: int = -1):
        seq = int((time.monotonic() - self.started) * FPS) + 1
        if seq == after_seq:
            return None, seq, 0.0
        return self._frame(), seq, self.started + seq / FPS

    def status(self) -> dict:
        return {"name": self.name, "online": True, "fps": FPS}

    def release(self) -> None:
        self.released_while_recording = bool(self.recorder and self.recorder.active)


class Events:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict]] = []

    def audit(self, event, **detail):
        self.rows.append((event, detail))

    def close(self) -> None:
        pass

    def names(self) -> list[str]:
        return [event for event, _ in self.rows]


def live_pipeline(audio=None) -> Pipeline:
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.cfg = settings
    pipeline.source = StampedCamera()
    pipeline.events = Events()
    pipeline.audio = audio
    pipeline.session_id = "sess-rec"
    pipeline._init_live()
    pipeline.process = lambda frame, now=None, label=None: {}
    return pipeline


def wait_for(condition, timeout: float = 10.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.01)


def red_pixels(image: np.ndarray) -> int:
    b, g, r = image[..., 0].astype(int), image[..., 1].astype(int), image[..., 2].astype(int)
    return int(((r > 200) & (g < 80) & (b < 80)).sum())


def read_frames(path) -> list[np.ndarray]:
    import cv2
    capture = cv2.VideoCapture(str(path))
    frames = []
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            frames.append(frame)
    finally:
        capture.release()
    return frames


@pytest.fixture
def recordings(tmp_path, monkeypatch):
    """Recordings go to a temporary directory; the free-space floor is off so
    the tests do not depend on this machine's disk."""
    from sentrix.io import recorder as recorder_module
    directory = tmp_path / "recordings"
    monkeypatch.setattr(paths, "RECORDINGS_DIR", directory)
    monkeypatch.setattr(recorder_module, "settings", replace(
        settings, recording=replace(settings.recording, min_free_mb=0)))
    return directory


@pytest.fixture
def live():
    """``start(pipeline)`` runs the live loop on a thread; stopped afterwards."""
    running = []

    def start(pipeline):
        stop = threading.Event()
        thread = threading.Thread(target=pipeline.run_live, args=(stop,), daemon=True)
        thread.start()
        running.append((pipeline, stop, thread))
        wait_for(lambda: pipeline._raw is not None, what="the first camera frame")
        return stop, thread

    yield start
    for pipeline, stop, thread in running:
        stop.set()
        thread.join(timeout=5.0)
        pipeline._close_recorder()


def sidecar(directory, name) -> dict:
    return json.loads((directory / f"{name}.json").read_text(encoding="utf-8"))


class TestRecordingFromTheLiveLoop:
    def test_frames_reach_the_recorder_and_the_file_has_every_one(self, recordings, live):
        pipeline = live_pipeline()
        live(pipeline)
        status = pipeline.start_recording(label="Replica gun", by="lc:device_admin")
        assert status["recording"] and status["fps"] == FPS
        assert status["microphone"] is None, "audio off: no microphone"
        recorder = pipeline.recorder
        wait_for(lambda: recorder.status()["frames"] >= 15, what="15 recorded frames")
        stopped = pipeline.stop_recording(by="lc:device_admin")
        assert stopped["finalizing"] or not stopped["active"]
        assert recorder.wait(10.0)

        items = recorder.list()
        assert len(items) == 1
        name = items[0]["name"]
        assert name.startswith("rec_") and name.endswith("_replica_gun")
        meta = sidecar(recordings, name)
        assert meta["stop_reason"] == "user" and meta["by"] == "lc:device_admin"
        assert meta["fps"] == FPS and meta["size"] == [WIDTH, HEIGHT] and meta["frames"] >= 15
        frames = read_frames(recordings / items[0]["files"]["video"])
        assert len(frames) == meta["frames"]
        context = meta["context"]
        assert context["camera"].startswith("capture:rtsp://***@") and "hunter2" not in \
            json.dumps(meta)
        assert context["frame_size"] == [WIDTH, HEIGHT] and context["fps"] == FPS
        assert context["profile"] == settings.node.profile and context["session"] == "sess-rec"
        assert {"recording_started", "recording_stopped"} <= set(pipeline.events.names())

    def test_rec_shows_on_the_preview_and_never_in_the_recording(self, recordings, live):
        pipeline = live_pipeline()
        live(pipeline)
        wait_for(lambda: pipeline.preview_seq > 3, what="preview frames")
        assert red_pixels(pipeline.latest_frame()[BADGE]) == 0, "no badge before recording"

        pipeline.start_recording(label="", by="lc:device_admin")
        seen = pipeline.preview_seq
        wait_for(lambda: pipeline.preview_seq > seen + 2, what="preview frames while recording")
        assert red_pixels(pipeline.latest_frame()[BADGE]) > 20, "the REC badge on the preview"
        recorder = pipeline.recorder
        wait_for(lambda: recorder.status()["frames"] >= 10, what="10 recorded frames")
        pipeline.stop_recording(by="lc:device_admin")
        assert recorder.wait(10.0)

        frames = read_frames(recordings / recorder.list()[0]["files"]["video"])
        assert frames
        for frame in frames:
            assert red_pixels(frame) == 0, "the REC badge leaked into the recording"
            # No HUD banner either: the frame is the camera's flat grey.
            assert int(np.abs(frame.astype(int) - GREY).max()) <= 8
        seen = pipeline.preview_seq
        wait_for(lambda: pipeline.preview_seq > seen + 2, what="preview frames after stop")
        assert red_pixels(pipeline.latest_frame()[BADGE]) == 0, "the badge goes with the recording"

    def test_microphone_blocks_reach_the_recorder(self, recordings, live, monkeypatch):
        from sentrix.perception import audio as A
        monkeypatch.setattr(A, "settings", replace(settings, audio=replace(settings.audio,
                                                                           model="off")))
        engine = A.AudioEngine(capture=False)            # no microphone thread, no model
        pipeline = live_pipeline(audio=engine)
        live(pipeline)
        status = pipeline.start_recording(label="talking", by="lc:device_admin")
        assert status["microphone"]["available"] is False   # capture=False: none open
        recorder = pipeline.recorder
        rate = settings.windows.audio_sample_rate
        tone = (0.2 * np.sin(np.arange(rate // 10) * 2 * np.pi * 440 / rate)).astype(np.float32)
        end = time.monotonic() + 1.2
        while time.monotonic() < end:
            engine.process_block(tone, time.monotonic())   # what the capture worker does
            time.sleep(0.1)
        wait_for(lambda: recorder.status()["audio"]["present"], what="audio in the recording")
        pipeline.stop_recording(by="lc:device_admin")
        assert recorder.wait(15.0)

        item = recorder.list()[0]
        meta = sidecar(recordings, item["name"])
        assert item["has_audio"] and meta["audio"]["present"] and meta["audio"]["rate"] == 16000
        assert abs(meta["audio"]["seconds"] - meta["frames"] / meta["fps"]) < 0.01, \
            "the sound ends with the video"
        # One AVI with its sound when ffmpeg is installed, else a separate WAV.
        if recorder.ffmpeg_path() and not meta["error"]:
            assert item["files"]["audio"] is None and meta["format"]["audio"] == "pcm_s16le"
        else:
            assert (recordings / item["files"]["audio"]).is_file()

        pipeline._close_recorder()
        assert pipeline._recorder_listener is None and engine._sample_listeners == []

    def test_shutdown_saves_a_recording_in_progress(self, recordings, live):
        pipeline = live_pipeline()
        stop, thread = live(pipeline)
        pipeline.start_recording(label="evening", by="lc:device_admin")
        recorder = pipeline.recorder
        pipeline.source.recorder = recorder
        wait_for(lambda: recorder.status()["frames"] >= 5, what="5 recorded frames")
        # What the lifespan does: the live loop first, then the pipeline.
        stop.set()
        thread.join(timeout=5.0)
        pipeline._stop = threading.Event()
        pipeline._tasks = queue.Queue(maxsize=4)
        pipeline._worker = threading.Thread(target=pipeline._task_loop, daemon=True)
        pipeline._worker.start()
        pipeline.incidents = SimpleNamespace(current=None)
        pipeline.shutdown()

        assert not recorder.active
        assert pipeline.source.released_while_recording is False, \
            "the recording is saved before the camera is released"
        items = recorder.list()
        assert len(items) == 1
        meta = sidecar(recordings, items[0]["name"])
        assert meta["stop_reason"] == "shutdown" and meta["frames"] >= 5
        assert not list(recordings.glob("*.part.*")), "no half-written file is left"

    def test_the_live_loop_stops_a_recording_as_shutdown_when_it_exits(self, recordings, live):
        # Shutdown waits up to 5 s for the inference thread (a clip mid-run on
        # the Pi) before it closes the recorder; no frame arrives meanwhile,
        # and the writer's camera-gap check would save it as camera_lost.
        pipeline = live_pipeline()
        stop, thread = live(pipeline)
        pipeline.start_recording(label="", by="lc:device_admin")
        recorder = pipeline.recorder
        wait_for(lambda: recorder.status()["frames"] >= 5, what="5 recorded frames")
        stop.set()
        thread.join(timeout=5.0)
        assert not recorder.wants_frames, "stopped as the loop exited, not at close()"
        assert recorder.status()["stop_reason"] == "shutdown"
        assert recorder.wait(10.0)
        assert sidecar(recordings, recorder.list()[0]["name"])["stop_reason"] == "shutdown"

    def test_the_live_loop_reports_whether_the_microphone_delivers(self, recordings, live):
        # The dot and the Portal heartbeat follow the engine (what
        # /api/health says), not whether a window was classified.
        from sentrix.pipeline.state import state
        audio = SimpleNamespace(available=True)
        pipeline = live_pipeline(audio=audio)
        stop, thread = live(pipeline)
        try:
            wait_for(lambda: state.get().get("mic_delivering") is True, what="mic_delivering")
            assert state.get()["mic_available"] is True
            audio.available = False
            wait_for(lambda: state.get().get("mic_delivering") is False, what="mic gone")
            assert state.get()["mic_available"] is False
        finally:
            stop.set()
            thread.join(timeout=5.0)
            state.update(mic_delivering=None)

    def test_the_recorder_never_starts_by_itself(self, recordings, live):
        pipeline = live_pipeline()
        live(pipeline)
        wait_for(lambda: pipeline.preview_seq > 5, what="preview frames")
        assert pipeline.recorder is None, "nothing builds a recorder but a request"
        assert not recordings.exists() or not any(recordings.iterdir())

        recorder = pipeline.get_recorder()
        assert recorder is pipeline.get_recorder() and not recorder.active
        seen = pipeline.preview_seq
        wait_for(lambda: pipeline.preview_seq > seen + 5, what="more preview frames")
        assert not recorder.active and recorder.list() == []
        assert not recordings.exists() or not any(recordings.iterdir())


class TestStartRules:
    def test_refused_before_the_first_frame_and_while_the_camera_is_offline(self, recordings):
        pipeline = live_pipeline()
        with pytest.raises(RecorderError) as refused:
            pipeline.start_recording(label="x", by="lc:device_admin")
        assert refused.value.code == "conflict" and refused.value.status == 409
        assert "camera" in refused.value.message
        assert pipeline.recorder is None, "a refusal builds nothing"
        pipeline._raw = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
        pipeline._offline = True
        with pytest.raises(RecorderError):
            pipeline.start_recording(label="x", by="lc:device_admin")

    def test_already_recording_and_nothing_to_stop(self, recordings):
        pipeline = live_pipeline()
        pipeline._raw = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
        try:
            pipeline.start_recording(label="a", by="lc:device_admin")
            with pytest.raises(RecorderError) as busy:
                pipeline.start_recording(label="b", by="lc:device_admin")
            assert busy.value.code == "conflict"
            # The refused start (a second tab, a second admin) leaves the REC
            # badge counting from the real start, not stuck at 00:00.
            assert pipeline._recording_elapsed(time.monotonic() + 125) >= 125
            pipeline.stop_recording(by="lc:device_admin")
            assert pipeline.recorder.wait(10.0)
            assert pipeline._recording_elapsed(time.monotonic()) is None, "no badge when idle"
            with pytest.raises(RecorderError) as idle:
                pipeline.stop_recording(by="lc:device_admin")
            assert idle.value.code == "conflict"
        finally:
            pipeline._close_recorder()

    @pytest.mark.parametrize("declared, measured, expected", [
        (15.0, 0.0, 15.0),           # Picamera2 declares its rate
        (60.0, 0.0, 30.0),           # clamped
        (2.0, 0.0, 5.0),
        (None, 14.2, 15.0),          # a webcam asked for 30 delivering 15
        (None, 15.5, 15.0),          # a 15 fps camera's window jitter
        (None, 7.5, 8.0),            # 30/4 in a dark room: never down to 5 (drops a third)
        (None, 7.4, 8.0),
        (None, 27.0, 30.0),          # between steps: up, repeating, never dropping
        (None, 40.0, 30.0),
        (None, 0.0, None),           # nothing known: the configured capture rate
    ])
    def test_recording_fps(self, declared, measured, expected):
        pipeline = live_pipeline()
        pipeline.source.status = (lambda: {"fps": declared}) if declared else (lambda: {})
        pipeline._video_fps = measured
        want = expected if expected is not None else min(30.0, max(
            5.0, float(settings.pipeline.capture_fps)))
        assert pipeline.recording_fps() == want


class TestRecorderSetUp:
    def test_built_once_recovering_a_crash_and_pruning_old_recordings(self, recordings):
        recordings.mkdir(parents=True)
        # A recording a power cut left half-written ...
        writer = open_mjpeg_writer(recordings / "rec_20261020_101500_crash.part.avi", 10.0,
                                   (64, 48), 90)
        for _ in range(5):
            writer.write(np.full((48, 64, 3), 120, np.uint8))
        writer.release()
        # ... and one past keep_days.
        old = "rec_20200101_000000_old"
        (recordings / f"{old}.avi").write_bytes(b"RIFF....AVI ")
        (recordings / f"{old}.json").write_text(json.dumps(
            {"name": old, "created_at": "2020-01-01T00:00:00+00:00", "seconds": 1.0}),
            encoding="utf-8")

        pipeline = live_pipeline()
        recorder = pipeline.get_recorder()
        assert pipeline.get_recorder() is recorder
        names = [item["name"] for item in recorder.list()]
        assert names == ["rec_20261020_101500_crash"]
        assert recorder.list()[0]["interrupted"]
        events = pipeline.events.names()
        assert events.count("recording_recovered") == 1 and events.count("recording_pruned") == 1
        pipeline.get_recorder()
        assert pipeline.events.names() == events, "recovery and pruning run once"

    def test_the_microphone_listener_is_added_once(self, recordings, monkeypatch):
        from sentrix.perception import audio as A
        monkeypatch.setattr(A, "settings", replace(settings, audio=replace(settings.audio,
                                                                           model="off")))
        engine = A.AudioEngine(capture=False)
        pipeline = live_pipeline(audio=engine)
        pipeline.get_recorder()
        pipeline.get_recorder()
        assert engine._sample_listeners == [pipeline._recorder_listener]
        pipeline._close_recorder()
        assert engine._sample_listeners == []

    def test_the_app_starts_the_hourly_housekeeping(self, recordings):
        from sentrix import app as app_module
        started = threading.Event()
        recorder = SimpleNamespace(start_housekeeping=started.set)
        app_module._prepare_recordings(SimpleNamespace(get_recorder=lambda: recorder))
        assert started.wait(10.0), "nothing would prune on a device nobody restarts"

    def test_a_replayed_soundtrack_has_no_listener(self, recordings):
        pipeline = live_pipeline(audio=SimpleNamespace(result_for=lambda t: (None, {})))
        pipeline.get_recorder()
        assert pipeline._recorder_listener is None
        assert pipeline.microphone_status() is None
