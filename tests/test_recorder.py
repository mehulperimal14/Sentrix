"""Training recorder (D20): the constant-frame-rate grid, audio alignment, the
frame queue, auto-stops, refusals, file names and serving rules, retention,
crash recovery and the ffmpeg mux.

Nothing opens a camera or a microphone: frames are synthetic numpy arrays and
audio blocks are generated samples handed to the taps. The recorder's real
writer thread runs; every wait is on a condition with a generous deadline,
never a fixed sleep. The clocks are fakes the test moves by hand.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from sentrix.config import settings
from sentrix.io import recorder as recorder_mod
from sentrix.io.recorder import Recorder, RecorderError, sanitize_label

T0 = 100.0
WALL = 1_790_000_000.0
DAY = 86400.0
MB = 1024 * 1024
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Disk:
    def __init__(self, free: int = 10**12) -> None:
        self.free = free

    def __call__(self, path):
        return SimpleNamespace(total=2 * 10**12, used=0, free=self.free)


class FakeWriter:
    """Keeps the frames it is given; its file grows 16 bytes a frame."""

    def __init__(self, path, fps, size, quality, factory) -> None:
        self.path, self.fps, self.size, self.quality = Path(path), fps, size, quality
        self.factory = factory
        self.frames: list[np.ndarray] = []
        self.released = False
        self.path.write_bytes(b"RIFF")

    def isOpened(self) -> bool:
        return True

    def write(self, frame) -> None:
        if self.factory.block and not self.factory.entered.is_set():
            self.factory.entered.set()
            self.factory.release.wait(10.0)
        if self.factory.fail_on is not None and len(self.frames) + 1 >= self.factory.fail_on:
            raise OSError("disk went away")
        self.frames.append(frame)
        with self.path.open("ab") as handle:
            handle.write(bytes(16))

    def release(self) -> None:
        self.released = True

    def values(self) -> list[int]:
        return [int(f[0, 0, 0]) for f in self.frames]


class WriterFactory:
    """``block=True``: the first write waits for ``release`` (a writer that
    has fallen behind), after setting ``entered``. ``fail_on=n``: the n-th
    write raises (the disk went away mid-recording)."""

    def __init__(self, block: bool = False) -> None:
        self.block = block
        self.fail_on: int | None = None
        self.entered = threading.Event()
        self.release = threading.Event()
        self.writers: list[FakeWriter] = []

    def __call__(self, path, fps, size, quality) -> FakeWriter:
        writer = FakeWriter(path, fps, size, quality, self)
        self.writers.append(writer)
        return writer


class FakeRun:
    """``subprocess.run`` for ffmpeg: copies the first input to the output
    (last argument), or fails."""

    def __init__(self, returncode: int = 0, stderr: bytes = b"", raises: Exception | None = None):
        self.returncode, self.stderr, self.raises = returncode, stderr, raises
        self.calls: list[tuple[list, dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.raises is not None:
            raise self.raises
        if self.returncode == 0:
            shutil.copyfile(argv[argv.index("-i") + 1], argv[-1])
        return SimpleNamespace(returncode=self.returncode, stdout=b"", stderr=self.stderr)


def _cfg(**over):
    values = dict(enabled=True, max_seconds=600.0, min_free_mb=1, max_total_mb=1000,
                  keep_days=14.0, jpeg_quality=90, max_width=1280, queue_seconds=60.0,
                  audio_tail_seconds=0.0, camera_gap_seconds=5.0)
    values.update(over)
    return dataclasses.replace(settings.recording, **values)


class Rig:
    def __init__(self, tmp_path: Path, *, ffmpeg=None, run=None, real_writer=False,
                 block=False, **cfg) -> None:
        self.dir = tmp_path / "recordings"
        self.clock = Clock(T0)
        self.wall = Clock(WALL)
        self.disk = Disk()
        self.factory = WriterFactory(block=block)
        self.audits: list[tuple[str, dict]] = []
        self.seq = 0
        self.recorder = Recorder(
            self.dir, _cfg(**cfg), clock=self.clock, wall=self.wall, ffmpeg=ffmpeg,
            run=run or FakeRun(raises=AssertionError("ffmpeg must not run")),
            writer_factory=None if real_writer else self.factory, disk_usage=self.disk,
            audit=lambda event, **detail: self.audits.append((event, detail)))

    def feed(self, times, *, first_value: int = 0, size=(64, 48)) -> None:
        """One frame per time (seconds after T0), frame k filled with
        ``first_value + k`` so the written order can be read back."""
        width, height = size
        for k, t in enumerate(times):
            self.seq += 1
            frame = np.full((height, width, 3), (first_value + k) % 256, np.uint8)
            self.recorder.on_frame(frame, self.seq, T0 + t)

    def audio(self, start: float, end: float, value, rate: int = 16000) -> None:
        """A block covering ``[start, end]`` seconds after T0."""
        count = int(round((end - start) * rate))
        samples = (np.full(count, value, np.float32) if np.isscalar(value)
                   else np.asarray(value, np.float32))
        self.recorder.on_audio(samples, T0 + end, rate)

    def finish(self, **stop) -> dict:
        self.recorder.stop(**stop)
        assert self.recorder.wait(timeout=30.0), "the recording was not saved in time"
        return self.recorder.status()

    def sidecar(self, name: str) -> dict:
        return json.loads((self.dir / f"{name}.json").read_text(encoding="utf-8"))

    def events(self, name: str) -> list[dict]:
        return [detail for event, detail in self.audits if event == name]

    def close(self) -> None:
        self.factory.release.set()
        self.recorder.close(timeout=10.0)


@pytest.fixture
def rig(tmp_path):
    made: list[Rig] = []

    def make(**kwargs) -> Rig:
        made.append(Rig(tmp_path, **kwargs))
        return made[-1]

    yield make
    for each in made:
        each.close()


def _until(predicate, seconds: float = 10.0) -> None:
    """Poll ``predicate`` (state another thread changes) up to a generous
    deadline."""
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.005)


def _pcm(value: float) -> int:
    return int(np.rint(value * 32767.0))


def _wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as reader:
        layout = reader.getframerate(), reader.getnchannels(), reader.getsampwidth()
        assert layout == (16000, 1, 2)
        return np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2")


def _read_avi(path: Path) -> tuple[int, float]:
    import cv2
    capture = cv2.VideoCapture(str(path))
    try:
        assert capture.isOpened(), f"cv2 cannot open {path}"
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        frames = 0
        while True:
            ok, _ = capture.read()
            if not ok:
                break
            frames += 1
        return frames, fps
    finally:
        capture.release()


def _audio_streams(path: Path) -> list[dict]:
    """The audio streams ffprobe (or ffmpeg's banner) reports."""
    if FFPROBE:
        out = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "a",
                              "-show_entries", "stream=codec_name,sample_rate,channels",
                              "-of", "json", str(path)], capture_output=True, text=True,
                             timeout=60, stdin=subprocess.DEVNULL)
        return json.loads(out.stdout or "{}").get("streams", [])
    out = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True,
                         text=True, timeout=60, stdin=subprocess.DEVNULL)
    return [{"codec_name": "pcm_s16le"}] if "Audio: pcm_s16le" in out.stderr else []


def _real_clip(path: Path, frames: int, fps: float = 10.0, size=(64, 48)) -> Path:
    writer = recorder_mod.open_mjpeg_writer(path, fps, size, 90)
    assert writer.isOpened()
    for k in range(frames):
        writer.write(np.full((size[1], size[0], 3), (k * 8) % 256, np.uint8))
    writer.release()
    return path


def _crashed_clip(path: Path, frames: int) -> Path:
    """An AVI cut before its index (OpenCV writes ``idx1`` on release). Its
    headers still hold the final sizes and frame count; :func:`_killed_clip`
    is what a real crash leaves."""
    full = _real_clip(path.with_name("whole.avi"), frames)
    data = full.read_bytes()
    full.unlink()
    path.write_bytes(data[:data.rfind(b"idx1")])
    return path


_KILL_WRITER = """
import os, sys
import numpy as np
from sentrix.io.recorder import open_mjpeg_writer
path, frames, fps = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
writer = open_mjpeg_writer(path, fps, (320, 240), 90)
assert writer.isOpened()
rng = np.random.default_rng(7)
for _ in range(frames):
    writer.write(rng.integers(0, 256, (240, 320, 3), dtype=np.uint8))
os._exit(0)                      # the power cut: no release(), nothing flushed
"""


def _killed_clip(path: Path, frames: int = 60, fps: float = 10.0) -> Path:
    """An AVI as a real crash leaves it: a process writes ``frames`` frames
    with the recorder's own writer and dies without ``release()``. The RIFF
    and movi sizes and the frame count in the headers stay 0, there is no
    index, and the frame OpenCV still buffered is lost. Noisy 320x240 frames,
    so enough reaches the disk (a few small flat frames would leave 0
    bytes)."""
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(root))
    proc = subprocess.run([sys.executable, "-c", _KILL_WRITER, str(path), str(frames), str(fps)],
                          cwd=str(root), env=env, capture_output=True, timeout=120,
                          stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[-2000:]
    data = path.read_bytes()
    assert data[:4] == b"RIFF" and int.from_bytes(data[4:8], "little") == 0, \
        "not a crashed file: the RIFF size was patched"
    assert b"idx1" not in data
    return path


def _tone_wav(path: Path, seconds: float, value: float = 0.25) -> Path:
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(np.full(int(seconds * 16000), _pcm(value), "<i2").tobytes())
    return path


def _name(wall: float = WALL, label: str = "") -> str:
    base = "rec_" + time.strftime("%Y%m%d_%H%M%S", time.localtime(wall))
    return f"{base}_{label}" if label else base


# ── the constant-frame-rate grid ──────────────────────────────────────────────
class TestGrid:
    def test_slots_repeat_the_previous_frame_and_drop_extra_ones(self, rig):
        r = rig()
        status = r.recorder.start(label="grid", fps=10)
        assert status["recording"] and status["fps"] == 10.0
        # 0.14 s falls in slot 1, already taken: dropped. 0.5 s is slot 5:
        # slots 3 and 4 repeat the frame of 0.2 s.
        r.feed([0.0, 0.1, 0.14, 0.2, 0.5, 0.6], first_value=10)
        status = r.finish()
        writer = r.factory.writers[0]
        assert writer.values() == [10, 11, 13, 13, 13, 14, 15]
        assert writer.fps == 10.0 and writer.size == (64, 48) and writer.quality == 90
        assert writer.released
        assert (status["frames"], status["duplicated"], status["dropped"]) == (7, 2, 1)
        meta = r.sidecar(status["name"])
        assert (meta["frames"], meta["duplicated"], meta["dropped"]) == (7, 2, 1)
        assert meta["seconds"] == pytest.approx(0.7) and meta["fps"] == 10.0
        assert meta["size"] == [64, 48] and meta["stop_reason"] == "user"
        assert meta["files"] == {"video": f"{status['name']}.avi", "audio": None,
                                 "meta": f"{status['name']}.json"}
        assert meta["audio"]["present"] is False and meta["interrupted"] is False

    def test_fps_defaults_to_15_and_is_clamped(self, rig):
        r = rig()
        assert r.recorder.start()["fps"] == 15.0
        r.finish()
        assert r.recorder.start(fps=100)["fps"] == 30.0
        r.finish()
        assert r.recorder.start(fps=1)["fps"] == 5.0
        r.finish()
        with pytest.raises(RecorderError) as err:
            r.recorder.start(fps="fast")
        assert (err.value.code, err.value.status) == ("invalid_args", 400)

    @pytest.mark.parametrize("asked, recorded", [(12.5, 12.0), (7.5, 8.0), (29.97, 30.0),
                                                  (14.6, 15.0)])
    def test_a_fractional_rate_is_recorded_at_the_whole_rate_the_file_stores(self, rig, asked,
                                                                             recorded):
        r = rig()
        assert r.recorder.start(fps=asked)["fps"] == recorded
        r.feed([0.0])
        status = r.finish()
        assert r.factory.writers[0].fps == recorded
        assert r.sidecar(status["name"])["fps"] == recorded

    def test_a_fractional_rate_keeps_the_sidecar_the_sound_and_the_file_together(self, rig):
        # OpenCV's AVI header stores 12.5 as 12/1: recorded on a 12.5 grid,
        # the file would play 4 % longer than its sound and its sidecar.
        r = rig(real_writer=True)
        name = r.recorder.start(fps=12.5)["name"]
        r.feed([k / 12 for k in range(24)])
        r.audio(0.0, 2.0, 0.25)
        status = r.finish()
        meta = r.sidecar(name)
        frames, fps = _read_avi(r.dir / f"{name}.avi")
        assert meta["fps"] == fps == status["fps"] == 12.0
        assert frames == meta["frames"] == 24 and meta["seconds"] == pytest.approx(2.0)
        assert len(_wav(r.dir / f"{name}.wav")) == 32000, "2.0 s of sound under 2.0 s of video"

    def test_the_writer_encodes_on_its_own_thread_at_the_asked_quality(self, tmp_path):
        # Given at open, both are ignored: quality 75, and each frame split
        # across OpenCV's shared thread pool, outside the writer's priority.
        import cv2
        writer = recorder_mod.open_mjpeg_writer(tmp_path / "q.avi", 10.0, (64, 48), 90)
        try:
            assert writer.isOpened()
            assert writer.get(cv2.VIDEOWRITER_PROP_QUALITY) == 90.0
            assert writer.get(cv2.VIDEOWRITER_PROP_NSTRIPES) == 1.0
        finally:
            writer.release()

    def test_real_mjpeg_writer_reads_back_at_a_constant_rate(self, rig):
        r = rig(real_writer=True)
        name = r.recorder.start(fps=10)["name"]
        # 20 slots, slot 5 missing: written as a repeat.
        r.feed([k / 10 for k in range(20) if k != 5])
        status = r.finish()
        assert status["frames"] == 20 and status["duplicated"] == 1
        frames, fps = _read_avi(r.dir / f"{name}.avi")
        assert frames == 20 and fps == pytest.approx(10.0)
        assert sorted(p.name for p in r.dir.iterdir()) == [f"{name}.avi", f"{name}.json"]

    def test_wide_frames_are_scaled_down_to_even_sides(self, rig):
        r = rig(max_width=641)
        r.recorder.start(fps=10)
        r.feed([0.0], size=(1001, 601))
        r.feed([0.1], size=(320, 240), first_value=1)      # another size: fitted too
        status = r.finish()
        writer = r.factory.writers[0]
        assert writer.size == (640, 384) and status["size"] == [640, 384]
        assert all(f.shape == (384, 640, 3) for f in writer.frames)

    def test_native_size_when_max_width_is_zero(self, rig):
        r = rig(max_width=0)
        r.recorder.start(fps=10)
        r.feed([0.0], size=(1001, 601))
        r.finish()
        assert r.factory.writers[0].size == (1001, 601)

    def test_taps_ignore_input_when_idle_and_never_raise(self, rig):
        r = rig()
        assert not r.recorder.wants_frames and not r.recorder.active
        r.feed([0.0])
        r.audio(0.0, 0.5, 0.1)
        assert r.factory.writers == []
        r.recorder.start(fps=10)
        assert r.recorder.wants_frames and r.recorder.active
        r.recorder.on_frame(None, 1, T0)
        r.recorder.on_frame(np.zeros((4, 4, 3), np.uint8), 2, float("nan"))
        r.recorder.on_audio(None, T0)
        r.recorder.on_audio(np.zeros(10, np.float32), "later")
        r.feed([0.0])
        status = r.finish()
        assert status["frames"] == 1
        assert not r.recorder.wants_frames and not r.recorder.active


# ── audio on the video's time line ────────────────────────────────────────────
class TestAudio:
    def _record(self, r: Rig, blocks, *, frames: int = 10, audio_first=()) -> tuple[str, dict]:
        name = r.recorder.start(fps=10, audio_device="USB mic")["name"]
        for block in audio_first:
            r.audio(*block)
        r.feed([k / 10 for k in range(frames)])
        for block in blocks:
            r.audio(*block)
        return name, r.finish()

    def test_sample_zero_is_frame_zero_and_the_track_ends_with_the_video(self, rig):
        r = rig()
        before, after = np.full(4000, -0.5, np.float32), np.full(4000, 0.25, np.float32)
        name, status = self._record(r, [(-0.25, 0.25, np.concatenate([before, after])),
                                        (0.25, 1.25, 0.25)])
        pcm = _wav(r.dir / f"{name}.wav")
        assert len(pcm) == 16000                         # 10 frames at 10 fps: 1.0 s
        assert np.all(pcm == _pcm(0.25)), "the part before frame 0 is cropped, the rest cut"
        assert status["audio"] == {"present": True, "device": "USB mic", "seconds": 1.0,
                                   "gaps": 0, "ignored": 0}
        meta = r.sidecar(name)
        assert meta["audio"]["rate"] == 16000 and meta["audio"]["present"] is True
        assert meta["files"]["audio"] == f"{name}.wav"
        assert meta["format"] == {"video": "mjpeg", "audio": "pcm_s16le"}

    def test_a_gap_is_zero_filled_and_an_overlap_trimmed(self, rig):
        r = rig()
        name, status = self._record(r, [(0.0, 0.25, 0.25), (0.5, 0.75, 0.5),
                                        (0.65, 1.0, 0.75)])
        pcm = _wav(r.dir / f"{name}.wav")
        assert len(pcm) == 16000
        assert np.all(pcm[:4000] == _pcm(0.25))
        assert np.all(pcm[4000:8000] == 0)
        assert np.all(pcm[8000:12000] == _pcm(0.5))
        assert np.all(pcm[12000:] == _pcm(0.75))
        assert status["audio"]["gaps"] == 1

    def test_a_short_track_is_padded_to_the_video(self, rig):
        r = rig()
        name, status = self._record(r, [(0.0, 0.5, 0.25)])
        pcm = _wav(r.dir / f"{name}.wav")
        assert len(pcm) == 16000
        assert np.all(pcm[:8000] == _pcm(0.25)) and np.all(pcm[8000:] == 0)
        assert status["audio"]["gaps"] == 0 and status["audio"]["seconds"] == 1.0

    def test_a_block_heard_before_the_first_frame_waits_for_it(self, rig):
        r = rig()
        name, _ = self._record(r, [(0.5, 1.0, 0.5)], audio_first=[(-0.5, 0.5, 0.25)])
        pcm = _wav(r.dir / f"{name}.wav")
        assert len(pcm) == 16000
        assert np.all(pcm[:8000] == _pcm(0.25)) and np.all(pcm[8000:] == _pcm(0.5))

    def test_a_block_at_another_rate_is_ignored_and_counted(self, rig):
        r = rig()
        name, status = self._record(r, [(0.0, 1.0, 0.25, 44100)])
        assert status["audio"]["present"] is False and status["audio"]["ignored"] == 1
        assert not (r.dir / f"{name}.wav").exists()
        assert r.sidecar(name)["files"]["audio"] is None

    def test_the_last_block_is_waited_for_after_stop(self, rig):
        r = rig(audio_tail_seconds=20.0)
        name = r.recorder.start(fps=10)["name"]
        r.feed([k / 10 for k in range(10)])
        r.audio(0.0, 0.5, 0.25)
        r.recorder.stop()
        # The microphone's block, one hop late: it arrives from the audio
        # thread while the writer is already saving, as it does live.
        late = threading.Timer(0.4, r.audio, args=(0.5, 1.0, 0.5))
        late.start()
        started = time.monotonic()
        try:
            assert r.recorder.wait(timeout=30.0)
        finally:
            late.join()
        assert time.monotonic() - started < 15.0, "covered: no need to wait out the tail"
        pcm = _wav(r.dir / f"{name}.wav")
        assert np.all(pcm[:8000] == _pcm(0.25)) and np.all(pcm[8000:] == _pcm(0.5))
        r.audio(1.0, 1.5, 0.5)                    # after saving: ignored
        assert len(_wav(r.dir / f"{name}.wav")) == 16000

    def test_without_a_tail_the_late_block_is_padded(self, rig):
        # The other side of the test above: with no wait the file is saved
        # before the late block, and its slots are silence.
        r = rig(audio_tail_seconds=0.0)
        name = r.recorder.start(fps=10)["name"]
        r.feed([k / 10 for k in range(10)])
        r.audio(0.0, 0.5, 0.25)
        r.recorder.stop()
        assert r.recorder.wait(timeout=30.0)
        r.audio(0.5, 1.0, 0.5)
        pcm = _wav(r.dir / f"{name}.wav")
        assert len(pcm) == 16000
        assert np.all(pcm[:8000] == _pcm(0.25)) and np.all(pcm[8000:] == 0)


# ── the frame queue ───────────────────────────────────────────────────────────
class TestQueue:
    def test_a_full_queue_drops_its_oldest_frame(self, rig):
        r = rig(block=True, queue_seconds=0.5)            # 5 frames at 10 fps
        r.recorder.start(fps=10)
        r.feed([0.0])
        assert r.factory.entered.wait(10.0), "the writer never took the first frame"
        r.feed([k / 10 for k in range(1, 9)], first_value=1)   # 8 frames into 5 places
        r.factory.release.set()
        status = r.finish()
        # Frames 1-3 were dropped; their slots repeat frame 0.
        assert r.factory.writers[0].values() == [0, 0, 0, 0, 4, 5, 6, 7, 8]
        assert (status["dropped"], status["duplicated"], status["frames"]) == (3, 3, 9)

    def test_while_saving_it_is_finalizing_and_refuses_a_new_start(self, rig):
        r = rig(block=True)
        name = r.recorder.start(fps=10)["name"]
        r.feed([0.0])
        assert r.factory.entered.wait(10.0)
        status = r.recorder.stop(by="admin")
        assert status["finalizing"] and not status["recording"] and status["active"]
        assert not r.recorder.wants_frames and r.recorder.active
        with pytest.raises(RecorderError) as err:
            r.recorder.start()
        assert err.value.code == "conflict" and "being saved" in err.value.message
        with pytest.raises(RecorderError):
            r.recorder.stop()
        with pytest.raises(RecorderError):
            r.recorder.delete(name)
        assert r.recorder.path_for(f"{name}.avi") is None
        r.factory.release.set()
        assert r.recorder.wait(timeout=30.0)
        assert r.recorder.path_for(f"{name}.avi") == r.dir / f"{name}.avi"
        assert r.events("recording_stopped")[0]["by"] == "admin"


# ── stops on their own ────────────────────────────────────────────────────────
class TestAutoStop:
    def test_max_length_from_the_frames(self, rig):
        r = rig(max_seconds=1.0)
        name = r.recorder.start(fps=10)["name"]
        r.feed([k / 10 for k in range(15)])
        assert r.recorder.wait(timeout=30.0)
        status = r.recorder.status()
        assert status["stop_reason"] == "max_length" and status["frames"] == 10
        assert r.sidecar(name)["seconds"] == pytest.approx(1.0)
        assert r.events("recording_stopped")[0]["reason"] == "max_length"

    def test_max_length_from_the_clock(self, rig):
        r = rig(max_seconds=1.0)
        r.recorder.start(fps=10)
        r.feed([0.0])
        r.clock.now = T0 + 1.5
        assert r.recorder.wait(timeout=30.0)
        assert r.recorder.status()["stop_reason"] == "max_length"

    def test_disk_full(self, rig):
        r = rig(min_free_mb=100)
        r.disk.free = 500 * MB
        name = r.recorder.start(fps=10)["name"]
        r.feed([0.0, 0.1, 0.2])
        r.disk.free = 50 * MB
        r.clock.now = T0 + 2.5                       # the next free-space check is due
        assert r.recorder.wait(timeout=30.0)
        status = r.recorder.status()
        assert status["stop_reason"] == "disk_full" and status["frames"] == 3
        assert r.sidecar(name)["stop_reason"] == "disk_full"

    def test_max_size_keeps_the_avi_readable(self, rig, monkeypatch):
        monkeypatch.setattr(recorder_mod, "MAX_VIDEO_BYTES", 64)
        r = rig()
        r.recorder.start(fps=10)
        r.feed([k / 10 for k in range(10)])              # 4 + 16 bytes a frame
        # The size is read with the free space, every 2 s: all frames first.
        _until(lambda: r.recorder.status()["frames"] == 10)
        r.clock.now = T0 + 2.5
        assert r.recorder.wait(timeout=30.0)
        status = r.recorder.status()
        assert status["stop_reason"] == "max_size" and status["frames"] == 10

    def test_camera_lost_between_frames(self, rig):
        r = rig()
        r.recorder.start(fps=10)
        r.feed([0.0, 0.1, 6.0])
        assert r.recorder.wait(timeout=30.0)
        status = r.recorder.status()
        assert status["stop_reason"] == "camera_lost" and status["frames"] == 2
        assert status["duplicated"] == 0, "the freeze is not written"

    def test_camera_lost_when_frames_stop(self, rig):
        r = rig()
        r.recorder.start(fps=10)
        r.feed([0.0, 0.1, 0.2, 0.3])
        r.clock.now = T0 + 6.0
        assert r.recorder.wait(timeout=30.0)
        status = r.recorder.status()
        assert status["stop_reason"] == "camera_lost" and status["frames"] == 4

    def test_a_recording_without_frames_is_discarded(self, rig):
        r = rig()
        name = r.recorder.start()["name"]
        status = r.finish()
        assert status["frames"] == 0 and "no camera frame" in status["last_error"]
        assert r.recorder.list() == [] and list(r.dir.iterdir()) == []
        assert r.events("recording_stopped")[0]["saved"] is False
        assert r.recorder.path_for(f"{name}.json") is None

    def test_close_finalises_and_is_idempotent(self, rig):
        r = rig()
        name = r.recorder.start(by="admin")["name"]
        r.feed([0.0, 0.1])
        assert r.recorder.close(timeout=30.0) is True
        assert r.recorder.close(timeout=30.0) is True
        assert r.recorder.status()["stop_reason"] == "shutdown"
        assert r.sidecar(name)["stop_reason"] == "shutdown"
        assert [event for event, _ in r.audits] == ["recording_started", "recording_stopped"]


# ── the video writer fails ────────────────────────────────────────────────────
class ClosedWriter:
    """What OpenCV returns when no MJPEG backend opens (a Pi build without it)."""

    def isOpened(self) -> bool:
        return False

    def release(self) -> None:
        pass


class TestWriterFailure:
    def test_a_writer_that_does_not_open_discards_and_says_why(self, rig):
        r = rig()
        r.recorder._writer_factory = lambda path, fps, size, quality: ClosedWriter()
        name = r.recorder.start(fps=10)["name"]
        r.feed([0.0, 0.1])
        assert r.recorder.wait(timeout=30.0)
        status = r.recorder.status()
        assert status["stop_reason"] == "error" and status["frames"] == 0
        assert "video writer did not open" in status["last_error"]
        assert "no camera frame" not in status["last_error"], "the writer's reason is kept"
        assert r.recorder.list() == [] and list(r.dir.iterdir()) == []
        [stopped] = r.events("recording_stopped")
        assert (stopped["name"], stopped["reason"], stopped["saved"]) == (name, "error", False)

    def test_a_write_that_fails_part_way_saves_the_frames_so_far(self, rig):
        r = rig()
        r.factory.fail_on = 3
        name = r.recorder.start(fps=10)["name"]
        r.feed([k / 10 for k in range(5)])
        assert r.recorder.wait(timeout=30.0)
        status = r.recorder.status()
        assert status["stop_reason"] == "error" and status["frames"] == 2
        assert "frame write failed: disk went away" in status["last_error"]
        meta = r.sidecar(name)
        assert (meta["stop_reason"], meta["frames"]) == ("error", 2)
        assert "frame write failed" in meta["error"]
        assert [e["name"] for e in r.recorder.list()] == [name]
        [stopped] = r.events("recording_stopped")
        assert stopped["reason"] == "error" and stopped["frames"] == 2


# ── refusals ──────────────────────────────────────────────────────────────────
class TestRefusals:
    def _refused(self, r: Rig, text: str) -> None:
        with pytest.raises(RecorderError) as err:
            r.recorder.start()
        assert (err.value.code, err.value.status) == ("conflict", 409)
        assert text in err.value.message

    def test_disabled(self, rig):
        r = rig(enabled=False)
        self._refused(r, "turned off")
        assert r.recorder.status()["enabled"] is False

    def test_already_recording(self, rig):
        r = rig()
        r.recorder.start()
        self._refused(r, "already recording")

    def test_stop_when_not_recording(self, rig):
        r = rig()
        with pytest.raises(RecorderError) as err:
            r.recorder.stop()
        assert err.value.code == "conflict"

    def test_low_disk(self, rig):
        r = rig(min_free_mb=100)
        r.disk.free = 99 * MB
        self._refused(r, "free")
        assert not r.recorder.active

    def test_over_the_total_quota(self, rig):
        r = rig(max_total_mb=1)
        r.dir.mkdir(parents=True)
        (r.dir / "rec_20260101_000000.avi").write_bytes(bytes(MB))
        (r.dir / "rec_20260101_000000.json").write_text(json.dumps(
            {"name": "rec_20260101_000000", "created_at": recorder_mod._iso(WALL)}))
        assert r.recorder.total_bytes() >= MB
        self._refused(r, "delete")


# ── names, files and what may be served ───────────────────────────────────────
class TestNames:
    @pytest.mark.parametrize("raw, clean", [
        ("  Gun Test! ", "gun_test"), ("knife-2 room A", "knife-2_room_a"),
        ("../etc/passwd", "etcpasswd"), ("x" * 60, "x" * 40), ("___", ""), ("", ""),
        (None, ""), ("Ünïcode ✓", "ncode"),
    ])
    def test_labels(self, raw, clean):
        assert sanitize_label(raw) == clean

    def test_names_are_local_time_and_unique(self, rig):
        r = rig()
        names = []
        for label in ("Gun Test", "Gun Test", "Gun Test", ""):
            names.append(r.recorder.start(label=label)["name"])
            r.feed([0.0])
            r.finish()
        assert names == [_name(label="gun_test"), _name(label="gun_test") + "_2",
                         _name(label="gun_test") + "_3", _name()]
        assert sorted(e["name"] for e in r.recorder.list()) == sorted(names)

    def test_list_is_newest_first(self, rig):
        r = rig()
        names = []
        for minutes in (0, 2, 1):
            r.wall.now = WALL + 60 * minutes
            names.append(r.recorder.start(label=f"m{minutes}")["name"])
            r.feed([0.0])
            r.finish()
        assert [e["name"] for e in r.recorder.list()] == [names[1], names[2], names[0]]

    def test_status_and_list(self, rig):
        r = rig()
        idle = r.recorder.status()
        assert idle["active"] is False and idle["name"] is None and idle["ffmpeg"] is False
        assert idle["free_bytes"] == r.disk.free and idle["max_seconds"] == 600.0
        status = r.recorder.start(label="desk", by="admin", meta={"camera": "cam0"})
        assert status["recording"] and status["name"] == _name(label="desk")
        assert status["started_at"] == recorder_mod._iso(WALL)
        r.feed([0.0, 0.1])
        done = r.finish()
        assert done["active"] is False and done["name"] == status["name"]
        assert done["bytes"] > 0 and done["last_error"] is None
        [entry] = r.recorder.list()
        assert entry == {"name": status["name"], "label": "desk",
                         "created_at": recorder_mod._iso(WALL), "seconds": done["seconds"],
                         "bytes": done["bytes"], "has_audio": False, "interrupted": False,
                         "files": {"video": f"{status['name']}.avi", "audio": None,
                                   "meta": f"{status['name']}.json"}}
        meta = r.sidecar(status["name"])
        assert meta["by"] == "admin" and meta["context"] == {"camera": "cam0"}
        assert r.recorder.total_bytes() == done["bytes"]
        started = r.events("recording_started")[0]
        assert started["name"] == status["name"] and started["by"] == "admin"

    def test_directory_defaults_to_var_recordings_at_call_time(self, tmp_path, monkeypatch):
        from sentrix import paths
        monkeypatch.setattr(paths, "RECORDINGS_DIR", tmp_path / "elsewhere")
        assert Recorder(cfg=_cfg()).directory == tmp_path / "elsewhere"

    def test_path_for_serves_only_finished_files(self, rig, tmp_path):
        r = rig()
        name = r.recorder.start()["name"]
        r.feed([0.0])
        r.finish()
        assert r.recorder.path_for(f"{name}.avi") == r.dir / f"{name}.avi"
        assert r.recorder.path_for(f"{name}.json") == r.dir / f"{name}.json"
        assert r.recorder.path_for(f"{name}.wav") is None          # none was recorded
        (r.dir / f"{name}.part.avi").write_bytes(b"x")
        (r.dir / "rec_20260101_000000.avi").write_bytes(b"x")      # no sidecar
        (tmp_path / "secret.avi").write_bytes(b"x")
        for bad in (f"{name}.part.avi", f"../{name}.avi", f"..\\{name}.avi", "../secret.avi",
                    str(r.dir / f"{name}.avi"), str(tmp_path / "secret.avi"),
                    f"sub/{name}.avi", f"{name}.mp4", f"{name}", "rec_20260101_000000.avi",
                    "rec_20990101_000000.avi", "", None, ".", f"{name.upper()}.AVI"):
            assert r.recorder.path_for(bad) is None, bad

    def test_path_for_refuses_the_recording_in_progress(self, rig):
        r = rig()
        name = r.recorder.start()["name"]
        (r.dir / f"{name}.json").write_text("{}")                  # even with a sidecar
        (r.dir / f"{name}.avi").write_bytes(b"x")
        assert r.recorder.path_for(f"{name}.avi") is None

    def test_delete(self, rig):
        r = rig()
        name = r.recorder.start()["name"]
        r.feed([0.0])
        r.finish()
        active = r.recorder.start(label="next")["name"]
        with pytest.raises(RecorderError) as err:
            r.recorder.delete(active, by="admin")
        assert err.value.code == "conflict"
        assert r.recorder.delete(name, by="admin") is True
        assert not any(p.name.startswith(f"{name}.") for p in r.dir.iterdir())
        assert r.recorder.delete(name) is False
        assert r.recorder.delete("../x") is False and r.recorder.delete("rec_x") is False
        assert r.events("recording_deleted") == [{"name": name, "by": "admin"}]


# ── retention ─────────────────────────────────────────────────────────────────
class TestPrune:
    def test_older_than_keep_days(self, rig):
        r = rig(keep_days=14.0)
        name = r.recorder.start()["name"]
        r.feed([0.0])
        r.finish()
        assert r.recorder.prune(now=WALL + 13 * DAY) == []
        assert r.recorder.prune(now=WALL + 15 * DAY) == [name]
        assert r.recorder.list() == [] and list(r.dir.iterdir()) == []
        [pruned] = r.events("recording_pruned")
        assert pruned["name"] == name and pruned["age_days"] == 15.0

    def test_start_prunes_first_with_the_wall_clock(self, rig):
        r = rig(keep_days=1.0)
        old = r.recorder.start()["name"]
        r.feed([0.0])
        r.finish()
        r.wall.now = WALL + 2 * DAY
        new = r.recorder.start()["name"]
        assert new != old and not (r.dir / f"{old}.json").exists()

    def test_zero_keeps_everything(self, rig):
        r = rig(keep_days=0.0)
        name = r.recorder.start()["name"]
        r.feed([0.0])
        r.finish()
        assert r.recorder.prune(now=WALL + 10_000 * DAY) == []
        assert [e["name"] for e in r.recorder.list()] == [name]

    def test_housekeeping_prunes_with_nobody_asking(self, rig):
        # A device that runs for weeks with no restart, no new recording and
        # no dashboard open still deletes footage past keep_days.
        r = rig(keep_days=1.0)
        name = r.recorder.start()["name"]
        r.feed([0.0])
        r.finish()
        assert r.recorder.start_housekeeping(every=0.02) is True
        assert r.recorder.start_housekeeping(every=0.02) is False, "one timer"
        time.sleep(0.1)
        assert [e["name"] for e in r.recorder.list()] == [name], "not old yet"
        r.wall.now = WALL + 2 * DAY
        _until(lambda: r.recorder.list() == [])
        assert r.events("recording_pruned")[0]["name"] == name
        thread = r.recorder._housekeeper
        r.recorder.close()
        thread.join(timeout=5.0)
        assert not thread.is_alive(), "close() stops the timer"

    def test_housekeeping_finishes_leftover_parts(self, rig):
        r = rig()
        _leftovers(r.dir)
        r.recorder.start_housekeeping(every=0.02)
        _until(lambda: [e["name"] for e in r.recorder.list()] == [NAME])

    def test_a_stop_never_waits_behind_housekeeping(self, rig):
        # A recovery's remux holds the control lock for seconds: Stop must
        # still end the filming of the room at once.
        r = rig()
        r.recorder.start(fps=10)
        r.feed([0.0])
        held, release = threading.Event(), threading.Event()

        def remux() -> None:
            with r.recorder._control:
                held.set()
                release.wait(10.0)

        busy = threading.Thread(target=remux, daemon=True)
        busy.start()
        assert held.wait(5.0)
        try:
            began = time.monotonic()
            assert r.recorder.stop()["finalizing"] or not r.recorder.active
            assert time.monotonic() - began < 2.0 and not r.recorder.wants_frames
        finally:
            release.set()
            busy.join(5.0)
        assert r.recorder.wait(timeout=30.0)


# ── crash recovery ────────────────────────────────────────────────────────────
NAME = "rec_20261028_101500_crash"


def _leftovers(directory: Path, *, audio: bool = True, frames: int = 30) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    _crashed_clip(directory / f"{NAME}.part.avi", frames)
    if audio:
        _tone_wav(directory / f"{NAME}.part.wav", 3.0)
    (directory / f"{NAME}.part.json").write_text(json.dumps({
        "name": NAME, "label": "crash", "by": "admin", "created_at": recorder_mod._iso(WALL),
        "fps": 10.0, "audio": {"device": "USB mic"}, "context": {"camera": "cam0"},
        "draft": True}))
    (directory / f"{NAME}.part.mux.avi").write_bytes(b"half a mux")


class TestRecover:
    def test_without_ffmpeg_the_parts_are_renamed(self, rig):
        r = rig()
        _leftovers(r.dir)
        assert r.recorder.recover() == [NAME]
        assert sorted(p.name for p in r.dir.iterdir()) == [
            f"{NAME}.avi", f"{NAME}.json", f"{NAME}.wav"]
        meta = r.sidecar(NAME)
        assert meta["interrupted"] is True and meta["stop_reason"] == "interrupted"
        assert (meta["frames"], meta["fps"], meta["size"]) == (30, 10.0, [64, 48])
        assert meta["label"] == "crash" and meta["context"] == {"camera": "cam0"}
        assert meta["audio"]["present"] is True and meta["audio"]["seconds"] == 3.0
        assert meta["files"]["audio"] == f"{NAME}.wav"
        [entry] = r.recorder.list()
        assert entry["interrupted"] is True and entry["has_audio"] is True
        assert _read_avi(r.dir / f"{NAME}.avi")[0] == 30
        assert r.events("recording_recovered")[0]["remuxed"] is False
        assert r.recorder.recover() == []

    def test_with_ffmpeg_the_avi_is_remuxed_with_its_sound(self, rig):
        run = FakeRun()
        r = rig(ffmpeg="ffmpeg-fake", run=run)
        _leftovers(r.dir)
        assert r.recorder.recover() == [NAME]
        [(argv, kwargs)] = run.calls
        assert argv[0] == "ffmpeg-fake" and isinstance(argv, list)
        assert argv[argv.index("-c:v") + 1] == "copy" and not kwargs.get("shell")
        assert argv[-1].endswith(f"{NAME}.part.mux.avi")
        assert sorted(p.name for p in r.dir.iterdir()) == [f"{NAME}.avi", f"{NAME}.json"]
        meta = r.sidecar(NAME)
        assert meta["audio"]["present"] is True and meta["files"]["audio"] is None
        assert r.events("recording_recovered")[0]["remuxed"] is True

    def test_with_ffmpeg_a_video_only_part_is_copied(self, rig):
        run = FakeRun()
        r = rig(ffmpeg="ffmpeg-fake", run=run)
        _leftovers(r.dir, audio=False)
        assert r.recorder.recover() == [NAME]
        [(argv, _)] = run.calls
        assert argv[argv.index("-c") + 1] == "copy" and "-c:a" not in argv
        assert r.sidecar(NAME)["audio"]["present"] is False

    def test_a_failed_remux_falls_back_to_renaming(self, rig):
        r = rig(ffmpeg="ffmpeg-fake", run=FakeRun(returncode=1, stderr=b"broken"))
        _leftovers(r.dir)
        assert r.recorder.recover() == [NAME]
        assert sorted(p.name for p in r.dir.iterdir()) == [
            f"{NAME}.avi", f"{NAME}.json", f"{NAME}.wav"]
        assert "broken" in r.sidecar(NAME)["error"]

    @pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")
    def test_real_ffmpeg_rebuilds_a_crashed_file(self, rig):
        r = rig(ffmpeg=FFMPEG, run=subprocess.run)
        _leftovers(r.dir)
        assert r.recorder.recover() == [NAME]
        assert _read_avi(r.dir / f"{NAME}.avi") == (30, pytest.approx(10.0))
        assert [s["codec_name"] for s in _audio_streams(r.dir / f"{NAME}.avi")] == ["pcm_s16le"]

    def test_the_recording_in_progress_is_left_alone(self, rig):
        r = rig()
        name = r.recorder.start()["name"]
        assert (r.dir / f"{name}.part.json").exists()
        assert r.recorder.recover() == []
        assert (r.dir / f"{name}.part.json").exists()

    def test_leftovers_of_a_finished_recording_are_removed(self, rig):
        r = rig()
        _leftovers(r.dir)
        (r.dir / f"{NAME}.json").write_text("{}")
        assert r.recorder.recover() == []
        assert [p.name for p in r.dir.iterdir()] == [f"{NAME}.json"]

    def test_leftovers_without_video_are_removed(self, rig):
        r = rig()
        _leftovers(r.dir)
        (r.dir / f"{NAME}.part.avi").unlink()
        assert r.recorder.recover() == []
        assert list(r.dir.iterdir()) == []

    def test_a_remux_into_the_space_kept_for_the_alarm_is_skipped(self, rig):
        # At start-up, with the alarm pipeline running: a remux writes the
        # whole video again, so it never goes under the free-space floor.
        run = FakeRun()
        r = rig(ffmpeg="ffmpeg-fake", run=run, min_free_mb=100)
        _leftovers(r.dir)
        r.disk.free = 100 * MB + 1000
        assert r.recorder.recover() == [NAME]
        assert run.calls == [], "ffmpeg must not copy the video into the floor"
        assert sorted(p.name for p in r.dir.iterdir()) == [
            f"{NAME}.avi", f"{NAME}.json", f"{NAME}.wav"]
        meta = r.sidecar(NAME)
        assert "not remuxed" in meta["error"] and "kept for the alarm" in meta["error"]
        assert meta["frames"] == 30 and meta["files"]["audio"] == f"{NAME}.wav"

    def test_a_complete_sidecar_is_finished_with_its_own_figures(self, rig):
        # The save wrote its whole sidecar, then died (or a rename was
        # refused) before the renames: nothing it says is lost.
        r = rig()
        r.dir.mkdir(parents=True)
        _real_clip(r.dir / f"{NAME}.part.avi", 30)
        _tone_wav(r.dir / f"{NAME}.part.wav", 3.0)
        meta = {"name": NAME, "label": "crash", "by": "admin",
                "created_at": recorder_mod._iso(WALL), "stopped_at": recorder_mod._iso(WALL + 3),
                "stop_reason": "user", "stopped_by": "admin", "seconds": 3.0, "fps": 10.0,
                "size": [64, 48], "frames": 30, "duplicated": 2, "dropped": 1,
                "audio": {"present": True, "device": "USB mic", "rate": 16000, "seconds": 3.0,
                          "gaps": 0, "ignored": 0},
                "files": {"video": f"{NAME}.avi", "audio": f"{NAME}.wav",
                          "meta": f"{NAME}.json"},
                "interrupted": False, "format": {"video": "mjpeg", "audio": "pcm_s16le"},
                "context": {"camera": "cam0"}, "error": None}
        (r.dir / f"{NAME}.part.json").write_text(json.dumps(meta))
        assert r.recorder.recover() == [NAME]
        assert sorted(p.name for p in r.dir.iterdir()) == [
            f"{NAME}.avi", f"{NAME}.json", f"{NAME}.wav"]
        assert r.sidecar(NAME) == meta, "stop reason, repeats, drops and gaps kept"

    def test_a_complete_sidecar_after_a_finished_mux(self, rig):
        r = rig()
        r.dir.mkdir(parents=True)
        _real_clip(r.dir / f"{NAME}.part.avi", 30)
        _tone_wav(r.dir / f"{NAME}.part.wav", 3.0)
        _real_clip(r.dir / f"{NAME}.part.mux.avi", 30)          # the mux, finished
        meta = {"name": NAME, "stop_reason": "max_length", "frames": 30, "fps": 10.0,
                "duplicated": 0, "dropped": 0,
                "audio": {"present": True, "seconds": 3.0, "gaps": 0},
                "files": {"video": f"{NAME}.avi", "audio": None, "meta": f"{NAME}.json"},
                "interrupted": False, "error": None}
        (r.dir / f"{NAME}.part.json").write_text(json.dumps(meta))
        assert r.recorder.recover() == [NAME]
        assert sorted(p.name for p in r.dir.iterdir()) == [f"{NAME}.avi", f"{NAME}.json"]
        assert r.sidecar(NAME)["stop_reason"] == "max_length"

    def test_a_video_that_already_holds_its_sound_is_not_called_silent(self, rig, monkeypatch):
        # A remux finished and its inputs were removed, then the process died
        # before the sidecar: no .wav is left, yet the AVI carries the sound.
        monkeypatch.setattr(recorder_mod, "_avi_has_audio", lambda path: True)
        r = rig()
        _leftovers(r.dir)
        (r.dir / f"{NAME}.part.wav").unlink()
        (r.dir / f"{NAME}.part.avi").rename(r.dir / f"{NAME}.avi")
        assert r.recorder.recover() == [NAME]
        meta = r.sidecar(NAME)
        assert meta["audio"]["present"] is True and meta["audio"]["seconds"] == 3.0
        assert meta["format"]["audio"] == "pcm_s16le" and meta["files"]["audio"] is None
        assert r.recorder.list()[0]["has_audio"] is True

    def test_a_wav_left_next_to_a_video_with_its_sound_is_dropped(self, rig, monkeypatch):
        monkeypatch.setattr(recorder_mod, "_avi_has_audio", lambda path: True)
        r = rig()
        _leftovers(r.dir)
        (r.dir / f"{NAME}.part.avi").rename(r.dir / f"{NAME}.avi")
        assert r.recorder.recover() == [NAME]
        assert sorted(p.name for p in r.dir.iterdir()) == [f"{NAME}.avi", f"{NAME}.json"], \
            "the sound is not kept twice"
        meta = r.sidecar(NAME)
        assert meta["audio"]["present"] is True and meta["audio"]["seconds"] == 3.0
        assert meta["files"]["audio"] is None

    @pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")
    def test_a_sound_track_is_read_from_the_avi_header(self, tmp_path):
        clip = _real_clip(tmp_path / "clip.avi", 10)
        assert recorder_mod._avi_has_audio(clip) is False
        muxed = tmp_path / "muxed.avi"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(clip),
                        "-i", str(_tone_wav(tmp_path / "t.wav", 1.0)), "-map", "0:v:0",
                        "-map", "1:a:0", "-c:v", "copy", "-c:a", "pcm_s16le", str(muxed)],
                       check=True, timeout=60, stdin=subprocess.DEVNULL)
        assert recorder_mod._avi_has_audio(muxed) is True
        assert recorder_mod._avi_has_audio(tmp_path / "missing.avi") is False

    def test_a_real_crash_without_ffmpeg(self, rig):
        # What os._exit leaves: zero sizes and frame count in the headers, no
        # index, and the frame OpenCV still held lost.
        r = rig()
        r.dir.mkdir(parents=True)
        _killed_clip(r.dir / f"{NAME}.part.avi", frames=60)
        assert r.recorder.recover() == [NAME]
        meta = r.sidecar(NAME)
        assert meta["frames"] >= 59 and meta["fps"] == 10.0 and meta["size"] == [320, 240]
        assert meta["seconds"] == pytest.approx(meta["frames"] / 10.0)
        frames, fps = _read_avi(r.dir / f"{NAME}.avi")
        assert frames == meta["frames"] and fps == pytest.approx(10.0)

    @pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")
    def test_a_real_crash_with_ffmpeg(self, rig):
        r = rig(ffmpeg=FFMPEG, run=subprocess.run)
        r.dir.mkdir(parents=True)
        _killed_clip(r.dir / f"{NAME}.part.avi", frames=60)
        _tone_wav(r.dir / f"{NAME}.part.wav", 6.0)
        assert r.recorder.recover() == [NAME]
        assert sorted(p.name for p in r.dir.iterdir()) == [f"{NAME}.avi", f"{NAME}.json"]
        meta = r.sidecar(NAME)
        assert meta["frames"] >= 59 and meta["audio"]["present"] is True
        assert _read_avi(r.dir / f"{NAME}.avi")[0] == meta["frames"]
        assert [s["codec_name"] for s in _audio_streams(r.dir / f"{NAME}.avi")] == ["pcm_s16le"]


# ── a save that fails part way ────────────────────────────────────────────────
class FlakyReplace:
    """``os.replace`` that refuses renames of ``<name>.part.avi`` (a virus
    scanner holding the file just closed: WinError 32) ``times`` times, or
    for as long as ``stuck`` is set."""

    def __init__(self, times: int = 0, stuck: bool = False) -> None:
        self.times, self.stuck, self.refused = times, stuck, 0
        self.real = os.replace

    def __call__(self, source, target):
        if str(source).endswith(".part.avi") and (self.stuck or self.refused < self.times):
            self.refused += 1
            raise PermissionError(13, "The process cannot access the file because it is "
                                      "being used by another process", str(source))
        return self.real(source, target)


@pytest.fixture
def flaky(monkeypatch):
    def install(**kwargs) -> FlakyReplace:
        replace = FlakyReplace(**kwargs)
        monkeypatch.setattr(os, "replace", replace)
        monkeypatch.setattr(recorder_mod, "REPLACE_BACKOFF_SECONDS", 0.0)
        return replace
    return install


class TestSaveFailure:
    def test_a_rename_refused_once_is_tried_again(self, rig, flaky):
        replace = flaky(times=1)
        r = rig()
        name = r.recorder.start(fps=10)["name"]
        r.feed([0.0, 0.1, 0.2])
        status = r.finish()
        assert replace.refused == 1
        assert status["last_error"] is None and [e["name"] for e in r.recorder.list()] == [name]
        assert r.sidecar(name)["stop_reason"] == "user"
        assert [event for event, _ in r.audits] == ["recording_started", "recording_stopped"]

    def test_a_save_that_keeps_failing_is_finished_by_the_writer(self, rig, flaky):
        # Every try in the save is refused; the writer then finishes the files
        # from the sidecar it had written, instead of leaving them hidden.
        replace = flaky(times=recorder_mod.REPLACE_ATTEMPTS)
        r = rig()
        name = r.recorder.start(fps=10, by="admin")["name"]
        r.feed([0.0, 0.1, 0.2])
        status = r.finish(by="admin")
        assert replace.refused == recorder_mod.REPLACE_ATTEMPTS
        assert [e["name"] for e in r.recorder.list()] == [name]
        assert r.recorder.path_for(f"{name}.avi") == r.dir / f"{name}.avi"
        meta = r.sidecar(name)
        assert (meta["stop_reason"], meta["frames"], meta["interrupted"]) == ("user", 3, False)
        assert "saving failed" in status["last_error"] and "finished" in status["last_error"]
        [stopped] = r.events("recording_stopped")
        assert stopped["saved"] is True and "saving failed" in stopped["error"]
        assert stopped["by"] == "admin" and stopped["frames"] == 3
        assert not list(r.dir.glob("*.part.*"))

    def test_a_save_that_cannot_finish_is_finished_before_the_next_take(self, rig, flaky):
        replace = flaky(stuck=True)
        r = rig()
        name = r.recorder.start(fps=10)["name"]
        r.feed([0.0, 0.1, 0.2])
        status = r.finish()
        assert r.recorder.list() == [] and "saving failed" in status["last_error"]
        [stopped] = r.events("recording_stopped")
        assert stopped["saved"] is False and "saving failed" in stopped["error"]
        replace.stuck = False                       # the scanner let go
        r.wall.now = WALL + 60
        r.recorder.start(fps=10)
        assert name in [e["name"] for e in r.recorder.list()]
        assert r.sidecar(name)["stop_reason"] == "user", "its own figures, not 'interrupted'"
        assert r.events("recording_recovered")[0]["name"] == name


# ── the ffmpeg mux ────────────────────────────────────────────────────────────
class TestMux:
    def _record_with_sound(self, r: Rig) -> str:
        name = r.recorder.start(fps=10, audio_device="USB mic")["name"]
        r.feed([k / 10 for k in range(10)])
        r.audio(0.0, 1.0, 0.25)
        r.finish()
        return name

    def test_sound_is_muxed_into_the_avi(self, rig):
        run = FakeRun()
        r = rig(ffmpeg="ffmpeg-fake", run=run)
        name = self._record_with_sound(r)
        [(argv, kwargs)] = run.calls
        part = r.dir / name
        assert argv == ["ffmpeg-fake", "-hide_banner", "-loglevel", "error", "-y",
                        "-i", f"{part}.part.avi", "-i", f"{part}.part.wav",
                        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                        "-c:a", "pcm_s16le", f"{part}.part.mux.avi"]
        assert not kwargs.get("shell") and kwargs["timeout"] == 300.0
        assert sorted(p.name for p in r.dir.iterdir()) == [f"{name}.avi", f"{name}.json"]
        meta = r.sidecar(name)
        assert meta["audio"]["present"] is True and meta["files"]["audio"] is None
        assert meta["error"] is None and r.recorder.status()["ffmpeg"] is True
        assert r.events("recording_stopped")[0]["has_audio"] is True

    def test_no_mux_into_the_space_kept_for_the_alarm(self, rig):
        # A recording that stopped near the floor (disk_full stops at it):
        # the mux would write the whole video again under it.
        run = FakeRun()
        r = rig(ffmpeg="ffmpeg-fake", run=run, min_free_mb=100)
        r.disk.free = 500 * MB
        name = r.recorder.start(fps=10, audio_device="USB mic")["name"]
        r.feed([k / 10 for k in range(10)])
        r.audio(0.0, 1.0, 0.25)
        _until(lambda: r.recorder.status()["frames"] == 10)
        r.disk.free = 100 * MB + 1000
        r.finish()
        assert run.calls == [], "ffmpeg must not run"
        assert sorted(p.name for p in r.dir.iterdir()) == [
            f"{name}.avi", f"{name}.json", f"{name}.wav"]
        meta = r.sidecar(name)
        assert meta["files"]["audio"] == f"{name}.wav" and meta["audio"]["present"] is True
        assert "separate .wav" in meta["error"] and "kept for the alarm" in meta["error"]

    @pytest.mark.parametrize("run", [FakeRun(returncode=1, stderr=b"boom"),
                                     FakeRun(raises=OSError("no such program"))])
    def test_a_failed_mux_keeps_the_sound_apart(self, rig, run):
        r = rig(ffmpeg="ffmpeg-fake", run=run)
        name = self._record_with_sound(r)
        assert sorted(p.name for p in r.dir.iterdir()) == [
            f"{name}.avi", f"{name}.json", f"{name}.wav"]
        assert len(_wav(r.dir / f"{name}.wav")) == 16000
        meta = r.sidecar(name)
        assert "mux failed" in meta["error"] and meta["files"]["audio"] == f"{name}.wav"
        assert "mux failed" in r.recorder.status()["last_error"]

    @pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")
    def test_real_ffmpeg_mux(self, rig):
        r = rig(ffmpeg=FFMPEG, run=subprocess.run, real_writer=True)
        name = r.recorder.start(fps=10)["name"]
        r.feed([k / 10 for k in range(20)])
        tone = (0.3 * np.sin(2 * np.pi * 440 * np.arange(16000) / 16000)).astype(np.float32)
        r.audio(0.0, 1.0, tone)
        r.audio(1.0, 2.0, tone)
        r.finish()
        path = r.dir / f"{name}.avi"
        assert sorted(p.name for p in r.dir.iterdir()) == [f"{name}.avi", f"{name}.json"]
        assert _read_avi(path) == (20, pytest.approx(10.0))
        streams = _audio_streams(path)
        assert [s["codec_name"] for s in streams] == ["pcm_s16le"]
        if FFPROBE:
            assert (streams[0]["sample_rate"], streams[0]["channels"]) == ("16000", 1)
