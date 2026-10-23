"""Siren relay on gpiozero (T-HW): polarity, auto-release, maximum on-time,
arming, the shared test limiter, and backend selection.

Every pin is a gpiozero ``MockFactory`` pin pretending to be a Raspberry Pi 5;
nothing touches real hardware, audio or the network.
"""

from __future__ import annotations

import pytest
from gpiozero.pins.mock import MockFactory

from sentrix.actor import Actor
from sentrix.io import actuators, gpio
from sentrix.io.actuators import ConsoleSiren, GpioSiren, build_siren, siren_test


class Clock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def factory():
    f = MockFactory(revision=gpio.MOCK_REVISION)
    yield f
    f.reset()


@pytest.fixture(autouse=True)
def _clean_arming(monkeypatch):
    monkeypatch.delenv("SENTRIX_SIREN_ARMED", raising=False)
    monkeypatch.delenv("SENTRIX_SIREN", raising=False)
    actuators.clear_arming_source()
    gpio._reset_shared_pin_factory()
    yield
    actuators.clear_arming_source()
    gpio._reset_shared_pin_factory()


def _siren(factory, clock=None, **kwargs) -> GpioSiren:
    return GpioSiren(pin_factory=factory, clock=clock or Clock(), **kwargs)


def _wait_until_off(siren: GpioSiren, seconds: float = 3.0) -> None:
    """Poll the relay until the real release timer has switched it off."""
    import time
    deadline = time.monotonic() + seconds
    while siren.is_on and time.monotonic() < deadline:
        time.sleep(0.02)


class TestPolarity:
    def test_active_high_npn_driver_starts_low_and_drives_high(self, factory):
        siren = _siren(factory, active_low=False)
        pin = factory.pin(17)
        assert pin.state is False, "claimed OFF: an NPN driver is off when the pin is low"
        siren.activate(1.0)
        assert pin.state is True and siren.is_on
        siren.deactivate()
        assert pin.state is False and not siren.is_on
        siren.close()

    def test_active_low_opto_board_starts_high_and_drives_low(self, factory):
        siren = _siren(factory, active_low=True)
        pin = factory.pin(17)
        assert pin.state is True, "an active-low relay is released by a HIGH level"
        siren.activate(1.0)
        assert pin.state is False
        siren.deactivate()
        assert pin.state is True
        siren.close()

    def test_default_polarity_comes_from_settings(self, factory):
        from sentrix.config import settings
        siren = _siren(factory)
        assert siren.active_low is (not settings.gpio.siren_active_high)
        assert siren.status()["active_high"] is settings.gpio.siren_active_high
        siren.close()

    def test_mock_pins_are_never_a_real_siren(self, factory):
        siren = _siren(factory)
        assert siren.available and not siren.is_real
        assert siren.status()["pin_factory"] == "mock"
        siren.close()


class TestRelease:
    def test_activation_releases_itself(self, factory):
        clock = Clock()
        siren = _siren(factory, clock)
        siren.activate(5.0)
        assert not siren.release_if_overdue()
        clock.now += 4.9
        assert not siren.release_if_overdue() and siren.is_on
        clock.now += 0.2
        assert siren.release_if_overdue()
        assert not siren.is_on and siren.status()["remaining_s"] is None
        siren.close()

    def test_real_timer_switches_it_off(self, factory):
        siren = GpioSiren(pin_factory=factory)
        siren.activate(0.15)
        assert siren.is_on
        _wait_until_off(siren)
        assert not siren.is_on, "the release timer must turn the relay off"
        siren.close()

    def test_a_timer_that_wakes_early_tries_again(self, factory):
        """A wait can end a few ms before time.monotonic() reaches the release
        time (Windows); the one-shot timer then found nothing overdue and the
        relay stayed on. The first reading on the timer thread is made 5 ms
        short here, as measured on the laptop."""
        import threading
        import time
        sirens, early = [], []

        def clock():
            if isinstance(threading.current_thread(), threading.Timer) and not early:
                early.append(True)
                return sirens[0]._release_at - 0.005
            return time.monotonic()

        siren = GpioSiren(pin_factory=factory, clock=clock)
        sirens.append(siren)
        siren.activate(0.1)
        _wait_until_off(siren)
        assert early, "the timer woke early once"
        assert not siren.is_on, "an early wake re-arms and still turns the relay off"
        siren.close()

    def test_extensions_never_pass_the_maximum_on_time(self, factory):
        clock = Clock()
        siren = _siren(factory, clock, max_on_seconds=10.0)
        siren.activate(6.0)
        clock.now += 5.0
        siren.activate(6.0)                     # would run to t+11
        assert siren.status()["remaining_s"] == pytest.approx(5.0)
        clock.now += 5.0
        assert siren.release_if_overdue(), "capped at 10 s from switch-on"
        siren.close()

    def test_one_long_request_is_clamped(self, factory):
        clock = Clock()
        siren = _siren(factory, clock, max_on_seconds=3.0)
        siren.activate(600.0)
        assert siren.status()["remaining_s"] == pytest.approx(3.0)
        siren.close()

    def test_held_off_once_the_maximum_is_used(self, factory):
        clock = Clock()
        siren = _siren(factory, clock, max_on_seconds=2.0)
        siren.activate(2.0)
        clock.now += 2.0                        # the timer has not fired yet
        siren.activate(5.0)
        assert not siren.is_on, "a period that reached its maximum is not extended"
        siren.close()

    def test_default_duration_is_the_alarm_duration(self, factory):
        from sentrix.config import settings
        siren = _siren(factory)
        siren.activate()
        assert siren.status()["remaining_s"] == pytest.approx(
            settings.alert.siren_duration_seconds)
        siren.close()

    def test_close_switches_off_and_frees_the_pin(self, factory):
        siren = _siren(factory)
        siren.activate(5.0)
        siren.close()
        again = _siren(factory)                 # the pin is free again
        assert again.available and factory.pin(17).state is False
        again.close()

    def test_missing_pin_factory_is_reported_not_raised(self, factory):
        busy = _siren(factory)
        second = _siren(factory)                # GPIO17 already reserved
        assert not second.available and "GPIO17" in second.error
        second.activate(1.0)                    # a no-op, never an exception
        busy.close()


class TestArming:
    def test_flag_and_input(self, monkeypatch):
        assert actuators.siren_arming() == {"armed": True, "source": "flag"}
        monkeypatch.setenv("SENTRIX_SIREN_ARMED", "0")
        assert actuators.siren_arming() == {"armed": False, "source": "flag"}
        actuators.set_arming_source(lambda: True)
        assert actuators.siren_arming() == {"armed": True, "source": "input"}
        actuators.set_arming_source(lambda: None)        # cannot tell: the flag decides
        assert actuators.siren_arming() == {"armed": False, "source": "flag"}

    def test_clear_only_removes_its_own_source(self):
        mine, other = (lambda: False), (lambda: True)
        actuators.set_arming_source(mine)
        actuators.clear_arming_source(other)
        assert actuators.siren_arming()["source"] == "input"
        actuators.clear_arming_source(mine)
        assert actuators.siren_arming()["source"] == "flag"

    def test_a_failing_input_falls_back_to_the_flag(self):
        actuators.set_arming_source(lambda: 1 / 0)
        assert actuators.siren_arming() == {"armed": True, "source": "flag"}


class TestSirenTest:
    def test_drives_the_relay_for_one_second(self, factory):
        clock = Clock()
        siren = _siren(factory, clock)
        result = siren_test(siren, actor=Actor.device_admin())
        assert result["ok"] and result["seconds"] == 1.0 and result["armed"] is True
        assert result["backend"] == "gpio" and result["is_real"] is False
        assert siren.is_on
        clock.now += 1.0
        assert siren.release_if_overdue() and not siren.is_on
        siren.close()

    def test_refused_while_disarmed_without_using_the_slot(self, monkeypatch):
        calls = []
        siren = ConsoleSiren()
        siren.activate = lambda duration=None: calls.append(duration)
        monkeypatch.setenv("SENTRIX_SIREN_ARMED", "0")
        refused = siren_test(siren, actor=Actor.device_admin())
        assert refused["code"] == "conflict" and refused["armed"] is False
        assert "SENTRIX_SIREN_ARMED=0" in refused["message"] and calls == []
        actuators.set_arming_source(lambda: False)
        key_off = siren_test(siren, actor=Actor.device_admin())
        assert key_off["code"] == "conflict" and "arming key" in key_off["message"]
        actuators.set_arming_source(lambda: True)
        assert siren_test(siren, actor=Actor.device_admin())["ok"]
        assert calls == [1.0]

    def test_limiter_retry_after(self):
        siren = ConsoleSiren()
        siren.activate = lambda duration=None: None
        assert actuators.siren_test_retry_after() is None
        assert siren_test(siren, actor=Actor.device_admin())["ok"]
        wait = actuators.siren_test_retry_after()
        assert wait is not None and 0 < wait <= 60
        again = siren_test(siren, actor=Actor.device_admin())
        assert again["code"] == "rate_limited" and again["armed"] is True

    def test_a_failing_backend_is_internal(self):
        siren = ConsoleSiren()

        def boom(duration=None):
            raise RuntimeError("relay board unplugged")

        siren.activate = boom
        result = siren_test(siren, actor=Actor.device_admin())
        assert result["code"] == "internal" and "unplugged" in result["message"]


class TestBuildSiren:
    def test_laptop_auto_never_claims_mock_pins(self, monkeypatch):
        monkeypatch.setattr(actuators, "_real_gpio_possible", lambda: False)
        for forced in ("", "auto"):
            monkeypatch.setenv("SENTRIX_SIREN", forced)
            siren = build_siren()
            assert siren.name in ("sound", "console"), forced

    def test_forced_gpio_on_a_laptop_is_the_mock_relay(self, monkeypatch):
        monkeypatch.setenv("SENTRIX_SIREN", "gpio")
        monkeypatch.setenv("SENTRIX_GPIO_PIN_FACTORY", "mock")
        monkeypatch.setattr(gpio, "shared_pin_factory",
                            lambda: gpio.PinFactoryInfo(MockFactory(revision=gpio.MOCK_REVISION),
                                                        "mock", False))
        siren = build_siren()
        try:
            assert isinstance(siren, GpioSiren) and not siren.is_real
        finally:
            siren.close()

    def test_forced_console(self, monkeypatch):
        monkeypatch.setenv("SENTRIX_SIREN", "console")
        assert isinstance(build_siren(), ConsoleSiren)

    def test_real_factory_gives_the_gpio_siren(self, monkeypatch, factory):
        info = gpio.PinFactoryInfo(factory, "lgpio", True)
        monkeypatch.setattr(gpio, "shared_pin_factory", lambda: info)
        monkeypatch.setattr(actuators, "_real_gpio_possible", lambda: True)
        siren = build_siren()
        try:
            assert isinstance(siren, GpioSiren) and siren.is_real
            assert siren.status()["real"] is True
        finally:
            siren.close()

    def test_base_actuators_report_status(self):
        status = ConsoleSiren().status()
        assert status == {"backend": "console", "real": False, "armed": True, "on": False}
