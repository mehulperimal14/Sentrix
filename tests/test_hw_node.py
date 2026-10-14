"""Board telemetry, node alerts, the systemd watchdog and the doctor's hardware
rows (T-HW). A fake ``/proc`` + ``/sys`` tree and a fake ``vcgencmd`` stand in
for the Raspberry Pi; nothing runs a real subprocess or opens a device.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from sentrix.config import settings
from sentrix.io import node, sensors
from sentrix.io.node import NodeService
from sentrix.io.watchdog import SystemdNotifier
from sentrix.pipeline.state import NODE_IDLE, SystemState

PI5 = "Raspberry Pi 5 Model B Rev 1.0"

PMIC = """\
     3V7_WL_SW_A current(0)=0.10637260A
     VDD_CORE_A current(7)=2.00000000A
     3V7_WL_SW_V volt(8)=3.70000000V
     VDD_CORE_V volt(15)=0.80000000V
     EXT5V_V volt(24)=5.13072000V
     BATT_V volt(25)=0.00000000V
     garbage line
"""


def fake_vcgencmd(outputs: dict[str, str | None]) -> sensors.Vcgencmd:
    calls = []

    def runner(argv, timeout):
        calls.append(argv[1:])
        return outputs.get(" ".join(argv[1:]))

    vc = sensors.Vcgencmd(runner)
    vc.calls = calls
    return vc


def pi_tree(root, *, model=PI5, temp_milli="48300", iio=None, config=None):
    (root / "proc/device-tree").mkdir(parents=True)
    (root / "proc/device-tree/model").write_bytes(model.encode() + b"\x00")
    (root / "proc/uptime").write_text("12345.67 4567.89\n")
    if temp_milli is not None:
        zone = root / "sys/class/thermal/thermal_zone0"
        zone.mkdir(parents=True)
        (zone / "temp").write_text(temp_milli + "\n")
    for index, (name, files) in enumerate((iio or {}).items()):
        # iio:deviceN on Linux; the reader matches on the "name" file, not the
        # directory name (a colon is not allowed in a Windows path).
        device = root / f"sys/bus/iio/devices/iio-device{index}"
        device.mkdir(parents=True)
        (device / "name").write_text(name + "\n")
        for filename, value in files.items():
            (device / filename).write_text(value + "\n")
    if config is not None:
        (root / "boot/firmware").mkdir(parents=True)
        (root / "boot/firmware/config.txt").write_text(config)
    return root


# ── parsing ───────────────────────────────────────────────────────────────────
class TestParsing:
    def test_throttled_flags(self):
        assert sensors.parse_throttled("throttled=0x50005\n") == 0x50005
        decoded = sensors.decode_throttled(0x50005)
        assert decoded["under_voltage"] and decoded["throttled"]
        assert decoded["under_voltage_occurred"] and decoded["throttled_occurred"]
        assert not decoded["freq_capped"] and decoded["limited"] is True
        assert decoded["raw"] == "0x50005"
        assert decoded["flags"] == ["under_voltage", "throttled", "under_voltage_occurred",
                                    "throttled_occurred"]

    def test_clean_and_history_only(self):
        assert sensors.decode_throttled(0)["flags"] == [] and not sensors.decode_throttled(0)["limited"]
        history = sensors.decode_throttled(0x80000)
        assert history["soft_temp_limit_occurred"] and not history["limited"]

    def test_unknown_throttle(self):
        assert sensors.parse_throttled(None) is None and sensors.parse_throttled("error") is None
        assert sensors.decode_throttled(None)["raw"] is None

    def test_measure_temp(self):
        assert sensors.parse_measure_temp("temp=48.3'C\n") == 48.3
        assert sensors.parse_measure_temp("") is None

    def test_pmic(self):
        readings = sensors.parse_pmic_adc(PMIC)
        assert readings["EXT5V_V"] == pytest.approx(5.13072)
        assert readings["VDD_CORE_A"] == 2.0 and "garbage" not in str(readings)
        # 3.7 V x 0.106 A + 0.8 V x 2.0 A (EXT5V has no current rail)
        assert sensors.pmic_power_watts(readings) == pytest.approx(1.99, abs=0.01)
        assert sensors.pmic_power_watts({}) is None


# ── platform and sysfs ────────────────────────────────────────────────────────
class TestPlatform:
    def test_pi_model(self, tmp_path):
        pi_tree(tmp_path)
        assert sensors.raspberry_pi_model(tmp_path) == PI5
        assert sensors.is_raspberry_pi_5(tmp_path)

    def test_other_boards_and_laptops(self, tmp_path):
        assert sensors.raspberry_pi_model(tmp_path) is None
        pi_tree(tmp_path, model="Orange Pi 5")
        assert not sensors.is_raspberry_pi(tmp_path)

    def test_pi4_is_a_pi_but_not_a_pi5(self, tmp_path):
        pi_tree(tmp_path, model="Raspberry Pi 4 Model B Rev 1.5")
        assert sensors.is_raspberry_pi(tmp_path) and not sensors.is_raspberry_pi_5(tmp_path)

    def test_temperature_from_the_thermal_zone_then_vcgencmd(self, tmp_path):
        pi_tree(tmp_path)
        assert sensors.read_cpu_temp(tmp_path) == 48.3
        empty = tmp_path / "empty"
        empty.mkdir()
        vc = fake_vcgencmd({"measure_temp": "temp=61.2'C"})
        assert sensors.read_cpu_temp(empty, vc) == 61.2
        assert sensors.read_cpu_temp(empty) is None

    def test_uptime(self, tmp_path):
        pi_tree(tmp_path)
        assert sensors.read_uptime(tmp_path) == 12345.7
        assert sensors.read_uptime(tmp_path / "nowhere") is None

    def test_vcgencmd_absent_is_none(self):
        vc = sensors.Vcgencmd(path=None)
        vc._path = None
        assert not vc.available and vc("get_throttled") is None


class TestIioSensors:
    def test_bme280(self, tmp_path):
        pi_tree(tmp_path, iio={"bme280": {"in_temp_input": "23450",
                                         "in_humidityrelative_input": "45123",
                                         "in_pressure_input": "100.325"}})
        sensor = sensors.IioEnvironmentSensor("bme280", tmp_path)
        assert sensor.present
        assert sensor.read() == {"sensor": "bme280", "temperature_c": 23.4,
                                 "humidity_pct": 45.1, "pressure_hpa": 1003.2}

    def test_dht22_through_the_dht11_driver_and_a_failed_read(self, tmp_path):
        pi_tree(tmp_path, iio={"dht11": {"in_temp_input": "21000",
                                        "in_humidityrelative_input": "60000"}})
        sensor = sensors.IioEnvironmentSensor("dht22", tmp_path)
        assert sensor.read()["humidity_pct"] == 60.0
        device = sensor.find()
        for name in ("in_temp_input", "in_humidityrelative_input"):
            (device / name).write_text("")          # EIO: the sensor did not answer
        assert sensor.read() is None

    def test_absent_and_unknown(self, tmp_path):
        assert sensors.IioEnvironmentSensor("bme280", tmp_path).read() is None
        with pytest.raises(ValueError):
            sensors.IioEnvironmentSensor("sht31", tmp_path)

    def test_enabled_sensors(self, tmp_path):
        kinds = [s.kind for s in sensors.environment_sensors(tmp_path, bme280=True, dht22=True)]
        assert kinds == ["bme280", "dht22"]
        assert sensors.environment_sensors(tmp_path) == []


class TestTelemetry:
    def test_full_reading_on_a_pi5(self, tmp_path):
        pi_tree(tmp_path, iio={"bme280": {"in_temp_input": "23000"}})
        vc = fake_vcgencmd({"get_throttled": "throttled=0x50000", "pmic_read_adc": PMIC})
        telemetry = sensors.Telemetry(root=tmp_path, vcgencmd=vc,
                                      sensors=sensors.environment_sensors(tmp_path, bme280=True))
        reading = telemetry.read()
        assert telemetry.model == PI5
        assert reading["cpu_temp_c"] == 48.3 and reading["throttled"] is False
        assert reading["under_voltage"] is False and reading["under_voltage_occurred"] is True
        assert reading["throttle_flags"] == ["under_voltage_occurred", "throttled_occurred"]
        assert reading["ext5v_v"] == pytest.approx(5.131) and reading["power_w"] > 0
        assert reading["environment"]["temperature_c"] == 23.0
        assert reading["uptime_s"] == 12345.7

    def test_laptop_reading_is_all_none(self, tmp_path):
        vc = sensors.Vcgencmd(path=None)
        vc._path = None
        reading = sensors.Telemetry(root=tmp_path, vcgencmd=vc).read()
        assert reading["cpu_temp_c"] is None and reading["throttled"] is None
        assert reading["ext5v_v"] is None and reading["environment"] is None


# ── node block and service ────────────────────────────────────────────────────
class FakeTelemetry:
    model = PI5

    def __init__(self) -> None:
        self.reading = {"cpu_temp_c": 50.0, "throttled": False, "under_voltage": False,
                        "ext5v_v": 5.1, "throttle_flags": [], "read_at": 1.0}

    def read(self):
        return dict(self.reading)


class Recorder:
    def __init__(self) -> None:
        self.audits, self.published = [], []

    def audit(self, event, **detail):
        self.audits.append((event, detail))

    def publish(self, kind, payload):
        self.published.append((kind, payload))


def _ctx(state=None, **node_overrides):
    node_cfg = replace(settings.node, **node_overrides)
    return SimpleNamespace(settings=SimpleNamespace(node=node_cfg, gpio=settings.gpio,
                                                    web=settings.web),
                           state=state or SystemState(), registry=None, extras={},
                           get_pipeline=lambda: pytest.fail("never build a pipeline"))


def _service(ctx=None, *, notifier=None, liveness=None, telemetry=None, **kwargs):
    recorder = Recorder()
    service = NodeService(ctx or _ctx(), telemetry=telemetry or FakeTelemetry(),
                          notifier=notifier or SystemdNotifier(env={}),
                          liveness=liveness, audit=recorder.audit, publish=recorder.publish,
                          run_thread=False, **kwargs)
    service.recorder = recorder
    return service


class TestNodeBlock:
    def test_merge_keeps_other_fields(self):
        live = SystemState()
        node.merge_node_block(live, cpu_temp_c=40.0)
        node.merge_node_block(live, door="open")
        block = live.get()["node"]
        assert block["cpu_temp_c"] == 40.0 and block["door"] == "open"
        assert set(NODE_IDLE) <= set(block) and block["updated_at"]

    def test_temperature_state(self):
        assert node.temperature_state(None, 80, 85) is None
        assert node.temperature_state(79.9, 80, 85) == "ok"
        assert node.temperature_state(80.0, 80, 85) == "warn"
        assert node.temperature_state(85.0, 80, 85) == "critical"


class TestNodeService:
    def test_poll_writes_the_node_block(self):
        live = SystemState()
        service = _service(_ctx(live, profile="pi5-clip"))
        service.start()
        service.poll_once()
        block = live.get()["node"]
        assert block["profile"] == "pi5-clip" and block["cpu_temp_c"] == 50.0
        assert block["temp_state"] == "ok" and block["ext5v_v"] == 5.1
        assert block["platform"] == {"model": PI5, "raspberry_pi": True}
        assert "read_at" not in block
        health = service.health()
        assert health["reads"] == 1 and health["cpu_temp_c"] == 50.0
        service.stop()

    def test_over_temperature_alerts_once_with_hysteresis(self):
        telemetry = FakeTelemetry()
        service = _service(telemetry=telemetry)
        for temp in (86.0, 87.0, 82.0, 86.0, 70.0, 90.0):
            telemetry.reading["cpu_temp_c"] = temp
            service.poll_once()
        alerts = [p for k, p in service.recorder.published if k == "node.alert"]
        assert [a["kind"] for a in alerts] == ["over_temp", "over_temp"], \
            "again only after cooling below the warning line"
        assert alerts[0]["detail"]["cpu_temp_c"] == 86.0
        assert [e for e, _ in service.recorder.audits] == ["node_alert", "node_alert"]

    def test_under_voltage_alert(self):
        telemetry = FakeTelemetry()
        service = _service(telemetry=telemetry)
        telemetry.reading.update(under_voltage=True, ext5v_v=4.6, throttled_raw="0x50005")
        service.poll_once()
        service.poll_once()
        telemetry.reading["under_voltage"] = False
        service.poll_once()
        kinds = [p["kind"] for k, p in service.recorder.published]
        assert kinds == ["under_voltage"]
        assert service.health()["alerts"] == []

    def test_a_failing_read_is_counted_not_raised(self):
        class Broken:
            model = None

            def read(self):
                raise OSError("vcgencmd hung")

        service = _service(telemetry=Broken())
        assert service.poll_once() == {} and service.read_errors == 1

    def test_thread_reads_on_its_interval(self):
        live = SystemState()
        telemetry = FakeTelemetry()
        service = NodeService(_ctx(live, interval_seconds=0.05), telemetry=telemetry,
                              notifier=SystemdNotifier(env={}), audit=lambda *a, **k: None,
                              publish=lambda *a: None)
        service.start()
        try:
            deadline = time.monotonic() + 3.0
            while service.reads < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            assert service.reads >= 2
        finally:
            service.stop()
        assert service._thread is None


# ── systemd notify / watchdog ─────────────────────────────────────────────────
class TestSystemdNotifier:
    def test_outside_systemd_everything_is_a_no_op(self):
        notifier = SystemdNotifier(env={})
        assert not notifier.enabled and notifier.watchdog_seconds is None
        assert not notifier.ready() and not notifier.watchdog()

    def test_messages_and_interval(self):
        sent = []
        notifier = SystemdNotifier(env={"NOTIFY_SOCKET": "@/org/sd", "WATCHDOG_USEC": "30000000"},
                                   sender=lambda addr, msg: sent.append((addr, msg)), pid=1)
        assert notifier.watchdog_seconds == 30.0 and notifier.ping_interval == 10.0
        notifier.ready("up")
        notifier.watchdog()
        notifier.stopping()
        assert sent == [("@/org/sd", b"READY=1\nSTATUS=up"), ("@/org/sd", b"WATCHDOG=1"),
                        ("@/org/sd", b"STOPPING=1")]

    def test_watchdog_for_another_pid_is_ignored(self):
        notifier = SystemdNotifier(env={"NOTIFY_SOCKET": "/run/x", "WATCHDOG_USEC": "1000000",
                                        "WATCHDOG_PID": "999"}, sender=lambda a, m: None, pid=1)
        assert notifier.enabled and notifier.watchdog_seconds is None

    def test_send_failure_is_reported_once(self, caplog):
        def broken(addr, msg):
            raise OSError("no socket")

        notifier = SystemdNotifier(env={"NOTIFY_SOCKET": "/run/x"}, sender=broken)
        assert not notifier.watchdog() and not notifier.watchdog()
        assert sum("sd_notify" in r.message for r in caplog.records) == 1


class TestWatchdog:
    def _notifier(self, sent):
        return SystemdNotifier(env={"NOTIFY_SOCKET": "/run/notify", "WATCHDOG_USEC": "9000000"},
                               sender=lambda addr, msg: sent.append(msg), pid=1)

    def test_ready_then_pings_while_alive(self):
        sent = []
        service = _service(notifier=self._notifier(sent), liveness=lambda: (True, "ok"))
        assert service.watchdog_once() and service.watchdog_once()
        assert sent == [b"READY=1\nSTATUS=SENTRIX running", b"WATCHDOG=1", b"WATCHDOG=1"]
        assert service.health()["watchdog"]["pings"] == 2

    def test_a_hung_process_gets_no_ping(self):
        sent = []
        alive = {"ok": False}
        service = _service(notifier=self._notifier(sent),
                           liveness=lambda: (alive["ok"], "ok" if alive["ok"]
                                             else "web event loop not answering"))
        assert not service.watchdog_once()
        assert sent == [] and service.missed_pings == 0, "start-up: no READY, not a miss"
        alive["ok"] = True
        assert service.watchdog_once()
        alive["ok"] = False
        assert not service.watchdog_once()
        assert sent == [b"READY=1\nSTATUS=SENTRIX running", b"WATCHDOG=1"]
        assert service.missed_pings == 1
        assert service.health()["watchdog"]["last_check"] == "web event loop not answering"

    def test_event_loop_check(self):
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        try:
            assert node.event_loop_answers(loop, 2.0)
            blocked = threading.Event()
            loop.call_soon_threadsafe(lambda: blocked.wait(1.0))   # a stuck handler
            assert not node.event_loop_answers(loop, 0.2)
            blocked.set()
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=2.0)
            loop.close()
        assert not node.event_loop_answers(loop, 0.1), "a closed loop is dead"
        assert node.event_loop_answers(None, 0.1), "no loop (tools, tests): nothing to check"

    def test_live_loop_check(self, monkeypatch):
        import sys
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        fake_app = SimpleNamespace(_thread=dead, _stop=threading.Event())
        monkeypatch.setitem(sys.modules, "sentrix.app", fake_app)
        assert not node.live_loop_alive()
        fake_app._stop.set()
        assert node.live_loop_alive(), "stopping on purpose is not a hang"
        fake_app._thread = None
        assert node.live_loop_alive(), "not started yet"

    def test_thread_sends_ready_and_keeps_pinging(self):
        sent = []
        notifier = SystemdNotifier(env={"NOTIFY_SOCKET": "/run/notify", "WATCHDOG_USEC": "600000"},
                                   sender=lambda addr, msg: sent.append(msg), pid=1)
        service = NodeService(_ctx(interval_seconds=60), telemetry=FakeTelemetry(),
                              notifier=notifier, liveness=lambda: (True, "ok"),
                              audit=lambda *a, **k: None, publish=lambda *a: None)
        service.start()
        try:
            assert notifier.ping_interval == 0.5
            deadline = time.monotonic() + 4.0
            while sent.count(b"WATCHDOG=1") < 3 and time.monotonic() < deadline:
                time.sleep(0.05)
            assert sent[0].startswith(b"READY=1") and sent.count(b"WATCHDOG=1") >= 3
        finally:
            service.stop()
        assert sent[-1] == b"STOPPING=1"

    def test_default_liveness_uses_both_checks(self, monkeypatch):
        sent = []
        service = _service(notifier=self._notifier(sent))
        monkeypatch.setattr(node, "live_loop_alive", lambda: False)
        assert service._default_liveness() == (False, "live capture loop stopped")
        monkeypatch.setattr(node, "live_loop_alive", lambda: True)
        assert service._default_liveness() == (True, "ok")


class TestFactoryAndPreflight:
    def test_off_without_flag_or_systemd(self, monkeypatch):
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
        assert node.build_service(_ctx()) is None

    def test_enabled(self, monkeypatch):
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
        service = node.build_service(_ctx(enabled=True))
        assert isinstance(service, NodeService) and service.telemetry_enabled

    def test_notify_only_under_systemd(self, monkeypatch):
        monkeypatch.setenv("NOTIFY_SOCKET", "/run/systemd/notify")
        monkeypatch.setenv("WATCHDOG_USEC", "30000000")
        monkeypatch.delenv("WATCHDOG_PID", raising=False)
        service = node.build_service(_ctx())
        assert isinstance(service, NodeService) and not service.telemetry_enabled
        assert service.telemetry is None and service.poll_once() == {}

    def test_preflight(self, monkeypatch):
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
        monkeypatch.setattr(node.sensors, "raspberry_pi_model", lambda root="/": PI5)
        monkeypatch.setenv("SENTRIX_SENSOR_BME280", "1")
        rows = {r["name"]: r for r in node.preflight(settings)}
        assert rows["profile"]["status"] == "warn", "laptop profile on a Pi"
        assert rows["bme280"]["status"] == "fail"
        assert rows["systemd watchdog"]["detail"].startswith("not under systemd")
        assert rows["service"]["detail"].startswith("off")


# ── doctor: hardware rows ─────────────────────────────────────────────────────
GOOD_CONFIG = """\
# comment
gpio=17=op,dl
dtparam=rtc_bbat_vchg=3000000
kernel_watchdog_timeout=10  # watchdog
"""


def _report(root, *, vc=None, cameras=None, eeprom="PSU_MAX_CURRENT=5000\n", net=None):
    rows = node.hardware_report(
        root=root, vcgencmd=vc or fake_vcgencmd({"get_throttled": "throttled=0x0",
                                                 "pmic_read_adc": PMIC}),
        camera_info=lambda: [{"Model": "imx708_wide"}] if cameras is None else cameras,
        eeprom=lambda: eeprom,
        network_status=lambda: net or {"available": True, "connected": True, "via": "wifi",
                                       "device": "wlan0", "ssid": "Home", "ip": "192.168.1.5"})
    return {r["name"]: r for r in rows}


class TestHardwareReport:
    def test_laptop_is_one_skipped_row(self, tmp_path):
        rows = node.hardware_report(root=tmp_path)
        assert rows == [{"status": "ok", "name": "platform", "detail":
                         "not a Raspberry Pi: hardware checks skipped (GPIO runs on mock pins)"}]

    def test_healthy_pi5(self, tmp_path):
        rows = _report(pi_tree(tmp_path, config=GOOD_CONFIG))
        for name in ("platform", "CSI camera", "CPU temperature", "throttling", "EXT5V",
                     "siren boot state", "RTC cell charging", "hardware watchdog",
                     "PSU_MAX_CURRENT", "network"):
            assert rows[name]["status"] == "ok", (name, rows[name])
        assert "imx708_wide" in rows["CSI camera"]["detail"]
        assert "not opened" in rows["CSI camera"]["detail"]
        assert rows["systemd unit"]["status"] == "warn"

    def test_problems_are_flagged(self, tmp_path):
        root = pi_tree(tmp_path, temp_milli="86000", config="dtparam=audio=on\n")
        vc = fake_vcgencmd({"get_throttled": "throttled=0x50005",
                            "pmic_read_adc": "EXT5V_V volt(24)=4.70000000V\n"})
        rows = _report(root, vc=vc, cameras=[], eeprom="BOOT_UART=0\n",
                       net={"available": True, "connected": False})
        assert rows["CPU temperature"]["status"] == "fail"
        assert rows["throttling"]["status"] == "fail"
        assert "under_voltage" in rows["throttling"]["detail"]
        assert rows["EXT5V"]["status"] == "warn"
        assert rows["CSI camera"]["status"] == "warn"
        assert rows["siren boot state"]["status"] == "warn"
        assert "gpio=17=op,dl" in rows["siren boot state"]["detail"]
        assert rows["PSU_MAX_CURRENT"]["status"] == "warn"
        assert rows["network"]["status"] == "warn"

    def test_active_low_siren_wants_drive_high(self, tmp_path):
        root = pi_tree(tmp_path, config="gpio=17=op,dl\n")
        gpio_low = replace(settings.gpio, siren_active_high=False)
        rows = {r["name"]: r for r in node.hardware_report(
            root=root, vcgencmd=fake_vcgencmd({}), camera_info=lambda: [],
            eeprom=lambda: None, network_status=lambda: {"available": False},
            settings=SimpleNamespace(gpio=gpio_low, node=settings.node))}
        assert rows["siren boot state"]["status"] == "warn"
        assert "gpio=17=op,dh" in rows["siren boot state"]["detail"]

    def test_pi4_skips_pi5_only_rows(self, tmp_path):
        rows = _report(pi_tree(tmp_path, model="Raspberry Pi 4 Model B Rev 1.5",
                               config=GOOD_CONFIG))
        assert "EXT5V" not in rows and "PSU_MAX_CURRENT" not in rows
        assert "RTC cell charging" not in rows

    def test_camera_enumeration_failure(self, tmp_path):
        def broken():
            raise RuntimeError("libcamera: no pipeline handler")

        rows = {r["name"]: r for r in node.hardware_report(
            root=pi_tree(tmp_path), vcgencmd=fake_vcgencmd({}), camera_info=broken,
            eeprom=lambda: None, network_status=lambda: {"available": False})}
        assert rows["CSI camera"]["status"] == "fail"

    def test_venv_flag(self, tmp_path, monkeypatch):
        import sys
        (tmp_path / "pyvenv.cfg").write_text("home = /usr/bin\ninclude-system-site-packages = true\n")
        monkeypatch.setattr(sys, "base_prefix", "/usr")
        assert node.venv_has_system_site_packages(str(tmp_path)) is True
        (tmp_path / "pyvenv.cfg").write_text("include-system-site-packages = false\n")
        assert node.venv_has_system_site_packages(str(tmp_path)) is False
        assert node.venv_has_system_site_packages("/usr") is None, "not a venv"
