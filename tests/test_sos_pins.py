"""The SOS PIN (track T-SOS): Argon2id verification, the duress PIN, the
shared 5-per-10-minutes limiter, and "help is never blocked"."""

from __future__ import annotations

import pytest

from sentrix.sos import pins

SOS_PIN, DURESS_PIN = "246810", "135791"


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture(scope="module")
def hashes():
    """Cheap Argon2id hashes: verification reads the parameters from the
    hash, so the device checks them exactly like the Portal's."""
    from argon2 import PasswordHasher
    cheap = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)
    return {"pin_hash": cheap.hash(SOS_PIN), "duress_pin_hash": cheap.hash(DURESS_PIN)}


def test_hash_pin_uses_the_portal_parameters():
    from argon2 import extract_parameters
    params = extract_parameters(pins.hash_pin(SOS_PIN))
    assert (params.time_cost, params.memory_cost, params.parallelism) == (2, 19456, 1)


@pytest.fixture
def profile(hashes):
    return {"schema": 1, "sos": dict(hashes)}


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def limiter(clock):
    return pins.PinLimiter(5, 600, clock=clock)


class TestCheck:
    def test_sos_and_duress_pins(self, profile, limiter):
        assert pins.check(SOS_PIN, profile, limiter=limiter).outcome == pins.OK
        assert pins.check(DURESS_PIN, profile, limiter=limiter).outcome == pins.DURESS
        wrong = pins.check("111111", profile, limiter=limiter)
        assert wrong.outcome == pins.INVALID and wrong.code == "pin_invalid"
        assert wrong.attempts_left == 4

    def test_malformed_pins_count_as_failures(self, profile, limiter):
        for bad in (None, "12345", "1234567", "abcdef", "24681x"):
            assert pins.check(bad, profile, limiter=limiter).outcome == pins.INVALID
        assert pins.check(SOS_PIN, profile, limiter=limiter).outcome == pins.LOCKED

    def test_five_failures_lock_for_the_window_then_back_off(self, profile, limiter, clock):
        for _ in range(5):
            assert pins.check("000000", profile, limiter=limiter).outcome == pins.INVALID
            clock.now += 10
        locked = pins.check(SOS_PIN, profile, limiter=limiter)
        assert locked.outcome == pins.LOCKED and locked.code == "pin_locked"
        assert locked.retry_after == pytest.approx(600 - 50)
        clock.now += 551                     # the oldest failure leaves the window
        assert pins.check(SOS_PIN, profile, limiter=limiter).outcome == pins.OK
        assert limiter.attempts_left() == 5, "the right PIN clears the failures"

    def test_help_is_never_blocked_duress_works_while_locked(self, profile, limiter):
        for _ in range(5):
            pins.check("000000", profile, limiter=limiter)
        assert pins.check(SOS_PIN, profile, limiter=limiter).outcome == pins.LOCKED
        assert pins.check(DURESS_PIN, profile, limiter=limiter).outcome == pins.DURESS
        # ... within a budget, so the lock is no oracle for the duress PIN.
        for _ in range(10):
            pins.check("999999", profile, limiter=limiter)
        assert pins.check(DURESS_PIN, profile, limiter=limiter).outcome == pins.LOCKED

    def test_the_limiter_is_shared_by_every_caller(self, profile):
        pins.LIMITER.reset()
        try:
            for _ in range(5):
                pins.check("000000", profile)          # the process-wide limiter
            assert pins.LIMITER.locked()
            assert pins.check(SOS_PIN, profile).outcome == pins.LOCKED
        finally:
            pins.LIMITER.reset()

    def test_no_pin_configured(self, limiter):
        assert pins.check(SOS_PIN, {"sos": {}}, limiter=limiter).outcome == pins.UNAVAILABLE
        assert pins.check(SOS_PIN, None, limiter=limiter).code == "unprocessable"
        assert not pins.configured({"sos": {}})

    def test_broken_hashes_never_raise(self, limiter):
        profile = {"sos": {"pin_hash": "not-a-hash", "duress_pin_hash": "$argon2id$v=19$x"}}
        assert pins.check(SOS_PIN, profile, limiter=limiter).outcome == pins.INVALID

    def test_standalone_console_password(self, monkeypatch, limiter):
        monkeypatch.setenv("SENTRIX_PASSWORD", "correct horse")
        profile = {"sos": {"pin_hash": None, "pin_source": "console_password"}}
        assert pins.configured(profile)
        assert pins.check("correct horse", profile, limiter=limiter).outcome == pins.OK
        assert pins.check("wrong", profile, limiter=limiter).outcome == pins.INVALID
        monkeypatch.setenv("SENTRIX_PASSWORD", "")
        assert pins.check("x", profile, limiter=limiter).outcome == pins.UNAVAILABLE

    def test_portal_hashes_verify_on_the_device(self, limiter):
        from sentrix_portal.security import hash_pin
        profile = {"sos": {"pin_hash": hash_pin(SOS_PIN)}}
        assert pins.check(SOS_PIN, profile, limiter=limiter).outcome == pins.OK


class TestDuressRules:
    def test_duress_pin_differs_in_two_digits(self):
        assert pins.digit_difference("246810", "246811") == 1
        assert not pins.duress_pin_acceptable("246810", "246811")
        assert pins.duress_pin_acceptable("246810", "246801")
        assert not pins.duress_pin_acceptable("246810", "12345")
        assert pins.valid_format("000000") and not pins.valid_format("00000")
