"""Hardware tunables: ``settings.gpio`` / ``settings.node`` fields with a
call-time environment override.

The fields live in ``sentrix/config.py`` (read once at import, like every
setting). Each accessor here first reads the environment variable at call
time, so a value set after import (a test, the doctor, a supervisor that
re-reads ``pi.env``) still applies, then the field, then the default.

Defaults and why:

* ``siren_max_on_seconds`` 90 — one on-period of the relay never exceeds
  this, whatever asked for it (the roadmap's hearing note: at 110 dB the safe
  daily dose is about 90 s at the rated distance). Alarms use 5 s.
* ``siren_arm_pin`` none — the key switch on the siren's 12 V line can carry a
  second contact to a GPIO (closed to GND = armed). Without it,
  ``siren_armed`` (default on) states the switch position for the software.
* ``tamper_pin`` none — an enclosure lid switch (closed to GND = lid shut).
* ``buzzer_enabled`` on — the local chirps for L2/L3 (docs/8_RASPBERRY_PI5_INTEGRATION.md §6).
* ``inputs`` pir, door, mains — the inputs wired; an unwired one would read a
  fixed level (door "open", mains "lost") and is left out instead.
* ``bme280`` / ``dht22`` off — room sensors through the kernel IIO drivers.
* ``picamera_main`` 1280x720, ``picamera_lores`` 640x480 — the Picamera2
  streams (docs/8_RASPBERRY_PI5_INTEGRATION.md §4: evidence/preview and analysis).
"""

from __future__ import annotations

import os
from typing import Any

_MISSING = object()


def _field(section: str, name: str, env: str | None = None) -> Any:
    """The environment override when set, else the config field."""
    if env and _env(env):
        return _MISSING
    from sentrix.config import settings
    return getattr(getattr(settings, section, None), name, _MISSING)


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _bool(section: str, field: str, env: str, default: bool) -> bool:
    value = _field(section, field, env)
    if value is not _MISSING:
        return bool(value)
    raw = _env(env).lower()
    return default if not raw else raw in ("1", "true", "yes", "on")


def _float(section: str, field: str, env: str, default: float) -> float:
    value = _field(section, field, env)
    if value is not _MISSING:
        return float(value)
    try:
        return float(_env(env) or default)
    except ValueError:
        return default


def _optional_pin(section: str, field: str, env: str) -> int | None:
    value = _field(section, field, env)
    if value is not _MISSING:
        return None if value in (None, "") else int(value)
    raw = _env(env)
    if not raw:
        return None
    try:
        pin = int(raw)
    except ValueError:
        return None
    return pin if 0 <= pin <= 27 else None


def _size(section: str, field: str, env: str, default: tuple[int, int]) -> tuple[int, int]:
    value = _field(section, field, env)
    if value is not _MISSING:
        return parse_size(value, default)
    return parse_size(_env(env), default)


def parse_size(value: Any, default: tuple[int, int]) -> tuple[int, int]:
    """``"1280x720"`` or ``(1280, 720)`` -> ``(1280, 720)``; anything else ->
    ``default``. Sizes are rounded down to even numbers (YUV420 needs them)."""
    if isinstance(value, (tuple, list)) and len(value) == 2:
        width, height = value
    else:
        parts = str(value or "").lower().replace("*", "x").split("x")
        if len(parts) != 2:
            return default
        width, height = parts
    try:
        width, height = int(width), int(height)
    except (TypeError, ValueError):
        return default
    if not (16 <= width <= 4608 and 16 <= height <= 3456):
        return default
    return width - width % 2, height - height % 2


def siren_max_on_seconds() -> float:
    return max(1.0, _float("gpio", "siren_max_on_seconds", "SENTRIX_SIREN_MAX_ON_SECONDS", 90.0))


def siren_arm_pin() -> int | None:
    return _optional_pin("gpio", "siren_arm_pin", "SENTRIX_SIREN_ARM_PIN")


def siren_armed_flag() -> bool:
    return _bool("gpio", "siren_armed", "SENTRIX_SIREN_ARMED", True)


def tamper_pin() -> int | None:
    return _optional_pin("gpio", "tamper_pin", "SENTRIX_TAMPER_PIN")


def buzzer_enabled() -> bool:
    return _bool("gpio", "buzzer_enabled", "SENTRIX_BUZZER_ENABLED", True)


INPUT_NAMES = ("pir", "door", "mains")


def fitted_inputs() -> frozenset[str]:
    """``SENTRIX_GPIO_INPUTS`` / ``settings.gpio.inputs``: the wired inputs of
    pir, door, mains ("none" or "" = none); unknown names are ignored."""
    value = _field("gpio", "inputs", "SENTRIX_GPIO_INPUTS")
    if value is _MISSING:
        value = _env("SENTRIX_GPIO_INPUTS") or ",".join(INPUT_NAMES)
    names = {part.strip().lower() for part in str(value or "").replace(";", ",").split(",")}
    return frozenset(names & set(INPUT_NAMES))


def bme280_enabled() -> bool:
    return _bool("node", "bme280", "SENTRIX_SENSOR_BME280", False)


def dht22_enabled() -> bool:
    return _bool("node", "dht22", "SENTRIX_SENSOR_DHT22", False)


def picamera_main_size() -> tuple[int, int]:
    return _size("node", "picamera_main", "SENTRIX_PICAMERA_MAIN", (1280, 720))


def picamera_lores_size() -> tuple[int, int]:
    return _size("node", "picamera_lores", "SENTRIX_PICAMERA_LORES", (640, 480))


__all__ = ["parse_size", "siren_max_on_seconds", "siren_arm_pin", "siren_armed_flag",
           "tamper_pin", "buzzer_enabled", "bme280_enabled", "dht22_enabled",
           "picamera_main_size", "picamera_lores_size"]
