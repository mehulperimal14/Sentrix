"""Raspberry Pi CSI camera through Picamera2 (docs/8_RASPBERRY_PI5_INTEGRATION.md §3, §4).

**OpenCV cannot open the CSI camera on a Pi 5** — libcamera owns it — so the
Camera Module 3 needs its own :class:`FrameSource`. USB webcams keep working
through ``CaptureSource``.

One Picamera2 configuration delivers two streams from every sensor frame:

* ``main`` 1280×720 — the preview, the HUD and evidence (what ``latest()`` and
  ``read()`` return, so the live loop and ``run_live`` are unchanged);
* ``lores`` 640×480 — the analysis stream the clip engine reads with
  :meth:`PiCameraSource.latest_lores` (same sequence number and timestamp as
  the main frame it came with).

Both are BGR ``uint8`` arrays, ready for OpenCV: Picamera2's ``RGB888`` is
laid out B, G, R. A Pi 4 can only produce a YUV420 lores stream; the source
falls back to it and converts.

A reader thread holds one ``capture_request()`` at a time and always releases
it (a request that is not released starves libcamera of buffers until the
camera stalls). The capture time is the frame's ``SensorTimestamp`` (the
monotonic clock libcamera and ``time.monotonic()`` share on Linux) whenever
it is plausible, else the arrival time. A camera that fails — ribbon cable
out, libcamera error — marks the source offline and is reopened every
``reconnect_seconds``; the live loop shows its "No camera signal" frame.

``picamera2`` comes from apt (``python3-picamera2``) and is visible only in a
venv made with ``--system-site-packages``; it is imported on first use, so
this module imports anywhere. Tests pass a fake module as ``picamera2_module``.

Source spec (``CAMERA_SOURCES``): ``picamera`` or ``picamera:<n>``, optionally
``?fps=15&main=1280x720&lores=640x480``. Defaults: ``SENTRIX_CAPTURE_FPS`` and
``SENTRIX_PICAMERA_MAIN`` / ``SENTRIX_PICAMERA_LORES``.

Orientation: a Camera Module's picture is upright with its ribbon connector at
the bottom (the IMX708's device tree rotation is 180 and libcamera's default
undoes it), which is how the SENTRIX enclosure mounts it. A module mounted
connector-up needs ``SENTRIX_PICAMERA_ROTATION=180``: libcamera then flips the
sensor's readout (``Transform(hflip=1, vflip=1)``), so every stream comes out
upright at no cost.
"""

from __future__ import annotations

import importlib
import threading
import time
from typing import Any
from urllib.parse import parse_qs

import numpy as np

from sentrix.config import settings
from sentrix.io import tunables
from sentrix.io.sources import FrameSource
from sentrix.utils.logging import get_logger

log = get_logger("picamera")

try:
    import cv2
except ImportError:                                    # pragma: no cover
    cv2 = None                                         # type: ignore[assignment]

#: A sensor timestamp further than this from time.monotonic() is not trusted.
_TIMESTAMP_SKEW_SECONDS = 5.0
_INSTALL_HINT = ("picamera2 is not importable: sudo apt install python3-picamera2 and "
                 "create the venv with --system-site-packages")


def _import_picamera2() -> Any:
    return importlib.import_module("picamera2")


def _transform_180() -> Any:
    """libcamera's 180 degree transform (``libcamera.Transform``: picamera2
    imports libcamera itself but does not re-export the class); ``None`` when
    it cannot be made. Never raises."""
    try:
        return importlib.import_module("libcamera").Transform(hflip=1, vflip=1)
    except Exception:
        return None


def parse_spec(spec: str) -> dict[str, Any]:
    """``"picamera:1?fps=15&main=1920x1080"`` -> ``{"camera_num": 1, "fps": 15.0,
    "main_size": (1920, 1080)}``. Raises ``ValueError`` on a bad spec."""
    head, _, query = spec.strip().partition("?")
    kind, _, number = head.partition(":")
    if kind.lower() not in ("picamera", "picam"):
        raise ValueError(f"not a picamera spec: {spec!r}")
    number = number.strip()
    if number and not number.isdigit():
        raise ValueError(f"camera number must be an integer: {spec!r}")
    options: dict[str, Any] = {"camera_num": int(number) if number else 0}
    params = {k.lower(): v[-1] for k, v in parse_qs(query).items()}
    if "fps" in params:
        fps = float(params["fps"])
        if not 1.0 <= fps <= 120.0:
            raise ValueError(f"fps out of range in {spec!r}")
        options["fps"] = fps
    for key, field in (("main", "main_size"), ("lores", "lores_size")):
        if key in params:
            size = tunables.parse_size(params[key], (0, 0))
            if size == (0, 0):
                raise ValueError(f"bad {key} size in {spec!r}")
            options[field] = size
    return options


class PiCameraSource(FrameSource):
    """Picamera2 main + lores streams behind the ``FrameSource`` API."""

    def __init__(self, camera_num: int = 0, *, main_size: tuple[int, int] | None = None,
                 lores_size: tuple[int, int] | None = None, fps: float | None = None,
                 rotation: int | None = None, reconnect_seconds: float = 2.0,
                 picamera2_module: Any = None, start: bool = True) -> None:
        self.camera_num = int(camera_num)
        self.name = f"picamera:{self.camera_num}"
        self.rotation = tunables.picamera_rotation() if rotation is None \
            else tunables.parse_rotation(rotation)
        self.applied_rotation = 0          # what the running camera really got
        self._transform: Any = None
        self._transform_checked = False
        self.main_size = tuple(main_size or tunables.picamera_main_size())
        lores = tuple(lores_size or tunables.picamera_lores_size())
        # libcamera: the lores stream may not be larger than the main one.
        self.lores_size = (min(lores[0], self.main_size[0]), min(lores[1], self.main_size[1]))
        self.fps = float(fps or settings.pipeline.capture_fps)
        self._reconnect = reconnect_seconds
        self._module = picamera2_module
        self._camera: Any = None
        self.lores_format: str | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._frame: np.ndarray | None = None
        self._lores: np.ndarray | None = None
        self._metadata: dict[str, Any] = {}
        self._seq = 0
        self._stamp = 0.0
        self._last_frame_at: float | None = None
        self.frames = 0
        self.errors = 0
        self.last_error: str | None = None
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name=f"sentrix-picam-{self.camera_num}")
        if start:
            self._thread.start()

    @classmethod
    def from_spec(cls, spec: str, **kwargs: Any) -> "PiCameraSource":
        return cls(**{**parse_spec(spec), **kwargs})

    # ── camera lifecycle ──────────────────────────────────────────────────────
    def _note_error(self, message: str) -> None:
        if message != self.last_error:            # once per distinct problem
            log.error("camera %s: %s", self.name, message)
        self.last_error = message
        self.errors += 1

    def _open(self) -> bool:
        try:
            module = self._module or _import_picamera2()
        except Exception:
            self._note_error(_INSTALL_HINT)
            return False
        try:
            info = getattr(module.Picamera2, "global_camera_info", None)
            if info is not None and len(info()) <= self.camera_num:
                self._note_error(f"no camera {self.camera_num} detected (ribbon cable; a Pi 5 "
                                 "needs the Standard-Mini cable)")
                return False
            camera = module.Picamera2(self.camera_num)
        except Exception as exc:
            self._note_error(f"open failed: {exc}")
            return False
        frame_us = int(round(1_000_000 / self.fps))
        extra: dict[str, Any] = {}
        if self.rotation == 180:
            if not self._transform_checked:       # once per source, not per reopen
                self._transform_checked = True
                self._transform = _transform_180()
                if self._transform is None:
                    log.warning("camera %s: rotation 180 asked for, but libcamera's Transform "
                                "is not available: the picture stays as the sensor gives it",
                                self.name)
            if self._transform is not None:
                extra["transform"] = self._transform
        last: Exception | None = None
        for lores_format in ("RGB888", "YUV420"):          # Pi 5, then Pi 4 and earlier
            try:
                config = camera.create_video_configuration(
                    main={"size": self.main_size, "format": "RGB888"},
                    lores={"size": self.lores_size, "format": lores_format},
                    controls={"FrameDurationLimits": (frame_us, frame_us)}, **extra)
                camera.configure(config)
                camera.start()
            except Exception as exc:
                last = exc
                try:
                    camera.stop()
                except Exception:
                    pass
                continue
            self._camera, self.lores_format = camera, lores_format
            self.last_error = None
            self.applied_rotation = 180 if "transform" in extra else 0
            log.info("camera %s started: main %dx%d, lores %dx%d %s, %.0f fps, rotation %d",
                     self.name, *self.main_size, *self.lores_size, lores_format, self.fps,
                     self.applied_rotation)
            return True
        self._note_error(f"configure failed: {last}")
        self._close_camera(camera)
        return False

    @staticmethod
    def _close_camera(camera: Any) -> None:
        for step in ("stop", "close"):
            try:
                getattr(camera, step)()
            except Exception:
                pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._camera is None and not self._open():
                self._stop.wait(self._reconnect)
                continue
            try:
                self._capture_one()
            except Exception as exc:
                self._note_error(f"capture failed: {exc}")
                camera, self._camera = self._camera, None
                if camera is not None:
                    self._close_camera(camera)
                self._stop.wait(self._reconnect)

    def _capture_one(self) -> None:
        request = self._camera.capture_request()     # blocks for the next frame
        try:
            main = np.ascontiguousarray(request.make_array("main"))
            lores = request.make_array("lores")
            metadata = dict(request.get_metadata() or {})
        finally:
            request.release()
        # The raw lores array is kept; it is converted (YUV420 -> BGR on a Pi 4)
        # only when ``latest_lores`` asks for it, not on every capture.
        arrived = time.monotonic()
        stamp = self._capture_time(metadata, arrived)
        with self._lock:
            self._frame, self._lores, self._metadata = main, lores, metadata
            self._seq += 1
            self._stamp = stamp
            self._last_frame_at = arrived
            self.frames += 1

    def _lores_bgr(self, lores: np.ndarray) -> np.ndarray:
        if self.lores_format == "YUV420":
            if cv2 is None:
                raise RuntimeError("OpenCV is needed to convert the YUV420 lores stream")
            return cv2.cvtColor(lores, cv2.COLOR_YUV2BGR_I420)
        return np.ascontiguousarray(lores)

    @staticmethod
    def _capture_time(metadata: dict[str, Any], arrived: float) -> float:
        stamp_ns = metadata.get("SensorTimestamp")
        if isinstance(stamp_ns, (int, float)) and stamp_ns > 0:
            stamp = stamp_ns / 1e9
            if abs(stamp - arrived) <= _TIMESTAMP_SKEW_SECONDS:
                return stamp
        return arrived

    # ── FrameSource ───────────────────────────────────────────────────────────
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

    def latest_lores(self, after_seq: int = -1) -> tuple[np.ndarray | None, int, float]:
        """The analysis stream, as ``latest()`` (same seq for the same frame)."""
        with self._lock:
            if self._lores is None or self._seq == after_seq:
                return None, self._seq, self._stamp
            raw, seq, stamp = self._lores, self._seq, self._stamp
        converted = self._lores_bgr(raw)
        return (converted.copy() if converted is raw else converted), seq, stamp

    @property
    def online(self) -> bool:
        with self._lock:
            last = self._last_frame_at
        if self._camera is None or last is None:
            return False
        return time.monotonic() - last <= max(2.0, 5.0 / self.fps)

    def metadata(self) -> dict[str, Any]:
        """The newest frame's libcamera metadata (exposure, gain, ``Lux``)."""
        with self._lock:
            return dict(self._metadata)

    def status(self) -> dict[str, Any]:
        meta = self.metadata()
        return {"name": self.name, "online": self.online, "frames": self.frames,
                "errors": self.errors, "last_error": self.last_error,
                "main": list(self.main_size), "lores": list(self.lores_size),
                "lores_format": self.lores_format, "fps": self.fps,
                "rotation": self.applied_rotation, "rotation_requested": self.rotation,
                "exposure_us": meta.get("ExposureTime"), "lux": meta.get("Lux")}

    def release(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=3.0)
        camera, self._camera = self._camera, None
        if camera is not None:
            self._close_camera(camera)


__all__ = ["PiCameraSource", "parse_spec"]
