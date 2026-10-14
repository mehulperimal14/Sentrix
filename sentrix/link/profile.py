"""The household profile on the device: accessor and encrypted cache (docs/7 §5.11).

The Portal is the authority for the profile (owner, contacts, address, PIN
hashes, consents, window); it pushes it over the link (``profile.push``) and
the device keeps the active version encrypted in ``profile_cache`` so an
offline SOS still has contacts, the Desk number and the PINs. Without a
Portal (standalone LC, or before the first push) :func:`standalone_profile`
builds one from the environment.

Readers (the SOS engine, ``sms.send``, the LC) call :func:`effective` (or
:func:`current` when they must know whether a Portal profile exists) and
:func:`add_listener` to hear about a new version. The link client calls
:func:`store` on ``profile.push`` and :func:`wipe` on unpair.

The cache key is ``<identity_dir>/profile.key``: 32 random bytes made on first
use (mode 0600), next to ``identity.json``, so it survives restarts and a
factory reset of ``var/`` (unlike a key derived from ``EVIDENCE_AES_KEY``,
which the vault replaces with a random one when unset). This protects the
cache against casual copying only; anyone with the SD card has both files.

Owner: track T-LINK (the foundation ships this working version; the
signatures are frozen).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Callable

from sentrix.paths import LINK_DIR
from sentrix.utils.logging import get_logger

log = get_logger("profile")

PROFILE_KEY_FILE = "profile.key"
_HKDF_INFO = b"sentrix-profile-v1"
SCHEMA = 1
#: Never accepted in a pushed profile (D14, docs/7 §5.11): credentials of the
#: Portal must not reach the device.
FORBIDDEN_KEYS = frozenset({"password_hash", "totp_secret", "totp_secret_enc",
                            "recovery_codes", "session_id", "id_hash", "csrf_token"})

_lock = threading.RLock()
_cached: dict[str, Any] | None = None
_cached_version: int | None = None
_listeners: list[Callable[[dict], None]] = []


# ── identity directory and key ────────────────────────────────────────────────
def identity_dir() -> Path:
    """``SENTRIX_DEVICE_IDENTITY_DIR``, else ``/etc/sentrix`` when it exists,
    else ``var/link`` (docs/7 §3.2)."""
    from sentrix.config import settings
    if settings.link.identity_dir:
        return Path(settings.link.identity_dir)
    system = Path("/etc/sentrix")
    return system if system.is_dir() else LINK_DIR


def _write_secret(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def profile_key() -> bytes:
    """The 32-byte cache key, made once (0600)."""
    path = identity_dir() / PROFILE_KEY_FILE
    try:
        data = path.read_bytes()
        if len(data) == 32:
            return data
        raise ValueError(f"{path} is not a 32-byte key")
    except FileNotFoundError:
        key = os.urandom(32)
        try:
            _write_secret(path, key)
        except FileExistsError:            # another thread made it first
            return path.read_bytes()
        log.info("profile cache key created at %s", path)
        return key


def _aead():
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
               info=_HKDF_INFO).derive(profile_key())
    return AESGCM(key)


def seal(profile: dict[str, Any]) -> bytes:
    from sentrix.link.protocol import canonical_json
    nonce = os.urandom(12)
    return nonce + _aead().encrypt(nonce, canonical_json(profile), None)


def unseal(blob: bytes) -> dict[str, Any]:
    return json.loads(_aead().decrypt(blob[:12], blob[12:], None).decode("utf-8"))


# ── validation ────────────────────────────────────────────────────────────────
def _forbidden(node: Any, path: str = "") -> str | None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in FORBIDDEN_KEYS:
                return f"{path}{key}"
            found = _forbidden(value, f"{path}{key}.")
            if found:
                return found
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found = _forbidden(value, f"{path}{index}.")
            if found:
                return found
    return None


def validate(profile: Any) -> str | None:
    """None when acceptable, else the reason (``invalid_args`` for the ack)."""
    if not isinstance(profile, dict):
        return "profile is not an object"
    if profile.get("schema") != SCHEMA:
        return f"unsupported profile schema {profile.get('schema')!r}"
    found = _forbidden(profile)
    if found:
        return f"forbidden field {found}"
    for contact in profile.get("contacts") or []:
        if isinstance(contact, dict) and contact.get("email"):
            return "contacts' e-mail addresses never reach the device"
    return None


# ── cache ─────────────────────────────────────────────────────────────────────
def store(profile: dict[str, Any], *, version: int,
          sha256: str | None = None) -> dict[str, Any]:
    """Make a pushed profile the active one. Returns the ``profile.ack``
    fields: ``{"version", "ok", "error"}``. A version not newer than the
    active one is acknowledged with ``error: "stale"`` and ignored."""
    from sentrix.link.protocol import sha256_hex
    from sentrix.storage import link_store

    reason = validate(profile)
    if reason is not None:
        return {"version": version, "ok": False, "error": reason}
    digest = sha256_hex(profile)
    if sha256 is not None and sha256 != digest:
        return {"version": version, "ok": False, "error": "sha256 mismatch"}
    with _lock:
        if version <= current_version():
            return {"version": version, "ok": True, "error": "stale"}
        link_store.save_profile(int(version), seal(profile), digest)
        _set_cache(profile, int(version))
        callbacks = list(_listeners)
    for callback in callbacks:
        try:
            callback(profile)
        except Exception as exc:
            log.error("profile listener failed: %s", exc)
    return {"version": version, "ok": True, "error": None}


def _set_cache(profile: dict | None, version: int | None) -> None:
    global _cached, _cached_version
    _cached, _cached_version = profile, version


def current() -> dict[str, Any] | None:
    """The active Portal profile, or None (none pushed yet, or the cache
    cannot be decrypted, e.g. the key file was replaced)."""
    from sentrix.storage import link_store
    with _lock:
        if _cached is not None:
            return _cached
        row = link_store.active_profile()
        if row is None:
            return None
        try:
            profile = unseal(row["blob"])
        except Exception as exc:
            log.error("cached profile v%s cannot be decrypted: %s", row["version"], exc)
            return None
        _set_cache(profile, int(row["version"]))
        return profile


def current_version() -> int:
    """The active profile's version, 0 when there is none."""
    with _lock:
        if _cached_version is not None:
            return _cached_version
        from sentrix.storage import link_store
        row = link_store.active_profile()
        return int(row["version"]) if row else 0


version = current_version


def wipe() -> int:
    """Unpair / factory reset: drop every cached profile."""
    from sentrix.storage import link_store
    with _lock:
        removed = link_store.wipe_profiles()
        _set_cache(None, None)
    return removed


def forget_cache() -> None:
    """Drop the in-memory copy (tests point the database elsewhere)."""
    with _lock:
        _set_cache(None, None)


def add_listener(callback: Callable[[dict], None]) -> None:
    with _lock:
        _listeners.append(callback)


def remove_listener(callback: Callable[[dict], None]) -> None:
    with _lock:
        if callback in _listeners:
            _listeners.remove(callback)


# ── standalone and helpers ────────────────────────────────────────────────────
def standalone_profile() -> dict[str, Any]:
    """A profile for a device without a Portal: the owner is
    ``ALERT_PHONE_NUMBER`` (the pre-portal dispatch target), no contacts, the
    Desk from ``SENTRIX_DESK_NUMBER``. PINs: ``SENTRIX_SOS_PIN_HASH`` (an
    Argon2id hash from ``sentrix_portal.security.hash_pin``) when set,
    otherwise ``pin_source: "console_password"``: the SOS PIN prompt on the LC
    accepts the console password instead (``sos/pins.py``)."""
    from sentrix import mode
    from sentrix.config import settings

    pin_hash = os.getenv("SENTRIX_SOS_PIN_HASH", "").strip() or None
    owner_phone = (os.getenv("ALERT_PHONE_NUMBER", "").strip()
                   or os.getenv("SENTRIX_USER_PHONE", "").strip())
    return {
        "schema": SCHEMA, "version": 0, "standalone": True, "issued_at": None,
        "device_id": None, "household_id": None, "customer_code": None,
        "language": "en", "test_mode": settings.test_mode,
        "owner": {"user_id": None, "name": os.getenv("SENTRIX_USER_NAME", "Resident"),
                  "phone": owner_phone or None, "phone_verified": False},
        "members": [], "contacts": [],
        "desk": {"name": "SENTRIX Desk (simulated)",
                 "phone": os.getenv("SENTRIX_DESK_NUMBER", "").strip() or None,
                 "simulated": True},
        "address": {"house": os.getenv("SENTRIX_USER_ADDRESS", "").strip() or None},
        "household": {"occupants": {}, "pets": None, "medical_notes": None},
        "nearest": {},
        "cameras": [{"camera_id": "CAM_0",
                     "room_label": os.getenv("SENTRIX_CAMERA_LOCATION", "Camera"),
                     "floor": None}],
        "sos": {"window_seconds": settings.sos.window_seconds,
                "safety_defaults": settings.sos.safety_defaults,
                "contacts_for_single_source_l3": settings.sos.contacts_for_single_source_l3,
                "pin_hash": pin_hash, "duress_pin_hash": None, "pin_version": 0,
                "pin_source": "env" if pin_hash else "console_password",
                "channels": {"telegram": False, "email": False, "sms": True},
                "templates": None},
        "consents": {"account_sos": True, "face_recognition": False,
                     "medical_notes": False, "sos_snapshot": False,
                     "remote_view": False, "desk_live_view": False,
                     "model_improvement": False, "notice_version": None},
        "mode": {"armed": mode.is_armed()},
    }


def effective() -> dict[str, Any]:
    """The Portal profile when one is active, else :func:`standalone_profile`."""
    return current() or standalone_profile()


def desk_number(profile: dict[str, Any] | None = None) -> str | None:
    """The Desk phone: the profile's ``desk.phone``, else ``SENTRIX_DESK_NUMBER``.
    Shown instead of 112 in test mode."""
    profile = profile if profile is not None else effective()
    phone = ((profile or {}).get("desk") or {}).get("phone")
    return phone or os.getenv("SENTRIX_DESK_NUMBER", "").strip() or None


def sms_allowlist(profile: dict[str, Any] | None = None) -> frozenset[str]:
    """Numbers ``sms.send`` may reach (docs/7 §5.6): the owner, CONFIRMED
    contacts and the Desk of the current profile, normalised to E.164."""
    from sentrix.alerting.guard import normalize_address
    profile = profile if profile is not None else effective()
    numbers = [(profile.get("owner") or {}).get("phone"), desk_number(profile)]
    numbers += [c.get("phone") for c in profile.get("contacts") or []
                if isinstance(c, dict) and c.get("consent_status") == "CONFIRMED"]
    return frozenset(normalize_address("modem_sms", n) for n in numbers if n)


__all__ = ["identity_dir", "profile_key", "seal", "unseal", "validate", "store",
           "current", "current_version", "version", "wipe", "forget_cache",
           "add_listener", "remove_listener", "standalone_profile", "effective",
           "desk_number", "sms_allowlist", "FORBIDDEN_KEYS", "PROFILE_KEY_FILE"]
