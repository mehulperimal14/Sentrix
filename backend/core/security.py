# core/security.py
#
# ARCHITECTURE: Cryptographic session management and upload sanitization.
# Replaces the insecure password-as-cookie pattern with HMAC-SHA256 signed
# tokens that include a timestamp and expiry check.
#
# API:
#   generate_token(password)        → signed session token string
#   verify_token(token, password)   → bool (valid + not expired)
#   sanitize_filename(filename)     → safe basename string
#   validate_image_upload(filename, content_type) → bool

import hashlib
import hmac
import os
import time
from typing import Optional

SESSION_EXPIRY_SECONDS = 43200  # 12 hours

# Session signing secret — loaded from env or derived from password.
# For best security, set SESSION_SECRET in .env to a random hex string:
#   python -c "import secrets; print(secrets.token_hex(32))"
_SESSION_SECRET = os.getenv("SESSION_SECRET", "").strip()

# Allowed image MIME types and extensions for face uploads
_ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
_ALLOWED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/jpg"}


def _get_signing_key(password: str) -> bytes:
    """
    Derive a stable signing key from the session secret and password.
    Uses PBKDF2-HMAC-SHA256 with a fixed salt from the secret.
    If SESSION_SECRET is not set, falls back to the password itself as the key
    (still much safer than storing the raw password in the cookie).
    """
    if _SESSION_SECRET:
        # Combine secret + password so the token is invalid if either changes
        key_material = f"{_SESSION_SECRET}:{password}".encode("utf-8")
        salt = hashlib.sha256(_SESSION_SECRET.encode()).digest()[:16]
    else:
        key_material = password.encode("utf-8")
        salt = b"sentrix-session-v1"

    return hashlib.pbkdf2_hmac("sha256", key_material, salt, iterations=100_000)


def generate_token(password: str) -> str:
    """
    Generate a signed session token.
    Format: {timestamp_hex}:{hmac_hex}
    The HMAC signs timestamp + password to prevent forgery.
    """
    timestamp = int(time.time())
    ts_hex = format(timestamp, "x")
    signing_key = _get_signing_key(password)
    payload = f"{ts_hex}:{password}"
    mac = hmac.new(signing_key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{ts_hex}:{mac}"


def verify_token(token: Optional[str], password: str) -> bool:
    """
    Verify a session token is valid and not expired.
    Returns True only if:
      1. Token has the correct format
      2. HMAC signature is valid (constant-time compare)
      3. Token is not older than SESSION_EXPIRY_SECONDS
    """
    if not token or not password:
        return False

    try:
        parts = token.split(":", 1)
        if len(parts) != 2:
            return False

        ts_hex, received_mac = parts
        timestamp = int(ts_hex, 16)

        # Check expiry first (cheap check before crypto)
        if time.time() - timestamp > SESSION_EXPIRY_SECONDS:
            return False

        # Recompute expected HMAC
        signing_key = _get_signing_key(password)
        payload = f"{ts_hex}:{password}"
        expected_mac = hmac.new(
            signing_key, payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        # Constant-time comparison to prevent timing attacks
        return hmac.compare_digest(received_mac, expected_mac)

    except Exception:
        return False


def sanitize_filename(filename: str) -> str:
    """
    Return a safe basename from a potentially malicious filename.
    - Strips directory components (prevents path traversal)
    - Strips leading dots/spaces
    - Limits to 120 characters
    """
    if not filename:
        return "upload"
    # Get just the basename, no directory components
    name = os.path.basename(filename)
    # Strip leading dots and spaces
    name = name.lstrip(". ")
    # Limit length
    name = name[:120] if len(name) > 120 else name
    # If empty after stripping, give a default
    return name if name else "upload"


def validate_image_upload(filename: str, content_type: Optional[str] = None) -> bool:
    """
    Validate that the uploaded file is an allowed image type.
    Checks both the file extension and the declared MIME type.
    Returns False for any non-image file.
    """
    if not filename:
        return False

    # Extension check
    ext = os.path.splitext(filename.lower())[1]
    if ext not in _ALLOWED_IMAGE_EXTENSIONS:
        return False

    # MIME type check (if provided by the client)
    if content_type:
        # Normalize: "image/jpeg; charset=..." → "image/jpeg"
        mime = content_type.split(";")[0].strip().lower()
        if mime not in _ALLOWED_IMAGE_MIME_TYPES:
            return False

    return True
