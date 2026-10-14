"""Session tokens, upload validation and login rate limiting."""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from collections import defaultdict, deque
from functools import lru_cache

from sentrix.utils.logging import get_logger

log = get_logger("security")

SESSION_EXPIRY_SECONDS = 43_200          # 12 hours
_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png"}
_ALLOWED_MIME = {"image/jpeg", "image/png", "image/jpg"}

#: JPEG / PNG magic bytes. Extension and Content-Type are both client-supplied,
#: so neither is evidence of anything; the file header is at least written by
#: whatever produced the bytes.
_MAGIC = ((b"\xff\xd8\xff", "jpeg"), (b"\x89PNG\r\n\x1a\n", "png"))


def _signing_key(password: str) -> bytes:
    return _derive_key(os.getenv("SESSION_SECRET", "").strip(), password)


@lru_cache(maxsize=8)
def _derive_key(secret: str, password: str) -> bytes:
    """100k-round PBKDF2, computed once per (secret, password) rather than on
    every authenticated request."""
    if secret:
        material = f"{secret}:{password}".encode()
        salt = hashlib.sha256(secret.encode()).digest()[:16]
    else:
        # Works, but every deployment with the same password shares a key.
        log.warning("SESSION_SECRET is not set — set it before deploying")
        material, salt = password.encode(), b"sentrix-session-v1"
    return hashlib.pbkdf2_hmac("sha256", material, salt, 100_000)


def generate_token(password: str) -> str:
    """``{timestamp_hex}:{hmac_hex}``, signed over timestamp and password."""
    ts_hex = format(int(time.time()), "x")
    mac = hmac.new(_signing_key(password), f"{ts_hex}:{password}".encode(),
                   hashlib.sha256).hexdigest()
    return f"{ts_hex}:{mac}"


def verify_token(token: str | None, password: str) -> bool:
    if not token or not password:
        return False
    try:
        ts_hex, received = token.split(":", 1)
        if time.time() - int(ts_hex, 16) > SESSION_EXPIRY_SECONDS:
            return False
        expected = hmac.new(_signing_key(password),
                            f"{ts_hex}:{password}".encode(),
                            hashlib.sha256).hexdigest()
        return hmac.compare_digest(received, expected)
    except (ValueError, AttributeError):
        return False


def sanitize_filename(filename: str) -> str:
    """Basename only, no traversal, no leading dots, length-capped."""
    if not filename:
        return ""
    name = os.path.basename(str(filename).replace("\\", "/")).lstrip(". ")
    name = "".join(c for c in name if c.isalnum() or c in "._- ").strip()
    return name[:120]


def validate_image(filename: str, content_type: str | None,
                   payload: bytes) -> tuple[bool, str]:
    """Validate an uploaded image by extension, MIME type *and* magic bytes."""
    if not filename:
        return False, "missing filename"
    if os.path.splitext(filename.lower())[1] not in _ALLOWED_EXTENSIONS:
        return False, "only .jpg, .jpeg and .png are accepted"
    if content_type:
        mime = content_type.split(";")[0].strip().lower()
        if mime not in _ALLOWED_MIME:
            return False, f"unexpected content type: {mime}"
    if not any(payload.startswith(magic) for magic, _ in _MAGIC):
        return False, "file content is not a JPEG or PNG"
    return True, "ok"


class RateLimiter:
    """Sliding-window limiter (at most ``max_attempts`` in any ``window_seconds``),
    used to stop password brute forcing and to cap captures and setup codes.
    Keys are client addresses on the LAN and actors for tunnelled users
    (``sentrix.web.auth.rate_key``).

    The previous login route had no limiter at all: with the default password
    of ``admin`` and a 12-hour session, an unauthenticated client could guess
    without any throttling.
    """

    def __init__(self, max_attempts: int = 8, window_seconds: float = 300.0) -> None:
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.max_attempts:
                return False
            hits.append(now)
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


login_limiter = RateLimiter()

__all__ = ["generate_token", "verify_token", "sanitize_filename",
           "validate_image", "RateLimiter", "login_limiter",
           "SESSION_EXPIRY_SECONDS"]
