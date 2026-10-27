"""GPIO service (T-HW): pin factory, debounced inputs, LEDs from the live
state, node alerts and the ``node`` state block. No buzzer (D19).

Every device runs on gpiozero's ``MockFactory`` (a Raspberry Pi 5); inputs are
driven with ``MockPin.drive_high/low``. The event log, the link outbox and the
pipeline's siren are injected fakes.
"""

from __future__ import annotations

import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from gpiozero.pins.mock import MockFactory, MockPin

from sentrix.config import settings
from sentrix.io import actuators, gpio
from sentrix.io.gpio import FAST, OFF, SLOW, SOLID, Debouncer, GpioService
from sentrix.pipeline.state import SystemState


class ManualTimer:
    """A ``threading.Timer`` stand-in the test fires by hand."""

    created: list["ManualTimer"] = []

    def __init__(self, delay, fn) -> None:
        self.delay, self.fn, self.cancelled, self.started = delay, fn, False, False
        ManualTimer.created.append(self)

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        if not self.cancelled:
            self.fn()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in ("SENTRIX_SIREN_ARM_PIN", "SENTRIX_TAMPER_PIN", "SENTRIX_SIREN_ARMED",
                 "SENTRIX_GPIO_INPUTS"):
        monkeypatch.delenv(name, raising=False)
    ManualTimer.created = []
    actuators.clear_arming_source()
    gpio._reset_shared_pin_factory()
    yield
    actuators.clear_arming_source()
    gpio._reset_shared_pin_factory()


class WiredPin(MockPin):
    """A mock pin with something attached: a level the outside world drives
    (``external``) survives the pull-up/down gpiozero sets when it claims the
    pin, as a PC817 output or a closed switch would."""

    external: bool | None = None

    def _set_pull(self, value):
        super()._set_pull(value)
        if self.external is not None and self._function == "input":
            self._change_state(self.external)


def wire(factory, pin: int, level: bool) -> None:
    """Hold input ``pin`` at ``level`` from outside, before or after it is claimed."""
    mock = factory.pin(pin)
    mock.external = level
    if mock.function == "input":
        mock.drive_high() if level else mock.drive_low()


@pytest.fixture
def factory():
    f = MockFactory(revision=gpio.MOCK_REVISION, pin_class=WiredPin)
    yield f
    f.reset()


class Recorder:
    def __init__(self) -> None:
        self.audits: list[tuple[str, dict]] = []
        self.published: list[tuple[str, dict]] = []

    def audit(self, event, **detail):
        self.audits.append((event, detail))

    def publish(self, kind, payload):
        self.published.append((kind, payload))

    def alerts(self):
        return [p["kind"] for k, p in self.published if k == "node.alert"]


def _ctx(state=None, *, bounce=0.0, setup_mode=False, **gpio_fields):
    cfg = replace(settings.gpio, input_bounce_seconds=bounce, **gpio_fields)
    web = replace(settings.web, setup_mode=setup_mode)
    fake_settings = SimpleNamespace(gpio=cfg, web=web, node=settings.node)
    return SimpleNamespace(settings=fake_settings, state=state or SystemState(),
                           get_pipeline=lambda: pytest.fail("never build a pipeline"),
                           registry=None, extras={})


def _service(factory, ctx=None, recorder=None, siren=None, **kwargs) -> GpioService:
    recorder = recorder or Recorder()
    service = GpioService(ctx or _ctx(), pin_factory=factory, audit=recorder.audit,
                          publish=recorder.publish, siren=lambda: siren, run_thread=False,
                          **kwargs)
    service.recorder = recorder
    return service


def _mains_on(factory) -> None:
    """PC817 output low = mains present (the pull-up idles high = mains lost)."""
    wire(factory, settings.gpio.mains_sense_pin, False)


# ── pin factory ───────────────────────────────────────────────────────────────
class TestPinFactory:
    def test_auto_off_a_pi_is_mock(self):
        info = gpio.resolve_pin_factory("", on_pi=False)
        assert info.kind == "mock" and not info.real and info.error is None
        assert info.factory.board_info.model == "5B"

    def test_explicit_mock_on_a_pi(self):
        info = gpio.resolve_pin_factory("mock", on_pi=True)
        assert info.kind == "mock" and not info.real

    def test_lgpio_failure_on_a_pi_is_loud_mock(self, monkeypatch):
        import sys
        import types

        def broken():
            raise OSError("no /dev/gpiochip0")

        fake = types.ModuleType("gpiozero.pins.lgpio")
        fake.LGPIOFactory = broken
        monkeypatch.setitem(sys.modules, "gpiozero.pins.lgpio", fake)
        info = gpio.resolve_pin_factory("", on_pi=True)
        assert info.kind == "mock" and not info.real
        assert "python3-lgpio" in info.error and "system-site-packages" in info.error

    def test_unknown_factory_name(self):
        info = gpio.resolve_pin_factory("rpigpio", on_pi=True)
        assert info.kind == "mock" and "rpigpio" in info.error

    def test_shared_factory_is_resolved_once(self, monkeypatch):
        calls = []

        def fake(name=None, *, on_pi=None):
            calls.append(name)
            return gpio.PinFactoryInfo(None, "mock", False)

        monkeypatch.setattr(gpio, "resolve_pin_factory", fake)
        assert gpio.shared_pin_factory() is gpio.shared_pin_factory()
        assert len(calls) == 1


# ── debounce ──────────────────────────────────────────────────────────────────
class TestDebouncer:
    def test_zero_bounce_settles_at_once(self):
        level = {"v": False}
        changes = []
        d = Debouncer(lambda: level["v"], changes.append, 0.0)
        level["v"] = True
        d.poke()
        assert changes == [True] and d.value is True
        d.poke()                                    # same level: nothing new
        assert changes == [True]

    def test_chatter_collapses_into_the_level_that_held(self):
        level = {"v": False}
        changes = []
        d = Debouncer(lambda: level["v"], changes.append, 0.05, timer_factory=ManualTimer)
        for value in (True, False, True):           # contact bounce
            level["v"] = value
            d.poke()
        assert changes == [] and len(ManualTimer.created) == 3
        assert all(t.cancelled for t in ManualTimer.created[:2])
        ManualTimer.created[-1].fire()
        assert changes == [True]

    def test_a_glitch_that_reverted_is_ignored(self):
        level = {"v": False}
        changes = []
        d = Debouncer(lambda: level["v"], changes.append, 0.05, timer_factory=ManualTimer)
        level["v"] = True
        d.poke()
        level["v"] = False                          # back before the timer fired
        ManualTimer.created[-1].fire()
        assert changes == [] and d.value is False

    def test_poll_catches_a_missed_edge(self):
        level = {"v": False}
        changes = []
        d = Debouncer(lambda: level["v"], changes.append, 0.0)
        level["v"] = True                           # no callback ever arrived
        d.poll()
        assert changes == [True]


# ── what the LEDs say ─────────────────────────────────────────────────────────
def _live(**overrides):
    base = {"level": 1, "camera_online": True,
            "link": {"enabled": False, "connected": False, "claimed": False},
            "node": {}, "sos": {"active": False, "state": None}}
    base.update(overrides)
    return base


class TestLedPatterns:
    @pytest.mark.parametrize("live,expected", [
        (_live(), {"green": SOLID, "amber": OFF, "red": OFF}),
        (_live(camera_online=False), {"green": OFF, "amber": FAST, "red": OFF}),
        (_live(link={"enabled": True, "connected": False, "claimed": False}),
         {"green": OFF, "amber": SOLID, "red": OFF}),
        (_live(link={"enabled": True, "connected": True, "claimed": False}),
         {"green": SLOW, "amber": OFF, "red": OFF}),
        (_live(link={"enabled": True, "connected": True, "claimed": True}),
         {"green": SOLID, "amber": OFF, "red": OFF}),
        (_live(level=2), {"green": SOLID, "amber": OFF, "red": OFF}),
        (_live(level=3), {"green": SOLID, "amber": OFF, "red": SLOW}),
        (_live(level=4), {"green": SOLID, "amber": OFF, "red": FAST}),
        (_live(level=5), {"green": SOLID, "amber": OFF, "red": SOLID}),
        (_live(node={"mains": False}), {"green": SOLID, "amber": FAST, "red": OFF}),
        (_live(node={"temp_state": "critical"}), {"green": SOLID, "amber": FAST, "red": OFF}),
        (_live(node={"under_voltage": True}), {"green": SOLID, "amber": FAST, "red": OFF}),
        (_live(node={"tamper": True}), {"green": SOLID, "amber": FAST, "red": OFF}),
        (_live(level=1, sos={"active": True, "state": "PENDING"}),
         {"green": SOLID, "amber": OFF, "red": SLOW}),
        (_live(level=4, sos={"active": True, "state": "PENDING"}),
         {"green": SOLID, "amber": OFF, "red": FAST}),
        (_live(level=2, sos={"active": True, "state": "FAILED"}),
         {"green": SOLID, "amber": OFF, "red": SOLID}),
        (_live(sos={"active": False, "state": "EXPIRED"}),
         {"green": SOLID, "amber": OFF, "red": OFF}),
    ])
    def test_table(self, live, expected):
        assert gpio.led_patterns(live) == expected

    def test_setup_mode_blinks_amber_unless_there_is_a_fault(self):
        assert gpio.led_patterns(_live(), setup_mode=True)["amber"] == SLOW
        assert gpio.led_patterns(_live(camera_online=False), setup_mode=True)["amber"] == FAST


# ── inputs and pins ───────────────────────────────────────────────────────────
class TestInputSpecs:
    def test_meanings(self):
        specs = {s.name: s for s in gpio.input_specs(settings.gpio)}
        assert set(specs) == {"pir", "door", "mains"}
        assert specs["pir"].value(True) is True and not specs["pir"].pull_up
        assert specs["door"].value(True) == "closed" and specs["door"].value(False) == "open"
        assert specs["mains"].pull_up is settings.gpio.mains_present_low
        assert specs["mains"].value(True) is True

    def test_optional_arming_and_tamper_inputs(self, monkeypatch):
        monkeypatch.setenv("SENTRIX_SIREN_ARM_PIN", "25")
        monkeypatch.setenv("SENTRIX_TAMPER_PIN", "22")
        specs = {s.name: s for s in gpio.input_specs(settings.gpio)}
        assert specs["siren_armed"].pin == 25 and specs["siren_armed"].value(True) is True
        assert specs["tamper"].pin == 22 and specs["tamper"].value(False) is True

    def test_bad_optional_pins_are_ignored(self, monkeypatch):
        monkeypatch.setenv("SENTRIX_SIREN_ARM_PIN", "99")
        monkeypatch.setenv("SENTRIX_TAMPER_PIN", "x")
        assert {s.name for s in gpio.input_specs(settings.gpio)} == {"pir", "door", "mains"}

    @pytest.mark.parametrize("value, names", [
        ("none", set()), ("", {"pir", "door", "mains"}), ("pir", {"pir"}),
        ("door, PIR;bogus", {"pir", "door"})])
    def test_only_the_wired_inputs(self, monkeypatch, value, names):
        monkeypatch.setenv("SENTRIX_GPIO_INPUTS", value)
        assert {s.name for s in gpio.input_specs(settings.gpio)} == names

    def test_pin_conflicts(self, monkeypatch):
        assert gpio.pin_conflicts(settings.gpio) == []
        monkeypatch.setenv("SENTRIX_SIREN_ARM_PIN", str(settings.gpio.led_amber_pin))
        assert gpio.pin_conflicts(settings.gpio) == [
            f"GPIO{settings.gpio.led_amber_pin}: led_amber, siren_armed"]


# ── the service ───────────────────────────────────────────────────────────────
class TestGpioService:
    def test_start_publishes_the_node_block(self, factory):
        live = SystemState()
        _mains_on(factory)
        service = _service(factory, _ctx(live))
        service.start()
        try:
            node = live.get()["node"]
            block = node["gpio"]
            assert block["pin_factory"] == "mock" and block["real"] is False
            assert block["leds"] == {"green": OFF, "amber": OFF, "red": OFF}
            assert block["pir"] is False and block["door"] == "open" and block["mains"] is True
            assert node["pir"] is False and node["door"] == "open" and node["mains"] is True
            assert node["siren_armed"] is True and node["updated_at"]
            assert service.recorder.alerts() == [], "mains present at start: no alert"
        finally:
            service.stop()

    def test_door_and_pir_changes_reach_the_block_and_the_event_log(self, factory):
        live = SystemState()
        _mains_on(factory)
        service = _service(factory, _ctx(live))
        service.start()
        try:
            factory.pin(settings.gpio.door_pin).drive_low()          # contact closes
            assert live.get()["node"]["door"] == "closed"
            factory.pin(settings.gpio.pir_pin).drive_high()
            assert live.get()["node"]["pir"] is True
            events = service.recorder.audits
            assert ("node_input", {"input": "door", "value": "closed"}) in events
            assert ("node_input", {"input": "pir", "value": True,
                                   "suppressed_since_last": 0}) in events
        finally:
            service.stop()

    def test_pir_rows_are_rate_limited(self, factory):
        now = {"t": 0.0}
        _mains_on(factory)
        service = _service(factory, clock=lambda: now["t"])
        service.start()
        try:
            pir = factory.pin(settings.gpio.pir_pin)
            for t in (0.0, 5.0, 10.0, 40.0):
                now["t"] = t
                pir.drive_high()
                pir.drive_low()
            rows = [d for e, d in service.recorder.audits if d.get("input") == "pir"]
            assert [r["suppressed_since_last"] for r in rows] == [0, 2]
        finally:
            service.stop()

    def test_mains_loss_and_return_are_node_alerts(self, factory):
        live = SystemState()
        _mains_on(factory)
        service = _service(factory, _ctx(live))
        service.start()
        try:
            mains = factory.pin(settings.gpio.mains_sense_pin)
            mains.drive_high()                                      # opto off: mains lost
            assert live.get()["node"]["mains"] is False
            mains.drive_low()
            assert service.recorder.alerts() == ["mains_lost", "mains_restored"]
            payload = service.recorder.published[0][1]
            assert payload["at"] and payload["detail"] == {}
            kinds = [d.get("kind") for e, d in service.recorder.audits if e == "node_alert"]
            assert kinds == ["mains_lost", "mains_restored"]
        finally:
            service.stop()

    def test_booting_on_battery_alerts_once(self, factory):
        service = _service(factory)                 # mains pin idles high: no mains
        service.start()
        try:
            assert service.recorder.alerts() == ["mains_lost"]
            assert service.recorder.published[0][1]["detail"] == {"at_start": True}
        finally:
            service.stop()

    def test_unwired_mains_and_door_are_not_faults(self, factory, monkeypatch):
        """A box without the PC817 or the door contact (the demo unit): no
        "mains lost" at boot, no amber fault, no fixed "open" door."""
        monkeypatch.setenv("SENTRIX_GPIO_INPUTS", "none")
        live = SystemState()
        live.update(camera_online=True)
        service = _service(factory, _ctx(live))      # mains pin idles high
        service.start()
        try:
            service.poll_once()
            node = live.get()["node"]
            assert service.recorder.alerts() == []
            assert node["mains"] is None and node["door"] is None and node["pir"] is None
            assert service.patterns["amber"] == OFF
        finally:
            service.stop()

    def test_tamper_and_arming_key(self, factory, monkeypatch):
        monkeypatch.setenv("SENTRIX_SIREN_ARM_PIN", "25")
        monkeypatch.setenv("SENTRIX_TAMPER_PIN", "22")
        _mains_on(factory)
        wire(factory, 22, False)                    # lid shut
        live = SystemState()
        service = _service(factory, _ctx(live))
        service.start()
        try:
            assert live.get()["node"]["siren_armed"] is False, "key open = disarmed"
            assert actuators.siren_arming() == {"armed": False, "source": "input"}
            factory.pin(25).drive_low()             # key turned on
            assert actuators.siren_armed() is True
            assert live.get()["node"]["siren_armed"] is True
            factory.pin(22).drive_high()            # lid opened
            assert live.get()["node"]["tamper"] is True
            assert service.recorder.alerts() == ["tamper"]
        finally:
            service.stop()
        assert actuators.siren_arming()["source"] == "flag", "stop unregisters the key"

    def test_debounced_input_waits_for_the_level_to_hold(self, factory):
        live = SystemState()
        _mains_on(factory)
        service = _service(factory, _ctx(live, bounce=0.05), timer_factory=ManualTimer)
        service.start()
        try:
            door = factory.pin(settings.gpio.door_pin)
            door.drive_low()
            door.drive_high()
            door.drive_low()
            assert live.get()["node"]["door"] == "open", "not accepted while bouncing"
            ManualTimer.created[-1].fire()
            assert live.get()["node"]["door"] == "closed"
        finally:
            service.stop()

    def test_leds_follow_the_live_state(self, factory):
        live = SystemState()
        live.update(camera_online=True)
        _mains_on(factory)
        service = _service(factory, _ctx(live))
        service.start()
        try:
            service.poll_once()
            assert service.patterns == {"green": SOLID, "amber": OFF, "red": OFF}
            assert factory.pin(settings.gpio.led_green_pin).state is True
            live.update(camera_online=False)
            service.poll_once()
            assert service.patterns == {"green": OFF, "amber": FAST, "red": OFF},                 "camera not up yet (or lost): a fault"
            live.update(camera_online=True)
            live.update(level=3)
            service.poll_once()
            assert service.patterns["red"] == SLOW
            live.update(level=5, camera_online=False)
            service.poll_once()
            assert service.patterns == {"green": OFF, "amber": FAST, "red": SOLID}
            assert factory.pin(settings.gpio.led_red_pin).state is True
            assert live.get()["node"]["gpio"]["leds"]["amber"] == FAST
        finally:
            service.stop()
        assert factory.pin(settings.gpio.led_red_pin).state is False, "LEDs off at stop"

    def test_no_buzzer_pin_is_claimed(self, factory):
        """D19: the buzzer is gone. GPIO26 stays free and the block has no
        buzzer, whatever the level does."""
        from gpiozero import OutputDevice
        live = SystemState()
        live.update(camera_online=True)
        _mains_on(factory)
        service = _service(factory, _ctx(live))
        service.start()
        try:
            live.update(level=3)
            service.poll_once()
            assert "buzzer" not in live.get()["node"]["gpio"]
            OutputDevice(26, pin_factory=factory).close()       # not reserved
        finally:
            service.stop()

    def test_poll_backs_up_the_siren_release_timer(self, factory):
        calls = []
        siren = SimpleNamespace(name="gpio", is_real=False,
                                release_if_overdue=lambda: calls.append(1) or False,
                                status=lambda: {"backend": "gpio", "on": False})
        _mains_on(factory)
        service = _service(factory, siren=siren)
        service.start()
        try:
            service.poll_once()
            assert calls == [1]
            assert service.snapshot()["siren"] == {"backend": "gpio", "on": False}
        finally:
            service.stop()

    def test_a_busy_pin_is_reported_and_the_rest_works(self, factory):
        from gpiozero import LED
        squatter = LED(settings.gpio.led_amber_pin, pin_factory=factory)
        live = SystemState()
        live.update(camera_online=True)
        _mains_on(factory)
        service = _service(factory, _ctx(live))
        service.start()
        try:
            assert "led_amber" in service.errors and "amber" not in service.leds
            assert service.health()["errors"]["led_amber"].startswith("GPIO12")
            service.poll_once()
            assert service.patterns["green"] == SOLID
        finally:
            service.stop()
            squatter.close()

    def test_stop_is_idempotent_and_frees_every_pin(self, factory):
        _mains_on(factory)
        service = _service(factory)
        service.start()
        service.stop()
        service.stop()
        again = _service(factory)
        again.start()
        try:
            assert again.errors == {}, "every pin was released by the first stop"
        finally:
            again.stop()

    def test_thread_runs_the_lamp_test_and_the_loop(self, factory):
        live = SystemState()
        live.update(camera_online=True)
        _mains_on(factory)
        service = GpioService(_ctx(live), pin_factory=factory, audit=lambda *a, **k: None,
                              publish=lambda *a: None, siren=lambda: None, poll_seconds=0.02)
        service.start()
        try:
            deadline = time.monotonic() + 3.0
            while service.patterns.get("green") != SOLID and time.monotonic() < deadline:
                time.sleep(0.02)
            assert service.patterns["green"] == SOLID
        finally:
            service.stop()


class TestFactoryAndPreflight:
    def test_off_unless_enabled(self, factory):
        assert gpio.build_service(_ctx()) is None
        ctx = _ctx()
        ctx.settings.gpio = replace(ctx.settings.gpio, enabled=True)
        service = gpio.build_service(ctx)
        assert isinstance(service, GpioService)

    def test_preflight_on_a_laptop(self, monkeypatch):
        monkeypatch.setattr(gpio.sensors, "raspberry_pi_model", lambda root="/": None)
        rows = {r["name"]: r for r in gpio.preflight(settings)}
        assert rows["pin factory"]["status"] == "ok" and "mock" in rows["pin factory"]["detail"]
        assert rows["pin map"]["status"] == "ok"
        assert rows["siren arming"]["status"] == "ok"
        assert rows["service"]["detail"].startswith("off")

    def test_preflight_on_a_pi_without_lgpio(self, monkeypatch):
        import importlib.util
        monkeypatch.setattr(gpio.sensors, "raspberry_pi_model",
                            lambda root="/": "Raspberry Pi 5 Model B Rev 1.0")
        real_find = importlib.util.find_spec
        monkeypatch.setattr(importlib.util, "find_spec",
                            lambda name, *a: None if name == "lgpio" else real_find(name, *a))
        rows = {r["name"]: r for r in gpio.preflight(settings)}
        assert rows["pin factory"]["status"] == "fail"
        assert "python3-lgpio" in rows["pin factory"]["detail"]

    def test_preflight_flags_conflicts_and_disarmed_siren(self, monkeypatch):
        monkeypatch.setattr(gpio.sensors, "raspberry_pi_model", lambda root="/": None)
        monkeypatch.setenv("SENTRIX_TAMPER_PIN", str(settings.gpio.pir_pin))
        monkeypatch.setenv("SENTRIX_SIREN_ARMED", "0")
        rows = {r["name"]: r for r in gpio.preflight(settings)}
        assert rows["pin map"]["status"] == "fail" and "pir, tamper" in rows["pin map"]["detail"]
        assert rows["siren arming"]["status"] == "warn"
