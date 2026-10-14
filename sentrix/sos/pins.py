"""The SOS PIN on the device: verification and the shared limiter (docs/7 §5.6).

Every caller that asks for the SOS PIN goes through :func:`check`: declining
or "I'm safe" on the LC or through the tunnel, a FALSE verdict with an open
request, disarming, and the link's ``sos.respond`` / ``verdict.set`` /
``mode.set`` commands. One process-wide :data:`LIMITER` counts the failures,
because the PIN is one secret per household and spreading guesses over
several screens or actors must not multiply the budget.

Rules (decisions D9, review "never a lockout that blocks help"):

* PINs are ``settings.sos.pin_length`` (6) digits, verified against the
  profile's Argon2id hashes (``sentrix_portal.security.hash_pin``) with
  ``argon2-cffi``. A standalone device without a Portal profile uses
  ``SENTRIX_SOS_PIN_HASH`` or, when that is unset, the console password
  (``pin_source: "console_password"``).
* The duress PIN (``duress_pin_hash``) is checked after the SOS PIN; both
  hashes are always verified so the answer time does not tell them apart.
* ``pin_attempts`` (5) wrong PINs in ``pin_window_seconds`` (10 min) lock the
  PIN: declining, "I'm safe" and disarming answer ``pin_locked`` until the
  oldest failure leaves the window. **Help is never blocked**: approving needs
  no PIN, and while the PIN is locked the duress PIN still works (a coerced
  resident can still call for help silently), within a second budget of the
  same size so the lock cannot become an unlimited oracle for the duress PIN.
* The PIN is never logged, stored or put into an event.

The counts live in memory: a restart clears them (documented limitation).
"""

from __future__ import annotations

import hmac
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Mapping

OK, DURESS, INVALID, LOCKED, UNAVAILABLE = "ok", "duress", "invalid", "locked", "unavailable"
#: The limiter key every SOS PIN check shares (one PIN per household).
SHARED_KEY = "sos-pin"


@dataclass(frozen=True)
class PinCheck:
    """``outcome``: ok (the SOS PIN) | duress | invalid | locked | unavailable
    (no PIN configured). ``retry_after``: seconds until a locked PIN accepts
    attempts again."""

    outcome: str
    retry_after: float = 0.0
    attempts_left: int | None = None

    @property
    def code(self) -> str:
        """The docs/7 §13 code a caller answers with when it is not ok/duress."""
        return {INVALID: "pin_invalid", LOCKED: "pin_locked",
                UNAVAILABLE: "unprocessable"}.get(self.outcome, "ok")


class PinLimiter:
    """Sliding-window failure counter, per key.

    ``failures`` within ``window`` seconds lock the key until the oldest one
    expires. A second budget (``locked_budget`` evaluations per window) bounds
    what may still be checked while locked (the duress PIN)."""

    def __init__(self, attempts: int = 5, window: float = 600.0, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.attempts = int(attempts)
        self.window = float(window)
        self.locked_budget = int(attempts)
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._locked_checks: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _trim(self, hits: deque[float], now: float) -> None:
        while hits and now - hits[0] >= self.window:
            hits.popleft()

    def retry_after(self, key: str = SHARED_KEY) -> float:
        """0 when attempts are allowed, else seconds until they are."""
        now = self._clock()
        with self._lock:
            hits = self._failures.get(key)
            if not hits:
                return 0.0
            self._trim(hits, now)
            if len(hits) < self.attempts:
                return 0.0
            return max(0.0, self.window - (now - hits[0]))

    def locked(self, key: str = SHARED_KEY) -> bool:
        return self.retry_after(key) > 0

    def attempts_left(self, key: str = SHARED_KEY) -> int:
        now = self._clock()
        with self._lock:
            hits = self._failures.get(key) or deque()
            self._trim(hits, now)
            return max(0, self.attempts - len(hits))

    def failure(self, key: str = SHARED_KEY) -> None:
        now = self._clock()
        with self._lock:
            hits = self._failures.setdefault(key, deque())
            self._trim(hits, now)
            hits.append(now)

    def success(self, key: str = SHARED_KEY) -> None:
        """A correct SOS PIN clears the failures (the resident is back)."""
        with self._lock:
            self._failures.pop(key, None)
            self._locked_checks.pop(key, None)

    def allow_locked_check(self, key: str = SHARED_KEY) -> bool:
        """While locked: may one more PIN be evaluated (for the duress PIN)?"""
        now = self._clock()
        with self._lock:
            hits = self._locked_checks.setdefault(key, deque())
            self._trim(hits, now)
            if len(hits) >= self.locked_budget:
                return False
            hits.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()
            self._locked_checks.clear()


def _limiter_from_settings() -> PinLimiter:
    from sentrix.config import settings
    return PinLimiter(settings.sos.pin_attempts, settings.sos.pin_window_seconds)


#: The process-wide limiter every caller shares.
LIMITER = _limiter_from_settings()

_HASHER = None
_HASHER_LOCK = threading.Lock()


def _hasher():
    """The Portal's parameters (Argon2id m=19 MiB, t=2, p=1: docs/7 §11)."""
    global _HASHER
    with _HASHER_LOCK:
        if _HASHER is None:
            from argon2 import PasswordHasher
            _HASHER = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1,
                                     hash_len=32, salt_len=16)
        return _HASHER


def _verify_hash(pin_hash: str | None, pin: str) -> bool:
    """Argon2id verify; any malformed hash or mismatch is False (never raises).
    Parameters come from the hash itself, so the Portal's m=19 MiB, t=2
    hashes verify with a default hasher."""
    if not pin_hash:
        return False
    from argon2.exceptions import InvalidHashError, VerificationError
    try:
        return bool(_hasher().verify(pin_hash, pin))
    except (VerificationError, InvalidHashError, ValueError, TypeError):
        return False


def hash_pin(pin: str) -> str:
    """Argon2id hash of a PIN (tests, ``SENTRIX_SOS_PIN_HASH`` set-up). The
    Portal hashes with ``sentrix_portal.security.hash_pin``; either verifies."""
    return _hasher().hash(pin)


def valid_format(pin: str | None, length: int | None = None) -> bool:
    if length is None:
        from sentrix.config import settings
        length = settings.sos.pin_length
    return bool(pin) and re.fullmatch(rf"\d{{{int(length)}}}", str(pin)) is not None


def pin_material(profile: Mapping[str, Any] | None) -> dict[str, Any]:
    """What a check needs from the profile: the hashes, or the console
    password for a standalone device. Never logged."""
    sos = (profile or {}).get("sos") if isinstance(profile, Mapping) else None
    sos = sos if isinstance(sos, Mapping) else {}
    source = sos.get("pin_source") or ("profile" if sos.get("pin_hash") else "none")
    material = {"source": source, "pin_hash": sos.get("pin_hash"),
                "duress_pin_hash": sos.get("duress_pin_hash"), "password": None}
    if source == "console_password":
        material["password"] = os.getenv("SENTRIX_PASSWORD", "").strip() or None
    return material


def configured(profile: Mapping[str, Any] | None) -> bool:
    material = pin_material(profile)
    return bool(material["pin_hash"] or material["password"])


def check(pin: str | None, profile: Mapping[str, Any] | None, *,
          limiter: PinLimiter | None = None, key: str = SHARED_KEY,
          length: int | None = None) -> PinCheck:
    """Verify ``pin`` against the household's SOS and duress PINs.

    Counts a wrong or malformed PIN as a failure, clears the failures on the
    SOS PIN, and answers ``locked`` (with ``retry_after``) when the limiter is
    locked; the duress PIN is still honoured then (help is never blocked).
    """
    limiter = limiter or LIMITER
    material = pin_material(profile)
    if not (material["pin_hash"] or material["password"]):
        return PinCheck(UNAVAILABLE)
    wait = limiter.retry_after(key)
    candidate = "" if pin is None else str(pin).strip()

    if material["password"] is not None and not material["pin_hash"]:
        # Standalone without a PIN hash: the console password stands in.
        if wait > 0:
            return PinCheck(LOCKED, retry_after=wait, attempts_left=0)
        if candidate and hmac.compare_digest(candidate.encode(), material["password"].encode()):
            limiter.success(key)
            return PinCheck(OK)
        limiter.failure(key)
        return PinCheck(INVALID, attempts_left=limiter.attempts_left(key))

    if wait > 0:
        if (material["duress_pin_hash"] and valid_format(candidate, length)
                and limiter.allow_locked_check(key)
                and _verify_hash(material["duress_pin_hash"], candidate)):
            return PinCheck(DURESS)
        return PinCheck(LOCKED, retry_after=wait, attempts_left=0)

    if not valid_format(candidate, length):
        limiter.failure(key)
        return PinCheck(INVALID, attempts_left=limiter.attempts_left(key))
    # Always both, so the time taken does not say which one matched.
    is_sos = _verify_hash(material["pin_hash"], candidate)
    is_duress = _verify_hash(material["duress_pin_hash"], candidate)
    if is_sos:
        limiter.success(key)
        return PinCheck(OK)
    if is_duress:
        return PinCheck(DURESS)
    limiter.failure(key)
    return PinCheck(INVALID, attempts_left=limiter.attempts_left(key))


def digit_difference(a: str, b: str) -> int:
    """Positions where two equal-length PINs differ (a duress PIN must differ
    from the SOS PIN in at least ``duress_min_digit_difference`` digits)."""
    if len(a) != len(b):
        return max(len(a), len(b))
    return sum(1 for x, y in zip(a, b) if x != y)


def duress_pin_acceptable(sos_pin: str, duress_pin: str, *, minimum: int | None = None,
                          length: int | None = None) -> bool:
    """The Portal's settings page and onboarding validate a new duress PIN
    with this (``settings.sos.duress_min_digit_difference``, default 2)."""
    if minimum is None:
        from sentrix.config import settings
        minimum = settings.sos.duress_min_digit_difference
    return (valid_format(sos_pin, length) and valid_format(duress_pin, length)
            and digit_difference(sos_pin, duress_pin) >= minimum)


__all__ = ["PinCheck", "PinLimiter", "LIMITER", "SHARED_KEY", "check", "configured",
           "pin_material", "hash_pin", "valid_format", "digit_difference",
           "duress_pin_acceptable", "OK", "DURESS", "INVALID", "LOCKED", "UNAVAILABLE"]
