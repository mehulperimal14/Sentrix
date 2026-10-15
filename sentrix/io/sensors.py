"""Board telemetry and optional room sensors (docs/8_RASPBERRY_PI5_INTEGRATION.md §3, §9; docs/7 §3.4).

What the Node page, the link heartbeat and ``tools/doctor.py`` report about
the unit itself:

* **CPU temperature** from ``/sys/class/thermal/thermal_zone0/temp`` (any
  Linux), else ``vcgencmd measure_temp``. Uncooled, a Pi 5 sits above 85 °C
  and throttles continuously under four-core load (docs/8_RASPBERRY_PI5_INTEGRATION.md §2).
* **Throttling** from ``vcgencmd get_throttled``: a bit field whose low bits
  are "now" and bits 16–19 "has happened since boot". Under-voltage *now* is
  the one that corrupts SD cards and browns out the modem.
* **EXT5V** (and an estimate of board power) from ``vcgencmd pmic_read_adc``
  on a Pi 5: the roadmap's power pass test is EXT5V ≥ 4.8 V with the siren
  pulsing and the modem sending.
* **BME280 / DHT22**, each behind its own flag, read through the kernel's IIO
  drivers (``dtoverlay=i2c-sensor,bme280`` / ``dtoverlay=dht11,gpiopin=4``;
  the dht11 driver also serves the DHT22). No Python sensor library: the
  kernel does the timing-critical part, which a Python bit-banged DHT22 read
  gets wrong under load. Readings are telemetry only — not in the feature
  contract until there is data to fit them on (docs/3 §7).

Everything degrades to ``None`` off a Pi (no vcgencmd, no IIO device): a
laptop reports what it can and never raises. Filesystem roots and the
vcgencmd runner are injectable, so tests run against a fake tree and a fake
``vcgencmd``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from sentrix.utils.logging import get_logger

log = get_logger("sensors")

#: ``vcgencmd get_throttled`` bits (Raspberry Pi documentation, "vcgencmd").
THROTTLE_BITS: dict[int, str] = {
    0: "under_voltage",
    1: "freq_capped",
    2: "throttled",
    3: "soft_temp_limit",
    16: "under_voltage_occurred",
    17: "freq_capped_occurred",
    18: "throttled_occurred",
    19: "soft_temp_limit_occurred",
}
#: The "now" bits that mean the CPU is running below its rated clock.
_LIMITED_NOW = ("freq_capped", "throttled", "soft_temp_limit")

#: Where the device tree names the board (either path works on Raspberry Pi OS).
_MODEL_FILES = ("proc/device-tree/model", "sys/firmware/devicetree/base/model")


# ── platform ──────────────────────────────────────────────────────────────────
def raspberry_pi_model(root: str | Path = "/") -> str | None:
    """The board name (``"Raspberry Pi 5 Model B Rev 1.0"``), or None when
    this is not a Raspberry Pi (a laptop, a PC, any other SBC)."""
    base = Path(root)
    for relative in _MODEL_FILES:
        try:
            text = (base / relative).read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        model = text.replace("\x00", "").strip()
        if "raspberry pi" in model.lower():
            return model
    return None


def is_raspberry_pi(root: str | Path = "/") -> bool:
    return raspberry_pi_model(root) is not None


def is_raspberry_pi_5(root: str | Path = "/") -> bool:
    model = raspberry_pi_model(root) or ""
    return model.lower().startswith("raspberry pi 5")


# ── vcgencmd ──────────────────────────────────────────────────────────────────
Runner = Callable[[list[str], float], "str | None"]


def _run_command(argv: list[str], timeout: float) -> str | None:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("%s failed: %s", argv[0], exc)
        return None
    if done.returncode != 0:
        log.debug("%s exited %s: %s", " ".join(argv), done.returncode,
                  done.stderr.strip()[:200])
        return None
    return done.stdout


class Vcgencmd:
    """``vcgencmd`` with a timeout; every call returns its stdout or None.

    ``runner(argv, timeout) -> stdout | None`` replaces the subprocess in
    tests. Without a runner the binary is looked up on PATH once; off a Pi
    it is absent and every call is a cheap None.
    """

    def __init__(self, runner: Runner | None = None, *, path: str | None = None,
                 timeout: float = 2.0) -> None:
        self._runner = runner
        self._path = path if path is not None else (None if runner else shutil.which("vcgencmd"))
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return self._runner is not None or self._path is not None

    def __call__(self, *args: str) -> str | None:
        if self._runner is not None:
            return self._runner(["vcgencmd", *args], self.timeout)
        if self._path is None:
            return None
        return _run_command([self._path, *args], self.timeout)


def parse_throttled(text: str | None) -> int | None:
    """``"throttled=0x50005"`` -> 0x50005."""
    if not text:
        return None
    match = re.search(r"throttled=(0x[0-9a-fA-F]+|\d+)", text)
    if match is None:
        return None
    return int(match.group(1), 0)


def decode_throttled(value: int | None) -> dict[str, Any]:
    """Every flag of ``get_throttled`` by name, plus ``flags`` (the names that
    are set), ``limited`` (the CPU is running below its rated clock now) and
    ``raw`` (hex, as vcgencmd prints it)."""
    if value is None:
        return {"raw": None, "flags": [], "limited": None,
                **{name: None for name in THROTTLE_BITS.values()}}
    decoded: dict[str, Any] = {name: bool(value >> bit & 1)
                               for bit, name in THROTTLE_BITS.items()}
    decoded["raw"] = hex(value)
    decoded["flags"] = [name for name in THROTTLE_BITS.values() if decoded[name]]
    decoded["limited"] = any(decoded[name] for name in _LIMITED_NOW)
    return decoded


def parse_measure_temp(text: str | None) -> float | None:
    """``"temp=48.3'C"`` -> 48.3."""
    if not text:
        return None
    match = re.search(r"temp=([-\d.]+)", text)
    return float(match.group(1)) if match else None


_PMIC_LINE = re.compile(r"^\s*(\S+)\s+(?:volt|current)\(\d+\)=([-\d.]+)\s*[VA]\s*$")


def parse_pmic_adc(text: str | None) -> dict[str, float]:
    """``vcgencmd pmic_read_adc`` (Pi 5) -> ``{"EXT5V_V": 5.13, "VDD_CORE_A":
    1.92, ...}``. Lines that do not parse are skipped."""
    readings: dict[str, float] = {}
    for line in (text or "").splitlines():
        match = _PMIC_LINE.match(line)
        if match:
            readings[match.group(1)] = float(match.group(2))
    return readings


def pmic_power_watts(readings: dict[str, float]) -> float | None:
    """Sum of volts x amps over the rails the PMIC reports both for
    (``<rail>_V`` and ``<rail>_A``). An estimate of the board's own draw,
    without USB devices; None when no rail pairs up."""
    total, paired = 0.0, 0
    for name, volts in readings.items():
        if not name.endswith("_V"):
            continue
        amps = readings.get(name[:-2] + "_A")
        if amps is not None:
            total += volts * amps
            paired += 1
    return round(total, 2) if paired else None


# ── sysfs / procfs ────────────────────────────────────────────────────────────
def read_cpu_temp(root: str | Path = "/", vcgencmd: Vcgencmd | None = None) -> float | None:
    """CPU temperature in °C: the thermal zone first (no subprocess), then
    ``vcgencmd measure_temp``."""
    zone = Path(root) / "sys/class/thermal/thermal_zone0/temp"
    try:
        return round(int(zone.read_text().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        pass
    if vcgencmd is not None:
        return parse_measure_temp(vcgencmd("measure_temp"))
    return None


def read_uptime(root: str | Path = "/") -> float | None:
    """System uptime in seconds (``/proc/uptime``), or None."""
    try:
        return round(float((Path(root) / "proc/uptime").read_text().split()[0]), 1)
    except (OSError, ValueError, IndexError):
        return None


# ── room sensors through the kernel's IIO drivers ─────────────────────────────
#: Driver names in ``/sys/bus/iio/devices/iio:deviceN/name``.
IIO_DRIVERS: dict[str, tuple[str, ...]] = {
    "bme280": ("bme280", "bmp280"),
    "dht22": ("dht11", "dht22"),
}


class IioEnvironmentSensor:
    """Temperature (and humidity, pressure) from a kernel IIO device.

    Units in sysfs: ``in_temp_input`` milli-°C, ``in_humidityrelative_input``
    milli-%RH, ``in_pressure_input`` kPa (bmp280 driver). A DHT22 read fails
    now and then (EIO: the sensor answers at most every 2 s); a failed read
    returns None for that interval and the next one tries again.
    """

    def __init__(self, kind: str, root: str | Path = "/") -> None:
        if kind not in IIO_DRIVERS:
            raise ValueError(f"unknown IIO sensor kind {kind!r}")
        self.kind = kind
        self._base = Path(root) / "sys/bus/iio/devices"
        self._device: Path | None = None

    def find(self) -> Path | None:
        if self._device is not None and self._device.exists():
            return self._device
        self._device = None
        try:
            candidates = sorted(self._base.iterdir())
        except OSError:
            return None
        for device in candidates:
            try:
                name = (device / "name").read_text().strip().lower()
            except OSError:
                continue
            if name in IIO_DRIVERS[self.kind]:
                self._device = device
                return device
        return None

    @property
    def present(self) -> bool:
        return self.find() is not None

    @staticmethod
    def _value(path: Path, scale: float) -> float | None:
        try:
            return float(path.read_text().strip()) * scale
        except (OSError, ValueError):
            return None

    def read(self) -> dict[str, Any] | None:
        device = self.find()
        if device is None:
            return None
        temp = self._value(device / "in_temp_input", 0.001)
        humidity = self._value(device / "in_humidityrelative_input", 0.001)
        pressure = self._value(device / "in_pressure_input", 10.0)      # kPa -> hPa
        if temp is None and humidity is None and pressure is None:
            return None
        return {"sensor": self.kind,
                "temperature_c": None if temp is None else round(temp, 1),
                "humidity_pct": None if humidity is None else round(humidity, 1),
                "pressure_hpa": None if pressure is None else round(pressure, 1)}


# ── one reading of everything ─────────────────────────────────────────────────
class Telemetry:
    """Reads the board once per call; every field None when unavailable.

    ``sensors`` are the enabled room sensors (``IioEnvironmentSensor``); the
    first one that answers fills ``environment``.
    """

    def __init__(self, *, root: str | Path = "/", vcgencmd: Vcgencmd | None = None,
                 sensors: list[IioEnvironmentSensor] | None = None) -> None:
        self.root = Path(root)
        self.vcgencmd = vcgencmd if vcgencmd is not None else Vcgencmd()
        self.sensors = list(sensors or [])
        self.model = raspberry_pi_model(self.root)

    def read(self) -> dict[str, Any]:
        throttled = decode_throttled(parse_throttled(self.vcgencmd("get_throttled")))
        pmic = parse_pmic_adc(self.vcgencmd("pmic_read_adc")) if self.vcgencmd.available else {}
        ext5v = pmic.get("EXT5V_V")
        environment = None
        for sensor in self.sensors:
            environment = sensor.read()
            if environment is not None:
                break
        return {
            "cpu_temp_c": read_cpu_temp(self.root, self.vcgencmd),
            "throttled": throttled["limited"],
            "throttled_raw": throttled["raw"],
            "throttle_flags": throttled["flags"],
            "under_voltage": throttled["under_voltage"],
            "under_voltage_occurred": throttled["under_voltage_occurred"],
            "ext5v_v": None if ext5v is None else round(ext5v, 3),
            "power_w": pmic_power_watts(pmic),
            "environment": environment,
            "uptime_s": read_uptime(self.root),
            "read_at": time.time(),
        }


def environment_sensors(root: str | Path = "/", *, bme280: bool = False,
                        dht22: bool = False) -> list[IioEnvironmentSensor]:
    """The enabled room sensors, BME280 first (it also gives pressure)."""
    sensors = []
    if bme280:
        sensors.append(IioEnvironmentSensor("bme280", root))
    if dht22:
        sensors.append(IioEnvironmentSensor("dht22", root))
    return sensors


__all__ = ["THROTTLE_BITS", "IIO_DRIVERS", "Vcgencmd", "Telemetry", "IioEnvironmentSensor",
           "raspberry_pi_model", "is_raspberry_pi", "is_raspberry_pi_5", "parse_throttled",
           "decode_throttled", "parse_measure_temp", "parse_pmic_adc", "pmic_power_watts",
           "read_cpu_temp", "read_uptime", "environment_sensors"]
