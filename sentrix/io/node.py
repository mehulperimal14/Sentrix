"""Device telemetry: the ``node`` state block, node alerts and the watchdog.

The ``node`` service (docs/7 §3.6, flag ``SENTRIX_NODE_ENABLED``) reads the
board every ``settings.node.interval_seconds`` (5 s) — CPU temperature,
throttling, EXT5V and board power, uptime, the optional BME280/DHT22 — and
writes the ``node`` block of the live state, which the Node page, ``GET
/api/node`` and the link heartbeat (``node: {cpu_temp_c, throttled,
under_voltage, mains, ext5v_v}``) read. Crossing into the critical
temperature and a new under-voltage are **node alerts**: an audit row in the
session event log and a ``node.alert`` event for the Portal (docs/7 §5.5).
The gpio service adds the inputs (PIR, door, mains, arming key, tamper) to
the same block through :func:`merge_node_block`; track T-HW is its only
writer.

The same service tells systemd the unit is ready and keeps its watchdog fed
(``sentrix.io.watchdog``) when the unit runs under
``deploy/pi5/systemd/sentrix.service``. It pings only while the event loop
answers and the live loop thread is running, so a hung process is restarted.
Because a ``Type=notify`` unit with ``WatchdogSec`` is killed without those
messages, the factory builds a notify-only service under systemd even with
``SENTRIX_NODE_ENABLED=0``.

:func:`hardware_report` is the doctor's hardware section: every row degrades
to "skipped" on a laptop.
"""

from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sentrix.io import sensors, tunables
from sentrix.io.watchdog import SystemdNotifier
from sentrix.utils.logging import get_logger

log = get_logger("node")

#: config.txt locations (Bookworm/Trixie, then older images).
CONFIG_TXT = ("boot/firmware/config.txt", "boot/config.txt")
#: Below this the roadmap's power pass test fails (EXT5V under full load).
EXT5V_MIN_V = 4.8
#: The Pi 5 PMIC flags under-voltage around here.
EXT5V_BROWNOUT_V = 4.63


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── the node block (single writer: track T-HW) ────────────────────────────────
_block_lock = threading.Lock()


def merge_node_block(state: Any, **fields: Any) -> dict[str, Any]:
    """Merge ``fields`` into ``state.node`` and stamp ``updated_at``."""
    from sentrix.pipeline.state import NODE_IDLE
    with _block_lock:
        current = dict(state.get().get("node") or NODE_IDLE)
        current.update(fields)
        current["updated_at"] = _iso_now()
        state.update(node=current)
        return current


def temperature_state(temp_c: float | None, warn_c: float, critical_c: float) -> str | None:
    if temp_c is None:
        return None
    if temp_c >= critical_c:
        return "critical"
    return "warn" if temp_c >= warn_c else "ok"


# ── liveness for the watchdog ─────────────────────────────────────────────────
def live_loop_alive() -> bool:
    """False only when the app's live loop thread has died while the app was
    not stopping (``sentrix.app.live_loop_alive``). The app module is never
    imported from here (it configures logging and loads .env): no app, alive."""
    app_module = sys.modules.get("sentrix.app")
    probe = getattr(app_module, "live_loop_alive", None)
    if callable(probe):
        return bool(probe())
    thread = getattr(app_module, "_thread", None)
    stopping = getattr(app_module, "_stop", None)
    if not isinstance(thread, threading.Thread):
        return True                     # not started yet, or not the live app
    if stopping is not None and stopping.is_set():
        return True
    return thread.is_alive()


def event_loop_answers(loop: Any, timeout: float) -> bool:
    """The server's event loop runs a callback within ``timeout`` (a handler
    that blocks the loop, or a dead loop, fails). No loop: nothing to check."""
    if loop is None:
        return True
    if loop.is_closed():
        return False
    answered = threading.Event()
    try:
        loop.call_soon_threadsafe(answered.set)
    except RuntimeError:
        return False
    return answered.wait(timeout)


# ── the service ───────────────────────────────────────────────────────────────
class NodeService:
    """Telemetry + watchdog (docs/7 §3.6 service; name ``node``).

    Injectable for tests: ``telemetry`` (anything with ``read() -> dict``),
    ``notifier`` (a ``SystemdNotifier``), ``liveness() -> (ok, reason)``,
    ``audit``/``publish``; ``run_thread=False`` leaves the loop to the caller
    (``poll_once``, ``watchdog_once``).
    """

    name = "node"

    def __init__(self, ctx: Any, *, telemetry: Any = None,
                 notifier: SystemdNotifier | None = None, telemetry_enabled: bool = True,
                 liveness: Callable[[], tuple[bool, str]] | None = None,
                 audit: Callable[..., None] | None = None,
                 publish: Callable[[str, dict], None] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 run_thread: bool = True) -> None:
        from sentrix.io.gpio import default_audit, default_publish
        self.ctx = ctx
        self.cfg = ctx.settings.node
        self.telemetry_enabled = telemetry_enabled
        if telemetry is None and telemetry_enabled:
            telemetry = sensors.Telemetry(sensors=sensors.environment_sensors(
                bme280=tunables.bme280_enabled(), dht22=tunables.dht22_enabled()))
        self.telemetry = telemetry
        self.notifier = notifier if notifier is not None else SystemdNotifier()
        self._liveness = liveness or self._default_liveness
        self._audit = audit or default_audit
        self._publish = publish or default_publish
        self._clock = clock
        self._run_thread = run_thread
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.last: dict[str, Any] = {}
        self.alerts: dict[str, bool] = {"over_temp": False, "under_voltage": False}
        self.reads = 0
        self.read_errors = 0
        self.pings = 0
        self.missed_pings = 0
        self.last_liveness: tuple[bool, str] | None = None
        self._ready_sent = False

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def start(self) -> None:
        self._stop.clear()
        model = getattr(self.telemetry, "model", None) if self.telemetry else None
        merge_node_block(self.ctx.state, profile=self.cfg.profile,
                         platform={"model": model, "raspberry_pi": model is not None},
                         telemetry=self.telemetry_enabled)
        if self._run_thread:
            self._thread = threading.Thread(target=self._run, daemon=True, name="sentrix-node")
            self._thread.start()
        log.info("node service started (telemetry %s, systemd watchdog %s)",
                 "on" if self.telemetry_enabled else "off",
                 f"{self.notifier.watchdog_seconds:.0f} s" if self.notifier.watchdog_seconds
                 else "off")

    def stop(self) -> None:
        if self._stop.is_set() and self._thread is None:
            return
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        self.notifier.stopping()

    def health(self) -> dict[str, Any]:
        with self._lock:
            last = dict(self.last)
        return {"profile": self.cfg.profile, "telemetry": self.telemetry_enabled,
                "reads": self.reads, "read_errors": self.read_errors,
                "cpu_temp_c": last.get("cpu_temp_c"), "temp_state": last.get("temp_state"),
                "throttled": last.get("throttled"), "under_voltage": last.get("under_voltage"),
                "ext5v_v": last.get("ext5v_v"),
                "watchdog": {"systemd": self.notifier.enabled,
                             "interval_s": self.notifier.watchdog_seconds,
                             "pings": self.pings, "missed": self.missed_pings,
                             "last_check": None if self.last_liveness is None
                             else self.last_liveness[1]},
                "alerts": [kind for kind, active in self.alerts.items() if active]}

    # ── telemetry ─────────────────────────────────────────────────────────────
    def poll_once(self) -> dict[str, Any]:
        """Read the board once, update the node block, raise node alerts."""
        if self.telemetry is None:
            return {}
        try:
            reading = dict(self.telemetry.read())
        except Exception as exc:
            self.read_errors += 1
            log.error("telemetry read failed: %s", exc)
            return {}
        self.reads += 1
        reading["temp_state"] = temperature_state(reading.get("cpu_temp_c"),
                                                  self.cfg.temp_warn_c, self.cfg.temp_critical_c)
        reading.pop("read_at", None)
        self._check_alerts(reading)
        with self._lock:
            self.last = reading
        merge_node_block(self.ctx.state, profile=self.cfg.profile, **reading)
        return reading

    def _check_alerts(self, reading: dict[str, Any]) -> None:
        temp, state_now = reading.get("cpu_temp_c"), reading.get("temp_state")
        if state_now == "critical" and not self.alerts["over_temp"]:
            self.alerts["over_temp"] = True
            self._alert("over_temp", {"cpu_temp_c": temp,
                                      "critical_c": self.cfg.temp_critical_c,
                                      "throttle_flags": reading.get("throttle_flags")})
        elif state_now == "ok" and self.alerts["over_temp"]:
            self.alerts["over_temp"] = False        # hysteresis: below the warning line
            log.info("CPU temperature back to normal (%.1f °C)", temp)
        if reading.get("under_voltage") is True and not self.alerts["under_voltage"]:
            self.alerts["under_voltage"] = True
            self._alert("under_voltage", {"ext5v_v": reading.get("ext5v_v"),
                                          "throttled_raw": reading.get("throttled_raw")})
        elif reading.get("under_voltage") is False and self.alerts["under_voltage"]:
            self.alerts["under_voltage"] = False
            log.info("supply voltage back to normal (EXT5V %s V)", reading.get("ext5v_v"))

    def _alert(self, kind: str, detail: dict[str, Any]) -> None:
        log.warning("node alert: %s %s", kind, detail)
        self._audit("node_alert", kind=kind, **detail)
        try:
            self._publish("node.alert", {"kind": kind, "at": _iso_now(), "detail": detail})
        except Exception as exc:
            log.error("node.alert %s not published: %s", kind, exc)

    # ── watchdog ──────────────────────────────────────────────────────────────
    def _default_liveness(self) -> tuple[bool, str]:
        timeout = min(5.0, (self.notifier.ping_interval or 15.0) / 2)
        if not event_loop_answers(self.ctx.extras.get("loop"), timeout):
            return False, "web event loop not answering"
        if not live_loop_alive():
            return False, "live capture loop stopped"
        return True, "ok"

    def watchdog_once(self) -> bool:
        """Ping systemd if the process is alive; True when a ping went out."""
        if not self.notifier.enabled:
            return False
        ok, reason = self._liveness()
        self.last_liveness = (ok, reason)
        if not ok:
            if not self._ready_sent:
                # Still inside the app's start-up (the lifespan holds the loop);
                # systemd's watchdog only runs once READY=1 has been sent.
                log.info("not ready for systemd yet: %s", reason)
                return False
            self.missed_pings += 1
            log.error("watchdog ping withheld: %s (systemd restarts the unit if this "
                      "persists)", reason)
            return False
        if not self._ready_sent:
            self._ready_sent = self.notifier.ready("SENTRIX running")
        if self.notifier.watchdog_seconds is None:
            return False
        sent = self.notifier.watchdog()
        if sent:
            self.pings += 1
        return sent

    def _run(self) -> None:
        interval = max(0.5, float(self.cfg.interval_seconds))
        ping = self.notifier.ping_interval
        next_read = next_ping = self._clock()
        while not self._stop.is_set():
            now = self._clock()
            if self.telemetry_enabled and now >= next_read:
                self.poll_once()
                next_read = now + interval
            if self.notifier.enabled and (now >= next_ping or not self._ready_sent):
                self.watchdog_once()
                next_ping = now + (ping or interval)
            waits = [next_read - self._clock()] if self.telemetry_enabled else []
            if self.notifier.enabled:
                waits.append(next_ping - self._clock())
            self._stop.wait(max(0.05, min(waits) if waits else interval))


def build_service(ctx: Any) -> NodeService | None:
    """The ``node`` service when ``SENTRIX_NODE_ENABLED=1``; a notify-only one
    when systemd runs this process as ``Type=notify`` (it waits for READY=1
    and, with ``WatchdogSec``, kills the unit without pings) even with the
    flag off; otherwise None."""
    notifier = SystemdNotifier()
    if ctx.settings.node.enabled:
        return NodeService(ctx, notifier=notifier)
    if notifier.enabled:
        log.info("SENTRIX_NODE_ENABLED=0 under systemd: running the node service "
                 "for readiness and the watchdog only")
        return NodeService(ctx, notifier=notifier, telemetry_enabled=False)
    return None


def preflight(settings: Any) -> list[dict[str, str]]:
    """Cheap local checks for ``tools/doctor.py`` (files and env only)."""
    rows: list[dict[str, str]] = []
    model = sensors.raspberry_pi_model()
    profile = settings.node.profile
    if model is not None and profile == "laptop":
        rows.append({"status": "warn", "name": "profile",
                     "detail": f"SENTRIX_PROFILE=laptop on {model} (set pi5-clip, "
                               f"pi5-hailo or pi5-node)"})
    else:
        rows.append({"status": "ok", "name": "profile", "detail": profile})
    notifier = SystemdNotifier()
    rows.append({"status": "ok", "name": "systemd watchdog",
                 "detail": f"every {notifier.watchdog_seconds:.0f} s" if notifier.watchdog_seconds
                 else "not under systemd (no watchdog)"})
    for kind, enabled in (("bme280", tunables.bme280_enabled()),
                          ("dht22", tunables.dht22_enabled())):
        if not enabled:
            continue
        present = sensors.IioEnvironmentSensor(kind).present
        rows.append({"status": "ok" if present else "fail", "name": kind,
                     "detail": "IIO device found" if present else
                     "enabled but no IIO device (dtoverlay in config.txt? see deploy/pi5)"})
    if not settings.node.enabled:
        rows.append({"status": "ok", "name": "service",
                     "detail": "off (SENTRIX_NODE_ENABLED=0): no telemetry on the Node page"})
    return rows


# ── doctor: hardware section ──────────────────────────────────────────────────
def _row(status: str, name: str, detail: str) -> dict[str, str]:
    return {"status": status, "name": name, "detail": detail}


def read_config_txt(root: str | Path = "/") -> tuple[Path | None, list[str]]:
    """The active ``config.txt`` and its non-comment lines."""
    for relative in CONFIG_TXT:
        path = Path(root) / relative
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = [line.split("#", 1)[0].strip() for line in text.splitlines()]
        return path, [line for line in lines if line]
    return None, []


def venv_has_system_site_packages(prefix: str | None = None) -> bool | None:
    """None outside a venv."""
    prefix = prefix if prefix is not None else sys.prefix
    if prefix == getattr(sys, "base_prefix", prefix):
        return None
    try:
        text = (Path(prefix) / "pyvenv.cfg").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "include-system-site-packages":
            return value.strip().lower() == "true"
    return False


def _camera_rows(camera_info: Callable[[], list] | None) -> list[dict[str, str]]:
    import importlib.util
    if camera_info is None:
        if importlib.util.find_spec("picamera2") is None:
            return [_row("fail", "picamera2", "not importable: sudo apt install "
                         "python3-picamera2 and a venv made with --system-site-packages")]
        from picamera2 import Picamera2
        camera_info = Picamera2.global_camera_info
    try:
        cameras = list(camera_info() or [])
    except Exception as exc:
        return [_row("fail", "CSI camera", f"libcamera enumeration failed: {exc}")]
    if not cameras:
        return [_row("warn", "CSI camera", "none detected (ribbon cable, Standard-Mini "
                     "cable on a Pi 5, or camera_auto_detect=1)")]
    names = ", ".join(str(c.get("Model", "?")) if isinstance(c, dict) else str(c)
                      for c in cameras)
    return [_row("ok", "CSI camera", f"{len(cameras)} found: {names} (enumerated, not opened)")]


def hardware_report(*, root: str | Path = "/", vcgencmd: sensors.Vcgencmd | None = None,
                    camera_info: Callable[[], list] | None = None,
                    eeprom: Callable[[], str | None] | None = None,
                    network_status: Callable[[], dict] | None = None,
                    settings: Any = None) -> list[dict[str, str]]:
    """The doctor's hardware rows. On anything but a Raspberry Pi it reports
    the platform and stops. Nothing here opens the camera stream or claims a
    GPIO pin (the running service may hold them)."""
    from sentrix.config import settings as live_settings
    settings = settings or live_settings
    model = sensors.raspberry_pi_model(root)
    if model is None:
        return [_row("ok", "platform", "not a Raspberry Pi: hardware checks skipped "
                     "(GPIO runs on mock pins)")]
    rows = [_row("ok", "platform", model)]
    pi5 = model.lower().startswith("raspberry pi 5")

    site = venv_has_system_site_packages()
    if site is not None:
        rows.append(_row("ok" if site else "fail", "venv system site packages",
                         "on (apt picamera2/libcamera/lgpio visible)" if site else
                         "OFF: recreate with python3 -m venv --system-site-packages .venv"))
    rows.extend(_camera_rows(camera_info))
    try:
        import cv2
        major = int(str(cv2.__version__).split(".")[0])
        rows.append(_row("ok" if major < 5 else "warn", "opencv",
                         f"{cv2.__version__}" + ("" if major < 5 else
                                                 " (untested; requirements-pi pins <5)")))
    except Exception as exc:
        rows.append(_row("fail", "opencv", f"not importable: {exc}"))

    vc = vcgencmd if vcgencmd is not None else sensors.Vcgencmd()
    if not vc.available:
        rows.append(_row("warn", "vcgencmd", "not found (raspi-utils): no throttle or "
                         "power readings"))
    else:
        temp = sensors.read_cpu_temp(root, vc)
        cfg = settings.node
        state = temperature_state(temp, cfg.temp_warn_c, cfg.temp_critical_c)
        rows.append(_row({"ok": "ok", "warn": "warn", "critical": "fail"}.get(state, "warn"),
                         "CPU temperature", "unknown" if temp is None else
                         f"{temp:.1f} °C (warn {cfg.temp_warn_c:.0f}, critical "
                         f"{cfg.temp_critical_c:.0f}; Active Cooler fitted?)"))
        throttled = sensors.decode_throttled(sensors.parse_throttled(vc("get_throttled")))
        if throttled["raw"] is None:
            rows.append(_row("warn", "throttling", "get_throttled did not answer"))
        else:
            status = ("fail" if throttled["under_voltage"] or throttled["limited"]
                      else "warn" if throttled["flags"] else "ok")
            rows.append(_row(status, "throttling", f"{throttled['raw']}" + (
                f": {', '.join(throttled['flags'])}" if throttled["flags"] else " (none)")))
        if pi5:
            pmic = sensors.parse_pmic_adc(vc("pmic_read_adc"))
            ext5v = pmic.get("EXT5V_V")
            if ext5v is None:
                rows.append(_row("warn", "EXT5V", "pmic_read_adc gave no EXT5V_V"))
            else:
                power = sensors.pmic_power_watts(pmic)
                status = ("fail" if ext5v < EXT5V_BROWNOUT_V else
                          "warn" if ext5v < EXT5V_MIN_V else "ok")
                rows.append(_row(status, "EXT5V", f"{ext5v:.2f} V (pass >= {EXT5V_MIN_V} V "
                                 f"under full load)" + ("" if power is None else
                                                         f", board ~{power:.1f} W")))

    path, lines = read_config_txt(root)
    if path is None:
        rows.append(_row("warn", "config.txt", "not found under /boot/firmware"))
    else:
        level = "dl" if settings.gpio.siren_active_high else "dh"
        pin = settings.gpio.siren_pin
        want = f"gpio={pin}=op,{level}"
        compact = [line.replace(" ", "") for line in lines]
        rows.append(_row("ok" if want in compact else "warn", "siren boot state",
                         f"{want} present" if want in compact else
                         f"add {want} to {path} (relay held off from power-on)"))
        if pi5:
            rtc = any(line.startswith("dtparam=rtc_bbat_vchg=") for line in compact)
            rows.append(_row("ok" if rtc else "warn", "RTC cell charging",
                             "rtc_bbat_vchg set" if rtc else
                             "dtparam=rtc_bbat_vchg=3000000 missing (ML-2020 cell only)"))
        watchdog = any(line.startswith("kernel_watchdog_timeout=") for line in compact)
        rows.append(_row("ok" if watchdog else "warn", "hardware watchdog",
                         "kernel_watchdog_timeout set" if watchdog else
                         "kernel_watchdog_timeout=10 missing from config.txt"))
    if pi5:
        text = eeprom() if eeprom is not None else _eeprom_config()
        if text is None:
            rows.append(_row("warn", "PSU_MAX_CURRENT", "rpi-eeprom-config not readable"))
        else:
            set_5a = any(line.replace(" ", "") == "PSU_MAX_CURRENT=5000"
                         for line in text.splitlines())
            rows.append(_row("ok" if set_5a else "warn", "PSU_MAX_CURRENT",
                             "5000 (USB 1.6 A with a non-PD supply)" if set_5a else
                             "unset: USB is limited to 600 mA on a non-PD 5 V supply "
                             "(sudo rpi-eeprom-config --edit)"))
    unit = Path(root) / "etc/systemd/system/sentrix.service"
    rows.append(_row("ok" if unit.exists() else "warn", "systemd unit",
                     "installed" if unit.exists() else
                     "not installed (deploy/pi5/install_pi.sh)"))
    from sentrix.io import network
    net = (network_status or network.status)()
    if not net.get("available"):
        rows.append(_row("warn", "network", "nmcli not available (NetworkManager)"))
    else:
        rows.append(_row("ok" if net.get("connected") else "warn", "network",
                         f"via {net.get('via')} {net.get('device') or ''} "
                         f"{net.get('ssid') or ''} {net.get('ip') or ''}".strip()
                         if net.get("connected") else "no connection"))
    return rows


def _eeprom_config() -> str | None:
    import shutil
    import subprocess
    tool = shutil.which("rpi-eeprom-config")
    if tool is None:
        return None
    try:
        done = subprocess.run([tool], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


__all__ = ["NodeService", "build_service", "preflight", "merge_node_block",
           "temperature_state", "hardware_report", "read_config_txt",
           "venv_has_system_site_packages", "live_loop_alive", "event_loop_answers"]
