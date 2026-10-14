"""USB 4G modem: SMS out and a ringing call over AT commands (docs/7 §8.3, Tier 1).

The modem is the device's own way out when the Internet is down: the offline
SOS prompt to the owner, the SOS to confirmed contacts and to the Desk number,
the link's ``sms.send`` and, optionally, a ringing L5 call. It sits behind the
channel router like every other transport, so the send guard decides first
(dry run by default; ``SENTRIX_LIVE_ALERTS=1`` *and* ``SENTRIX_MODEM_LIVE=1``;
in test mode only allowlisted numbers). Two device rules apply on top, here,
whatever the Portal says (docs/7 §5.6):

* only numbers in the current profile (owner, CONFIRMED contacts, Desk:
  ``sentrix.link.profile.sms_allowlist``);
* at most ``link.sms_daily_cap`` (30) SMS segments per local day.

Layers:

* :class:`AtModem` — the AT command layer over a serial-like port (pyserial,
  or a fake in tests): ``AT``/``ATE0``/``AT+CMEE``, ``AT+CMGF``, ``AT+CSCS``,
  ``AT+CSMP``, ``AT+CNMI``, ``AT+CMGS`` with the ``>`` prompt and Ctrl-Z,
  ``AT+CSQ`` (signal), ``AT+CREG?``/``AT+CEREG?`` (registration),
  ``AT+CPIN?``, ``AT+COPS?``, ``ATD…;``/``ATH`` (ringing call), and
  ``AT+CMGL``/``AT+CMGD`` (the inbound read API kept ready for Tier 2 reply
  parsing). Unsolicited results (``+CDS`` status reports, ``+CMTI`` new SMS,
  ``RING``) are separated from command responses.
* SMS are sent in **PDU mode** by default: GSM-7 (with the extension table)
  when every character allows it, UCS-2 otherwise (Hindi), concatenated with
  an 8-bit reference header beyond one segment, a status report requested and
  24 h validity. Text mode (``AT+CMGF=1``, ASCII, one SMS per 153-character
  part) remains for modules without PDU support. The modem idles in text mode
  (``AT+CMGL`` output is readable).
* :class:`ModemWorker` — owns the port on its own thread: connects (probing
  the serial ports when ``SENTRIX_MODEM_PORT`` is empty), keeps signal and
  registration fresh, reads unsolicited results, reconnects with back-off.
  The router's thread sends through it under the modem's lock.
* :class:`MmcliWorker` — the same interface over ModemManager's ``mmcli``
  (``SENTRIX_MODEM_PORT=mmcli`` or ``mmcli:<index>``), for a Pi where
  ModemManager already owns the port.
* :class:`ModemSmsTransport`, :class:`ModemCallTransport` — the channel
  transports (``modem_sms``, ``modem_call``) with the two device rules.

Delivery reports: a ``+CDS`` report is queued by the worker
(:meth:`ModemWorker.take_reports`) and applied by the channels service
outside the router's lock (``Router.confirm_delivery``): a SENT SMS becomes
DELIVERED, or FAILED when the network gave up.

Jio is VoLTE-only: SMS needs IMS registration, which some LTE modules lack
(portal review). ``status()`` reports registration so a demo checks it first.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable

from sentrix.alerting import guard
from sentrix.alerting.channels.base import Message, Recipient, SendResult

log = logging.getLogger("sentrix.channels.modem")

CTRL_Z = b"\x1a"
ESC = b"\x1b"

#: Final results that end a voice dial (they are unsolicited after a call).
CALL_FINALS = ("NO CARRIER", "BUSY", "NO ANSWER", "NO DIALTONE")
#: Unsolicited result codes the driver understands; anything else in a
#: response is response text.
URC_PREFIXES = ("+CDS:", "+CDSI:", "+CMTI:", "+CMT:", "RING", "+CLIP:", "+CUSD:",
                "VOICE CALL:", "NO CARRIER")
#: +CMS ERROR causes a retry cannot fix (unassigned number, unidentified
#: subscriber, invalid mandatory information, invalid PDU / text parameter).
PERMANENT_CMS_ERRORS = frozenset({1, 28, 96, 304, 305})
#: Description fragments of serial ports that are modem AT ports (probe order).
MODEM_PORT_HINTS = ("at port", "at interface", "modem", "simcom", "simtech", "quectel",
                    "huawei", "zte", "sierra", "telit", "fibocom", "u-blox", "gsm", "lte")
#: Relative validity 167 = 24 hours (TS 23.040 §9.2.3.12.1).
VALIDITY_24H = 0xA7
MAX_PARTS = 6

_NUMBER = re.compile(r"^\+?\d{6,15}$")


# ── errors ────────────────────────────────────────────────────────────────────
class ModemUnavailable(RuntimeError):
    """No modem is connected (unplugged, not found, not initialised)."""


class AtError(RuntimeError):
    """A command answered ERROR / +CME ERROR / +CMS ERROR, or timed out."""

    def __init__(self, command: str, response: str, *, sent_parts: int = 0,
                 permanent: bool = False) -> None:
        super().__init__(f"{command.strip()} -> {response}")
        self.command = command.strip()
        self.response = response
        self.sent_parts = sent_parts
        self.permanent = permanent

    @property
    def code(self) -> int | None:
        match = re.search(r"\+CM[ES] ERROR:\s*(\d+)", self.response)
        return int(match.group(1)) if match else None

    @property
    def retryable(self) -> bool:
        if self.permanent:
            return False
        return not (self.response.startswith("+CMS ERROR")
                    and self.code in PERMANENT_CMS_ERRORS)


# ── GSM 03.38 / TS 23.040 SMS-SUBMIT encoding ─────────────────────────────────
#: The GSM 7-bit default alphabet; index = code. 0x1B is the escape to the
#: extension table.
GSM7_BASIC = ("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
              "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
GSM7_EXTENSION = {"\f": 0x0A, "^": 0x14, "{": 0x28, "}": 0x29, "\\": 0x2F, "[": 0x3C,
                  "~": 0x3D, "]": 0x3E, "|": 0x40, "€": 0x65}
_GSM7_INDEX = {ch: i for i, ch in enumerate(GSM7_BASIC) if ch != "\x1b"}
#: Typographic characters that would force UCS-2 (70 characters per SMS)
#: for no reason; replaced when the rest of the text is GSM-7.
SMS_TRANSLITERATION = {"–": "-", "—": "-", "‐": "-", "−": "-", "‘": "'", "’": "'",
                       "‚": ",", "“": '"', "”": '"', "„": '"', "…": "...", "·": "-",
                       "•": "-", " ": " ", " ": " ", " ": " ", "\t": " ",
                       "₹": "Rs."}


def transliterate(text: str) -> str:
    return "".join(SMS_TRANSLITERATION.get(ch, ch) for ch in text)


def gsm7_septets(text: str) -> list[int] | None:
    """Septet codes of ``text``, or None when a character is not in GSM-7."""
    out: list[int] = []
    for ch in text:
        code = _GSM7_INDEX.get(ch)
        if code is not None:
            out.append(code)
            continue
        ext = GSM7_EXTENSION.get(ch)
        if ext is None:
            return None
        out.extend((0x1B, ext))
    return out


def pack_septets(septets: list[int], fill_bits: int = 0) -> bytes:
    """Pack 7-bit codes LSB first, after ``fill_bits`` zero bits (UDH padding)."""
    out = bytearray()
    acc, bits = 0, fill_bits
    for septet in septets:
        acc |= (septet & 0x7F) << bits
        bits += 7
        while bits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            bits -= 8
    if bits:
        out.append(acc & 0xFF)
    return bytes(out)


def encode_address(number: str) -> bytes:
    """TP-DA: digit count, type of number, swapped semi-octets."""
    if not _NUMBER.match(number or ""):
        raise ValueError("phone number must be E.164 digits")
    digits = number.lstrip("+")
    kind = 0x91 if number.startswith("+") else 0x81
    padded = digits + ("F" if len(digits) % 2 else "")
    swapped = "".join(padded[i + 1] + padded[i] for i in range(0, len(padded), 2))
    return bytes([len(digits), kind]) + bytes.fromhex(swapped)


def _split_septets(septets: list[int], size: int) -> list[list[int]]:
    parts, current = [], []
    i = 0
    while i < len(septets):
        width = 2 if septets[i] == 0x1B else 1          # never split an escape pair
        if len(current) + width > size:
            parts.append(current)
            current = []
        current.extend(septets[i:i + width])
        i += width
    if current:
        parts.append(current)
    return parts


def _split_ucs2(text: str, units: int) -> list[bytes]:
    parts, current, used = [], bytearray(), 0
    for ch in text:
        encoded = ch.encode("utf-16-be")                  # 2 or 4 bytes (surrogates)
        width = len(encoded) // 2
        if used + width > units:
            parts.append(bytes(current))
            current, used = bytearray(), 0
        current.extend(encoded)
        used += width
    if current:
        parts.append(bytes(current))
    return parts


def _limit(text: str, capacity: int, measure: Callable[[str], int]) -> str:
    """Cut ``text`` so it fits ``capacity`` units, ending with "..."."""
    if measure(text) <= capacity:
        return text
    cut = text[:capacity]                           # every character is >= 1 unit
    while cut and measure(cut + "...") > capacity:
        cut = cut[:-1]
    return cut + "..."


def sms_parts(text: str, *, max_parts: int = MAX_PARTS) -> tuple[str, list[Any]]:
    """("gsm7" | "ucs2", segments). GSM-7 segments are septet lists (160, or
    153 each when concatenated), UCS-2 segments UTF-16-BE bytes (70 / 67
    characters). Text beyond ``max_parts`` segments is cut with "..."."""
    plain = transliterate(text)
    septets = gsm7_septets(plain)
    if septets is not None:
        if len(septets) <= 160:
            return "gsm7", [septets]
        plain = _limit(plain, 153 * max_parts, lambda t: len(gsm7_septets(t) or ()))
        return "gsm7", _split_septets(gsm7_septets(plain) or [], 153)
    if len(text.encode("utf-16-be")) <= 140:
        return "ucs2", [text.encode("utf-16-be")]
    text = _limit(text, 67 * max_parts, lambda t: len(t.encode("utf-16-be")) // 2)
    return "ucs2", _split_ucs2(text, 67)


def encode_sms_submit(number: str, text: str, *, reference: int = 0,
                      status_report: bool = True, validity: int = VALIDITY_24H,
                      max_parts: int = MAX_PARTS) -> list[tuple[int, str]]:
    """SMS-SUBMIT PDUs for ``AT+CMGS``: ``[(tpdu_length, hex)]``. The hex
    starts with ``00`` (use the SIM's SMSC); the length excludes it."""
    coding, segments = sms_parts(text, max_parts=max_parts)
    total = len(segments)
    address = encode_address(number)
    out = []
    for index, segment in enumerate(segments, 1):
        first = 0x01 | 0x10                         # SMS-SUBMIT, relative validity
        if status_report:
            first |= 0x20
        udh = b""
        if total > 1:
            first |= 0x40
            udh = bytes([0x05, 0x00, 0x03, reference & 0xFF, total, index])
        if coding == "gsm7":
            fill = (7 - (len(udh) * 8) % 7) % 7 if udh else 0
            body = udh + pack_septets(segment, fill)
            length = (len(udh) * 8 + fill) // 7 + len(segment)
            dcs = 0x00
        else:
            body = udh + segment
            length = len(body)
            dcs = 0x08
        tpdu = bytes([first, 0x00]) + address + bytes([0x00, dcs, validity, length]) + body
        out.append((len(tpdu), "00" + tpdu.hex().upper()))
    return out


def text_mode_parts(text: str, *, max_parts: int = MAX_PARTS) -> list[str]:
    """Text-mode fallback: printable ASCII, one SMS per part, "(i/n) " numbered."""
    plain = "".join(ch if 32 <= ord(ch) < 127 or ch == "\n" else "?"
                    for ch in transliterate(text))
    if len(plain) <= 160:
        return [plain]
    plain = _limit(plain, 147 * max_parts, len)
    chunks = [plain[i:i + 147] for i in range(0, len(plain), 147)]
    return [f"({i}/{len(chunks)}) {chunk}" for i, chunk in enumerate(chunks, 1)]


def decode_status_report(pdu_hex: str) -> tuple[int, int]:
    """(message reference, TP-ST) of an SMS-STATUS-REPORT PDU (with SCA)."""
    data = bytes.fromhex(pdu_hex.strip())
    i = 1 + data[0]                                 # skip the SMSC address
    first = data[i]
    if first & 0x03 != 0x02:
        raise ValueError("not an SMS-STATUS-REPORT")
    mr = data[i + 1]
    digits = data[i + 2]
    i += 3 + 1 + (digits + 1) // 2                  # length, type, semi-octets
    i += 7 + 7                                      # service centre time, discharge time
    return mr, data[i]


def report_outcome(status: int) -> str:
    """TP-ST (TS 23.040 §9.2.3.15): delivered | pending | failed."""
    if status < 0x20:
        return "delivered"
    if status < 0x40:
        return "pending"                            # the SMSC is still trying
    return "failed"


def _fields(text: str) -> list[str]:
    """Comma-separated AT fields; commas inside quotes kept."""
    out, current, quoted = [], [], False
    for ch in text:
        if ch == '"':
            quoted = not quoted
        elif ch == "," and not quoted:
            out.append("".join(current).strip())
            current = []
            continue
        current.append(ch)
    out.append("".join(current).strip())
    return [f[1:-1] if len(f) >= 2 and f[0] == f[-1] == '"' else f for f in out]


@dataclass(frozen=True)
class InboundSms:
    """One stored SMS from ``AT+CMGL`` (Tier 2 reply parsing reads these)."""

    index: int
    status: str
    sender: str
    received: str
    text: str


# ── AT command layer ──────────────────────────────────────────────────────────
class AtModem:
    """AT commands over a serial-like port (``read(n)``, ``write(b)``,
    ``in_waiting``, ``close()``). Thread-safe: one command at a time."""

    def __init__(self, port: Any, *, sms_mode: str = "pdu",
                 on_urc: Callable[[str, list[str]], None] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 command_timeout: float = 5.0) -> None:
        if sms_mode not in ("pdu", "text"):
            raise ValueError("sms_mode must be pdu or text")
        self.port = port
        self.sms_mode = sms_mode
        self.command_timeout = command_timeout
        self.status_reports = False
        self._on_urc = on_urc
        self._clock = clock
        self._buffer = bytearray()
        self._lock = threading.RLock()
        self._reference = int(time.time()) & 0xFF

    # ── I/O ──────────────────────────────────────────────────────────────────
    def _write(self, data: bytes) -> None:
        self.port.write(data)
        flush = getattr(self.port, "flush", None)
        if flush is not None:
            flush()

    def _fill(self) -> None:
        waiting = getattr(self.port, "in_waiting", 0) or 1
        chunk = self.port.read(waiting)
        if chunk:
            self._buffer.extend(chunk)

    def _readline(self, deadline: float) -> str | None:
        """The next non-empty line, or None at the deadline (the port is read
        at least once, so a zero timeout still picks up waiting data)."""
        filled = False
        while True:
            idx = self._buffer.find(b"\n")
            if idx >= 0:
                raw = bytes(self._buffer[:idx])
                del self._buffer[:idx + 1]
                line = raw.decode("ascii", "replace").strip()
                if line:
                    return line
                continue
            if filled and self._clock() >= deadline:
                return None
            self._fill()
            filled = True

    @staticmethod
    def _is_error(line: str) -> bool:
        return line == "ERROR" or line.startswith(("+CME ERROR", "+CMS ERROR"))

    @staticmethod
    def _is_urc(line: str, command: str, finals: tuple[str, ...] = ()) -> bool:
        if line in finals:
            return False
        if not line.startswith(URC_PREFIXES):
            return False
        # A query answers with its own prefix (AT+CMGL -> +CMGL:); +CMT: vs
        # +CMTI: and +CDS: are never command answers here.
        prefix = line.split(":", 1)[0]
        return not (prefix.startswith("+") and command.upper().startswith("AT" + prefix))

    def _dispatch_urc(self, line: str, deadline: float) -> None:
        extra: list[str] = []
        head = line.split(":", 1)
        body = head[1].strip() if len(head) > 1 else ""
        # Two-line results: +CMT (header, text) and a PDU-mode +CDS (length, PDU).
        if line.startswith("+CMT:") or (line.startswith("+CDS:") and re.fullmatch(r"\d+", body)):
            follow = self._readline(min(deadline, self._clock() + 1.0))
            if follow is not None:
                extra.append(follow)
        if self._on_urc is not None:
            try:
                self._on_urc(line, extra)
            except Exception as exc:                 # never break a command
                log.error("modem URC handler failed: %s", exc)

    def _collect(self, command: str, deadline: float,
                 finals: tuple[str, ...] = ()) -> list[str]:
        lines: list[str] = []
        while True:
            line = self._readline(deadline)
            if line is None:
                raise AtError(command, "timeout")
            if line == command.strip():                 # echo (before ATE0)
                continue
            if line == "OK":
                return lines
            if self._is_error(line):
                raise AtError(command, line)
            if line in finals:
                return lines + [line]
            if self._is_urc(line, command, finals):
                self._dispatch_urc(line, deadline)
                continue
            lines.append(line)

    def command(self, command: str, *, timeout: float | None = None,
                finals: tuple[str, ...] = ()) -> list[str]:
        """Send one command; returns the response lines (without OK).
        Raises :class:`AtError` on ERROR / +CME / +CMS or a timeout."""
        with self._lock:
            self._write(command.encode("ascii") + b"\r")
            return self._collect(command, self._clock() + (timeout or self.command_timeout),
                                 finals)

    def _optional(self, command: str, timeout: float | None = None) -> bool:
        try:
            self.command(command, timeout=timeout)
            return True
        except AtError as exc:
            log.info("modem: %s not supported (%s)", command, exc.response)
            return False

    def _wait_prompt(self, command: str, deadline: float) -> None:
        """Wait for the ``>`` prompt after ``AT+CMGS``; ESC cancels on failure."""
        while True:
            while self._buffer[:1] in (b"\r", b"\n"):
                del self._buffer[:1]
            if self._buffer[:1] == b">":
                del self._buffer[:1]
                if self._buffer[:1] == b" ":
                    del self._buffer[:1]
                return
            idx = self._buffer.find(b"\n")
            if idx >= 0:
                line = bytes(self._buffer[:idx]).decode("ascii", "replace").strip()
                del self._buffer[:idx + 1]
                if self._is_error(line):
                    raise AtError(command, line)
                if line and self._is_urc(line, command):
                    self._dispatch_urc(line, deadline)
                continue
            if self._clock() >= deadline:
                self._write(ESC)
                raise AtError(command, "timeout waiting for the > prompt")
            self._fill()

    def _submit(self, command: str, payload: bytes, timeout: float = 60.0) -> int:
        """``AT+CMGS``: prompt, payload, Ctrl-Z; returns the message reference."""
        self._write(command.encode("ascii") + b"\r")
        self._wait_prompt(command, self._clock() + 10.0)
        self._write(payload + CTRL_Z)
        for line in self._collect(command, self._clock() + timeout):
            match = re.match(r"\+CMGS:\s*(\d+)", line)
            if match:
                return int(match.group(1))
        raise AtError(command, "no +CMGS message reference")

    # ── set-up and status ────────────────────────────────────────────────────
    def sync(self, attempts: int = 3) -> bool:
        """``AT`` until OK (an ESC first cancels a prompt left by a crash)."""
        with self._lock:
            self._write(ESC)
            for _ in range(attempts):
                try:
                    self.command("AT", timeout=1.0)
                    return True
                except AtError:
                    self._buffer.clear()
            return False

    def initialise(self) -> None:
        """Echo off, verbose errors, text mode at rest, IRA character set,
        status reports routed as ``+CDS`` URCs (when the module allows)."""
        with self._lock:
            if not self.sync():
                raise ModemUnavailable("no answer to AT")
            self.command("ATE0")
            self._optional("AT+CMEE=2")
            self.command("AT+CMGF=1")
            self._optional('AT+CSCS="IRA"')
            self._optional("AT+CSMP=49,167,0,0")
            self.status_reports = self._optional("AT+CNMI=2,1,0,1,0")

    def sim_ready(self) -> bool:
        try:
            return any("READY" in line for line in self.command("AT+CPIN?"))
        except AtError:
            return False

    def signal(self) -> dict[str, Any]:
        """``AT+CSQ``: rssi 0..31 -> dBm = -113 + 2 * rssi; 99 = unknown."""
        for line in self.command("AT+CSQ"):
            match = re.match(r"\+CSQ:\s*(\d+),\s*(\d+)", line)
            if match:
                rssi = int(match.group(1))
                known = rssi != 99 and rssi <= 31
                return {"rssi": rssi if known else None,
                        "signal_dbm": -113 + 2 * rssi if known else None}
        return {"rssi": None, "signal_dbm": None}

    def registration(self) -> dict[str, Any]:
        """CS (``+CREG``) and EPS (``+CEREG``) registration: 1 home, 5 roaming."""
        states: dict[str, int | None] = {"creg": None, "cereg": None}
        for key, command in (("creg", "AT+CREG?"), ("cereg", "AT+CEREG?")):
            try:
                for line in self.command(command):
                    match = re.match(r"\+C(?:E)?REG:\s*\d+,\s*(\d+)", line)
                    if match:
                        states[key] = int(match.group(1))
            except AtError:
                continue
        values = [v for v in states.values() if v is not None]
        return {"registered": any(v in (1, 5) for v in values),
                "roaming": any(v == 5 for v in values) and not any(v == 1 for v in values),
                **states}

    def operator(self) -> str | None:
        try:
            for line in self.command("AT+COPS?"):
                fields = _fields(line.split(":", 1)[1]) if ":" in line else []
                if len(fields) >= 3 and fields[2]:
                    return fields[2]
        except AtError:
            pass
        return None

    def poll_urcs(self, timeout: float = 0.0) -> int:
        """Read whatever arrived unprompted; returns the URCs dispatched."""
        count = 0
        with self._lock:
            deadline = self._clock() + timeout
            while True:
                line = self._readline(deadline)
                if line is None:
                    return count
                if self._is_urc(line, ""):
                    self._dispatch_urc(line, deadline)
                    count += 1

    # ── SMS and calls ────────────────────────────────────────────────────────
    def _next_reference(self) -> int:
        self._reference = (self._reference + 1) & 0xFF
        return self._reference

    def count_parts(self, text: str) -> int:
        if self.sms_mode == "text":
            return len(text_mode_parts(text))
        return len(sms_parts(text)[1])

    def send_sms(self, number: str, text: str) -> list[int]:
        """Send ``text``; returns the message reference of every segment."""
        if not _NUMBER.match(number or ""):
            raise AtError("AT+CMGS", "invalid number", permanent=True)
        with self._lock:
            if self.sms_mode == "text":
                return self._send_text(number, text)
            return self._send_pdu(number, text)

    def _send_pdu(self, number: str, text: str) -> list[int]:
        pdus = encode_sms_submit(number, text, reference=self._next_reference(),
                                 status_report=True)
        references: list[int] = []
        self.command("AT+CMGF=0")
        try:
            for length, pdu in pdus:
                try:
                    references.append(self._submit(f"AT+CMGS={length}", pdu.encode("ascii")))
                except AtError as exc:
                    raise AtError(exc.command, f"{exc.response} (segment "
                                  f"{len(references) + 1} of {len(pdus)})",
                                  sent_parts=len(references)) from exc
        finally:
            self._optional("AT+CMGF=1")
        return references

    def _send_text(self, number: str, text: str) -> list[int]:
        references: list[int] = []
        for part in text_mode_parts(text):
            try:
                references.append(self._submit(f'AT+CMGS="{number}"',
                                               part.encode("ascii", "replace")))
            except AtError as exc:
                raise AtError(exc.command, exc.response,
                              sent_parts=len(references)) from exc
        return references

    def dial(self, number: str, ring_seconds: float = 30.0) -> str:
        """Ring ``number`` (a voice call nobody has to answer), then hang up.
        Returns "rang" or the network's final result (BUSY, NO ANSWER, …)."""
        if not _NUMBER.match(number or ""):
            raise AtError("ATD", "invalid number", permanent=True)
        with self._lock:
            lines = self.command(f"ATD{number};", timeout=30.0, finals=CALL_FINALS)
            if lines and lines[-1] == "NO ANSWER":
                return "NO ANSWER"                      # it rang out
            if lines and lines[-1] in CALL_FINALS:
                raise AtError("ATD", lines[-1])         # BUSY, NO CARRIER, NO DIALTONE
            deadline = self._clock() + ring_seconds
            ended = None
            while self._clock() < deadline:
                line = self._readline(min(deadline, self._clock() + 0.5))
                if line is None:
                    continue
                if line in CALL_FINALS:
                    ended = line
                    break
                if self._is_urc(line, ""):
                    self._dispatch_urc(line, deadline)
            if ended is None and not self._optional("ATH"):
                self._optional("AT+CHUP")
            return ended or "rang"

    def read_messages(self, status: str = "REC UNREAD") -> list[InboundSms]:
        """Stored SMS (text mode ``AT+CMGL``). Reading marks them read."""
        if status not in ("REC UNREAD", "REC READ", "ALL"):
            raise ValueError("status must be REC UNREAD, REC READ or ALL")
        with self._lock:
            self.command("AT+CMGF=1")
            lines = self.command(f'AT+CMGL="{status}"', timeout=15.0)
        messages: list[InboundSms] = []
        header, body = None, []
        for line in lines + ["+CMGL: end"]:
            if line.startswith("+CMGL:"):
                if header is not None:
                    fields = _fields(header.split(":", 1)[1])
                    try:
                        messages.append(InboundSms(
                            index=int(fields[0]), status=fields[1] if len(fields) > 1 else "",
                            sender=fields[2] if len(fields) > 2 else "",
                            received=fields[4] if len(fields) > 4 else "",
                            text="\n".join(body)))
                    except (ValueError, IndexError):
                        log.warning("modem: unreadable +CMGL entry %r", header)
                header, body = line, []
            elif header is not None:
                body.append(line)
        return messages

    def delete_message(self, index: int) -> None:
        with self._lock:
            self.command(f"AT+CMGD={int(index)}")

    def close(self) -> None:
        with self._lock:
            try:
                self.port.close()
            except Exception:
                pass


# ── the worker that owns the port ─────────────────────────────────────────────
def _open_serial(port: str, baud: int) -> Any:
    import serial                                       # pyserial
    return serial.Serial(port=port, baudrate=baud, timeout=0.1, write_timeout=5)


def _list_ports() -> list[tuple[str, str]]:
    from serial.tools import list_ports
    return [(p.device, f"{p.description or ''} {p.manufacturer or ''}".strip())
            for p in list_ports.comports()]


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None


class ModemWorker:
    """Owns the modem: connect (or probe), status every ``status_seconds``,
    unsolicited results, reconnect with back-off. Sends come from the router's
    thread and run under the modem's lock."""

    backend = "at"

    def __init__(self, port: str = "", baud: int = 115200, *, sms_mode: str = "pdu",
                 opener: Callable[[str, int], Any] | None = None,
                 lister: Callable[[], list[tuple[str, str]]] | None = None,
                 status_seconds: float = 30.0, idle_seconds: float = 0.5,
                 reconnect_seconds: tuple[float, float] = (5.0, 60.0),
                 clock: Callable[[], float] = time.monotonic,
                 modem_clock: Callable[[], float] | None = None) -> None:
        self.port = (port or "").strip()
        self.baud = int(baud)
        self.sms_mode = sms_mode
        self._opener = opener or _open_serial
        self._lister = lister or _list_ports
        self.status_seconds = status_seconds
        self.idle_seconds = idle_seconds
        self.reconnect_seconds = reconnect_seconds
        self._clock = clock
        self._modem_clock = modem_clock or clock
        self._modem: AtModem | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._reports: deque[tuple[int, int]] = deque(maxlen=256)
        self._next_connect = 0.0
        self._connect_failures = 0
        self._next_status = 0.0
        self._status: dict[str, Any] = {"backend": self.backend, "present": False,
                                        "registered": False, "signal_dbm": None,
                                        "port": self.port or None, "error": None}
        self.inbound_pending = 0

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="sentrix-modem")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        self._drop(None)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.step()
            self._stop.wait(self.idle_seconds)

    def step(self) -> None:
        """One iteration of the worker loop (public so tests drive it)."""
        now = self._clock()
        modem = self._modem
        if modem is None:
            if now >= self._next_connect:
                self._connect()
            return
        try:
            if now >= self._next_status:
                self.refresh()
            else:
                modem.poll_urcs(0.0)
        except AtError as exc:
            self._set(error=str(exc))
        except OSError as exc:                            # serial.SerialException included
            self._drop(exc)

    # ── connection ───────────────────────────────────────────────────────────
    def _candidates(self) -> list[str]:
        if self.port:
            return [self.port]
        try:
            ports = self._lister()
        except Exception as exc:
            log.warning("modem: cannot list serial ports: %s", exc)
            return []
        hinted = [d for d, desc in ports if any(h in desc.lower() for h in MODEM_PORT_HINTS)]
        return hinted + [d for d, _ in ports if d not in hinted]

    def _connect(self) -> bool:
        error = "no modem answered AT on any serial port"
        for name in self._candidates():
            try:
                handle = self._opener(name, self.baud)
            except Exception as exc:
                error = f"{name}: {exc}"
                continue
            modem = AtModem(handle, sms_mode=self.sms_mode, on_urc=self._on_urc,
                            clock=self._modem_clock)
            try:
                modem.initialise()
            except (AtError, ModemUnavailable, OSError) as exc:
                modem.close()
                error = f"{name}: {exc}"
                continue
            with self._lock:
                self._modem = modem
            self._connect_failures = 0
            self._set(port=name, present=True, error=None,
                      status_reports=modem.status_reports)
            log.info("modem ready on %s (%s mode)", name, self.sms_mode)
            try:
                self.refresh()
            except (AtError, OSError) as exc:
                self._set(error=str(exc))
            return True
        self._connect_failures += 1
        low, high = self.reconnect_seconds
        self._next_connect = self._clock() + min(high, low * 2 ** (self._connect_failures - 1))
        self._set(present=False, registered=False, signal_dbm=None, error=error)
        if self._connect_failures == 1:
            log.warning("modem not available: %s", error)
        return False

    def _drop(self, exc: Exception | None) -> None:
        with self._lock:
            modem, self._modem = self._modem, None
        if modem is not None:
            modem.close()
            if exc is not None:
                log.warning("modem disconnected: %s", exc)
                self._next_connect = self._clock() + self.reconnect_seconds[0]
        if exc is not None:
            self._set(present=False, registered=False, signal_dbm=None, error=str(exc))

    def refresh(self) -> dict[str, Any]:
        """SIM, signal, registration and operator, now."""
        modem = self._require()
        sim = modem.sim_ready()
        signal = modem.signal()
        registration = modem.registration()
        operator = modem.operator()
        self._next_status = self._clock() + self.status_seconds
        self._set(present=True, sim_ready=sim, registered=sim and registration["registered"],
                  roaming=registration["roaming"], creg=registration["creg"],
                  cereg=registration["cereg"], operator=operator,
                  checked_at=_iso(time.time()), **signal)
        return self.status()

    def _require(self) -> AtModem:
        modem = self._modem
        if modem is None:
            raise ModemUnavailable(self._status.get("error") or "modem not connected")
        return modem

    def _set(self, **fields: Any) -> None:
        with self._lock:
            self._status.update(fields)

    # ── unsolicited results ──────────────────────────────────────────────────
    def _on_urc(self, line: str, extra: list[str]) -> None:
        """Runs on whichever thread holds the modem: record only."""
        if line.startswith("+CDS:"):
            body = line.split(":", 1)[1].strip()
            try:
                if extra and re.fullmatch(r"\d+", body):
                    mr, st = decode_status_report(extra[0])
                else:
                    fields = _fields(body)
                    mr, st = int(fields[1]), int(fields[-1])
            except (ValueError, IndexError) as exc:
                log.warning("modem: unreadable status report %r (%s)", line, exc)
                return
            self._reports.append((mr, st))
        elif line.startswith(("+CMTI:", "+CMT:")):
            self.inbound_pending += 1
            log.info("modem: inbound SMS waiting (reply parsing is Tier 2)")

    def take_reports(self) -> list[tuple[int, int]]:
        """Delivery reports received since the last call: [(reference, TP-ST)]."""
        out = []
        while self._reports:
            out.append(self._reports.popleft())
        return out

    # ── what the transports call ─────────────────────────────────────────────
    @property
    def present(self) -> bool:
        return self._modem is not None

    def available(self) -> tuple[bool, str]:
        status = self.status()
        if not status.get("present"):
            return False, status.get("error") or "modem not connected"
        if status.get("sim_ready") is False:
            return False, "SIM not ready (missing, or PIN locked)"
        if not status.get("registered"):
            return False, "not registered on the mobile network"
        return True, f"{status.get('operator') or 'network'} {status.get('signal_dbm')} dBm"

    def count_parts(self, text: str) -> int:
        if self.sms_mode == "text":
            return len(text_mode_parts(text))
        return len(sms_parts(text)[1])

    def send_sms(self, number: str, text: str) -> list[int]:
        modem = self._require()
        try:
            return modem.send_sms(number, text)
        except OSError as exc:
            self._drop(exc)
            raise ModemUnavailable(str(exc)) from exc

    def dial(self, number: str, ring_seconds: float = 30.0) -> str:
        modem = self._require()
        try:
            return modem.dial(number, ring_seconds)
        except OSError as exc:
            self._drop(exc)
            raise ModemUnavailable(str(exc)) from exc

    def read_inbound(self, status: str = "REC UNREAD") -> list[InboundSms]:
        """Tier 2 hook: stored SMS. Nothing acts on them yet."""
        messages = self._require().read_messages(status)
        self.inbound_pending = 0
        return messages

    def delete_inbound(self, index: int) -> None:
        self._require().delete_message(index)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {**self._status, "backend": self.backend,
                    "present": self._modem is not None and self._status.get("present", False),
                    "inbound_pending": self.inbound_pending}


# ── ModemManager alternative ──────────────────────────────────────────────────
def _run_mmcli(args: list[str], timeout: float) -> tuple[int, str, str]:
    completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                               check=False)
    return completed.returncode, completed.stdout, completed.stderr


def _mm_quote(value: str) -> str:
    """Quote a value for mmcli's ``key=value`` lists (no escapes there)."""
    if '"' not in value:
        return f'"{value}"'
    if "'" not in value:
        return f"'{value}'"
    return '"' + value.replace('"', "'") + '"'


class MmcliWorker:
    """The modem through ModemManager (``mmcli``), when it owns the port.
    Same interface as :class:`ModemWorker`; no delivery reports."""

    backend = "mmcli"

    def __init__(self, modem: str = "any", *,
                 runner: Callable[[list[str], float], tuple[int, str, str]] | None = None,
                 status_seconds: float = 30.0, idle_seconds: float = 1.0,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.modem = modem or "any"
        self._runner = runner or _run_mmcli
        self.status_seconds = status_seconds
        self.idle_seconds = idle_seconds
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_status = 0.0
        self._status: dict[str, Any] = {"backend": self.backend, "present": False,
                                        "registered": False, "signal_dbm": None,
                                        "port": f"mmcli:{self.modem}", "error": None}
        self.inbound_pending = 0

    def _mmcli(self, *args: str, timeout: float = 30.0) -> str:
        try:
            code, out, err = self._runner(["mmcli", *args], timeout)
        except FileNotFoundError as exc:
            raise ModemUnavailable("mmcli not installed (ModemManager)") from exc
        except subprocess.TimeoutExpired as exc:
            raise AtError("mmcli " + " ".join(args[:2]), "timeout") from exc
        if code != 0:
            message = (err or out).strip().splitlines()[-1:] or ["failed"]
            if "couldn't find" in message[0].lower() or "no modems" in message[0].lower():
                raise ModemUnavailable(message[0])
            raise AtError("mmcli " + " ".join(args[:2]), message[0])
        return out

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="sentrix-mmcli")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            self.step()
            self._stop.wait(self.idle_seconds)

    def step(self) -> None:
        if self._clock() >= self._next_status:
            try:
                self.refresh()
            except (AtError, ModemUnavailable) as exc:
                self._set(present=False, registered=False, error=str(exc))
                self._next_status = self._clock() + self.status_seconds

    def refresh(self) -> dict[str, Any]:
        info = json.loads(self._mmcli("-m", self.modem, "-J") or "{}").get("modem", {})
        generic, gpp = info.get("generic", {}), info.get("3gpp", {})
        quality = (generic.get("signal-quality") or {}).get("value")
        reg = gpp.get("registration-state") or ""
        self._next_status = self._clock() + self.status_seconds
        self._set(present=True, registered=reg in ("home", "roaming"), roaming=reg == "roaming",
                  sim_ready=generic.get("state") not in ("locked", "failed", None),
                  operator=gpp.get("operator-name") or None,
                  signal_percent=int(quality) if str(quality or "").isdigit() else None,
                  signal_dbm=None, state=generic.get("state"), error=None,
                  checked_at=_iso(time.time()))
        return self.status()

    def _set(self, **fields: Any) -> None:
        with self._lock:
            self._status.update(fields)

    @property
    def present(self) -> bool:
        return bool(self._status.get("present"))

    def available(self) -> tuple[bool, str]:
        status = self.status()
        if not status.get("present"):
            return False, status.get("error") or "ModemManager reports no modem"
        if not status.get("registered"):
            return False, "not registered on the mobile network"
        return True, f"{status.get('operator') or 'network'} (ModemManager)"

    def count_parts(self, text: str) -> int:
        return len(sms_parts(text)[1])

    @staticmethod
    def _created(output: str, kind: str) -> str:
        match = re.search(rf"(/org/freedesktop/ModemManager1/{kind}/\d+)", output)
        if not match:
            raise AtError(f"mmcli create {kind}", output.strip() or "no object path")
        return match.group(1)

    def send_sms(self, number: str, text: str) -> list[int]:
        if not _NUMBER.match(number or ""):
            raise AtError("mmcli sms", "invalid number", permanent=True)
        with self._send_lock:
            spec = f"text={_mm_quote(transliterate(text))},number={_mm_quote(number)}"
            path = self._created(self._mmcli("-m", self.modem, f"--messaging-create-sms={spec}"),
                                 "SMS")
            try:
                self._mmcli("-s", path, "--send", timeout=60.0)
            finally:
                try:
                    self._mmcli("-m", self.modem, f"--messaging-delete-sms={path}")
                except (AtError, ModemUnavailable):
                    pass
        return []

    def dial(self, number: str, ring_seconds: float = 30.0) -> str:
        if not _NUMBER.match(number or ""):
            raise AtError("mmcli call", "invalid number", permanent=True)
        with self._send_lock:
            path = self._created(self._mmcli("-m", self.modem,
                                             f"--voice-create-call=number={number}"), "Call")
            try:
                self._mmcli("-o", path, "--start")
                self._sleep(ring_seconds)
                self._mmcli("-o", path, "--hangup")
            finally:
                try:
                    self._mmcli("-m", self.modem, f"--voice-delete-call={path}")
                except (AtError, ModemUnavailable):
                    pass
        return "rang"

    def take_reports(self) -> list[tuple[int, int]]:
        return []

    def read_inbound(self, status: str = "REC UNREAD") -> list[InboundSms]:
        return []

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {**self._status, "backend": self.backend,
                    "inbound_pending": self.inbound_pending}


def build_worker(port: str = "", baud: int = 115200) -> ModemWorker | MmcliWorker:
    """``SENTRIX_MODEM_PORT``: "" probe, ``COM5`` / ``/dev/ttyUSB2``, or
    ``mmcli`` / ``mmcli:<index>`` for ModemManager."""
    spec = (port or "").strip()
    if spec.lower().startswith("mmcli"):
        _, _, index = spec.partition(":")
        return MmcliWorker(index or "any")
    return ModemWorker(spec, baud)


# ── daily cap ─────────────────────────────────────────────────────────────────
class DailyCounter:
    """SMS segments sent today (local date); refuses beyond ``cap``."""

    def __init__(self, cap: int = 30, *, today: Callable[[], date] = date.today) -> None:
        self.cap = int(cap)
        self._today = today
        self._day = today()
        self._used = 0
        self._lock = threading.Lock()

    def _roll(self) -> None:
        day = self._today()
        if day != self._day:
            self._day, self._used = day, 0

    def take(self, n: int = 1) -> bool:
        with self._lock:
            self._roll()
            if self._used + n > self.cap:
                return False
            self._used += n
            return True

    def give_back(self, n: int = 1) -> None:
        with self._lock:
            self._roll()
            self._used = max(0, self._used - n)

    def seed(self, used: int) -> None:
        """Segments already sent today (from the outbox, at start)."""
        with self._lock:
            self._roll()
            self._used = max(self._used, int(used))

    @property
    def used(self) -> int:
        with self._lock:
            self._roll()
            return self._used

    @property
    def remaining(self) -> int:
        return max(0, self.cap - self.used)


def provider_id_for(references: list[int]) -> str:
    return "modem-mr:" + ",".join(str(r) for r in references)


def references_of(provider_id: str | None) -> list[int]:
    """Inverse of :func:`provider_id_for` (empty for anything else)."""
    if not provider_id or not provider_id.startswith("modem-mr:"):
        return []
    return [int(x) for x in provider_id.split(":", 1)[1].split(",") if x.isdigit()]


def segments_of(provider_id: str | None) -> int:
    """Segments a SENT row used (one when the backend reports no references)."""
    return max(1, len(references_of(provider_id)))


# ── transports ────────────────────────────────────────────────────────────────
def profile_allowlist() -> frozenset[str]:
    """Owner, CONFIRMED contacts and Desk of the current profile (E.164)."""
    from sentrix.link import profile
    try:
        return profile.sms_allowlist()
    except Exception as exc:                            # the cache must not stop an SOS
        log.warning("profile unreadable (%s): using the standalone profile", exc)
        return profile.sms_allowlist(profile.standalone_profile())


def _not_in_profile() -> SendResult:
    return SendResult(False, error="not_allowed_recipient: not in the device profile "
                                   "(owner, confirmed contacts, Desk)",
                      retryable=False, suppressed=True)


class ModemSmsTransport:
    """``modem_sms``: profile numbers only, ``cap`` segments a day."""

    name = "modem_sms"

    def __init__(self, worker: Any, *, counter: DailyCounter | None = None,
                 allowlist: Callable[[], frozenset[str]] = profile_allowlist) -> None:
        self.worker = worker
        self.counter = counter or DailyCounter()
        self._allowlist = allowlist

    def state(self) -> tuple[bool, str]:
        available, detail = self.worker.available()
        if available and self.counter.remaining == 0:
            return False, f"daily SMS cap reached ({self.counter.cap})"
        return available, detail

    def send(self, recipient: Recipient, message: Message) -> SendResult:
        number = guard.normalize_address(self.name, recipient.address)
        if number not in self._allowlist():
            return _not_in_profile()
        parts = self.worker.count_parts(message.text)
        if not self.counter.take(parts):
            return SendResult(False, error=f"daily_cap: {self.counter.cap} SMS segments a "
                                           "day already sent", retryable=False)
        try:
            references = self.worker.send_sms(number, message.text)
        except ModemUnavailable as exc:
            self.counter.give_back(parts)
            return SendResult(False, error=f"modem unavailable: {exc}", retryable=True)
        except AtError as exc:
            self.counter.give_back(parts - exc.sent_parts)
            return SendResult(False, error=f"modem: {exc}", retryable=exc.retryable)
        return SendResult(True, provider_id=provider_id_for(references))


class ModemCallTransport:
    """``modem_call``: ring a profile number for ``ring_seconds`` (no speech:
    the SMS carries the words; the ring makes the owner look)."""

    name = "modem_call"

    def __init__(self, worker: Any, *, ring_seconds: float = 30.0,
                 allowlist: Callable[[], frozenset[str]] = profile_allowlist) -> None:
        self.worker = worker
        self.ring_seconds = ring_seconds
        self._allowlist = allowlist

    def state(self) -> tuple[bool, str]:
        return self.worker.available()

    def send(self, recipient: Recipient, message: Message) -> SendResult:
        number = guard.normalize_address(self.name, recipient.address)
        if number not in self._allowlist():
            return _not_in_profile()
        try:
            outcome = self.worker.dial(number, self.ring_seconds)
        except ModemUnavailable as exc:
            return SendResult(False, error=f"modem unavailable: {exc}", retryable=True)
        except AtError as exc:
            return SendResult(False, error=f"modem: {exc}", retryable=exc.retryable)
        return SendResult(True, provider_id=f"modem-call:{outcome}")


__all__ = ["AtModem", "AtError", "ModemUnavailable", "ModemWorker", "MmcliWorker",
           "ModemSmsTransport", "ModemCallTransport", "DailyCounter", "InboundSms",
           "build_worker", "encode_sms_submit", "decode_status_report", "report_outcome",
           "sms_parts", "text_mode_parts", "gsm7_septets", "pack_septets",
           "encode_address", "transliterate", "provider_id_for", "references_of",
           "segments_of", "profile_allowlist", "GSM7_BASIC", "GSM7_EXTENSION"]
