"""Portal settings, read from the environment when the app is created.

Unlike the device's ``sentrix.config`` (read once at import), the Portal builds
a :class:`PortalSettings` per app so tests can point it at a temporary
database. Secrets are fields here (the Portal has no ``/api/config``), but are
never logged or rendered.

Keys: every HMAC digest the Portal stores (device bootstrap and token, claim
and activation codes, action tokens, session ids, the audit chain) and every
sealed column depend on ``secret_key`` / ``data_key``. In test mode, when the
environment does not set them, :meth:`PortalSettings.checked` generates each
**once** into ``<data_dir>/secret_key`` and ``<data_dir>/data_key`` (mode 0600)
and reuses them, so a restart of the VM strands no device and no login. With
test mode off both must come from the environment. Only explicit test
fixtures (:meth:`PortalSettings.for_tests`) get throwaway keys.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field, replace
from pathlib import Path

from sentrix.alerting.guard import test_mode_flag
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
SECRET_KEY_FILE = "secret_key"
DATA_KEY_FILE = "data_key"


def _platform_url() -> str:
    """The public URL a hosting platform announces: Render's
    ``RENDER_EXTERNAL_URL``, else Railway's ``RAILWAY_PUBLIC_DOMAIN``."""
    render = _str("RENDER_EXTERNAL_URL", "")
    if render:
        return render
    railway = _str("RAILWAY_PUBLIC_DOMAIN", "")
    return f"https://{railway}" if railway else ""


def _default_data_dir() -> str:
    return _str("PORTAL_DATA_DIR", str(PORTAL_VAR_DIR))


def _key_file(directory: str, name: str) -> tuple[str, bool]:
    """(key, created): read ``<directory>/<name>``, or create it once (0600)."""
    path = Path(directory) / name
    try:
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value, False
    except FileNotFoundError:
        pass
    value = secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:                       # created meanwhile
        return path.read_text(encoding="utf-8").strip(), False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value)
    return value, True


@dataclass(frozen=True)
class PortalSettings:
    database_url: str = DEFAULT_DATABASE_URL
    #: HMAC key for session ids, codes, tokens, audit chain and IP hashes. In
    #: test mode without the env var: generated once into <data_dir>/secret_key.
    secret_key: str = ""
    #: Column encryption (phones, medical notes, TOTP secrets). In test mode
    #: without the env var: generated once into <data_dir>/data_key.
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
    #: X-Real-IP / X-Forwarded-For are trusted only from these addresses
    #: (Caddy); "*" trusts any peer, for a platform whose edge is the only
    #: way in (Railway).
    trusted_proxies: tuple = ("127.0.0.1", "::1")
    #: Shown instead of 112 in test mode, and the simulated Desk's number.
    desk_number: str = ""
    telegram_bot_name: str = ""
    #: ``polling`` (dev: getUpdates long polling) or ``webhook``
    #: (``<base_url>/hooks/telegram``, https, TELEGRAM_WEBHOOK_SECRET required).
    telegram_mode: str = "polling"
    #: Who to complain to before the Data Protection Board (privacy notice).
    grievance_contact: str = "the SENTRIX capstone team, CSED, Thapar Institute, Patiala"
    notice_version: str = "2026-10-1"
    api_docs: bool = False
    #: Key files, exports, and SOS snapshots when not in the database.
    data_dir: str = field(default_factory=_default_data_dir)
    #: Start ``app.BACKGROUND_MODULES`` (hub watcher, cloud SOS timers,
    #: notification worker, retention) in the lifespan. Off in tests.
    workers: bool = False
    extras: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, **overrides) -> "PortalSettings":
        values = dict(
            database_url=_str("PORTAL_DATABASE_URL", DEFAULT_DATABASE_URL),
            secret_key=_str("PORTAL_SECRET_KEY", ""),
            data_key=_str("PORTAL_DATA_KEY", ""),
            base_url=_str("PORTAL_BASE_URL", _platform_url()
                          or "http://portal.localhost:8100").rstrip("/"),
            test_mode=test_mode_flag("PORTAL_TEST_MODE"),
            host=_str("PORTAL_HOST", "127.0.0.1"),
            # A platform that picks the port (Railway, Render) sets PORT.
            port=_int("PORTAL_PORT", _int("PORT", 8100)),
            secure_cookies=_flag("PORTAL_SECURE_COOKIES", False),
            trusted_proxies=tuple(p.strip() for p in _str(
                "PORTAL_TRUSTED_PROXIES", "127.0.0.1,::1").split(",") if p.strip()),
            desk_number=_str("PORTAL_DESK_NUMBER", ""),
            telegram_bot_name=_str("PORTAL_TELEGRAM_BOT_NAME", ""),
            telegram_mode=_str("SENTRIX_TELEGRAM_MODE", "polling").lower(),
            grievance_contact=_str("PORTAL_GRIEVANCE_CONTACT",
                                   "the SENTRIX capstone team, CSED, Thapar Institute, Patiala"),
            api_docs=_flag("PORTAL_API_DOCS", False),
            data_dir=_default_data_dir(),
            workers=_flag("PORTAL_WORKERS", False),
        )
        values.update(overrides)
        return cls(**values).checked()

    @classmethod
    def for_tests(cls, directory, **overrides) -> "PortalSettings":
        """Throwaway settings in ``directory`` (a pytest ``tmp_path``): its own
        SQLite file and data dir, random keys, test mode on, no workers."""
        directory = Path(directory)
        values = dict(database_url=f"sqlite:///{(directory / 'portal.db').as_posix()}",
                      data_dir=str(directory / "portal-data"),
                      secret_key=secrets.token_hex(32), data_key=secrets.token_hex(32),
                      test_mode=True, workers=False)
        values.update(overrides)
        return cls(**values).checked()

    def checked(self) -> "PortalSettings":
        """Fill the keys (see the module docstring); refuse to run without them
        when test mode is off."""
        secret, data = self.secret_key, self.data_key
        created: list[str] = []
        if not secret:
            if not self.test_mode:
                raise RuntimeError("PORTAL_SECRET_KEY must be set when "
                                   "PORTAL_TEST_MODE is off")
            secret, made = _key_file(self.data_dir, SECRET_KEY_FILE)
            created += [SECRET_KEY_FILE] if made else []
        if not data:
            if not self.test_mode:
                raise RuntimeError("PORTAL_DATA_KEY must be set when "
                                   "PORTAL_TEST_MODE is off")
            data, made = _key_file(self.data_dir, DATA_KEY_FILE)
            created += [DATA_KEY_FILE] if made else []
        if secret == self.secret_key and data == self.data_key:
            return self
        return replace(self, secret_key=secret, data_key=data,
                       extras={**self.extras, "key_files": not self.secret_key,
                               "key_files_created": created})


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


__all__ = ["PortalSettings", "DEFAULT_DATABASE_URL", "SECRET_KEY_FILE", "DATA_KEY_FILE",
           "set_current", "current"]
