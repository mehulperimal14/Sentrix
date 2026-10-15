"""The send guard: the one gate every outbound message passes (decision D13).

``.env`` in a developer checkout may hold live Twilio keys, a bot token and an
e-mail API key, and the modem holds a real SIM. A test, a replay, a benchmark
or a rehearsal must never reach a real person by accident. So every transport
asks this module first, on every message, and the module re-reads the
environment each time (nothing here is cached).

Rules, in order (the first that applies decides):

1. Unknown channel, empty or malformed address -> refused.
2. An emergency number (112, 100, 101, 102, 108, ...) -> always refused:
   SENTRIX never contacts emergency services itself.
3. ``SENTRIX_LIVE_ALERTS`` is not 1 -> refused ("dry run"). Callers record the
   rendered message as SUPPRESSED, so a demo can still show what would be sent.
4. The channel's own switch (``CHANNEL_SWITCHES``) is not 1 -> refused.
5. Test mode (``SENTRIX_TEST_MODE``, **on unless set to 0**) and the address is
   not in ``SENTRIX_TEST_RECIPIENTS`` -> refused. The device enforces this
   itself; it never relies on the Portal's copy of the list.
6. Otherwise allowed. In test mode the text must start with the test banner
   (:func:`prepare_text` adds it; :func:`assert_may_send` checks it).

Stdlib only: the Portal imports it with ``SendPolicy.from_env(
test_mode_env="PORTAL_TEST_MODE")``.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

from sentrix.utils.redact import mask_recipient

#: Every test message starts with this (decision D13).
TEST_PREFIX = "[SENTRIX DEMO – TEST, NOT A REAL EMERGENCY]"
#: The same banner in GSM-7 characters (an en dash forces UCS-2: 70 chars/SMS).
TEST_PREFIX_GSM = "[SENTRIX DEMO - TEST, NOT A REAL EMERGENCY]"

#: Channel name -> its own live switch. Both this and SENTRIX_LIVE_ALERTS must
#: be on for anything to leave the machine.
CHANNEL_SWITCHES: dict[str, str] = {
    "modem_sms": "SENTRIX_MODEM_LIVE",
    "modem_call": "SENTRIX_MODEM_LIVE",
    "twilio_sms": "SENTRIX_TWILIO_ENABLED",
    "twilio_call": "SENTRIX_TWILIO_ENABLED",
    "telegram": "SENTRIX_TELEGRAM_LIVE",
    "email": "SENTRIX_EMAIL_LIVE",
    "webpush": "SENTRIX_WEBPUSH_LIVE",
}
#: Channels whose text travels as SMS (GSM-7 banner).
SMS_CHANNELS = frozenset({"modem_sms", "twilio_sms"})
PHONE_CHANNELS = frozenset({"modem_sms", "modem_call", "twilio_sms", "twilio_call"})

#: Indian emergency and helpline short codes; never a SENTRIX recipient.
EMERGENCY_NUMBERS = frozenset({"112", "100", "101", "102", "108", "1091", "1098",
                               "1070", "1078", "1930", "911", "999"})

_E164 = re.compile(r"^\+[1-9]\d{6,14}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_TG = re.compile(r"^tg:\d{1,20}$")


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


_FALSY = ("0", "false", "no", "off")


def test_mode_flag(name: str = "SENTRIX_TEST_MODE") -> bool:
    """Test mode is a default-ON safety switch: it fails closed.

    Only an explicit falsy value ("0", "false", "no", "off") turns it off. Any
    other value (a typo such as ``y``, ``ture`` or ``enabled``) keeps test mode
    on and logs a warning (D13).
    """
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return True
    if raw in _FALSY:
        return False
    if raw not in ("1", "true", "yes", "on"):
        logging.getLogger("sentrix.guard").warning(
            "%s=%r is not a recognised value; test mode stays ON", name, raw)
    return True


def normalize_address(channel: str, address: str | None) -> str:
    """Canonical form used for the allowlist and dedupe keys.

    Phones: E.164 without spaces or dashes (a bare 10-digit Indian mobile gets
    ``+91``). E-mail: lower case. Telegram: ``tg:<chat id>``.
    """
    text = (address or "").strip()
    if not text:
        return ""
    if channel in PHONE_CHANNELS:
        digits = re.sub(r"[\s\-().]", "", text)
        if re.fullmatch(r"[6-9]\d{9}", digits):
            digits = "+91" + digits
        return digits
    if channel in ("email",):
        return text.lower()
    if channel == "telegram":
        return text if text.startswith("tg:") else f"tg:{text}"
    return text


def _canonical_entry(entry: str) -> str:
    text = entry.strip()
    if not text:
        return ""
    if "@" in text:
        return text.lower()
    if text.startswith("tg:"):
        return text
    digits = re.sub(r"[\s\-().]", "", text)
    if re.fullmatch(r"[6-9]\d{9}", digits):
        digits = "+91" + digits
    return digits


def _valid(channel: str, address: str) -> bool:
    if channel in PHONE_CHANNELS:
        return bool(_E164.match(address)) or address.lstrip("+") in EMERGENCY_NUMBERS
    if channel == "email":
        return bool(_EMAIL.match(address))
    if channel == "telegram":
        return bool(_TG.match(address))
    return bool(address)


def is_emergency_number(address: str | None) -> bool:
    digits = re.sub(r"[^\d]", "", address or "")
    if digits.startswith("91") and len(digits) <= 6:
        digits = digits[2:]
    return digits in EMERGENCY_NUMBERS


@dataclass(frozen=True)
class SendPolicy:
    """The switches as they are right now. Build a fresh one per send."""

    test_mode: bool = True
    live_alerts: bool = False
    switches: dict = field(default_factory=dict)
    allowlist: frozenset = frozenset()

    @classmethod
    def from_env(cls, test_mode_env: str = "SENTRIX_TEST_MODE") -> "SendPolicy":
        raw = os.getenv("SENTRIX_TEST_RECIPIENTS", "")
        # Entries are separated by commas, semicolons or newlines; spaces inside
        # a phone number ("98123 45610") are allowed.
        entries = (_canonical_entry(x) for x in re.split(r"[,;\n]+", raw))
        allow = frozenset(e for e in entries if e)
        return cls(
            test_mode=test_mode_flag(test_mode_env),
            live_alerts=_flag("SENTRIX_LIVE_ALERTS", False),
            switches={ch: _flag(env, False) for ch, env in CHANNEL_SWITCHES.items()},
            allowlist=allow,
        )

    def channel_live(self, channel: str) -> bool:
        return self.live_alerts and bool(self.switches.get(channel, False))


@dataclass(frozen=True)
class Decision:
    """Outcome of one guard check. Truthy when the message may be sent."""

    allowed: bool
    reason: str
    channel: str
    recipient_masked: str
    test_mode: bool

    def __bool__(self) -> bool:
        return self.allowed

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason, "channel": self.channel,
                "recipient": self.recipient_masked, "test_mode": self.test_mode}


class SendRefused(RuntimeError):
    """Raised by :func:`assert_may_send`; ``decision`` says why."""

    def __init__(self, decision: Decision) -> None:
        super().__init__(f"send refused ({decision.channel} -> "
                         f"{decision.recipient_masked}): {decision.reason}")
        self.decision = decision


def may_send(channel: str, recipient: str | None, *,
             policy: SendPolicy | None = None) -> Decision:
    """May a message on ``channel`` go to ``recipient`` now? Never raises."""
    policy = policy or SendPolicy.from_env()
    address = normalize_address(channel, recipient)
    masked = mask_recipient(address)

    def decide(allowed: bool, reason: str) -> Decision:
        return Decision(allowed, reason, channel, masked, policy.test_mode)

    if channel not in CHANNEL_SWITCHES:
        return decide(False, f"unknown channel {channel!r}")
    if not address or not _valid(channel, address):
        return decide(False, "missing or malformed recipient address")
    if channel in PHONE_CHANNELS and is_emergency_number(address):
        return decide(False, "SENTRIX never contacts emergency numbers itself")
    if not policy.live_alerts:
        return decide(False, "SENTRIX_LIVE_ALERTS is off (dry run)")
    if not policy.switches.get(channel, False):
        return decide(False, f"{CHANNEL_SWITCHES[channel]} is off")
    if policy.test_mode and address not in policy.allowlist:
        return decide(False, "test mode: recipient is not in SENTRIX_TEST_RECIPIENTS")
    return decide(True, "test mode: allowlisted recipient" if policy.test_mode
                  else "live")


def prefix_for(channel: str) -> str:
    return TEST_PREFIX_GSM if channel in SMS_CHANNELS else TEST_PREFIX


def prepare_text(channel: str, text: str, *, policy: SendPolicy | None = None) -> str:
    """The text as it must be sent: in test mode it starts with the banner."""
    policy = policy or SendPolicy.from_env()
    if not policy.test_mode:
        return text
    banner = prefix_for(channel)
    if text.startswith(TEST_PREFIX) or text.startswith(TEST_PREFIX_GSM):
        return text
    return f"{banner} {text}"


def assert_may_send(channel: str, recipient: str | None, text: str | None = None, *,
                    policy: SendPolicy | None = None) -> Decision:
    """Like :func:`may_send` but raises :class:`SendRefused`; in test mode the
    text (when given) must already carry the banner."""
    policy = policy or SendPolicy.from_env()
    decision = may_send(channel, recipient, policy=policy)
    if not decision:
        raise SendRefused(decision)
    if policy.test_mode and text is not None and not (
            text.startswith(TEST_PREFIX) or text.startswith(TEST_PREFIX_GSM)):
        raise SendRefused(Decision(False, "test mode: text lacks the test banner",
                                   channel, decision.recipient_masked, True))
    return decision


def test_mode(policy: SendPolicy | None = None) -> bool:
    return (policy or SendPolicy.from_env()).test_mode


test_mode.__test__ = False   # a helper, not a pytest test


def status(policy: SendPolicy | None = None) -> dict:
    """For /api/health, the Node page and ``doctor``: no addresses, counts only."""
    policy = policy or SendPolicy.from_env()
    return {
        "test_mode": policy.test_mode,
        "live_alerts": policy.live_alerts,
        "channels_live": {ch: policy.channel_live(ch) for ch in CHANNEL_SWITCHES},
        "test_recipients": len(policy.allowlist),
    }


__all__ = ["TEST_PREFIX", "TEST_PREFIX_GSM", "CHANNEL_SWITCHES", "SMS_CHANNELS",
           "PHONE_CHANNELS", "EMERGENCY_NUMBERS", "SendPolicy", "Decision",
           "SendRefused", "may_send", "assert_may_send", "prepare_text",
           "prefix_for", "normalize_address", "is_emergency_number",
           "test_mode", "status"]
