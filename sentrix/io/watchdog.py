"""systemd readiness and watchdog notifications (``sd_notify`` without libsystemd).

``deploy/pi5/systemd/sentrix.service`` runs the device process as
``Type=notify`` with ``WatchdogSec=``: systemd waits for ``READY=1`` and then
restarts the unit unless ``WATCHDOG=1`` arrives at least once per watchdog
interval. The node service (``sentrix.io.node``) sends both, and sends the
watchdog ping **only while the process is really alive**: the web server's
event loop answers and the live capture loop is running. A hung process
therefore stops pinging and systemd restarts it (``Restart=always``); the
board's hardware watchdog (``kernel_watchdog_timeout`` in ``config.txt`` plus
systemd's ``RuntimeWatchdogSec``) covers a hung kernel.

The protocol is one datagram per message on the AF_UNIX socket named by
``NOTIFY_SOCKET`` (a leading ``@`` is the abstract namespace). Outside systemd
(a laptop, the tests) ``NOTIFY_SOCKET`` is unset and every call is a no-op.
"""

from __future__ import annotations

import os
import socket
from typing import Callable, Mapping

from sentrix.utils.logging import get_logger

log = get_logger("watchdog")


def _send_datagram(address: str, message: bytes) -> None:
    if address.startswith("@"):
        address = "\0" + address[1:]
    family = getattr(socket, "AF_UNIX", None)
    if family is None:                              # pragma: no cover - Windows
        raise OSError("AF_UNIX is not available on this platform")
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.connect(address)
        sock.sendall(message)


class SystemdNotifier:
    """``ready()``, ``watchdog()``, ``status(text)``, ``stopping()``.

    ``env`` defaults to the process environment; ``sender(address, bytes)``
    replaces the socket in tests. ``watchdog_seconds`` is systemd's interval
    (``WATCHDOG_USEC``), None when no watchdog was requested for this PID.
    """

    def __init__(self, env: Mapping[str, str] | None = None,
                 sender: Callable[[str, bytes], None] | None = None,
                 pid: int | None = None) -> None:
        env = os.environ if env is None else env
        self.address = (env.get("NOTIFY_SOCKET") or "").strip() or None
        self._sender = sender or _send_datagram
        self.watchdog_seconds: float | None = None
        usec = (env.get("WATCHDOG_USEC") or "").strip()
        owner = (env.get("WATCHDOG_PID") or "").strip()
        pid = os.getpid() if pid is None else pid
        if usec.isdigit() and int(usec) > 0 and (not owner or owner == str(pid)):
            self.watchdog_seconds = int(usec) / 1_000_000
        self._failed = False

    @property
    def enabled(self) -> bool:
        return self.address is not None

    @property
    def ping_interval(self) -> float | None:
        """How often to ping: a third of the interval, so one late check is
        survivable."""
        return None if self.watchdog_seconds is None else max(0.5, self.watchdog_seconds / 3)

    def notify(self, *lines: str) -> bool:
        if self.address is None:
            return False
        try:
            self._sender(self.address, "\n".join(lines).encode("utf-8"))
            self._failed = False
            return True
        except OSError as exc:
            if not self._failed:                    # say it once, not every ping
                log.error("sd_notify to %s failed: %s", self.address, exc)
            self._failed = True
            return False

    def ready(self, status: str | None = None) -> bool:
        return self.notify("READY=1", *([f"STATUS={status}"] if status else []))

    def watchdog(self) -> bool:
        return self.notify("WATCHDOG=1")

    def status(self, text: str) -> bool:
        return self.notify(f"STATUS={text}")

    def stopping(self) -> bool:
        return self.notify("STOPPING=1")


__all__ = ["SystemdNotifier"]
