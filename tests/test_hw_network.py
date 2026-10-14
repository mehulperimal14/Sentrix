"""nmcli wrapper for /setup/wifi and the Node page (T-HW). A fake nmcli answers;
no network is touched and the pre-shared key never reaches a log."""

from __future__ import annotations

import logging

import pytest

from sentrix.io import network
from sentrix.io.network import Nmcli


class FakeNmcli:
    """Answers by the argv prefix; records every call."""

    def __init__(self, answers: dict[tuple, tuple[int, str, str]] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout):
        self.calls.append(argv[1:])
        args = tuple(argv[1:])
        for prefix in sorted(self.answers, key=len, reverse=True):
            if args[:len(prefix)] == prefix:
                return self.answers[prefix]
        return 0, "", ""


SCAN = "\n".join([
    "Home:72:WPA2",
    "Home:40:WPA2",                       # the same SSID on another AP
    r"Cafe\:Free:55:",                    # an escaped colon, open network
    ":90:WPA2",                           # hidden network
    "Office:81:WPA2 802.1X",
])


class TestScan:
    def test_parse_dedupe_and_order(self):
        fake = FakeNmcli({("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list"):
                          (0, SCAN, "")})
        rows = network.scan(Nmcli(fake))
        assert rows == [{"ssid": "Office", "signal": 81, "security": "WPA2 802.1X"},
                        {"ssid": "Home", "signal": 72, "security": "WPA2"},
                        {"ssid": "Cafe:Free", "signal": 55, "security": "open"}]
        assert fake.calls[0][-2:] == ["--rescan", "yes"]

    def test_failure_and_unavailable(self):
        assert network.scan(Nmcli(FakeNmcli({("-t",): (10, "", "Wi-Fi is disabled")}))) == []
        absent = Nmcli(path=None)
        absent._path = None
        assert network.scan(absent) == []

    def test_split_terse(self):
        assert network.split_terse(r"a\:b:c\\d:") == ["a:b", "c\\d", ""]


class TestConnect:
    def test_saves_a_metric_600_profile_and_brings_it_up(self, caplog):
        fake = FakeNmcli()
        caplog.set_level(logging.DEBUG, logger="sentrix")
        result = network.connect("Home WiFi", "correct horse battery", Nmcli(fake))
        assert result == {"ok": True, "error": None}
        delete, add, up = fake.calls
        assert delete[:3] == ["connection", "delete", "id"]
        assert add[:2] == ["connection", "add"]
        options = dict(zip(add[2::2], add[3::2]))
        assert options["con-name"] == "sentrix-wifi-Home-WiFi" and options["ssid"] == "Home WiFi"
        assert options["ipv4.route-metric"] == "600" and options["ipv6.route-metric"] == "600"
        assert options["wifi-sec.key-mgmt"] == "wpa-psk"
        assert up == ["--wait", "45", "connection", "up", "id", "sentrix-wifi-Home-WiFi"]
        assert "correct horse" not in caplog.text, "the PSK is never logged"

    def test_open_network_has_no_security(self):
        fake = FakeNmcli()
        assert network.connect("Cafe", "", Nmcli(fake))["ok"]
        assert "wifi-sec.psk" not in fake.calls[1]

    @pytest.mark.parametrize("ssid,psk,error", [
        ("", "password1", "1-32 bytes"),
        ("x" * 33, "password1", "1-32 bytes"),
        ("bad\nname", "password1", "control characters"),
        ("Home", "short", "8-63"),
        ("Home", "é" * 10, "8-63"),
    ])
    def test_validation(self, ssid, psk, error):
        fake = FakeNmcli()
        result = network.connect(ssid, psk, Nmcli(fake))
        assert not result["ok"] and error in result["error"] and fake.calls == []

    def test_hex_psk_is_accepted(self):
        assert network.validate("Home", "a" * 64) is None

    def test_wrong_password(self):
        fake = FakeNmcli({("--wait",): (4, "", "Error: Connection activation failed: "
                                                "Secrets were required, but not provided.")})
        assert network.connect("Home", "password1", Nmcli(fake)) == {"ok": False,
                                                                     "error": "wrong password"}

    def test_profile_not_saved(self):
        fake = FakeNmcli({("connection", "add"): (2, "", "Error: invalid property 'x'")})
        result = network.connect("Home", "password1", Nmcli(fake))
        assert not result["ok"] and "invalid property" in result["error"]

    def test_without_networkmanager(self):
        absent = Nmcli(path=None)
        absent._path = None
        result = network.connect("Home", "password1", absent)
        assert not result["ok"] and "nmcli" in result["error"]


def _status_answers(device_rows: str, routes: dict[str, str], *, ssid_rows: str = ""):
    answers = {("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"):
               (0, device_rows, "")}
    for device, route in routes.items():
        answers[("-g", "IP4.ROUTE", "device", "show", device)] = (0, route, "")
        answers[("-g", "IP4.ADDRESS", "device", "show", device)] = (0, f"10.0.0.{len(device)}/24", "")
    answers[("-t", "-f", "ACTIVE,SSID", "device", "wifi", "list")] = (0, ssid_rows, "")
    return answers


class TestStatus:
    def test_wifi_carries_the_default_route(self):
        fake = FakeNmcli(_status_answers(
            "wlan0:wifi:connected:sentrix-wifi-Home\nusb0:ethernet:connected:sentrix-modem-ecm\n"
            "lo:loopback:connected (externally):lo\n",
            {"wlan0": "dst = 0.0.0.0/0, nh = 192.168.1.1, mt = 600 | "
                      "dst = 192.168.1.0/24, nh = 0.0.0.0, mt = 600",
             "usb0": "dst = 0.0.0.0/0, nh = 10.1.1.1, mt = 700"},
            ssid_rows="no:Neighbour\nyes:Home\n"))
        status = network.status(Nmcli(fake))
        assert status == {"connected": True, "ssid": "Home", "ip": "10.0.0.5", "via": "wifi",
                          "device": "wlan0", "available": True}

    def test_failover_to_an_ecm_modem_reads_as_modem(self):
        fake = FakeNmcli(_status_answers(
            "wlan0:wifi:connected:sentrix-wifi-Home\nusb0:ethernet:connected:sentrix-modem-ecm\n",
            {"wlan0": "dst = 0.0.0.0/0, nh = 192.168.1.1, mt = 20600",   # failed check
             "usb0": "dst = 0.0.0.0/0, nh = 10.1.1.1, mt = 700"}))
        status = network.status(Nmcli(fake))
        assert status["via"] == "modem" and status["device"] == "usb0" and status["ssid"] is None

    def test_disconnected_and_unavailable(self):
        fake = FakeNmcli(_status_answers("wlan0:wifi:disconnected:--\n", {}))
        assert network.status(Nmcli(fake))["connected"] is False
        absent = Nmcli(path=None)
        absent._path = None
        assert network.status(absent) == {"connected": None, "ssid": None, "ip": None,
                                          "via": None, "device": None, "available": False}
