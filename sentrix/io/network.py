"""Wi-Fi setup and network status through NetworkManager's ``nmcli``.

Used by T-WEB's ``/setup/wifi`` page (which exists only while the unit is in
physical setup mode, ``SENTRIX_SETUP_MODE=1``) and by the Node page and
``doctor``. Three calls, frozen by docs/7 §2.3:

* ``scan() -> [{ssid, signal, security}]`` — visible networks, strongest
  first, one row per SSID, hidden networks left out;
* ``connect(ssid, psk) -> {ok, error}`` — saves a Wi-Fi profile with the
  failover route metric (600: below a ModemManager modem's 700, above
  Ethernet's 100; ``deploy/pi5/networkmanager/``) and brings it up;
* ``status() -> {connected, ssid, ip, via}`` — what carries the default
  route now (``via``: wifi, ethernet, modem or None).

The pre-shared key is never logged and never stored by SENTRIX; it is handed
to NetworkManager, which keeps it in its root-only profile store. (It is on
nmcli's argv for the duration of one call — acceptable in setup mode, where
the unit is in the installer's hands; there is no stdin route for it.)

Off Linux, or without nmcli, every call degrades: ``scan`` is empty,
``connect`` fails with a reason, ``status`` reports ``available: False``. The
command runner is injectable, so tests use a fake nmcli.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import Any, Callable

from sentrix.utils.logging import get_logger

log = get_logger("network")

#: Route metrics of the failover design (lower wins).
WIFI_ROUTE_METRIC = 600
MODEM_ROUTE_METRIC = 700
#: Prefix of the Wi-Fi profiles SENTRIX creates.
PROFILE_PREFIX = "sentrix-wifi-"

#: ``runner(argv, timeout) -> (returncode, stdout, stderr)``.
Runner = Callable[[list[str], float], tuple[int, str, str]]


def _run(argv: list[str], timeout: float) -> tuple[int, str, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              check=False)
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except OSError as exc:
        return 127, "", str(exc)
    return done.returncode, done.stdout, done.stderr


class Nmcli:
    """A small, injectable ``nmcli``."""

    def __init__(self, runner: Runner | None = None, path: str | None = None) -> None:
        self._runner = runner
        self._path = path if path is not None else (None if runner else shutil.which("nmcli"))

    @property
    def available(self) -> bool:
        return self._runner is not None or self._path is not None

    def __call__(self, *args: str, timeout: float = 15.0) -> tuple[int, str, str]:
        if self._runner is not None:
            return self._runner(["nmcli", *args], timeout)
        if self._path is None:
            return 127, "", "nmcli not available"
        return _run([self._path, *args], timeout)


_default = Nmcli()


def split_terse(line: str) -> list[str]:
    """One ``nmcli -t`` line -> fields (``:`` separates, ``\\:`` and ``\\\\``
    are escaped)."""
    fields, current, escaped = [], [], False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    fields.append("".join(current))
    return fields


def scan(nmcli: Nmcli | None = None, *, rescan: bool = True) -> list[dict[str, Any]]:
    """Visible Wi-Fi networks, strongest first, one row per SSID."""
    nmcli = nmcli or _default
    if not nmcli.available:
        return []
    code, out, err = nmcli("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                           "--rescan", "yes" if rescan else "no", timeout=30.0)
    if code != 0:
        log.warning("Wi-Fi scan failed: %s", err.strip()[:200])
        return []
    best: dict[str, dict[str, Any]] = {}
    for line in out.splitlines():
        fields = split_terse(line)
        if len(fields) < 3 or not fields[0]:
            continue
        ssid, signal, security = fields[0], fields[1], ":".join(fields[2:])
        try:
            strength = int(signal)
        except ValueError:
            strength = 0
        row = {"ssid": ssid, "signal": strength,
               "security": security.strip() if security.strip() not in ("", "--") else "open"}
        if ssid not in best or strength > best[ssid]["signal"]:
            best[ssid] = row
    return sorted(best.values(), key=lambda r: (-r["signal"], r["ssid"]))


def validate(ssid: str, psk: str | None) -> str | None:
    """Why these credentials cannot be right, or None."""
    if not ssid or len(ssid.encode("utf-8")) > 32:
        return "the network name must be 1-32 bytes"
    if any(ord(c) < 32 for c in ssid):
        return "the network name contains control characters"
    if psk:
        if re.fullmatch(r"[0-9a-fA-F]{64}", psk):
            return None
        if not 8 <= len(psk) <= 63 or any(not 32 <= ord(c) <= 126 for c in psk):
            return "the password must be 8-63 printable characters"
    return None


def profile_name(ssid: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", ssid).strip("-")[:40] or "network"
    return PROFILE_PREFIX + slug


def _error_text(err: str) -> str:
    text = err.strip().splitlines()[-1] if err.strip() else "nmcli failed"
    lowered = text.lower()
    if "secrets were required" in lowered or "802-1x" in lowered or "psk" in lowered:
        return "wrong password"
    if "no network with ssid" in lowered:
        return "network not found"
    return text[:200]


def connect(ssid: str, psk: str | None, nmcli: Nmcli | None = None, *,
            ifname: str = "wlan0") -> dict[str, Any]:
    """Save a Wi-Fi profile for ``ssid`` (metric 600, autoconnect) and bring
    it up. An existing SENTRIX profile for the same SSID is replaced."""
    nmcli = nmcli or _default
    problem = validate(ssid, psk)
    if problem:
        return {"ok": False, "error": problem}
    if not nmcli.available:
        return {"ok": False, "error": "nmcli not available (NetworkManager is not installed)"}
    name = profile_name(ssid)
    nmcli("connection", "delete", "id", name, timeout=15.0)       # absent is fine
    args = ["connection", "add", "type", "wifi", "con-name", name, "ifname", ifname,
            "ssid", ssid, "connection.autoconnect", "yes",
            "connection.autoconnect-priority", "10",
            "ipv4.route-metric", str(WIFI_ROUTE_METRIC),
            "ipv6.route-metric", str(WIFI_ROUTE_METRIC)]
    if psk:
        args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", psk]
    code, _, err = nmcli(*args, timeout=15.0)
    if code != 0:
        log.warning("Wi-Fi profile for %r not saved: %s", ssid, _error_text(err))
        return {"ok": False, "error": _error_text(err)}
    code, _, err = nmcli("--wait", "45", "connection", "up", "id", name, timeout=60.0)
    if code != 0:
        log.warning("Wi-Fi %r did not come up: %s", ssid, _error_text(err))
        return {"ok": False, "error": _error_text(err)}
    log.info("Wi-Fi %r connected (profile %s)", ssid, name)
    return {"ok": True, "error": None}


_VIA = {"wifi": "wifi", "ethernet": "ethernet", "gsm": "modem", "cdma": "modem",
        "modem": "modem", "bt": "bluetooth"}


def status(nmcli: Nmcli | None = None) -> dict[str, Any]:
    """``{connected, ssid, ip, via, device, available}`` for the device that
    holds the lowest-metric default route."""
    nmcli = nmcli or _default
    result: dict[str, Any] = {"connected": None, "ssid": None, "ip": None, "via": None,
                              "device": None, "available": nmcli.available}
    if not nmcli.available:
        return result
    code, out, _ = nmcli("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status")
    if code != 0:
        return result
    connected = []
    for line in out.splitlines():
        fields = split_terse(line)
        if len(fields) >= 4 and fields[2].startswith("connected") and fields[1] != "loopback":
            connected.append(fields)
    result["connected"] = bool(connected)
    if not connected:
        return result
    device, kind = _default_route_device(nmcli, connected)
    result["device"] = device
    # A 4G dongle in ECM/RNDIS mode is an Ethernet-type device named usb0 or
    # wwan0 to NetworkManager; only ModemManager modems have type gsm.
    if kind == "ethernet" and device.startswith(("usb", "wwan")):
        kind = "modem"
    result["via"] = _VIA.get(kind, kind)
    code, out, _ = nmcli("-g", "IP4.ADDRESS", "device", "show", device)
    if code == 0 and out.strip():
        result["ip"] = out.strip().splitlines()[0].split("|")[0].split("/")[0].strip()
    if kind == "wifi":
        code, out, _ = nmcli("-t", "-f", "ACTIVE,SSID", "device", "wifi", "list",
                             "ifname", device, "--rescan", "no")
        for line in out.splitlines() if code == 0 else []:
            fields = split_terse(line)
            if len(fields) >= 2 and fields[0] == "yes":
                result["ssid"] = fields[1]
                break
    return result


def _default_route_device(nmcli: Nmcli, connected: list[list[str]]) -> tuple[str, str]:
    """The connected device with the lowest IPv4 route metric."""
    best: tuple[int, str, str] | None = None
    for fields in connected:
        device, kind = fields[0], fields[1]
        code, out, _ = nmcli("-g", "IP4.ROUTE", "device", "show", device)
        metric = 1 << 30
        if code == 0:
            for route in out.replace("|", "\n").splitlines():
                if "dst = 0.0.0.0/0" in route:
                    match = re.search(r"mt = (\d+)", route)
                    if match:
                        metric = min(metric, int(match.group(1)))
        if best is None or metric < best[0]:
            best = (metric, device, kind)
    assert best is not None
    return best[1], best[2]


__all__ = ["Nmcli", "scan", "connect", "status", "validate", "profile_name",
           "split_terse", "WIFI_ROUTE_METRIC", "MODEM_ROUTE_METRIC"]
