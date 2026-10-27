"""Training recordings from the dashboard (D20).

The models fail in the deployment room because none of their training footage
came from it (CLAUDE.md §5, M6). The cheapest fix is footage from the camera
the device really uses, hung where it really hangs, with the room's own sound:
the device admin presses Record on the dashboard, acts out a scene (or records
an ordinary evening) and downloads the file on the local network afterwards.
Recordings never pass through the Portal: its tunnel carries at most 16 MiB
and continuous video stays on the device (D12).

What a finished recording ``<name>`` is:

* ``<name>.avi``: the camera's raw frames (no HUD) as MJPEG at a constant
  frame rate, with the microphone's sound as a PCM s16le 16 kHz mono track
  when one was connected and ffmpeg is installed. MJPEG because every frame is
  a JPEG of the camera image: frames come out exactly for a dataset, OpenCV
  writes it without any codec installed, and a file cut short by a crash is
  still readable up to its last frame. Constant frame rate because dataset
  tools address a frame as time x fps; a variable-rate file would put labels
  on the wrong frame.
* ``<name>.wav``: the sound on its own, only when ffmpeg is missing or the mux
  failed. Sample 0 is under frame 0 and both end together either way.
* ``<name>.json``: the sidecar (who, when, how long, frames repeated or
  dropped, audio gaps, the camera and device context).

While recording the files are ``<name>.part.avi``, ``<name>.part.wav`` and a
draft ``<name>.part.json``; they are never listed or served. Saving writes the
whole sidecar to ``<name>.part.json`` before any file is renamed, so
:meth:`Recorder.recover` turns whatever a crash or a failed save left into a
finished recording, keeping the real figures when the sidecar was complete.

Threads: the capture thread and the audio worker only hand items over (the
taps are O(1), never raise and never block); the writer thread
``sentrix-recorder`` does the scaling, encoding and file I/O, at a lower
priority on Linux so the alarm pipeline keeps the CPU on a Pi (the JPEG
encode runs on that thread, not on OpenCV's shared pool). A writer that
falls behind loses the oldest queued frames, never the camera's rate.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import wave
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np

from sentrix import paths
from sentrix.config import RecordingConfig, settings
from sentrix.link.protocol import ERROR_CODES
from sentrix.utils.logging import get_logger

log = get_logger("recorder")

#: The microphone's rate (``settings.windows.audio_sample_rate``) and the
#: audio track's. Blocks at any other rate are ignored and counted.
RATE = 16000
DEFAULT_FPS = 15.0
MIN_FPS, MAX_FPS = 5.0, 30.0
MB = 1024 * 1024
#: Free space and the video's size are measured this often while recording
#: (seconds).
DISK_CHECK_SECONDS = 2.0
#: A recording stops (``max_size``) once its video reaches this size. OpenCV's
#: AVI writer patches 32-bit chunk sizes and offsets on release, and many
#: readers take them as signed: past 2 GiB the file would be unreadable. Ten
#: minutes at 30 fps of a noisy 1280 px picture can get there; the check every
#: 2 s leaves well over 100 MB of headroom.
MAX_VIDEO_BYTES = 1900 * 1024 * 1024
#: The writer wakes at least this often to check the limits (seconds).
IDLE_WAIT_SECONDS = 0.25
#: An audio block stamped within this much of where the track has got to is
#: written straight on. Block stamps jitter by a few milliseconds (more when the
#: audio backend falls back to the callback time); zero-filling or trimming
#: every jitter would click. The track stays within 30 ms of the video, under
#: half a frame at 15 fps.
AUDIO_JITTER_SECONDS = 0.03
#: Audio blocks waiting for the writer; older ones are dropped (and then
#: zero-filled as a gap).
AUDIO_QUEUE_BLOCKS = 512
MUX_TIMEOUT_SECONDS = 300.0
#: A rename that fails is tried this many times, waiting 0.1, 0.2, 0.4... s in
#: between: on Windows a virus scanner or the search indexer briefly opens a
#: file that was just closed, and the rename fails with a sharing violation.
REPLACE_ATTEMPTS = 5
REPLACE_BACKOFF_SECONDS = 0.1
#: :meth:`Recorder.start_housekeeping` finishes leftover ``.part`` files and
#: deletes recordings past ``keep_days`` this often (seconds).
HOUSEKEEPING_SECONDS = 3600.0

_NAME = r"rec_\d{8}_\d{6}(?:_[a-z0-9][a-z0-9_-]{0,59})?"
NAME_RE = re.compile(rf"^{_NAME}$")
_FILE_RE = re.compile(rf"^(?P<name>{_NAME})\.(?P<ext>avi|wav|json)$")
_PART_RE = re.compile(rf"^(?P<name>{_NAME})\.part(?:\.mux)?\.(?:avi|wav|json)$")
#: A stream header (``strh``) of type ``auds`` in an AVI's header list.
_AUDIO_STREAM = re.compile(rb"strh.{4}auds", re.S)


class RecorderError(Exception):
    """A refused control call. ``code`` is a docs/7 §13 error code
    (``sentrix.link.protocol.ERROR_CODES``), ``status`` its HTTP status."""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = int(status)


def _refuse(code: str, message: str) -> RecorderError:
    return RecorderError(code, message, ERROR_CODES.get(code, 409))


class _Auto:
    def __repr__(self) -> str:
        return "AUTO"


#: ``Recorder(ffmpeg=AUTO)``: ``shutil.which("ffmpeg")`` each time it is needed,
#: so installing ffmpeg needs no restart.
AUTO: Any = _Auto()


def sanitize_label(label: Any) -> str:
    """A label as it goes into a file name: lower case, spaces as ``_``, only
    ``a-z 0-9 _ -``, at most 40 characters. May come out empty."""
    text = re.sub(r"\s+", "_", str(label or "").strip().lower())
    text = re.sub(r"[^a-z0-9_-]", "", text).strip("_-")
    return text[:40].strip("_-")


def open_mjpeg_writer(path: Path, fps: float, size: tuple[int, int], quality: int):
    """OpenCV's own MJPEG AVI writer (no codec needed) at JPEG ``quality``;
    the default backend if that one does not open."""
    import cv2

    fourcc = cv2.VideoWriter_fourcc(*"MJPG")
    quality = int(min(100, max(1, int(quality))))
    size = (int(size[0]), int(size[1]))
    try:
        writer = cv2.VideoWriter(str(path), cv2.CAP_OPENCV_MJPEG, fourcc, float(fps), size,
                                 [cv2.VIDEOWRITER_PROP_QUALITY, quality])
        if writer.isOpened():
            _tune_writer(writer, quality)
            return writer
        writer.release()
    except cv2.error as exc:
        log.debug("OpenCV MJPEG writer unavailable: %s", exc)
    writer = cv2.VideoWriter(str(path), fourcc, float(fps), size)
    if writer.isOpened():
        _tune_writer(writer, quality)
    return writer


def _tune_writer(writer: Any, quality: int) -> None:
    """What OpenCV's MJPEG writer ignores when given at open, set after it:
    the JPEG quality (it would write at 75), and one stripe per frame. By
    default each frame's encode is split across OpenCV's shared thread pool,
    whose threads run at normal priority (the pipeline's own cv2 calls made
    them), so the writer thread's lower priority would cover a quarter of
    the work; with one stripe the encode runs on the writer thread."""
    import cv2

    for prop, value in ((cv2.VIDEOWRITER_PROP_QUALITY, quality),
                        (cv2.VIDEOWRITER_PROP_NSTRIPES, 1)):
        try:
            writer.set(prop, value)
        except cv2.error as exc:
            log.debug("video writer property %s not set: %s", prop, exc)


def _slot(seconds: float, rate: float) -> int:
    """The index of the grid slot ``seconds`` falls in (round half up: Python's
    ``round`` rounds half to even, which would wobble on exact half slots)."""
    return int(math.floor(seconds * rate + 0.5))


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts)).astimezone().isoformat(timespec="seconds")


def _parse_iso(text: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(text)).timestamp()
    except (TypeError, ValueError):
        return None


def _target_size(width: int, height: int, max_width: int) -> tuple[int, int]:
    """The recorded size: the camera's, or scaled down to ``max_width`` with
    even sides (some players and encoders refuse odd ones)."""
    if max_width and max_width > 0 and width > max_width:
        new_width = max(2, int(max_width) // 2 * 2)
        new_height = max(2, int(round(height * new_width / width / 2.0)) * 2)
        return new_width, new_height
    return int(width), int(height)


def _to_pcm(samples: np.ndarray) -> bytes:
    return np.rint(np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("cannot remove %s: %s", path, exc)


def _replace(source: Path, target: Path) -> None:
    """``os.replace``, tried again for a moment when it fails
    (``REPLACE_ATTEMPTS``): a file just closed may be held open briefly by a
    virus scanner or the search indexer (Windows, WinError 32). A missing
    source fails at once."""
    delay = float(REPLACE_BACKOFF_SECONDS)
    for attempt in range(1, max(1, int(REPLACE_ATTEMPTS)) + 1):
        try:
            os.replace(source, target)
            return
        except FileNotFoundError:
            raise
        except OSError as exc:
            if attempt >= REPLACE_ATTEMPTS:
                raise
            log.debug("renaming %s failed (%s); trying again", Path(source).name, exc)
            time.sleep(delay)
            delay *= 2


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def _wav_seconds(path: Path) -> float:
    """The length of a WAV, also of one a crash cut short (its header can
    claim more than the file holds)."""
    try:
        with wave.open(str(path), "rb") as reader:
            frames, rate = reader.getnframes(), reader.getframerate() or RATE
            width, channels = reader.getsampwidth() or 2, reader.getnchannels() or 1
        held = max(0, _size(path) - 44) // (width * channels)
        return min(frames, held) / float(rate)
    except (OSError, EOFError, wave.Error):
        return max(0, _size(path) - 44) / 2.0 / RATE


def _probe_video(path: Path) -> tuple[int | None, float | None, list[int] | None]:
    """Frame count, frame rate and size of an AVI (OpenCV on a file path,
    never a device)."""
    try:
        import cv2
        capture = cv2.VideoCapture(str(path))
        try:
            if not capture.isOpened():
                return None, None, None
            frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            if frames <= 0:
                frames = 0
                while capture.grab():
                    frames += 1
            fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0) or None
            size = [int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
                    int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))]
            return frames, fps, size if all(size) else None
        finally:
            capture.release()
    except Exception as exc:
        log.debug("cannot read %s: %s", path, exc)
        return None, None, None


def _avi_has_audio(path: Path) -> bool:
    """Does an AVI carry a sound track? Its header list (``hdrl``, at the
    start of the file) holds one stream header per stream, typed ``vids`` or
    ``auds``: read from the bytes, without decoding anything or needing
    ffprobe."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(64 * 1024)
    except OSError:
        return False
    return _AUDIO_STREAM.search(head) is not None


def _lower_priority() -> None:
    """Nice +10 for the calling thread, best effort, Linux only (there a
    thread id addresses one thread, elsewhere it would renice the process)."""
    if not sys.platform.startswith("linux") or not hasattr(os, "setpriority"):
        return
    try:
        tid = threading.get_native_id()
        current = os.getpriority(os.PRIO_PROCESS, tid)
        os.setpriority(os.PRIO_PROCESS, tid, min(19, current + 10))
    except (OSError, AttributeError) as exc:
        log.debug("cannot lower the recorder's priority: %s", exc)


class _Session:
    """One recording. The capture-side fields (queues, grid, stop flag) change
    under the recorder's condition lock; the writer-side fields belong to the
    writer thread (status reads its counters without the lock: plain ints)."""

    def __init__(self, *, name: str, label: str, by: str, fps: float, audio_device: Any,
                 context: dict, directory: Path, cfg: RecordingConfig, created_wall: float,
                 started_mono: float) -> None:
        self.name, self.label, self.by, self.fps = name, label, by, fps
        self.audio_device = None if audio_device in (None, "") else str(audio_device)
        self.context, self.directory, self.cfg = context, directory, cfg
        self.created_wall, self.started_mono = created_wall, started_mono
        self.max_frames = max(1, _slot(float(cfg.max_seconds), fps))
        self.queue_max = max(4, math.ceil(float(cfg.queue_seconds) * fps))
        # The audio track may run this far past the video (a stalled camera or
        # a full frame queue) before blocks are thrown away.
        self.hold_samples = int((float(cfg.queue_seconds) + float(cfg.camera_gap_seconds)
                                 + float(cfg.audio_tail_seconds) + 1.0) * RATE)
        # capture side
        self.frames: deque = deque()
        self.audio_in: deque = deque(maxlen=AUDIO_QUEUE_BLOCKS)
        self.t0: float | None = None
        self.last_index = -1
        self.last_seq: Any = None
        self.last_captured: float | None = None
        self.dropped = 0
        self.audio_ignored = 0
        self.stopping = False
        self.accept_audio = True
        self.stop_reason: str | None = None
        self.stop_by = ""
        self.stopped_wall: float | None = None
        # writer side
        self.writer: Any = None
        self.size: tuple[int, int] | None = None
        self.last_frame: np.ndarray | None = None
        self.frames_written = 0
        self.duplicated = 0
        self.wav: wave.Wave_write | None = None
        self.audio_pos = 0                  # samples in the WAV
        self.pending: deque = deque()       # after the WAV: arrays, or ints of silence
        self.pending_len = 0
        self.early: deque = deque()         # blocks heard before the first frame
        self.early_len = 0
        self.gaps = 0
        self.disk_at = started_mono
        self.error: str | None = None
        # once finished
        self.has_audio = False
        self.audio_seconds = 0.0
        self.files: dict | None = None
        self.saved = False

    def part(self, ext: str) -> Path:
        return self.directory / f"{self.name}.part.{ext}"

    def final(self, ext: str) -> Path:
        return self.directory / f"{self.name}.{ext}"


class Recorder:
    """Records the camera (and microphone) to ``directory`` on demand.

    Control calls (:meth:`start`, :meth:`stop`, :meth:`delete`, ...) are
    thread-safe; the taps (:meth:`on_frame`, :meth:`on_audio`) are called on
    the capture and audio threads. ``clock`` is the pipeline's monotonic clock
    (frames and audio blocks are stamped on it), ``wall`` names files and
    dates sidecars; tests inject both, the writer, ``run`` (ffmpeg),
    ``disk_usage`` and ``audit``.
    """

    def __init__(self, directory: Path | str | None = None, cfg: RecordingConfig | None = None,
                 *, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, ffmpeg: Any = AUTO,
                 run: Callable[..., Any] = subprocess.run,
                 writer_factory: Callable[..., Any] | None = None,
                 disk_usage: Callable[[Any], Any] = shutil.disk_usage,
                 audit: Callable[..., None] | None = None) -> None:
        self._directory = None if directory is None else Path(directory)
        self._cfg = cfg
        self._clock = clock
        self._wall = wall
        self._ffmpeg = ffmpeg
        self._run = run
        self._writer_factory = writer_factory or open_mjpeg_writer
        self._disk_usage = disk_usage
        self._audit = audit
        self._control = threading.RLock()                 # start/stop/delete/prune/recover
        self._cond = threading.Condition(threading.Lock())  # queues and state: O(1) holds only
        self._session: _Session | None = None
        self._last: _Session | None = None
        self._last_error: str | None = None
        self._thread: threading.Thread | None = None
        self._housekeeper: threading.Thread | None = None
        self._housekeeping_stop = threading.Event()

    # ── configuration ─────────────────────────────────────────────────────────
    @property
    def directory(self) -> Path:
        """``var/recordings`` unless given (looked up at call time, so a test
        can move ``sentrix.paths.RECORDINGS_DIR``)."""
        return self._directory if self._directory is not None else Path(paths.RECORDINGS_DIR)

    @property
    def cfg(self) -> RecordingConfig:
        return self._cfg if self._cfg is not None else settings.recording

    def ffmpeg_path(self) -> str | None:
        if self._ffmpeg is AUTO:
            return shutil.which("ffmpeg")
        return str(self._ffmpeg) if self._ffmpeg else None

    # ── control ───────────────────────────────────────────────────────────────
    def start(self, *, label: str = "", by: str = "", fps: float | None = None,
              audio_device: str | None = None, meta: dict | None = None) -> dict:
        """Start a recording; returns :meth:`status`. Refused (``conflict``)
        while disabled, busy, short of free space or over the total quota."""
        rate = self._valid_fps(fps)
        cfg = self.cfg
        with self._control:
            if not cfg.enabled:
                raise _refuse("conflict", "recording is turned off on this device "
                                          "(SENTRIX_RECORDING_ENABLED=0)")
            with self._cond:
                busy = self._session
            if busy is not None:
                raise _refuse("conflict", f"already recording ({busy.name})" if not busy.stopping
                              else f"the last recording ({busy.name}) is still being saved")
            directory = self.directory
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise _refuse("conflict", f"cannot create {directory}: {exc}") from exc
            # A take whose save failed is finished first: listed, downloadable
            # and counted against the quota before the next one starts.
            self.recover()
            self.prune()
            free = self._free_bytes(directory)
            if free is not None and cfg.min_free_mb > 0 and free < cfg.min_free_mb * MB:
                raise _refuse("conflict", f"only {free // MB} MB free on the device; "
                                          f"{cfg.min_free_mb} MB are kept for the alarm's "
                                          "evidence and logs")
            total = self.total_bytes()
            if cfg.max_total_mb > 0 and total >= cfg.max_total_mb * MB:
                raise _refuse("conflict", f"recordings already take {total // MB} MB of the "
                                          f"{cfg.max_total_mb} MB allowed: download and delete "
                                          "some first")
            clean = sanitize_label(label)
            wall = float(self._wall())
            name = self._unique_name(directory, clean, wall)
            session = _Session(name=name, label=clean, by=str(by or ""), fps=rate,
                               audio_device=audio_device, context=dict(meta or {}),
                               directory=directory, cfg=cfg, created_wall=wall,
                               started_mono=float(self._clock()))
            try:
                _write_json(session.part("json"), self._draft(session))
            except OSError as exc:
                raise _refuse("conflict", f"cannot write to {directory}: {exc}") from exc
            thread = threading.Thread(target=self._run_session, args=(session,),
                                      name="sentrix-recorder", daemon=True)
            with self._cond:
                self._session = session
                self._last_error = None
            try:
                thread.start()
            except RuntimeError as exc:
                with self._cond:
                    self._session = None
                _unlink(session.part("json"))
                raise _refuse("conflict", f"cannot start the recorder thread: {exc}") from exc
            self._thread = thread
            with self._cond:
                snapshot = self._snapshot_locked()
        log.info("recording %s started (%.0f fps, by %s)", name, rate, by or "-")
        self._emit("recording_started", name=name, label=clean, by=str(by or ""), fps=rate,
                   audio_device=session.audio_device)
        return self._complete(snapshot)

    def stop(self, *, by: str = "", reason: str = "user") -> dict:
        """Ask the writer to finish; returns :meth:`status` (``finalizing``).
        The files are complete once :meth:`wait` returns. Takes only the
        state lock, never the control lock: a stop must not wait behind a
        recovery's remux on the housekeeping thread."""
        with self._cond:
            session = self._session
            if session is None or session.stopping:
                raise _refuse("conflict", "not recording")
            self._stop_locked(session, reason or "user", by)
            snapshot = self._snapshot_locked()
        return self._complete(snapshot)

    def wait(self, timeout: float | None = None) -> bool:
        """Until no recording is active or being saved; False on timeout."""
        with self._cond:
            return self._cond.wait_for(lambda: self._session is None, timeout)

    def close(self, timeout: float = 10.0) -> bool:
        """Stop a recording in progress (``shutdown``) and the housekeeping,
        and wait for the recording's files."""
        self._housekeeping_stop.set()
        with self._cond:
            session = self._session
            if session is not None:
                self._stop_locked(session, "shutdown", "")
        return self.wait(timeout)

    def start_housekeeping(self, every: float = HOUSEKEEPING_SECONDS) -> bool:
        """Every ``every`` seconds, on a daemon thread: finish the ``.part``
        files a crash or a failed save left (:meth:`recover`) and delete the
        recordings past ``keep_days`` (:meth:`prune`), whether or not anyone
        opens the dashboard. A device that runs for weeks would otherwise
        keep the people filmed on disk past the days the card promises.
        Stopped by :meth:`close`; False when it already runs."""
        with self._cond:
            if self._housekeeper is not None and self._housekeeper.is_alive():
                return False
            stop = threading.Event()
            self._housekeeping_stop = stop
            thread = threading.Thread(target=self._housekeep, args=(stop, max(0.01, float(every))),
                                      name="sentrix-recorder-housekeeping", daemon=True)
            self._housekeeper = thread
        thread.start()
        return True

    def _housekeep(self, stop: threading.Event, every: float) -> None:
        while not stop.wait(every):
            for step in (self.recover, self.prune):
                try:
                    step()
                except Exception as exc:            # the timer outlives one bad pass
                    log.warning("recordings: %s failed: %s", step.__name__, exc)

    # ── taps ──────────────────────────────────────────────────────────────────
    @property
    def wants_frames(self) -> bool:
        """True while recording (started and not stopping): the capture thread
        checks it before handing a frame over."""
        session = self._session
        return session is not None and not session.stopping

    @property
    def active(self) -> bool:
        """Recording or saving."""
        return self._session is not None

    @property
    def recording_since(self) -> float | None:
        """When the recording in progress started, on ``clock`` (the REC
        badge's mm:ss); None while idle or saving. Read from the session, so
        a refused second start cannot reset it."""
        session = self._session
        return None if session is None or session.stopping else session.started_mono

    def on_frame(self, frame: np.ndarray, seq: int, captured_at: float) -> None:
        """A camera frame (BGR uint8, the capture source's own copy: it is kept
        by reference, so nobody may draw on it afterwards). Places it on the
        constant-rate grid and queues it for the writer."""
        session = self._session
        if session is None or session.stopping:
            return
        try:
            at = float(captured_at)
            if not math.isfinite(at) or getattr(frame, "ndim", 0) not in (2, 3):
                return
            with self._cond:
                if session is not self._session or session.stopping:
                    return
                if seq is not None and seq == session.last_seq:
                    return
                session.last_seq = seq
                if session.t0 is None:
                    session.t0, index = at, 0
                else:
                    last = session.last_captured
                    if last is not None and at - last > session.cfg.camera_gap_seconds:
                        # A long freeze is worse training data than a short file.
                        self._stop_locked(session, "camera_lost", "")
                        return
                    index = _slot(at - session.t0, session.fps)
                if session.last_captured is None or at > session.last_captured:
                    session.last_captured = at
                if index >= session.max_frames:
                    self._stop_locked(session, "max_length", "")
                    return
                if index <= session.last_index:
                    session.dropped += 1             # a faster camera: this slot is taken
                    return
                if len(session.frames) >= session.queue_max:
                    session.frames.popleft()          # the writer fell behind
                    session.dropped += 1
                session.frames.append((frame, index))
                session.last_index = index
                self._cond.notify_all()
        except Exception as exc:
            log.debug("recorder frame tap failed: %s", exc)

    def on_audio(self, samples: np.ndarray, end_at: float, rate: int = RATE) -> None:
        """A microphone block (float32 mono; ``end_at`` = monotonic time of its
        last sample). Queued for the writer, which places it on the video's
        time line."""
        session = self._session
        if session is None or not session.accept_audio:
            return
        try:
            if int(rate) != RATE:
                with self._cond:
                    session.audio_ignored += 1
                return
            block = np.asarray(samples, dtype=np.float32).reshape(-1)
            end = float(end_at)
            if block.size == 0 or not math.isfinite(end):
                return
            with self._cond:
                if session is not self._session or not session.accept_audio:
                    return
                session.audio_in.append((block, end))
                self._cond.notify_all()
        except Exception as exc:
            log.debug("recorder audio tap failed: %s", exc)

    # ── status ────────────────────────────────────────────────────────────────
    def status(self) -> dict:
        with self._cond:
            snapshot = self._snapshot_locked()
        return self._complete(snapshot)

    def _snapshot_locked(self) -> dict:
        session = self._session or self._last
        active = self._session is not None
        cfg = self.cfg
        out: dict[str, Any] = {
            "enabled": bool(cfg.enabled), "active": active,
            "recording": active and not session.stopping,
            "finalizing": active and session.stopping,
            "name": None, "label": "", "started_at": None, "seconds": 0.0, "frames": 0,
            "duplicated": 0, "dropped": 0, "fps": None, "size": None,
            "audio": {"present": False, "device": None, "seconds": 0.0, "gaps": 0, "ignored": 0},
            "stop_reason": None, "last_error": self._last_error,
            "max_seconds": float(cfg.max_seconds),
        }
        if session is not None:
            if active:
                audio_present = (session.wav is not None or session.pending_len > 0
                                 or session.early_len > 0 or bool(session.audio_in))
                audio_seconds = (session.audio_pos + session.pending_len) / RATE
            else:
                audio_present, audio_seconds = session.has_audio, session.audio_seconds
            out.update({
                "name": session.name, "label": session.label,
                "started_at": _iso(session.created_wall),
                "seconds": round(session.frames_written / session.fps, 3),
                "frames": session.frames_written, "duplicated": session.duplicated,
                "dropped": session.dropped, "fps": session.fps,
                "size": list(session.size) if session.size else None,
                "audio": {"present": audio_present, "device": session.audio_device,
                          "seconds": round(audio_seconds, 3), "gaps": session.gaps,
                          "ignored": session.audio_ignored},
                "stop_reason": session.stop_reason,
                "last_error": session.error if active else self._last_error,
            })
        out["_session"], out["_active"] = session, active
        return out

    def _complete(self, snapshot: dict) -> dict:
        """The snapshot plus the figures that need the disk."""
        session, active = snapshot.pop("_session"), snapshot.pop("_active")
        size = 0
        if session is not None:
            if active:
                size = sum(_size(session.part(ext)) for ext in ("avi", "wav"))
            elif session.files:
                size = sum(_size(session.directory / name)
                           for name in session.files.values() if name)
        snapshot["bytes"] = size
        snapshot["free_bytes"] = self._free_bytes(self.directory)
        snapshot["ffmpeg"] = self.ffmpeg_path() is not None
        return snapshot

    # ── finished recordings ───────────────────────────────────────────────────
    def list(self) -> list[dict]:
        """Finished recordings, newest first."""
        directory = self.directory
        try:
            entries = os.listdir(directory)
        except OSError:
            return []
        names = {m.group("name") for e in entries
                 if (m := _FILE_RE.match(e)) and m.group("ext") == "json"}
        out = []
        for name in names:
            meta_path = directory / f"{name}.json"
            meta = _read_json(meta_path) or {}
            created = meta.get("created_at")
            created_ts = _parse_iso(created)
            if created_ts is None:
                try:
                    created_ts = meta_path.stat().st_mtime
                except OSError:
                    created_ts = 0.0
                created = _iso(created_ts)
            files = {"video": f"{name}.avi" if (directory / f"{name}.avi").is_file() else None,
                     "audio": f"{name}.wav" if (directory / f"{name}.wav").is_file() else None,
                     "meta": f"{name}.json"}
            out.append({
                "name": name, "label": meta.get("label", ""), "created_at": created,
                "seconds": meta.get("seconds"),
                "bytes": sum(_size(directory / f) for f in files.values() if f),
                "has_audio": bool((meta.get("audio") or {}).get("present")),
                "interrupted": bool(meta.get("interrupted")), "files": files,
                "_ts": created_ts,
            })
        out.sort(key=lambda e: (e["_ts"], e["name"]), reverse=True)
        for entry in out:
            del entry["_ts"]
        return out

    def total_bytes(self) -> int:
        """All finished recordings together."""
        return sum(entry["bytes"] for entry in self.list())

    def path_for(self, filename: str) -> Path | None:
        """The file to serve for ``filename``: only the exact name of a finished
        recording's ``.avi``, ``.wav`` or ``.json`` in the directory. Never a
        ``.part`` file, never a path, never anything resolving outside."""
        if not isinstance(filename, str) or not filename or ".part." in filename:
            return None
        if "/" in filename or "\\" in filename or os.path.basename(filename) != filename:
            return None
        match = _FILE_RE.match(filename)
        if match is None:
            return None
        name = match.group("name")
        with self._cond:
            if self._session is not None and self._session.name == name:
                return None
        directory = self.directory
        path = directory / filename
        try:
            if path.resolve().parent != directory.resolve():
                return None
        except OSError:
            return None
        if not path.is_file() or not (directory / f"{name}.json").is_file():
            return None
        return path

    def delete(self, name: str, *, by: str = "") -> bool:
        """Delete a finished recording; False when there is none by that name.
        The one in progress is refused (``conflict``)."""
        if not isinstance(name, str) or not NAME_RE.match(name):
            return False
        with self._control:
            with self._cond:
                session = self._session
            if session is not None and session.name == name:
                raise _refuse("conflict", "that recording is still in progress: stop it first")
            found = self._remove(name)
        if found:
            log.info("recording %s deleted by %s", name, by or "-")
            self._emit("recording_deleted", name=name, by=str(by or ""))
        return found

    def prune(self, now: float | None = None) -> list[str]:
        """Delete finished recordings older than ``keep_days`` (0 keeps them)."""
        days = float(self.cfg.keep_days)
        if days <= 0:
            return []
        now = float(self._wall()) if now is None else float(now)
        removed: list[str] = []
        with self._control:
            for entry in self.list():
                created = _parse_iso(entry["created_at"])
                if created is None or now - created <= days * 86400.0:
                    continue
                try:
                    if self._remove(entry["name"]):
                        removed.append(entry["name"])
                        self._emit("recording_pruned", name=entry["name"],
                                   age_days=round((now - created) / 86400.0, 1))
                except RecorderError as exc:
                    log.warning("cannot prune %s: %s", entry["name"], exc.message)
        if removed:
            log.info("pruned %d recording(s) older than %g days", len(removed), days)
        return removed

    def _remove(self, name: str) -> bool:
        """The media first and the sidecar last, so a file that cannot be
        deleted (being downloaded, on Windows) stays listed."""
        directory = self.directory
        found = False
        for ext in ("avi", "wav", "json"):
            path = directory / f"{name}.{ext}"
            if not path.exists():
                continue
            try:
                path.unlink()
            except OSError as exc:
                raise _refuse("conflict", f"cannot delete {path.name}: {exc}") from exc
            found = True
        return found

    # ── crash recovery ────────────────────────────────────────────────────────
    def recover(self) -> list[str]:
        """Turn the ``.part`` files a crash or a failed save left into
        finished recordings. A save that got as far as its sidecar is
        finished with that sidecar's figures; otherwise the sidecar is rebuilt
        from the draft and the files (``interrupted``). With ffmpeg the AVI is
        remuxed, which rebuilds the index OpenCV writes only on release;
        without it the file is renamed (OpenCV and ffmpeg read an AVI without
        an index). Runs at start-up, before every start and on the
        housekeeping timer; the recording in progress is left alone."""
        directory = self.directory
        try:
            entries = os.listdir(directory)
        except OSError:
            return []
        names = sorted({m.group("name") for e in entries if (m := _PART_RE.match(e))})
        recovered: list[str] = []
        with self._control:
            with self._cond:
                active = self._session.name if self._session is not None else None
            for name in names:
                if name == active:
                    continue
                try:
                    if self._recover_one(directory, name):
                        recovered.append(name)
                except Exception as exc:
                    log.error("cannot recover recording %s: %s", name, exc)
        return recovered

    def _recover_one(self, directory: Path, name: str) -> bool:
        part = {ext: directory / f"{name}.part.{ext}" for ext in ("avi", "wav", "json")}
        mux = directory / f"{name}.part.mux.avi"
        final = {ext: directory / f"{name}.{ext}" for ext in ("avi", "wav", "json")}
        if final["json"].exists():       # finished already: the leftovers are stale
            for path in (*part.values(), mux):
                _unlink(path)
            return False
        draft = _read_json(part["json"]) or {}
        if not draft.get("draft") and isinstance(draft.get("files"), dict):
            # The save wrote its whole sidecar, then stopped part way through
            # the renames: finish them and keep its figures.
            finished = self._finish_saved(name, draft, part, final, mux)
            if finished is not None:
                return finished
        _unlink(mux)                     # an unfinished mux; its inputs are still here
        if not part["avi"].exists() and not final["avi"].exists():
            log.warning("recording %s left no video: removing its leftovers", name)
            for path in part.values():
                _unlink(path)
            return False
        wav = part["wav"] if part["wav"].exists() else final["wav"]
        audio_seconds = _wav_seconds(wav) if wav.exists() else 0.0
        remuxed, error, muxed_audio = False, None, False
        if part["avi"].exists():
            if final["avi"].exists():
                _unlink(part["avi"])
            else:
                ffmpeg = self.ffmpeg_path()
                with_audio = part["wav"].exists() and audio_seconds > 0
                need = _size(part["avi"]) + (_size(part["wav"]) if with_audio else 0)
                short = self._short_of_room(directory, self.cfg, need) if ffmpeg else None
                if short is not None:
                    error = f"not remuxed: {short}"
                    log.warning("recording %s: %s", name, error)
                elif ffmpeg:
                    argv = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                            "-i", str(part["avi"])]
                    if with_audio:
                        argv += ["-i", str(part["wav"]), "-map", "0:v:0", "-map", "1:a:0",
                                 "-c:v", "copy", "-c:a", "pcm_s16le"]
                    else:
                        argv += ["-map", "0:v:0", "-c", "copy"]
                    argv.append(str(mux))
                    error = self._ffmpeg_run(argv, mux)
                    if error is None:
                        _replace(mux, final["avi"])
                        _unlink(part["avi"])
                        if with_audio:
                            _unlink(part["wav"])
                            muxed_audio = True
                        remuxed = True
                    else:
                        _unlink(mux)
                        error = f"remux failed: {error}"
                if not final["avi"].exists():
                    _replace(part["avi"], final["avi"])
        frames, read_fps, size = _probe_video(final["avi"])
        # The rate a reader takes from the file (an older draft may hold a
        # fraction the AVI header could not store).
        fps = read_fps or draft.get("fps")
        if not muxed_audio and _avi_has_audio(final["avi"]):
            # A mux (or remux) finished before the crash: the sound is in the
            # AVI, and a .wav still lying next to it is the same sound again.
            muxed_audio = True
            _unlink(part["wav"])
            if audio_seconds <= 0 and frames and fps:
                audio_seconds = frames / fps      # the track was padded to the video
        elif part["wav"].exists():
            if final["wav"].exists():
                _unlink(part["wav"])
            else:
                _replace(part["wav"], final["wav"])
        has_audio = audio_seconds > 0 and (muxed_audio or final["wav"].exists())
        try:
            stopped = _iso(final["avi"].stat().st_mtime)
        except OSError:
            stopped = None
        files = {"video": final["avi"].name,
                 "audio": final["wav"].name if final["wav"].exists() and not muxed_audio
                 else None,
                 "meta": final["json"].name}
        audio_draft = draft.get("audio") if isinstance(draft.get("audio"), dict) else {}
        sidecar = {
            "name": name, "label": draft.get("label", ""), "by": draft.get("by", ""),
            "created_at": draft.get("created_at") or stopped, "stopped_at": stopped,
            "stop_reason": "interrupted", "stopped_by": "",
            "seconds": round(frames / fps, 3) if frames and fps else None,
            "fps": fps, "size": size, "frames": frames, "duplicated": None, "dropped": None,
            "audio": {"present": has_audio, "device": audio_draft.get("device"), "rate": RATE,
                      "seconds": round(audio_seconds, 3) if has_audio else 0.0,
                      "gaps": None, "ignored": None},
            "files": files, "interrupted": True,
            "format": {"video": "mjpeg", "audio": "pcm_s16le" if has_audio else None},
            "context": draft.get("context") or {}, "error": error,
        }
        _write_json(part["json"], sidecar)
        _replace(part["json"], final["json"])
        log.warning("recovered interrupted recording %s (%s frames%s)", name, frames,
                    ", with sound" if has_audio else "")
        self._emit("recording_recovered", name=name, frames=frames, has_audio=has_audio,
                   remuxed=remuxed)
        return True

    def _finish_saved(self, name: str, meta: dict, part: dict, final: dict,
                      mux: Path) -> bool | None:
        """Finish a save that wrote its whole sidecar (``meta``) and stopped
        during the renames: the files it names take their final names, and
        the sidecar is kept as it is. None when the files on disk do not match
        it (the sidecar is then rebuilt like a crash's)."""
        files = meta.get("files") or {}
        audio_name = files.get("audio")
        muxed = bool((meta.get("audio") or {}).get("present")) and not audio_name
        if muxed:
            if mux.exists():             # the sidecar is written only after the mux succeeded
                _replace(mux, final["avi"])
            if final["avi"].exists():
                _unlink(part["avi"])
                _unlink(part["wav"])
        else:
            _unlink(mux)
            if part["avi"].exists() and not final["avi"].exists():
                _replace(part["avi"], final["avi"])
            if audio_name:
                if part["wav"].exists() and not final["wav"].exists():
                    _replace(part["wav"], final["wav"])
            else:
                _unlink(part["wav"])
        if not final["avi"].exists() or (audio_name and not final["wav"].exists()):
            return None
        _unlink(part["avi"])
        _replace(part["json"], final["json"])
        has_audio = bool((meta.get("audio") or {}).get("present"))
        log.warning("finished saving recording %s (%s frames%s)", name, meta.get("frames"),
                    ", with sound" if has_audio else "")
        self._emit("recording_recovered", name=name, frames=meta.get("frames"),
                   has_audio=has_audio, remuxed=False)
        return True

    # ── the writer thread ─────────────────────────────────────────────────────
    def _run_session(self, session: _Session) -> None:
        _lower_priority()
        try:
            self._record(session)
        except Exception as exc:
            log.exception("recording %s failed", session.name)
            session.error = session.error or f"recording failed: {exc}"
            self._request_stop(session, "error")
        try:
            self._finalise(session)
        except Exception as exc:
            log.exception("saving recording %s failed", session.name)
            session.error = session.error or f"saving failed: {exc}"
            for close in (getattr(session.writer, "release", None),
                          getattr(session.wav, "close", None)):
                if close is not None:
                    try:
                        close()
                    except Exception:
                        pass
            self._salvage(session)
        finally:
            with self._cond:
                session.accept_audio = False
                self._last = session
                self._last_error = session.error
                self._session = None
                self._cond.notify_all()

    def _salvage(self, session: _Session) -> None:
        """Saving failed part way (a rename refused, the disk full): finish
        the files as :meth:`recover` would, now rather than at the next start,
        so the take is listed, downloadable and counted. What cannot be
        finished now stays ``.part`` for the next start or housekeeping pass.
        Audited either way (``recording_stopped`` with ``saved`` and the
        error)."""
        saved = False
        try:
            saved = self._recover_one(session.directory, session.name)
        except Exception as exc:
            log.error("recording %s: finishing the files failed too: %s", session.name, exc)
        session.saved = bool(saved)
        if saved:
            meta = _read_json(session.final("json")) or {}
            session.files = meta.get("files") if isinstance(meta.get("files"), dict) else None
            session.error = f"{session.error} (the files were finished afterwards)"
        else:
            session.error = (f"{session.error} (the .part files are finished later: before "
                             "the next recording or at the next start)")
        seconds = round(session.frames_written / session.fps, 3)
        self._emit("recording_stopped", name=session.name, by=session.stop_by,
                   reason=session.stop_reason, seconds=seconds, frames=session.frames_written,
                   has_audio=session.has_audio, saved=session.saved, error=session.error)

    def _record(self, session: _Session) -> None:
        """Write queued frames and audio until stopped and drained."""
        while True:
            with self._cond:
                if not session.frames and not session.audio_in and not session.stopping:
                    self._cond.wait(IDLE_WAIT_SECONDS)
                item = session.frames.popleft() if session.frames else None
                blocks = list(session.audio_in)
                session.audio_in.clear()
                done = session.stopping and item is None
            if item is not None:
                self._write_frame(session, *item)
            self._take_audio(session, blocks)
            self._commit_audio(session, self._video_samples(session))
            if done:
                return
            self._check_limits(session)

    def _check_limits(self, session: _Session) -> None:
        """The stops no frame triggers: a camera that went silent, the length
        limit without frames, the disk filling up and the AVI size limit."""
        if session.stopping:
            return
        cfg = session.cfg
        now = float(self._clock())
        last = session.last_captured if session.last_captured is not None else session.started_mono
        origin = session.t0 if session.t0 is not None else session.started_mono
        if now - last > cfg.camera_gap_seconds:
            self._request_stop(session, "camera_lost")
        elif now - origin >= cfg.max_seconds:
            self._request_stop(session, "max_length")
        elif now - session.disk_at >= DISK_CHECK_SECONDS:
            session.disk_at = now
            free = self._free_bytes(session.directory)
            if free is not None and cfg.min_free_mb > 0 and free < cfg.min_free_mb * MB:
                self._request_stop(session, "disk_full")
            elif _size(session.part("avi")) >= MAX_VIDEO_BYTES:
                self._request_stop(session, "max_size")

    def _write_frame(self, session: _Session, frame: np.ndarray, index: int) -> None:
        if session.error:
            return
        try:
            image = self._fit(session, frame)
            if session.writer is None:
                path = session.part("avi")
                writer = self._writer_factory(path, session.fps, session.size,
                                              int(session.cfg.jpeg_quality))
                if writer is None or not writer.isOpened():
                    session.error = f"the video writer did not open {path.name}"
                    self._request_stop(session, "error")
                    return
                session.writer = writer
            missing = index - session.frames_written
            if missing < 0:
                return
            # The slots the camera left empty repeat the previous frame (the
            # first one, before it), so frame n is always at t0 + n / fps.
            filler = session.last_frame if session.last_frame is not None else image
            for _ in range(missing):
                session.writer.write(filler)
                session.frames_written += 1
                session.duplicated += 1
            session.writer.write(image)
            session.frames_written += 1
            session.last_frame = image
        except Exception as exc:
            log.error("recording %s: frame write failed: %s", session.name, exc)
            session.error = session.error or f"frame write failed: {exc}"
            self._request_stop(session, "error")

    def _fit(self, session: _Session, frame: np.ndarray) -> np.ndarray:
        """BGR uint8 at the recording's size (the first frame's, scaled down to
        ``max_width``)."""
        import cv2

        if frame.dtype != np.uint8:
            frame = np.clip(frame, 0, 255).astype(np.uint8)
        if frame.ndim == 2:
            frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        elif frame.shape[2] == 4:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
        height, width = frame.shape[:2]
        if session.size is None:
            session.size = _target_size(width, height, int(session.cfg.max_width))
        if (width, height) != session.size:
            frame = cv2.resize(frame, session.size, interpolation=cv2.INTER_AREA)
        return frame

    # ── audio ─────────────────────────────────────────────────────────────────
    def _video_samples(self, session: _Session) -> int:
        """The video's length so far, in audio samples."""
        return int(round(session.frames_written * RATE / session.fps))

    def _take_audio(self, session: _Session, blocks: list) -> None:
        """Place queued blocks on the track; the ones heard before the first
        frame wait (at most ``queue_seconds``) until it fixes t0."""
        if session.t0 is not None and session.early:
            blocks = list(session.early) + list(blocks)
            session.early.clear()
            session.early_len = 0
        for samples, end_at in blocks:
            samples = np.nan_to_num(samples)
            if session.t0 is None:
                session.early.append((samples, end_at))
                session.early_len += len(samples)
                cap = int(float(session.cfg.queue_seconds) * RATE)
                while session.early_len > cap and session.early:
                    session.early_len -= len(session.early.popleft()[0])
                continue
            self._place_audio(session, samples, end_at)

    def _place_audio(self, session: _Session, samples: np.ndarray, end_at: float) -> None:
        """Sample position = (block start - t0) x 16 kHz: cropped before t0,
        a gap zero-filled, an overlap trimmed."""
        position = _slot(end_at - len(samples) / RATE - session.t0, RATE)
        if position < 0:
            samples = samples[-position:]
            position = 0
            if not len(samples):
                return
        end = session.audio_pos + session.pending_len
        limit = self._video_samples(session) + session.hold_samples
        if position >= limit:
            with self._cond:                # far past the video: the camera stalled
                session.audio_ignored += 1
            return
        jitter = int(AUDIO_JITTER_SECONDS * RATE)
        shift = position - end
        if shift > jitter:
            self._pend(session, shift)
            session.gaps += 1
            end = position
        elif shift < -jitter:
            samples = samples[-shift:]
            if not len(samples):
                return
        room = limit - end
        if room > 0:
            self._pend(session, samples[:room])

    @staticmethod
    def _pend(session: _Session, item: np.ndarray | int) -> None:
        """Queue samples (an array) or that many samples of silence (an int)."""
        size = item if isinstance(item, int) else len(item)
        if size > 0:
            session.pending.append(item)
            session.pending_len += size

    def _commit_audio(self, session: _Session, upto: int) -> None:
        """Write pending audio into the WAV up to sample ``upto`` (the video's
        end): the track never runs past the video, so nothing has to be cut
        at the end."""
        room = upto - session.audio_pos
        while room > 0 and session.pending:
            item = session.pending[0]
            size = item if isinstance(item, int) else len(item)
            take = min(size, room)
            if isinstance(item, int):
                self._write_pcm(session, None, take)
                if take == size:
                    session.pending.popleft()
                else:
                    session.pending[0] = size - take
            else:
                self._write_pcm(session, item[:take], take)
                if take == size:
                    session.pending.popleft()
                else:
                    session.pending[0] = item[take:]
            session.pending_len -= take
            session.audio_pos += take
            room -= take

    def _write_pcm(self, session: _Session, samples: np.ndarray | None, count: int) -> None:
        """Samples, or ``count`` samples of silence, into ``<name>.part.wav``
        (opened at the first one: no microphone, no WAV). The wave module
        rewrites the header after every write, so a crash leaves a valid file."""
        if session.wav is None:
            writer = wave.open(str(session.part("wav")), "wb")
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(RATE)
            session.wav = writer
        if samples is not None:
            session.wav.writeframes(_to_pcm(samples))
            return
        chunk = b"\x00\x00" * RATE
        while count > 0:
            step = min(count, RATE)
            session.wav.writeframes(chunk[:step * 2])
            count -= step

    # ── finalisation ──────────────────────────────────────────────────────────
    def _finalise(self, session: _Session) -> None:
        cfg = session.cfg
        target = self._video_samples(session)
        # The microphone's block covering the video's end arrives up to one
        # block later: wait for it (real time; it comes from the audio thread).
        if (session.wav is not None or session.pending_len or session.early_len
                or session.audio_in):
            deadline = time.monotonic() + max(0.0, float(cfg.audio_tail_seconds))
            while True:
                with self._cond:
                    blocks = list(session.audio_in)
                    session.audio_in.clear()
                self._take_audio(session, blocks)
                self._commit_audio(session, target)
                if session.audio_pos + session.pending_len >= target:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                with self._cond:
                    if not session.audio_in:
                        self._cond.wait(min(remaining, IDLE_WAIT_SECONDS))
        with self._cond:
            session.accept_audio = False
            blocks = list(session.audio_in)
            session.audio_in.clear()
        self._take_audio(session, blocks)
        self._commit_audio(session, target)
        if session.writer is not None:
            try:
                session.writer.release()
            except Exception as exc:
                session.error = session.error or f"closing the video failed: {exc}"
        if session.wav is not None:
            # Padded (or, past ``target``, never written) to the video's length.
            session.pending.clear()
            session.pending_len = 0
            if session.audio_pos < target:
                self._pend(session, target - session.audio_pos)
                self._commit_audio(session, target)
            session.wav.close()
        session.has_audio = session.wav is not None
        session.audio_seconds = session.audio_pos / RATE if session.has_audio else 0.0
        if session.frames_written == 0:
            self._discard(session)
            return
        files = {"video": session.final("avi").name, "audio": None,
                 "meta": session.final("json").name}
        out = session.directory / f"{session.name}.part.mux.avi"
        muxed = False
        if session.has_audio:
            ffmpeg = self.ffmpeg_path()
            # The mux writes the whole video again while the parts still
            # exist: never into the space kept for the alarm's evidence.
            short = (self._short_of_room(session.directory, cfg, _size(session.part("avi"))
                                         + _size(session.part("wav"))) if ffmpeg else None)
            if short is not None:
                session.error = session.error or f"sound kept in a separate .wav: {short}"
                log.warning("recording %s: sound kept in a separate .wav: %s", session.name, short)
            elif ffmpeg:
                error = self._mux(ffmpeg, session.part("avi"), session.part("wav"), out)
                if error is None:
                    muxed = True
                else:
                    _unlink(out)
                    session.error = session.error or f"audio mux failed: {error}"
                    log.error("recording %s: %s (video and .wav kept apart)",
                              session.name, error)
            if not muxed:
                files["audio"] = session.final("wav").name
        session.files = files
        # The whole sidecar before any rename: a crash or a refused rename from
        # here on leaves a sidecar recover() finishes with these figures.
        _write_json(session.part("json"), self._sidecar(session))
        if muxed:
            _replace(out, session.final("avi"))
            _unlink(session.part("avi"))
            _unlink(session.part("wav"))
        else:
            if session.has_audio:
                _replace(session.part("wav"), session.final("wav"))
            _replace(session.part("avi"), session.final("avi"))
        _replace(session.part("json"), session.final("json"))
        session.saved = True
        seconds = round(session.frames_written / session.fps, 3)
        log.info("recording %s saved: %.1f s, %d frames (%d repeated, %d dropped)%s, stop: %s",
                 session.name, seconds, session.frames_written, session.duplicated,
                 session.dropped, ", with sound" if session.has_audio else "",
                 session.stop_reason)
        self._emit("recording_stopped", name=session.name, by=session.stop_by,
                   reason=session.stop_reason, seconds=seconds, frames=session.frames_written,
                   has_audio=session.has_audio)

    def _discard(self, session: _Session) -> None:
        """No frame was written: nothing worth keeping."""
        for path in (session.part("avi"), session.part("wav"), session.part("json"),
                     session.directory / f"{session.name}.part.mux.avi"):
            _unlink(path)
        session.has_audio, session.audio_seconds = False, 0.0
        session.error = session.error or "no camera frame arrived: nothing was saved"
        log.warning("recording %s discarded: %s", session.name, session.error)
        self._emit("recording_stopped", name=session.name, by=session.stop_by,
                   reason=session.stop_reason, seconds=0.0, frames=0, has_audio=False,
                   saved=False)

    def _short_of_room(self, directory: Path, cfg: RecordingConfig, need: int) -> str | None:
        """Why a copy of ``need`` bytes must not be written now, or None: it
        would take free space under the ``min_free_mb`` floor kept for the
        alarm's evidence, snapshots and logs (a recording that stopped
        ``disk_full`` is at that floor already)."""
        floor = int(cfg.min_free_mb) * MB
        if floor <= 0:
            return None
        free = self._free_bytes(directory)
        if free is None or free - need >= floor:
            return None
        return (f"the copy ffmpeg writes needs {math.ceil(need / MB)} MB and only "
                f"{max(0, free) // MB} MB are free, {cfg.min_free_mb} MB of which are kept for "
                "the alarm's evidence and logs")

    def _mux(self, ffmpeg: str, video: Path, audio: Path, out: Path) -> str | None:
        argv = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
                "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                "-c:a", "pcm_s16le", str(out)]
        return self._ffmpeg_run(argv, out)

    def _ffmpeg_run(self, argv: list[str], out: Path) -> str | None:
        """Run ffmpeg (an argv list, never a shell); None on success, else why
        it failed."""
        try:
            proc = self._run(argv, capture_output=True, timeout=MUX_TIMEOUT_SECONDS,
                             check=False, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return f"ffmpeg timed out after {MUX_TIMEOUT_SECONDS:.0f} s"
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return f"ffmpeg did not run: {exc}"
        if proc.returncode != 0:
            stderr = proc.stderr or b""
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", "replace")
            return f"ffmpeg exited with {proc.returncode}: {stderr.strip()[-300:]}"
        if _size(out) == 0:
            return "ffmpeg wrote no output"
        return None

    # ── helpers ───────────────────────────────────────────────────────────────
    def _request_stop(self, session: _Session, reason: str, by: str = "") -> None:
        with self._cond:
            self._stop_locked(session, reason, by)

    def _stop_locked(self, session: _Session, reason: str, by: str) -> None:
        if session.stopping:
            return
        session.stopping = True
        session.stop_reason = reason
        session.stop_by = str(by or "")
        session.stopped_wall = float(self._wall())
        self._cond.notify_all()
        if reason not in ("user", "shutdown"):
            log.warning("recording %s stopping: %s", session.name, reason)

    @staticmethod
    def _valid_fps(fps: Any) -> float:
        """The recording's rate: within 5-30 fps and a whole number. OpenCV's
        AVI writer stores the rate rounded (12.5 as 12/1), so a fraction
        would put the grid, the sound and the sidecar on one rate and the
        file on another: 4 % apart at 12.5, 25 s by the end of 10 minutes.
        Rounded like OpenCV's ``cvRound`` (half to even, as Python's
        ``round``), so the file says the same."""
        if fps is None:
            return DEFAULT_FPS
        try:
            value = float(fps)
        except (TypeError, ValueError):
            raise _refuse("invalid_args", "fps must be a number") from None
        if not math.isfinite(value):
            raise _refuse("invalid_args", "fps must be a number")
        return float(round(min(MAX_FPS, max(MIN_FPS, value))))

    @staticmethod
    def _unique_name(directory: Path, label: str, wall: float) -> str:
        """``rec_YYYYMMDD_HHMMSS[_label]`` in local time, ``_2``, ``_3``... when
        a file by that name (finished or not) exists."""
        base = "rec_" + time.strftime("%Y%m%d_%H%M%S", time.localtime(wall))
        if label:
            base += f"_{label}"
        try:
            taken = {entry.split(".", 1)[0] for entry in os.listdir(directory)}
        except OSError:
            taken = set()
        name, n = base, 2
        while name in taken:
            name, n = f"{base}_{n}", n + 1
        return name

    def _free_bytes(self, directory: Path | None = None) -> int | None:
        """Free space where recordings go (its nearest existing parent before
        the first recording creates it); None when it cannot be measured."""
        path = Path(directory) if directory is not None else self.directory
        for candidate in (path, *path.parents):
            if candidate.exists():
                try:
                    return int(self._disk_usage(str(candidate)).free)
                except Exception as exc:
                    log.debug("disk usage of %s failed: %s", candidate, exc)
                    return None
        return None

    def _draft(self, session: _Session) -> dict:
        """What :meth:`recover` needs if the process dies mid-recording."""
        return {"name": session.name, "label": session.label, "by": session.by,
                "created_at": _iso(session.created_wall), "fps": session.fps,
                "audio": {"device": session.audio_device}, "context": session.context,
                "draft": True}

    def _sidecar(self, session: _Session) -> dict:
        return {
            "name": session.name, "label": session.label, "by": session.by,
            "created_at": _iso(session.created_wall), "stopped_at": _iso(session.stopped_wall),
            "stop_reason": session.stop_reason, "stopped_by": session.stop_by,
            "seconds": round(session.frames_written / session.fps, 3), "fps": session.fps,
            "size": list(session.size) if session.size else None,
            "frames": session.frames_written, "duplicated": session.duplicated,
            "dropped": session.dropped,
            "audio": {"present": session.has_audio, "device": session.audio_device,
                      "rate": RATE, "seconds": round(session.audio_seconds, 3),
                      "gaps": session.gaps, "ignored": session.audio_ignored},
            "files": session.files, "interrupted": False,
            "format": {"video": "mjpeg", "audio": "pcm_s16le" if session.has_audio else None},
            "context": session.context, "error": session.error,
        }

    def _emit(self, event: str, **detail: Any) -> None:
        if self._audit is None:
            return
        try:
            self._audit(event, **detail)
        except Exception as exc:
            log.debug("recorder audit %s failed: %s", event, exc)


__all__ = ["AUTO", "RATE", "NAME_RE", "Recorder", "RecorderError", "open_mjpeg_writer",
           "sanitize_label"]
