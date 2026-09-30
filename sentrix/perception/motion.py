"""Frame-difference motion scoring and ambient light estimation.

Also the home of ``is_night``. That feature appears in the TCI formula in every
document and is one of the five inputs the shipped booster expects, but nothing
in the previous codebase ever produced it: ``scores.get("is_night", False)``
always returned the default, so one of five model inputs was a hard-wired zero.
It needs no hardware — mean frame luminance is enough.
"""

from __future__ import annotations

import numpy as np

from sentrix.utils.logging import get_logger

log = get_logger("motion")

try:
    import cv2
except ImportError:                                    # pragma: no cover
    cv2 = None                                         # type: ignore[assignment]

#: Mean V-channel value below which the scene counts as night / low light.
_NIGHT_LUMA = 60.0
#: Exponential smoothing on the luminance estimate, so a passing headlight or a
#: camera auto-exposure hunt does not flip the night flag.
_LUMA_ALPHA = 0.05


class MotionEngine:
    """Gaussian-blurred absolute frame difference, normalised to ``[0, 1]``.

    Blur radius is derived from frame size rather than fixed at 21 px, so the
    score means the same thing on a 480p webcam and a 1080p camera.
    """

    def __init__(self, gain: float = 10.0) -> None:
        self._prev: np.ndarray | None = None
        self._gain = gain
        self._luma: float | None = None

    def reset(self) -> None:
        self._prev = None
        self._luma = None

    @staticmethod
    def _odd(value: int) -> int:
        value = max(3, value)
        return value if value % 2 else value + 1

    def score(self, frame: np.ndarray) -> float:
        if cv2 is None or frame is None or frame.size == 0:
            return 0.0
        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            k = self._odd(int(min(gray.shape[:2]) * 0.04))
            gray = cv2.GaussianBlur(gray, (k, k), 0)
            if self._prev is None or self._prev.shape != gray.shape:
                self._prev = gray
                return 0.0
            diff = cv2.absdiff(self._prev, gray)
            self._prev = gray
            return float(min(float(np.mean(diff)) / 255.0 * self._gain, 1.0))
        except Exception as exc:
            log.debug("motion scoring failed: %s", exc)
            return 0.0

    def luminance(self, frame: np.ndarray) -> float:
        """Smoothed mean luminance in ``[0, 255]``."""
        if cv2 is None or frame is None or frame.size == 0:
            return self._luma if self._luma is not None else 128.0
        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            value = float(np.mean(gray))
        except Exception:
            return self._luma if self._luma is not None else 128.0
        if self._luma is None:
            self._luma = value
        else:
            self._luma += _LUMA_ALPHA * (value - self._luma)
        return self._luma

    def is_night(self, frame: np.ndarray) -> bool:
        """True when the scene is dark enough to count as night / low light."""
        return self.luminance(frame) < _NIGHT_LUMA


__all__ = ["MotionEngine"]
