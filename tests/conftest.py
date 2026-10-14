"""Test-session safety net and shared fixtures (foundation-owned; docs/7 §14).

This module is imported by pytest before any test module, so the environment
below is in place before anything imports ``sentrix``:

* **No live alert switch survives.** ``sentrix/app.py`` calls ``load_dotenv``
  when imported, and ``load_dotenv`` never overrides a variable that is
  already set, so every switch is set here explicitly (test mode on, live
  alerts and every channel switch off, no allowlist, no provider keys). A
  developer's ``.env`` with live Twilio keys therefore reaches no test.
* **Every service is off** and runtime files go to a temporary directory
  (the device ``var/`` tree via ``SENTRIX_VAR_DIR``, Portal data dir and
  database, device identity dir).
* **No network.** A socket guard allows loopback only; anything else raises
  :class:`NetworkBlocked` (an ``OSError``) and is recorded in
  ``BLOCKED_CONNECTIONS``.

Shared fixtures: ``temp_db`` (the device database on a temporary SQLite file,
tables created), ``portal_app`` (a Portal on its own temporary database and
keys), ``fake_router`` (a ``ChannelRouter`` with a ``FakeTransport`` on every
channel, a memory outbox and a live-in-test-mode policy). After every test
the listener registry, the services registry, the runtime mode, the cached
profile and the siren-test limiter are reset.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import sys
import tempfile
from pathlib import Path

# ── environment, before any sentrix import ────────────────────────────────────
_SESSION_DIR = Path(tempfile.mkdtemp(prefix="sentrix-tests-"))

_FORCED_ENV = {
    "SENTRIX_TEST_MODE": "1", "PORTAL_TEST_MODE": "1", "SENTRIX_LIVE_ALERTS": "0",
    "SENTRIX_TEST_RECIPIENTS": "",
    # channel switches (sentrix.alerting.guard.CHANNEL_SWITCHES)
    "SENTRIX_MODEM_LIVE": "0", "SENTRIX_TWILIO_ENABLED": "0",
    "SENTRIX_TELEGRAM_LIVE": "0", "SENTRIX_EMAIL_LIVE": "0", "SENTRIX_WEBPUSH_LIVE": "0",
    # services
    "SENTRIX_SOS_ENABLED": "0", "SENTRIX_LINK_ENABLED": "0",
    "SENTRIX_CHANNELS_ENABLED": "0", "SENTRIX_MODEM_ENABLED": "0",
    "SENTRIX_GPIO_ENABLED": "0", "SENTRIX_NODE_ENABLED": "0", "PORTAL_WORKERS": "0",
    # provider credentials: empty, so .env cannot supply them
    "TWILIO_ACCOUNT_SID": "", "TWILIO_AUTH_TOKEN": "", "TWILIO_PHONE_NUMBER": "",
    "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_WEBHOOK_SECRET": "", "BREVO_API_KEY": "",
    "RESEND_API_KEY": "", "SENTRIX_PORTAL_URL": "",
    # runtime files: the whole device var/ tree (database, events, evidence,
    # snapshots, faces, calibration, mode) in the session dir, never var/
    "SENTRIX_VAR_DIR": str(_SESSION_DIR / "var"),
    # no systemd notify socket: a lifespan test never builds the watchdog
    "NOTIFY_SOCKET": "", "WATCHDOG_USEC": "",
    "PORTAL_DATA_DIR": str(_SESSION_DIR / "portal"),
    "PORTAL_DATABASE_URL": f"sqlite:///{(_SESSION_DIR / 'portal' / 'portal.db').as_posix()}",
    "SENTRIX_DEVICE_IDENTITY_DIR": str(_SESSION_DIR / "identity"),
}
os.environ.update(_FORCED_ENV)

if "sentrix" in sys.modules:                       # pragma: no cover - plugin order
    raise RuntimeError("sentrix was imported before tests/conftest.py set the "
                       "test environment")

# ── socket guard ──────────────────────────────────────────────────────────────
import pytest  # noqa: E402

BLOCKED_CONNECTIONS: list[str] = []


class NetworkBlocked(OSError):
    """A test tried to reach a non-loopback address."""


def _loopback(host) -> bool:
    if host in (None, "", "localhost") or str(host).endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False


def _check_address(address) -> None:
    if isinstance(address, tuple) and address and not _loopback(address[0]):
        BLOCKED_CONNECTIONS.append(repr(address))
        raise NetworkBlocked(f"tests/conftest.py socket guard: no network ({address!r})")


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo


def _guarded_connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        _check_address(address)
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        _check_address(address)
    return _real_connect_ex(self, address)


def _guarded_getaddrinfo(host, *args, **kwargs):
    if not _loopback(host.decode() if isinstance(host, bytes) else host):
        BLOCKED_CONNECTIONS.append(f"resolve {host!r}")
        raise socket.gaierror(socket.EAI_NONAME,
                              f"tests/conftest.py socket guard: no network ({host!r})")
    return _real_getaddrinfo(host, *args, **kwargs)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
socket.getaddrinfo = _guarded_getaddrinfo

if sys.platform == "win32":
    # asyncio's Proactor loop connects through ConnectEx, not socket.connect.
    try:
        from asyncio import windows_events

        _real_proactor_connect = windows_events.IocpProactor.connect

        def _guarded_proactor_connect(self, conn, address):
            if conn.family in (socket.AF_INET, socket.AF_INET6):
                _check_address(address)
            return _real_proactor_connect(self, conn, address)

        windows_events.IocpProactor.connect = _guarded_proactor_connect
    except Exception:                               # pragma: no cover
        pass


def pytest_terminal_summary(terminalreporter):
    if BLOCKED_CONNECTIONS:
        terminalreporter.write_line(
            f"socket guard blocked {len(BLOCKED_CONNECTIONS)} network attempt(s): "
            f"{sorted(set(BLOCKED_CONNECTIONS))[:10]}", yellow=True)


# ── per-test reset ────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _sentrix_isolation(tmp_path, monkeypatch):
    """Runtime mode in a temp file; registries and caches empty afterwards."""
    from sentrix import mode
    monkeypatch.setattr(mode, "MODE_PATH", tmp_path / "mode.json")
    yield
    from sentrix import listeners, services
    listeners.clear()
    services.registry.clear()
    mode.clear_override()
    if "sentrix.link.profile" in sys.modules:
        sys.modules["sentrix.link.profile"].forget_cache()
    if "sentrix.io.actuators" in sys.modules:
        actuators = sys.modules["sentrix.io.actuators"]
        actuators._reset_siren_test_limiter()
        actuators.clear_arming_source()
    if "sentrix.io.gpio" in sys.modules:
        sys.modules["sentrix.io.gpio"]._reset_shared_pin_factory()


# ── shared fixtures ───────────────────────────────────────────────────────────
@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """The device database on a temporary SQLite file, every table created.
    Never ``var/sentrix.db``."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from sentrix.storage import database

    engine = create_engine(f"sqlite:///{tmp_path / 'device.db'}",
                           connect_args={"check_same_thread": False})
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal",
                        sessionmaker(bind=engine, autocommit=False, autoflush=False))
    database.init_db()
    if "sentrix.link.profile" in sys.modules:
        sys.modules["sentrix.link.profile"].forget_cache()
    yield engine
    engine.dispose()


@pytest.fixture
def portal_app(tmp_path):
    """A Portal app on its own temporary database and keys (no workers)."""
    from sentrix_portal.app import create_app
    from sentrix_portal.settings import PortalSettings
    app = create_app(PortalSettings.for_tests(tmp_path))
    yield app
    app.state.db.dispose()


def live_test_policy():
    """A send policy with every switch on in test mode, allowlisting the
    addresses the shared fixtures use. Messages still only reach fakes."""
    from sentrix.alerting import guard
    return guard.SendPolicy(test_mode=True, live_alerts=True,
                            switches={ch: True for ch in guard.CHANNEL_SWITCHES},
                            allowlist=frozenset({"+919812345610", "owner@example.com",
                                                 "tg:12345"}))


@pytest.fixture
def fake_router():
    """``(router, transports)``: a ChannelRouter with a FakeTransport per
    channel and a memory outbox, under :func:`live_test_policy`."""
    from sentrix.alerting import guard
    from sentrix.alerting.channels.base import (Channel, ChannelRouter, FakeTransport,
                                                MemoryOutbox)
    transports = {name: FakeTransport(name) for name in guard.CHANNEL_SWITCHES}
    router = ChannelRouter({name: Channel(name, t, policy_factory=live_test_policy)
                            for name, t in transports.items()}, MemoryOutbox())
    return router, transports
