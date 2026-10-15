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

The relay is driven through gpiozero (lgpio on the Pi 5). The first GPIO
siren imported ``RPi.GPIO``, which does not work on a Pi 5: it would have
fallen back to a console stub with only a debug log line (docs/8_RASPBERRY_PI5_INTEGRATION.md §3).

Wiring (roadmap v2; ``settings.gpio``):

    GPIO17 (pin 11) -> 1 kΩ -> NPN base (4.7 kΩ base pull-down to GND)
    relay coil between 5 V and the collector, 1N4007 across the coil
    siren on the relay's NO contact, from its own fused 12 V supply

With that NPN driver the relay is **active-high** (``SENTRIX_SIREN_ACTIVE_HIGH
=1``, the default) and ``gpio=17=op,dl`` in ``config.txt`` holds it off from
power-on until this process claims the pin. An optocoupler relay board that
switches on a *low* input is active-low: set ``SENTRIX_SIREN_ACTIVE_HIGH=0``
and ``gpio=17=op,dh``. Getting this backwards means a siren that sounds from
boot until level 5, then stops — prove the polarity with an LED in place of
the siren first (``deploy/pi5/README.md``).

Safety properties of :class:`GpioSiren`: the pin starts OFF; every
activation is released by a timer; one on-period never exceeds
``siren_max_on_seconds`` (90 s) however often it is extended; the relay is
switched off when the process exits normally, and the systemd unit's
``ExecStopPost`` drives the pin low even after ``kill -9``.

Siren arming: a key switch on the siren's 12 V line can report its position
on a GPIO (``SENTRIX_SIREN_ARM_PIN``; the gpio service registers it with
:func:`set_arming_source`); without that input, ``SENTRIX_SIREN_ARMED``
(default on) states it. A disarmed siren still gets its relay driven on a
real alarm — the key switch is what silences the line — but a *test* is
refused, so "siren tested" is never reported while nothing could sound.

Frozen interface (docs/7 §2.2): ``build_siren() -> Actuator``;
``Actuator.activate(duration=None)``, ``.deactivate()``, ``.name``,
``.is_real``; and :func:`siren_test`, which ``POST /api/node/siren-test`` and
the link's ``siren.test`` command both call so they share one limiter.
"""

from __future__ import annotations

import atexit
import os
import platform
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Callable

from sentrix.config import settings
from sentrix.io import tunables
from sentrix.paths import STATIC_DIR
from sentrix.utils.logging import get_logger

log = get_logger("actuators")


# ── arming ────────────────────────────────────────────────────────────────────
_arming_lock = threading.Lock()
_arming_source: Callable[[], "bool | None"] | None = None


def set_arming_source(source: Callable[[], "bool | None"]) -> None:
    """Register the arming key's reader (the gpio service's input). It
    returns True/False, or None when it cannot tell (then the flag decides)."""
    global _arming_source
    with _arming_lock:
        _arming_source = source


def clear_arming_source(source: Callable[[], "bool | None"] | None = None) -> None:
    """Forget the reader (only ``source``, when given)."""
    global _arming_source
    with _arming_lock:
        if source is None or _arming_source == source:
            _arming_source = None


def siren_arming() -> dict[str, Any]:
    """``{"armed": bool, "source": "input" | "flag"}``."""
    with _arming_lock:
        source = _arming_source
    if source is not None:
        try:
            value = source()
        except Exception as exc:
            log.error("siren arming input failed: %s", exc)
            value = None
        if value is not None:
            return {"armed": bool(value), "source": "input"}
    return {"armed": tunables.siren_armed_flag(), "source": "flag"}


def siren_armed() -> bool:
    return siren_arming()["armed"]


# ── backends ──────────────────────────────────────────────────────────────────
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

    @property
    def armed(self) -> bool:
        """The siren line's arming state (see the module docstring)."""
        return siren_armed()

    def status(self) -> dict[str, Any]:
        """For the Node page and the ``gpio`` state block."""
        return {"backend": self.name, "real": self.is_real, "armed": self.armed,
                "on": False}


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
        # The microphone must not hear its own siren as the room (D15).
        from sentrix.io import self_output
        self_output.mark("siren", float(duration))
        threading.Thread(target=self._play, args=(duration,), daemon=True).start()

    def _play(self, duration: float) -> None:
        """Play the file and stop it after ``duration`` (siren.wav is ~6 s;
        the siren test is 1 s, the L5 alarm 5 s)."""
        log.warning("*** LOCAL SIREN ACTIVATED (%.1fs) ***", duration)
        system = platform.system()
        path = str(self.sound_path)
        exists = self.sound_path.exists()
        stop: Callable[[], None] | None = None
        try:
            if system == "Windows":
                import winsound
                if exists:
                    winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
                    stop = lambda: winsound.PlaySound(None, 0)  # noqa: E731
                else:
                    winsound.Beep(1000, int(duration * 1000))
            elif system == "Darwin":
                if exists:
                    stop = subprocess.Popen(["afplay", path]).terminate
            else:
                for player in ("paplay", "aplay"):
                    if exists:
                        try:
                            stop = subprocess.Popen([player, path]).terminate
                            break
                        except FileNotFoundError:
                            continue
            if stop is not None:
                time.sleep(max(0.0, float(duration)))
                stop()
        except Exception as exc:
            log.debug("siren playback failed: %s", exc)


class GpioSiren(Actuator):
    """The siren relay on a gpiozero ``OutputDevice`` (lgpio on the Pi 5).

    ``active_low`` overrides ``settings.gpio.siren_active_high`` (opto relay
    boards); ``pin_factory`` overrides the process-wide factory (tests pass a
    ``MockFactory``). On mock pins the relay logic still runs — the Node page
    shows its state — but ``is_real`` is False, so nothing claims a deterrent.
    """

    name = "gpio"

    def __init__(self, pin: int | None = None, *, active_low: bool | None = None,
                 pin_factory: Any = None, max_on_seconds: float | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        cfg = settings.gpio
        self.pin = cfg.siren_pin if pin is None else int(pin)
        self.active_low = (not cfg.siren_active_high) if active_low is None else bool(active_low)
        self.max_on_seconds = (tunables.siren_max_on_seconds() if max_on_seconds is None
                               else max(0.1, float(max_on_seconds)))
        self._clock = clock
        self._lock = threading.RLock()
        self._device: Any = None
        self._timer: threading.Timer | None = None
        self._on_since: float | None = None
        self._release_at: float | None = None
        self.last_on_at: float | None = None
        self.activations = 0
        self.pin_factory_kind = "none"
        self._real = False
        self.error: str | None = None
        try:
            from gpiozero import OutputDevice
            from sentrix.io import gpio as gpio_io
            if pin_factory is None:
                info = gpio_io.shared_pin_factory()
                factory, kind, real, self.error = info.factory, info.kind, info.real, info.error
            else:
                factory, kind = pin_factory, gpio_io._kind_of(pin_factory)
                real = kind != "mock"
            # initial_value=False: the pin is driven to OFF the moment it is
            # claimed, whatever the polarity.
            self._device = OutputDevice(self.pin, active_high=not self.active_low,
                                        initial_value=False, pin_factory=factory)
            self.pin_factory_kind, self._real = kind, real
            log.info("GPIO siren on GPIO%d (active-%s, %s pins, max on %.0f s)", self.pin,
                     "low" if self.active_low else "high", kind, self.max_on_seconds)
            atexit.register(self._off_at_exit)
        except Exception as exc:
            self.error = f"GPIO{self.pin}: {exc}"
            log.warning("GPIO siren unavailable: %s", self.error)

    @property
    def available(self) -> bool:
        """A pin is claimed (real or mock)."""
        return self._device is not None

    @property
    def is_real(self) -> bool:
        return self._device is not None and self._real

    @property
    def is_on(self) -> bool:
        with self._lock:
            return self._device is not None and bool(self._device.value)

    def activate(self, duration: float | None = None) -> None:
        """Relay on for ``duration`` (the alarm's 5 s by default). A call while
        it is on extends the period, never past ``max_on_seconds`` from when it
        switched on."""
        if self._device is None:
            return
        duration = float(duration or settings.alert.siren_duration_seconds)
        with self._lock:
            now = self._clock()
            if self._on_since is None:
                self._on_since = now
            limit = self._on_since + self.max_on_seconds
            release_at = min(max(now + duration, self._release_at or 0.0), limit)
            if release_at <= now:
                log.warning("siren held off: already on for its maximum %.0f s",
                            self.max_on_seconds)
                self._off()
                return
            self._device.on()
            from sentrix.io import self_output
            self_output.mark("siren", release_at - now)
            self.last_on_at = time.time()
            self.activations += 1
            self._release_at = release_at
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(release_at - now, self.release_if_overdue)
            self._timer.daemon = True
            self._timer.start()
        log.warning("*** GPIO SIREN ON (GPIO%d, %.1fs%s) ***", self.pin, release_at - now,
                    "" if self._real else ", mock pins: no relay")

    def deactivate(self) -> None:
        if self._device is None:
            return
        with self._lock:
            self._off()

    def release_if_overdue(self) -> bool:
        """Switch off when the period has run out; True if it did. The
        release timer calls it, and the gpio service's loop calls it too, as a
        second line should the timer thread ever die."""
        with self._lock:
            if self._release_at is None or self._clock() < self._release_at:
                return False
            self._off()
        log.info("GPIO siren off (GPIO%d)", self.pin)
        return True

    def _off(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._on_since = None
        self._release_at = None
        try:
            self._device.off()
        except Exception as exc:
            log.error("GPIO siren off failed: %s", exc)

    def _off_at_exit(self) -> None:
        try:
            if self._device is not None and not self._device.closed:
                self._device.off()
        except Exception:
            pass

    def close(self) -> None:
        """Off, then release the pin (it floats; the NPN base pull-down keeps
        the relay off)."""
        if self._device is None:
            return
        with self._lock:
            self._off()
            try:
                self._device.close()
            except Exception:
                pass
            self._device = None
        atexit.unregister(self._off_at_exit)

    def status(self) -> dict[str, Any]:
        with self._lock:
            remaining = (None if self._release_at is None
                         else round(max(0.0, self._release_at - self._clock()), 1))
            on = self._device is not None and bool(self._device.value)
        return {"backend": self.name, "real": self.is_real, "armed": self.armed,
                "on": on, "remaining_s": remaining, "pin": self.pin,
                "active_high": not self.active_low, "pin_factory": self.pin_factory_kind,
                "max_on_seconds": self.max_on_seconds, "activations": self.activations,
                "last_on_at": self.last_on_at, "error": self.error}


def build_siren() -> Actuator:
    """Pick the strongest available siren backend.

    ``SENTRIX_SIREN`` forces a specific one: ``gpio``, ``sound`` or
    ``console``; ``auto`` (or unset) picks a real GPIO relay, then the sound
    file, then the console. Forcing ``gpio`` off a Pi gives the relay logic on
    mock pins (``is_real`` False), so the Node page can be exercised on a
    laptop.
    """
    forced = os.getenv("SENTRIX_SIREN", "").strip().lower()
    if forced == "auto":
        forced = ""
    if forced == "console":
        return ConsoleSiren()
    if forced == "gpio" or (forced == "" and _real_gpio_possible()):
        siren = GpioSiren()
        if siren.is_real or (forced == "gpio" and siren.available):
            if not siren.is_real:
                log.warning("SENTRIX_SIREN=gpio on mock pins: no relay is connected")
            return siren
        siren.close()
        if forced == "gpio":
            log.warning("SENTRIX_SIREN=gpio requested but no GPIO available")
    if forced in ("sound", ""):
        try:
            return SoundSiren()
        except Exception:
            pass
    return ConsoleSiren()


def _real_gpio_possible() -> bool:
    """Only try to claim the relay pin where a real factory resolves."""
    try:
        from sentrix.io import gpio as gpio_io
        return gpio_io.shared_pin_factory().real
    except Exception as exc:
        log.debug("GPIO probe failed: %s", exc)
        return False


# ── the shared test ───────────────────────────────────────────────────────────
_test_lock = threading.Lock()
_last_test: float | None = None


def siren_test_retry_after() -> float | None:
    """Seconds until the next test is allowed, or None."""
    cfg = settings.link
    with _test_lock:
        if _last_test is None:
            return None
        wait = cfg.siren_test_min_interval_seconds - (time.monotonic() - _last_test)
    return round(wait, 1) if wait > 0 else None


def siren_test(siren: Actuator, *, actor) -> dict:
    """Sound ``siren`` for ``settings.link.siren_test_seconds`` (1 s), at most
    once per ``siren_test_min_interval_seconds`` (60 s) for the whole process.

    Callers check who may test (device admin, or an owner with a fresh
    step-up: the LC route; the link's command rules). A disarmed siren line
    (arming key off) is refused with ``conflict`` before the limiter, so it
    costs no slot. ``actor`` is a ``sentrix.actor.Actor``; the test is written
    to the live event log as ``siren_test``. Returns ``{ok, code, message,
    retry_after, seconds, backend, is_real, armed}`` with a docs/7 §13 code
    (``rate_limited`` when too soon).
    """
    global _last_test

    cfg = settings.link
    base = {"backend": siren.name, "is_real": siren.is_real}
    arming = siren_arming()
    if not arming["armed"]:
        why = ("the arming key is off" if arming["source"] == "input"
               else "SENTRIX_SIREN_ARMED=0")
        return {"ok": False, "code": "conflict", "retry_after": None,
                "message": f"the siren is disarmed ({why}): nothing would sound",
                "seconds": 0.0, "armed": False, **base}
    now = time.monotonic()
    with _test_lock:
        if _last_test is not None and now - _last_test < cfg.siren_test_min_interval_seconds:
            wait = cfg.siren_test_min_interval_seconds - (now - _last_test)
            return {"ok": False, "code": "rate_limited", "retry_after": round(wait, 1),
                    "message": "the siren was tested less than a minute ago",
                    "seconds": 0.0, "armed": True, **base}
        _last_test = now
    try:
        siren.activate(cfg.siren_test_seconds)
    except Exception as exc:
        log.error("siren test failed: %s", exc)
        return {"ok": False, "code": "internal", "retry_after": None,
                "message": f"siren failed: {exc}", "seconds": 0.0, "armed": True, **base}
    engine = sys.modules.get("sentrix.pipeline.engine")
    pipeline = getattr(engine, "_pipeline", None) if engine else None
    if pipeline is not None:
        pipeline.events.audit("siren_test", by=getattr(actor, "ref", str(actor)),
                              backend=siren.name, seconds=cfg.siren_test_seconds)
    log.warning("siren test by %s (%s, %.1fs)", getattr(actor, "ref", actor),
                siren.name, cfg.siren_test_seconds)
    return {"ok": True, "code": "ok", "retry_after": None, "message": "",
            "seconds": cfg.siren_test_seconds, "armed": True, **base}


def _reset_siren_test_limiter() -> None:
    """Tests only."""
    global _last_test
    with _test_lock:
        _last_test = None


__all__ = ["Actuator", "ConsoleSiren", "SoundSiren", "GpioSiren", "build_siren",
           "siren_test", "siren_test_retry_after", "siren_arming", "siren_armed",
           "set_arming_source", "clear_arming_source"]
