"""Track T-CH: the USB modem — PDU codec, AT driver, worker, transports, mmcli.

No serial port is opened: :class:`FakeModem` is a port that speaks AT like a
SIM7600/EC25 from a script, on a fake clock (an empty read advances it 0.1 s,
as pyserial's read timeout would), so timeouts cost no real time.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

import pytest

from sentrix.alerting import guard
from sentrix.alerting.channels import modem as M
from sentrix.alerting.channels.base import (DELIVERED, FAILED, SENT, SUPPRESSED, Message,
                                            Recipient)

TEAM = "+919812345610"


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeModem:
    """A serial-like port with a scripted AT modem behind it."""

    def __init__(self, clock: FakeClock | None = None, *, first_reference: int = 5) -> None:
        self.clock = clock or FakeClock()
        self.out = bytearray()          # modem -> host
        self.inbuf = bytearray()        # host -> modem, unprocessed
        self.commands: list[str] = []
        self.payloads: list[bytes] = []
        self.cancelled = 0
        self.closed = False
        self.awaiting = False
        self.reference = first_reference
        self.replies: dict[str, object] = {
            "AT": "OK", "ATE0": "OK", "AT+CMEE=2": "OK", "AT+CMGF=1": "OK",
            "AT+CMGF=0": "OK", 'AT+CSCS="IRA"': "OK", "AT+CSMP=49,167,0,0": "OK",
            "AT+CNMI=2,1,0,1,0": "OK", "AT+CPIN?": "+CPIN: READY\r\n\r\nOK",
            "AT+CSQ": "+CSQ: 18,99\r\n\r\nOK", "AT+CREG?": "+CREG: 0,1\r\n\r\nOK",
            "AT+CEREG?": "+CEREG: 0,1\r\n\r\nOK",
            "AT+COPS?": '+COPS: 0,0,"Jio 4G",7\r\n\r\nOK', "ATH": "OK", "AT+CHUP": "OK",
        }
        self.cmgs_reply: str | None = None      # e.g. "+CMS ERROR: 38" for the payload
        self.prompt = True
        self.after_reply: list[str] = []        # lines emitted after the next reply
        self.on_read_empty = None

    # serial interface
    @property
    def in_waiting(self) -> int:
        return len(self.out)

    def write(self, data: bytes) -> int:
        if self.closed:
            raise OSError("port closed")
        self.inbuf.extend(data)
        self._process()
        return len(data)

    def read(self, size: int = 1) -> bytes:
        if self.closed:
            raise OSError("device disconnected")
        if not self.out:
            self.clock.now += 0.1
            if self.on_read_empty is not None:
                self.on_read_empty(self)
            return b""
        chunk = bytes(self.out[:size])
        del self.out[:size]
        return chunk

    def close(self) -> None:
        self.closed = True

    # script helpers
    def emit(self, *lines: str) -> None:
        for line in lines:
            self.out.extend(f"\r\n{line}\r\n".encode())

    def _process(self) -> None:
        while True:
            if self.awaiting:
                stops = [i for i in (self.inbuf.find(b"\x1a"), self.inbuf.find(b"\x1b"))
                         if i >= 0]
                if not stops:
                    return
                idx = min(stops)
                payload, stop = bytes(self.inbuf[:idx]), self.inbuf[idx]
                del self.inbuf[:idx + 1]
                self.awaiting = False
                if stop == 0x1B:
                    self.cancelled += 1
                    continue
                self.payloads.append(payload)
                if self.cmgs_reply:
                    self.emit(self.cmgs_reply)
                else:
                    self.emit(f"+CMGS: {self.reference}", "OK")
                    self.reference = (self.reference + 1) & 0xFF
                continue
            if self.inbuf[:1] == b"\x1b":
                del self.inbuf[:1]
                self.cancelled += 1
                continue
            idx = self.inbuf.find(b"\r")
            if idx < 0:
                return
            line = self.inbuf[:idx].decode("ascii", "replace").strip()
            del self.inbuf[:idx + 1]
            if line:
                self.commands.append(line)
                self._respond(line)

    def _respond(self, command: str) -> None:
        if command.startswith("AT+CMGS"):
            if self.prompt:
                self.out.extend(b"\r\n> ")
                self.awaiting = True
            return
        if command.startswith("ATD"):
            reply = self.replies.get("ATD", "OK")
        else:
            reply = self.replies.get(command, "ERROR")
        if callable(reply):
            reply = reply(command)
        if reply is not None:
            self.emit(*str(reply).split("\r\n\r\n"))
        if self.after_reply:
            self.emit(*self.after_reply)
            self.after_reply = []


def connected(**kwargs) -> tuple[M.AtModem, FakeModem]:
    port = FakeModem()
    urcs: list[tuple[str, list[str]]] = []
    modem = M.AtModem(port, clock=port.clock, on_urc=lambda line, extra: urcs.append(
        (line, extra)), **kwargs)
    modem.urcs = urcs
    modem.initialise()
    port.commands.clear()
    return modem, port


# ── decoding helpers for the assertions (independent of the encoder) ──────────
def unpack_septets(data: bytes, count: int) -> list[int]:
    bits = int.from_bytes(data, "little")
    return [(bits >> (7 * i)) & 0x7F for i in range(count)]


def gsm_text(septets: list[int]) -> str:
    reverse_ext = {v: k for k, v in M.GSM7_EXTENSION.items()}
    out, escape = [], False
    for s in septets:
        if escape:
            out.append(reverse_ext[s])
            escape = False
        elif s == 0x1B:
            escape = True
        else:
            out.append(M.GSM7_BASIC[s])
    return "".join(out)


def decode_submit(pdu_hex: str) -> dict:
    data = bytes.fromhex(pdu_hex)
    assert data[0] == 0, "use the SIM's SMSC"
    first, i = data[1], 2
    i += 1                                              # TP-MR
    digits, kind = data[i], data[i + 1]
    raw = data[i + 2:i + 2 + (digits + 1) // 2]
    number = "".join(f"{b & 0x0F:x}{b >> 4:x}" for b in raw)[:digits]
    i += 2 + (digits + 1) // 2
    dcs = data[i + 1]
    i += 2
    validity = data[i] if first & 0x18 == 0x10 else None
    i += 1 if validity is not None else 0
    udl, ud = data[i], data[i + 1:]
    udh = b""
    if first & 0x40:
        udh = ud[1:1 + ud[0]]
    if dcs == 0x08:
        text = ud[(1 + len(udh)) if udh else 0:].decode("utf-16-be")
    else:
        header_septets = -(-((len(udh) + 1) * 8) // 7) if udh else 0
        text = gsm_text(unpack_septets(ud, udl)[header_septets:])
    return {"first": first, "number": ("+" if kind == 0x91 else "") + number, "dcs": dcs,
            "validity": validity, "udh": udh, "text": text, "udl": udl}


# ── codec ─────────────────────────────────────────────────────────────────────
class TestPduCodec:
    def test_gsm7_alphabet_and_packing_vector(self):
        assert len(M.GSM7_BASIC) == 128
        assert M.pack_septets(M.gsm7_septets("hellohello")).hex().upper() == \
            "E8329BFD4697D9EC37"

    def test_address_semi_octets(self):
        assert M.encode_address("+919812345610").hex() == "0c91198921436501"
        assert M.encode_address("+1234567").hex() == "0791214365f7"
        with pytest.raises(ValueError):
            M.encode_address("98 1234")

    def test_single_segment_submit(self):
        (length, pdu), = M.encode_sms_submit(TEAM, "hello", status_report=False)
        assert pdu == "0011000C911989214365010000A705E8329BFD06" and length == 19
        decoded = decode_submit(pdu)
        assert decoded["number"] == TEAM and decoded["text"] == "hello"
        assert decoded["validity"] == M.VALIDITY_24H

    def test_status_report_is_requested(self):
        (_, pdu), = M.encode_sms_submit(TEAM, "x")
        assert decode_submit(pdu)["first"] & 0x20

    def test_the_gsm_test_banner_stays_gsm7_with_extension_characters(self):
        text = guard.prepare_text("modem_sms", "SOS {test} | 50€ ~ ok",
                                  policy=guard.SendPolicy())
        coding, parts = M.sms_parts(text)
        assert coding == "gsm7" and len(parts) == 1
        (_, pdu), = M.encode_sms_submit(TEAM, text)
        assert decode_submit(pdu)["text"] == text

    def test_long_text_is_concatenated_with_a_reference_header(self):
        text = "SENTRIX SOS SX-7F3A: HANDGUN, Living room 14:32. " * 7
        pdus = M.encode_sms_submit(TEAM, text, reference=0x42)
        assert len(pdus) == 3
        decoded = [decode_submit(pdu) for _, pdu in pdus]
        assert [d["udh"] for d in decoded] == [bytes([0, 3, 0x42, 3, n]) for n in (1, 2, 3)]
        assert all(d["first"] & 0x40 for d in decoded)
        assert "".join(d["text"] for d in decoded) == text
        assert all(d["udl"] <= 160 for d in decoded)
        assert all(length == len(bytes.fromhex(pdu)) - 1 for length, pdu in pdus)

    def test_escape_pairs_are_never_split(self):
        text = "a" * 152 + "[" + "b" * 10
        parts = M.sms_parts(text)[1]
        assert len(parts) == 2 and parts[0][-1] != 0x1B
        pdus = M.encode_sms_submit(TEAM, text)
        assert "".join(decode_submit(p)["text"] for _, p in pdus) == text

    def test_typography_is_transliterated_to_keep_gsm7(self):
        coding, _ = M.sms_parts("Riya’s phone – call “now”…")
        assert coding == "gsm7"
        (_, pdu), = M.encode_sms_submit(TEAM, "Riya’s phone – call “now”…")
        assert decode_submit(pdu)["text"] == "Riya's phone - call \"now\"..."

    def test_hindi_is_ucs2_and_concatenated_at_67_characters(self):
        text = "लिविंग रूम में संभावित हथियार। " * 6
        coding, parts = M.sms_parts(text)
        assert coding == "ucs2" and all(len(p) // 2 <= 67 for p in parts)
        pdus = M.encode_sms_submit(TEAM, text)
        decoded = [decode_submit(p) for _, p in pdus]
        assert {d["dcs"] for d in decoded} == {0x08}
        assert "".join(d["text"] for d in decoded) == text

    def test_surrogate_pairs_stay_whole(self):
        text = "x" * 66 + "🚨" + "y" * 10
        parts = M.sms_parts(text)[1]
        assert "".join(p.decode("utf-16-be") for p in parts) == text

    def test_very_long_text_is_cut_at_the_part_limit(self):
        pdus = M.encode_sms_submit(TEAM, "z" * 5000, max_parts=4)
        text = "".join(decode_submit(p)["text"] for _, p in pdus)
        assert len(pdus) == 4 and text.endswith("...") and len(text) == 4 * 153

    def test_text_mode_parts(self):
        assert M.text_mode_parts("short") == ["short"]
        parts = M.text_mode_parts("é" * 10 + "x" * 300)
        assert len(parts) == 3 and parts[0].startswith("(1/3) ")
        assert all(len(p) <= 160 and p.isascii() for p in parts)

    def test_status_report_pdu(self):
        pdu = ("07911989000000F0" "06" "2A" "0C91198921436501"
               "62014121000022" "62014121000522" "00")
        assert M.decode_status_report(pdu) == (0x2A, 0)
        with pytest.raises(ValueError):
            M.decode_status_report("00" + "01" + "00" * 30)
        assert [M.report_outcome(s) for s in (0x00, 0x01, 0x30, 0x41, 0x62)] == [
            "delivered", "delivered", "pending", "failed", "failed"]


# ── the AT layer ──────────────────────────────────────────────────────────────
class TestAtModem:
    def test_initialise_sequence(self):
        port = FakeModem()
        modem = M.AtModem(port, clock=port.clock)
        modem.initialise()
        assert port.commands == ["AT", "ATE0", "AT+CMEE=2", "AT+CMGF=1", 'AT+CSCS="IRA"',
                                 "AT+CSMP=49,167,0,0", "AT+CNMI=2,1,0,1,0"]
        assert modem.status_reports is True and port.cancelled == 1   # ESC before AT

    def test_optional_commands_may_fail(self):
        port = FakeModem()
        del port.replies["AT+CNMI=2,1,0,1,0"]
        port.replies["AT+CMEE=2"] = "+CME ERROR: 4"
        modem = M.AtModem(port, clock=port.clock)
        modem.initialise()
        assert modem.status_reports is False

    def test_no_answer_is_unavailable(self):
        port = FakeModem()
        port.replies["AT"] = None
        with pytest.raises(M.ModemUnavailable):
            M.AtModem(port, clock=port.clock).initialise()

    def test_echo_is_ignored(self):
        port = FakeModem()
        port.replies["AT+CSQ"] = "AT+CSQ\r\n\r\n+CSQ: 20,0\r\n\r\nOK"
        modem = M.AtModem(port, clock=port.clock)
        assert modem.signal() == {"rssi": 20, "signal_dbm": -73}

    def test_signal_and_registration(self):
        modem, port = connected()
        assert modem.signal() == {"rssi": 18, "signal_dbm": -77}
        port.replies["AT+CSQ"] = "+CSQ: 99,99\r\n\r\nOK"
        assert modem.signal() == {"rssi": None, "signal_dbm": None}
        assert modem.registration()["registered"] is True
        port.replies["AT+CREG?"] = "+CREG: 0,5\r\n\r\nOK"
        port.replies["AT+CEREG?"] = "+CME ERROR: 100"
        reg = modem.registration()
        assert reg["registered"] and reg["roaming"] and reg["cereg"] is None
        port.replies["AT+CREG?"] = "+CREG: 0,2\r\n\r\nOK"
        assert modem.registration()["registered"] is False
        assert modem.operator() == "Jio 4G" and modem.sim_ready()

    def test_send_sms_in_pdu_mode(self):
        modem, port = connected()
        references = modem.send_sms(TEAM, "hello")
        assert references == [5]
        assert port.commands == ["AT+CMGF=0", "AT+CMGS=19", "AT+CMGF=1"]
        assert decode_submit(port.payloads[0].decode())["text"] == "hello"

    def test_long_sms_sends_every_segment(self):
        modem, port = connected()
        references = modem.send_sms(TEAM, "y" * 400)
        assert references == [5, 6, 7] and len(port.payloads) == 3
        assert "".join(decode_submit(p.decode())["text"] for p in port.payloads) == "y" * 400

    def test_network_error_keeps_text_mode_and_reports_the_segment(self):
        modem, port = connected()
        port.cmgs_reply = "+CMS ERROR: 38"
        with pytest.raises(M.AtError) as raised:
            modem.send_sms(TEAM, "hello")
        assert raised.value.code == 38 and raised.value.retryable
        assert "segment 1 of 1" in str(raised.value) and raised.value.sent_parts == 0
        assert port.commands[-1] == "AT+CMGF=1"

    def test_unassigned_number_is_permanent(self):
        modem, port = connected()
        port.cmgs_reply = "+CMS ERROR: 1"
        with pytest.raises(M.AtError) as raised:
            modem.send_sms(TEAM, "hello")
        assert not raised.value.retryable

    def test_missing_prompt_is_cancelled_with_escape(self):
        modem, port = connected()
        port.prompt = False
        before = port.cancelled
        with pytest.raises(M.AtError) as raised:
            modem.send_sms(TEAM, "hello")
        assert "prompt" in str(raised.value) and port.cancelled == before + 1

    def test_invalid_numbers_never_reach_the_modem(self):
        modem, port = connected()
        with pytest.raises(M.AtError) as raised:
            modem.send_sms("12;ATD112", "x")
        assert not raised.value.retryable and port.commands == []

    def test_text_mode_send(self):
        modem, port = connected(sms_mode="text")
        assert modem.send_sms(TEAM, "x" * 200) == [5, 6]
        assert port.commands == [f'AT+CMGS="{TEAM}"'] * 2
        assert port.payloads[0].startswith(b"(1/2) ") and modem.count_parts("x" * 200) == 2

    def test_urcs_inside_a_response_are_dispatched(self):
        modem, port = connected()
        port.replies["AT+CSQ"] = ('+CDS: 6,5,"+919812345610",145,"26/10/15,10:00:00+22",'
                                  '"26/10/15,10:00:05+22",0\r\n\r\n+CSQ: 18,99\r\n\r\nOK')
        assert modem.signal()["signal_dbm"] == -77
        assert modem.urcs[0][0].startswith("+CDS: 6,5,")

    def test_poll_reads_unsolicited_lines_including_two_line_ones(self):
        modem, port = connected()
        port.emit('+CMTI: "SM",3', "RING", "+CDS: 25")
        port.out.extend(b"\r\n07911989000000F0062A0C9119892143650162014121000022620141210005"
                        b"2200\r\n")
        assert modem.poll_urcs(0.0) == 3
        assert modem.urcs[2][1] == ["07911989000000F0062A0C9119892143650162014121000022"
                                    "6201412100052200"]

    def test_read_and_delete_messages(self):
        modem, port = connected()
        port.replies['AT+CMGL="REC UNREAD"'] = (
            '+CMGL: 3,"REC UNREAD","+919812345610",,"26/10/15,10:00:00+22"\r\n1\r\n'
            '+CMGL: 4,"REC UNREAD","+919800000000",,"26/10/15,10:01:00+22"\r\n'
            'line one\r\nline, two\r\n\r\nOK')
        port.replies["AT+CMGD=3"] = "OK"
        messages = modem.read_messages()
        assert [(m.index, m.sender, m.text) for m in messages] == [
            (3, "+919812345610", "1"), (4, "+919800000000", "line one\nline, two")]
        assert messages[0].received == "26/10/15,10:00:00+22"
        modem.delete_message(3)
        assert port.commands[-1] == "AT+CMGD=3"
        with pytest.raises(ValueError):
            modem.read_messages("SENT")

    def test_dial_rings_then_hangs_up(self):
        modem, port = connected()
        assert modem.dial(TEAM, ring_seconds=2.0) == "rang"
        assert port.commands == [f"ATD{TEAM};", "ATH"]

    def test_dial_ends_when_the_network_says_so(self):
        modem, port = connected()
        port.after_reply = ["NO CARRIER"]
        assert modem.dial(TEAM, ring_seconds=30.0) == "NO CARRIER"
        assert "ATH" not in port.commands

    def test_busy_at_dial_is_an_error(self):
        modem, port = connected()
        port.replies["ATD"] = "BUSY"
        with pytest.raises(M.AtError):
            modem.dial(TEAM)
        port.replies["ATD"] = "NO ANSWER"
        assert modem.dial(TEAM) == "NO ANSWER"


# ── the worker ────────────────────────────────────────────────────────────────
def worker_with(port: FakeModem | None = None, **kwargs) -> tuple[M.ModemWorker, FakeModem]:
    port = port or FakeModem()
    opened = []

    def opener(name, baud):
        opened.append((name, baud))
        return port

    worker = M.ModemWorker(kwargs.pop("port_name", "COM7"), opener=opener,
                           clock=port.clock, **kwargs)
    worker.opened = opened
    return worker, port


class TestModemWorker:
    def test_connects_initialises_and_reports(self):
        worker, port = worker_with()
        worker.step()
        status = worker.status()
        assert status["present"] and status["registered"] and status["signal_dbm"] == -77
        assert status["operator"] == "Jio 4G" and status["sim_ready"] and status["port"] == "COM7"
        assert worker.available() == (True, "Jio 4G -77 dBm")
        assert worker.opened == [("COM7", 115200)]

    def test_probe_prefers_ports_that_look_like_modems(self):
        port = FakeModem()
        tried = []

        def opener(name, baud):
            tried.append(name)
            if name == "COM3":
                raise OSError("access denied")
            return port

        worker = M.ModemWorker("", opener=opener, clock=port.clock, lister=lambda: [
            ("COM3", "Standard Serial over Bluetooth"), ("COM9", "Simcom HS-USB AT PORT 9001")])
        worker.step()
        assert tried == ["COM9"] and worker.status()["port"] == "COM9"

    def test_no_modem_backs_off(self):
        clock = FakeClock()
        worker = M.ModemWorker("", opener=lambda n, b: (_ for _ in ()).throw(OSError("x")),
                               lister=lambda: [], clock=clock, reconnect_seconds=(5.0, 60.0))
        attempts = []
        original = worker._connect
        worker._connect = lambda: attempts.append(clock.now) or original()
        for t in range(0, 40):
            clock.now = float(t)
            worker.step()
        assert attempts == [0.0, 5.0, 15.0, 35.0]
        status = worker.status()
        assert not status["present"] and "no modem" in status["error"]
        assert worker.available()[0] is False

    def test_unregistered_and_sim_problems_are_not_available(self):
        port = FakeModem()
        port.replies["AT+CREG?"] = "+CREG: 0,3\r\n\r\nOK"
        port.replies["AT+CEREG?"] = "+CEREG: 0,0\r\n\r\nOK"
        worker, _ = worker_with(port)
        worker.step()
        assert worker.available() == (False, "not registered on the mobile network")
        port.replies["AT+CPIN?"] = "+CME ERROR: 10"
        worker.refresh()
        assert worker.available()[1].startswith("SIM not ready")

    def test_disconnect_drops_and_reconnects(self):
        worker, port = worker_with()
        worker.step()
        port.closed = True
        worker._next_status = 0.0
        worker.step()
        assert not worker.status()["present"]
        with pytest.raises(M.ModemUnavailable):
            worker.send_sms(TEAM, "x")
        port.closed = False
        port.clock.now += 6
        worker.step()
        assert worker.status()["present"]

    def test_delivery_reports_are_queued_not_applied(self):
        worker, port = worker_with()
        worker.step()
        port.emit('+CDS: 6,5,"+919812345610",145,"26/10/15,10:00:00+22",'
                  '"26/10/15,10:00:05+22",0', "+CDS: 25")
        port.out.extend(b"\r\n07911989000000F0062A0C91198921436501620141210000226201412100"
                        b"05224100\r\n")
        worker.step()
        assert worker.take_reports() == [(5, 0), (0x2A, 0x41)]
        assert worker.take_reports() == []

    def test_inbound_sms_is_only_counted(self):
        worker, port = worker_with()
        worker.step()
        port.emit('+CMTI: "SM",7')
        worker.step()
        assert worker.status()["inbound_pending"] == 1
        port.replies['AT+CMGL="REC UNREAD"'] = (
            '+CMGL: 7,"REC UNREAD","+919812345610",,"26/10/15,10:00:00+22"\r\n1\r\n\r\nOK')
        assert worker.read_inbound()[0].text == "1"
        assert worker.status()["inbound_pending"] == 0

    def test_thread_start_and_stop(self):
        worker, port = worker_with(idle_seconds=0.01)
        worker.start()
        try:
            for _ in range(200):
                if worker.status()["present"]:
                    break
                __import__("time").sleep(0.01)
            assert worker.status()["present"]
        finally:
            worker.stop()
        assert port.closed and not worker.status()["present"]

    def test_build_worker_selects_the_backend(self):
        assert isinstance(M.build_worker("COM5"), M.ModemWorker)
        assert M.build_worker("").port == ""
        mm = M.build_worker("mmcli:2")
        assert isinstance(mm, M.MmcliWorker) and mm.modem == "2"
        assert M.build_worker("mmcli").modem == "any"


# ── cap and transports ────────────────────────────────────────────────────────
class TestDailyCounter:
    def test_cap_day_roll_seed_and_give_back(self):
        today = [date(2026, 10, 15)]
        counter = M.DailyCounter(30, today=lambda: today[0])
        assert counter.take(29) and not counter.take(2) and counter.take(1)
        assert counter.remaining == 0
        counter.give_back(3)
        assert counter.used == 27
        today[0] = date(2026, 10, 16)
        assert counter.used == 0
        counter.seed(12)
        assert counter.used == 12

    def test_provider_ids(self):
        assert M.provider_id_for([5, 6]) == "modem-mr:5,6"
        assert M.references_of("modem-mr:5,6") == [5, 6] and M.references_of("tg:1:2") == []
        assert M.segments_of("modem-mr:5,6") == 2 and M.segments_of(None) == 1


class StubWorker:
    def __init__(self, error: Exception | None = None, parts: int = 1):
        self.error = error
        self.parts = parts
        self.sent: list[tuple[str, str]] = []
        self.dialled: list[str] = []

    def available(self):
        return True, "ok"

    def count_parts(self, text):
        return self.parts

    def send_sms(self, number, text):
        if self.error is not None:
            raise self.error
        self.sent.append((number, text))
        return list(range(self.parts))

    def dial(self, number, seconds):
        if self.error is not None:
            raise self.error
        self.dialled.append(number)
        return "rang"


def recipient(address=TEAM):
    return Recipient("owner", "Riya", "modem_sms", address, "owner")


MESSAGE = Message(template="sos.prompt", step="t0", text="Need help? Reply 1.")


class TestModemTransports:
    def test_only_profile_numbers(self):
        worker = StubWorker()
        transport = M.ModemSmsTransport(worker, allowlist=lambda: frozenset({TEAM}))
        refused = transport.send(recipient("+919800000000"), MESSAGE)
        assert refused.suppressed and not refused.retryable and "not_allowed" in refused.error
        assert transport.send(recipient("98123 45610"), MESSAGE).provider_id == "modem-mr:0"
        assert worker.sent == [(TEAM, MESSAGE.text)]

    def test_daily_cap_counts_segments(self):
        worker = StubWorker(parts=2)
        counter = M.DailyCounter(3)
        transport = M.ModemSmsTransport(worker, counter=counter,
                                        allowlist=lambda: frozenset({TEAM}))
        assert transport.send(recipient(), MESSAGE).ok
        capped = transport.send(recipient(), MESSAGE)
        assert not capped.ok and not capped.retryable and capped.error.startswith("daily_cap")
        assert counter.used == 2 and transport.state() == (True, "ok")
        counter.take(1)
        assert transport.state() == (False, "daily SMS cap reached (3)")

    def test_errors_give_unsent_segments_back(self):
        counter = M.DailyCounter(30)
        partial = M.AtError("AT+CMGS=40", "+CMS ERROR: 38", sent_parts=1)
        transport = M.ModemSmsTransport(StubWorker(partial, parts=3), counter=counter,
                                        allowlist=lambda: frozenset({TEAM}))
        result = transport.send(recipient(), MESSAGE)
        assert not result.ok and result.retryable and counter.used == 1
        down = M.ModemSmsTransport(StubWorker(M.ModemUnavailable("unplugged")), counter=counter,
                                   allowlist=lambda: frozenset({TEAM}))
        result = down.send(recipient(), MESSAGE)
        assert result.retryable and "unplugged" in result.error and counter.used == 1

    def test_call_transport(self):
        worker = StubWorker()
        transport = M.ModemCallTransport(worker, allowlist=lambda: frozenset({TEAM}))
        call = Recipient("owner", "Riya", "modem_call", TEAM, "owner")
        assert transport.send(call, MESSAGE).provider_id == "modem-call:rang"
        assert transport.send(replace(call, address="+919800000000"), MESSAGE).suppressed

    def test_default_allowlist_is_the_profile(self, temp_db, monkeypatch):
        monkeypatch.setenv("ALERT_PHONE_NUMBER", TEAM)
        monkeypatch.setenv("SENTRIX_DESK_NUMBER", "+919811111111")
        assert M.profile_allowlist() == frozenset({TEAM, "+919811111111"})


# ── ModemManager ──────────────────────────────────────────────────────────────
class FakeMmcli:
    def __init__(self, missing: bool = False):
        self.calls: list[list[str]] = []
        self.missing = missing
        self.info = {"modem": {"generic": {"state": "registered",
                                           "signal-quality": {"value": "70"}},
                               "3gpp": {"registration-state": "home",
                                        "operator-name": "Airtel"}}}

    def __call__(self, args, timeout):
        if self.missing:
            raise FileNotFoundError("mmcli")
        self.calls.append(args)
        if args[-1] == "-J":
            return 0, json.dumps(self.info), ""
        if any(a.startswith("--messaging-create-sms=") for a in args):
            return 0, "Successfully created new SMS: /org/freedesktop/ModemManager1/SMS/3\n", ""
        if any(a.startswith("--voice-create-call=") for a in args):
            return 0, "Successfully created new call: /org/freedesktop/ModemManager1/Call/1\n", ""
        return 0, "successfully done\n", ""


class TestMmcli:
    def test_status_send_and_call(self):
        runner = FakeMmcli()
        worker = M.MmcliWorker("any", runner=runner, sleep=lambda s: None)
        worker.step()
        status = worker.status()
        assert status["present"] and status["registered"] and status["signal_percent"] == 70
        assert worker.available() == (True, "Airtel (ModemManager)")
        assert worker.send_sms(TEAM, "it's \"ok\"") == []
        create = next(a for a in runner.calls if a[-1].startswith("--messaging-create-sms="))
        assert create[-1] == ("--messaging-create-sms=text=\"it's 'ok'\","
                              f"number=\"{TEAM}\"")
        assert ["mmcli", "-s", "/org/freedesktop/ModemManager1/SMS/3", "--send"] in runner.calls
        assert worker.dial(TEAM, 1.0) == "rang"
        assert ["mmcli", "-o", "/org/freedesktop/ModemManager1/Call/1", "--hangup"] in runner.calls

    def test_missing_mmcli_is_unavailable(self):
        worker = M.MmcliWorker(runner=FakeMmcli(missing=True))
        worker.step()
        assert worker.available()[0] is False
        with pytest.raises(M.ModemUnavailable):
            worker.send_sms(TEAM, "x")


# ── end to end: router -> modem -> delivery report ────────────────────────────
def test_sms_to_the_owner_is_sent_then_delivered(temp_db, monkeypatch):
    from sentrix.alerting.channels import router as R
    from sentrix.config import settings
    for name in ("SENTRIX_LIVE_ALERTS", "SENTRIX_MODEM_LIVE"):
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("SENTRIX_TEST_RECIPIENTS", TEAM)
    monkeypatch.setenv("ALERT_PHONE_NUMBER", TEAM)
    worker, port = worker_with()
    worker.step()
    configured = replace(settings, channels=replace(settings.channels, enabled=True,
                                                    modem_enabled=True))
    service = R.build_device_channels(configured, modem_worker=worker)
    heard = []
    service.add_listener(lambda item: heard.append(item.status))
    target = Recipient("owner", "Riya", "modem_sms", TEAM, "owner")
    assert service.enqueue(target, MESSAGE)
    assert service.enqueue(Recipient("contact:9", "X", "modem_sms", "+919800000000",
                                     "contact"), MESSAGE)
    service.step()
    assert sorted(heard) == [SENT, SUPPRESSED]
    text = decode_submit(port.payloads[0].decode())["text"]
    assert text.startswith(guard.TEST_PREFIX_GSM) and "Need help?" in text
    port.emit('+CDS: 6,5,"+919812345610",145,"26/10/15,10:00:00+22",'
              '"26/10/15,10:00:05+22",0')
    worker.step()
    service.step()
    assert heard[-1] == DELIVERED
    assert service.status()["modem"]["sms_today"] == 1
    assert service.status()["outbox"][DELIVERED] == 1


def test_failed_delivery_report_fails_the_sms(temp_db, monkeypatch):
    from sentrix.alerting.channels import router as R
    from sentrix.config import settings
    for name in ("SENTRIX_LIVE_ALERTS", "SENTRIX_MODEM_LIVE"):
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("SENTRIX_TEST_RECIPIENTS", TEAM)
    monkeypatch.setenv("ALERT_PHONE_NUMBER", TEAM)
    worker, port = worker_with()
    worker.step()
    configured = replace(settings, channels=replace(settings.channels, enabled=True,
                                                    modem_enabled=True))
    service = R.build_device_channels(configured, modem_worker=worker)
    service.enqueue(recipient(), MESSAGE)
    service.step()
    port.emit('+CDS: 6,5,"+919812345610",145,"26/10/15,10:00:00+22",'
              '"26/10/15,10:00:05+22",65')
    worker.step()
    service.step()
    item = service.recent()[0]
    assert item["status"] == FAILED and "0x41" in item["error"]


def test_the_cap_is_seeded_from_todays_outbox(temp_db, monkeypatch):
    from sentrix.alerting.channels import router as R
    from sentrix.config import settings
    for name in ("SENTRIX_LIVE_ALERTS", "SENTRIX_MODEM_LIVE"):
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("SENTRIX_TEST_RECIPIENTS", TEAM)
    monkeypatch.setenv("ALERT_PHONE_NUMBER", TEAM)
    configured = replace(settings, channels=replace(settings.channels, enabled=True,
                                                    modem_enabled=True))
    worker, _ = worker_with()
    worker.step()
    first = R.build_device_channels(configured, modem_worker=worker)
    first.enqueue(recipient(), replace(MESSAGE, text="y" * 300))
    first.step()
    segments = M.segments_of(first.recent()[0]["provider_id"])
    assert segments == 3, "300 characters plus the GSM test banner"
    restarted = R.build_device_channels(configured, modem_worker=StubWorker())
    restarted._seed_sms_counter()
    assert restarted.sms_counter.used == segments
