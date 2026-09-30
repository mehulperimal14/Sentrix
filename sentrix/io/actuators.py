"""Actuators — the hardware seam on the output side.

The previous ``hardware/siren.py`` contained no GPIO code at all. It was a
desktop beep player (``winsound.Beep`` / ``afplay`` / ``paplay``), while
``docs/3`` section 5 presented a ``GPIO.output(GPIO_SIREN_PIN, GPIO.HIGH)``
snippet captioned as "hardware/siren.py (simplified)" and ``docs/5`` listed the
GPIO relay as operational. Its ``sound_path`` also pointed at a pre-restructure
directory that no longer existed, so even the beep fell through to a fallback
tone.

Here the interface is explicit and the implementations are honest about what
they do. ``build_siren()`` selects the best available backend and logs which one
it chose, so the running system reports its real capability rather than the
documented one.

Wiring, for when the relay arrives (unchanged from ``docs/3``):

    Pin 2  (5V)          -> relay VCC
    Pin 11 (GPIO17/BCM17)-> relay IN
    Pin 14 (GND)         -> relay GND

``active_low`` matters: most cheap optocoupler relay boards are active-low, so
driving the pin HIGH *releases* the relay. Getting this backwards means a siren
that runs continuously until level 5, then stops.
"""

from __future__ import annotations

import os
import platform
import subprocess
import threading
from abc import ABC, abstractmethod

from sentrix.config import settings
from sentrix.paths import STATIC_DIR
from sentrix.utils.logging import get_logger

log = get_logger("actuators")


class Actuator(ABC):
    """Something the system can physically or audibly trigger."""

    name: str = "actuator"

    @abstractmethod
    def activate(self, duration: float | None = None) -> None:
        ...

    def deactivate(self) -> None:
        pass

    @property
    def is_real(self) -> bool:
        """False for stubs. Surfaced in ``/api/health`` so the dashboard cannot
        claim a deterrent that does not exist."""
        return False


class ConsoleSiren(Actuator):
    """Logs only. The correct default when no audio device or GPIO exists."""

    name = "console"

    def activate(self, duration: float | None = None) -> None:
        log.warning("*** SIREN (console stub) — no audio or GPIO backend ***")


class SoundSiren(Actuator):
    """Plays ``static/sounds/siren.wav`` through the host audio device."""

    name = "sound"

    def __init__(self) -> None:
        self.sound_path = STATIC_DIR / "sounds" / "siren.wav"
        if not self.sound_path.exists():
            log.warning("siren.wav missing at %s — falling back to a tone",
                        self.sound_path)

    @property
    def is_real(self) -> bool:
        return True

    def activate(self, duration: float | None = None) -> None:
        duration = duration or settings.alert.siren_duration_seconds
        threading.Thread(target=self._play, args=(duration,), daemon=True).start()

    def _play(self, duration: float) -> None:
        log.warning("*** LOCAL SIREN ACTIVATED (%.1fs) ***", duration)
        system = platform.system()
        path = str(self.sound_path)
        exists = self.sound_path.exists()
        try:
            if system == "Windows":
                import winsound
                if exists:
                    winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
                else:
                    winsound.Beep(1000, int(duration * 1000))
            elif system == "Darwin":
                if exists:
                    subprocess.Popen(["afplay", path])
            else:
                for player in ("paplay", "aplay"):
                    if exists:
                        try:
                            subprocess.Popen([player, path])
                            break
                        except FileNotFoundError:
                            continue
        except Exception as exc:
            log.debug("siren playback failed: %s", exc)


class GpioSiren(Actuator):
    """Drives a relay on a Raspberry Pi or Orange Pi. Inert elsewhere.

    Not currently exercised — no hardware is attached — but it is written and
    selected automatically the moment a GPIO library is importable, so bringing
    up the relay is a wiring job rather than a coding one.
    """

    name = "gpio"

    def __init__(self, pin: int = 17, active_low: bool = True) -> None:
        self.pin = pin
        self.active_low = active_low
        self._gpio = None
        self._timer: threading.Timer | None = None
        try:
            import RPi.GPIO as GPIO
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            GPIO.setup(self.pin, GPIO.OUT,
                       initial=GPIO.HIGH if active_low else GPIO.LOW)
            self._gpio = GPIO
            log.info("GPIO siren ready on BCM%d (active_%s)",
                     pin, "low" if active_low else "high")
        except Exception as exc:
            log.debug("GPIO unavailable: %s", exc)

    @property
    def is_real(self) -> bool:
        return self._gpio is not None

    def activate(self, duration: float | None = None) -> None:
        if self._gpio is None:
            return
        duration = duration or settings.alert.siren_duration_seconds
        log.warning("*** GPIO SIREN ON (BCM%d, %.1fs) ***", self.pin, duration)
        self._gpio.output(self.pin,
                          self._gpio.LOW if self.active_low else self._gpio.HIGH)
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(duration, self.deactivate)
        self._timer.daemon = True
        self._timer.start()

    def deactivate(self) -> None:
        if self._gpio is None:
            return
        self._gpio.output(self.pin,
                          self._gpio.HIGH if self.active_low else self._gpio.LOW)


def build_siren() -> Actuator:
    """Pick the strongest available siren backend.

    ``SENTRIX_SIREN`` forces a specific one: ``gpio``, ``sound`` or ``console``.
    """
    forced = os.getenv("SENTRIX_SIREN", "").strip().lower()
    if forced == "console":
        return ConsoleSiren()
    if forced in ("gpio", ""):
        gpio = GpioSiren()
        if gpio.is_real:
            return gpio
        if forced == "gpio":
            log.warning("SENTRIX_SIREN=gpio requested but no GPIO available")
    if forced in ("sound", ""):
        try:
            return SoundSiren()
        except Exception:
            pass
    return ConsoleSiren()


__all__ = ["Actuator", "ConsoleSiren", "SoundSiren", "GpioSiren", "build_siren"]
