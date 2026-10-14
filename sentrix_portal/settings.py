"""Portal settings, read from the environment when the app is created.

Unlike the device's ``sentrix.config`` (read once at import), the Portal builds
a :class:`PortalSettings` per app so tests can point it at a temporary
database. Secrets are fields here (the Portal has no ``/api/config``), but are
never logged or rendered.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field

from sentrix.paths import PORTAL_VAR_DIR


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _str(name: str, default: str) -> str:
    return os.getenv(name, "").strip() or default


DEFAULT_DATABASE_URL = f"sqlite:///{(PORTAL_VAR_DIR / 'portal.db').as_posix()}"


@dataclass(frozen=True)
class PortalSettings:
    database_url: str = DEFAULT_DATABASE_URL
    #: HMAC key for sessions ids, codes, audit chain and IP hashes. Generated
    #: (ephemeral, with a warning) only in test mode.
    secret_key: str = ""
    #: Column encryption (phones, medical notes, TOTP secrets); derived from
    #: ``secret_key`` when unset, in test mode only.
    data_key: str = ""
    base_url: str = "http://portal.localhost:8100"
    #: Decision D13: on unless PORTAL_TEST_MODE=0.
    test_mode: bool = True
    host: str = "127.0.0.1"
    port: int = 8100
    cookie_name: str = "portal_session"
    csrf_cookie_name: str = "portal_csrf"
    csrf_header: str = "X-CSRF-Token"
    secure_cookies: bool = False
    session_seconds: int = 12 * 3600
    session_idle_seconds: int = 2 * 3600
    step_up_seconds: int = 300
    activation_ttl_seconds: int = 72 * 3600
    claim_attempts: int = 5
    claim_window_seconds: int = 900
    #: X-Forwarded-For is trusted only from these addresses (Caddy).
    trusted_proxies: tuple = ("127.0.0.1", "::1")
    #: Shown instead of 112 in test mode, and the simulated Desk's number.
    desk_number: str = ""
    telegram_bot_name: str = ""
    notice_version: str = "2026-10-1"
    api_docs: bool = False
    #: Where uploaded SOS snapshots and exports live when not in the database.
    data_dir: str = str(PORTAL_VAR_DIR)
    extras: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, **overrides) -> "PortalSettings":
        values = dict(
            database_url=_str("PORTAL_DATABASE_URL", DEFAULT_DATABASE_URL),
            secret_key=_str("PORTAL_SECRET_KEY", ""),
            data_key=_str("PORTAL_DATA_KEY", ""),
            base_url=_str("PORTAL_BASE_URL", "http://portal.localhost:8100").rstrip("/"),
            test_mode=_flag("PORTAL_TEST_MODE", True),
            host=_str("PORTAL_HOST", "127.0.0.1"),
            port=_int("PORTAL_PORT", 8100),
            secure_cookies=_flag("PORTAL_SECURE_COOKIES", False),
            trusted_proxies=tuple(p.strip() for p in _str(
                "PORTAL_TRUSTED_PROXIES", "127.0.0.1,::1").split(",") if p.strip()),
            desk_number=_str("PORTAL_DESK_NUMBER", ""),
            telegram_bot_name=_str("PORTAL_TELEGRAM_BOT_NAME", ""),
            api_docs=_flag("PORTAL_API_DOCS", False),
            data_dir=_str("PORTAL_DATA_DIR", str(PORTAL_VAR_DIR)),
        )
        values.update(overrides)
        return cls(**values).checked()

    def checked(self) -> "PortalSettings":
        """Fill development secrets in test mode; refuse to run without them
        otherwise."""
        secret, data = self.secret_key, self.data_key
        if not secret:
            if not self.test_mode:
                raise RuntimeError("PORTAL_SECRET_KEY must be set when "
                                   "PORTAL_TEST_MODE is off")
            secret = secrets.token_hex(32)
        if not data:
            if not self.test_mode:
                raise RuntimeError("PORTAL_DATA_KEY must be set when "
                                   "PORTAL_TEST_MODE is off")
            data = secret
        if secret == self.secret_key and data == self.data_key:
            return self
        from dataclasses import replace
        return replace(self, secret_key=secret, data_key=data,
                       extras={**self.extras, "ephemeral_secret": not self.secret_key})


_current: PortalSettings | None = None


def set_current(settings: PortalSettings) -> None:
    global _current
    _current = settings


def current() -> PortalSettings:
    """The settings of the app created last (helpers outside a request)."""
    global _current
    if _current is None:
        _current = PortalSettings.from_env()
    return _current


__all__ = ["PortalSettings", "DEFAULT_DATABASE_URL", "set_current", "current"]
