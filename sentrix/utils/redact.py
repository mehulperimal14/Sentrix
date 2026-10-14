"""Redaction of credentials and personal numbers before anything is shown or sent.

A camera source such as ``rtsp://admin:hunter2@192.168.1.9/s1`` used to reach
``/api/health`` verbatim (``camera_source = f"capture:{source}"``). Once the
Portal tunnels that endpoint, the password would cross the internet and could
be stored in the cloud. Every source name, heartbeat, link payload and log line
that can carry a URL goes through :func:`redact_url` first (docs/7 §12).

Stdlib only, so the Portal can import it too.
"""

from __future__ import annotations

import re

#: ``scheme://user[:password]@`` — the userinfo part of any URL in a string.
_USERINFO = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<userinfo>[^/@\s]+)@")
#: Credential-looking query parameters (``?password=...&token=...``).
_SECRET_QUERY = re.compile(
    r"(?P<key>[?&;](?:pass(?:word)?|pwd|token|secret|key|auth|sig|signature|"
    r"access_token|api_key|apikey)=)(?P<value>[^&;#\s]*)", re.IGNORECASE)

MASK = "***"


def redact_url(text: str | None) -> str:
    """Hide userinfo and secret query values in every URL inside ``text``.

    ``capture:rtsp://u:p@host/s`` -> ``capture:rtsp://***@host/s``. Works on
    composite names (``multi:capture:rtsp://...,file:x.mp4``) and on plain
    strings without a URL (returned unchanged).
    """
    if not text:
        return "" if text is None else text
    out = _USERINFO.sub(lambda m: f"{m.group('scheme')}{MASK}@", str(text))
    return _SECRET_QUERY.sub(lambda m: f"{m.group('key')}{MASK}", out)


def mask_phone(number: str | None) -> str:
    """``+919812345610`` -> ``+91 98xxxxxx10`` (UI and logs; never for sending)."""
    if not number:
        return ""
    digits = re.sub(r"[^\d+]", "", str(number))
    if digits.startswith("+91") and len(digits) == 13:
        local = digits[3:]
        return f"+91 {local[:2]}{'x' * 6}{local[-2:]}"
    if len(digits) <= 4:
        return "x" * len(digits)
    return f"{digits[:3]}{'x' * (len(digits) - 5)}{digits[-2:]}"


def mask_email(address: str | None) -> str:
    """``riya.sharma@example.in`` -> ``r***@example.in``."""
    if not address or "@" not in str(address):
        return MASK if address else ""
    local, _, domain = str(address).partition("@")
    return f"{local[:1]}{MASK}@{domain}"


def mask_recipient(address: str | None) -> str:
    """Mask a phone number, e-mail address or Telegram chat id for display."""
    if not address:
        return ""
    text = str(address)
    if "@" in text:
        return mask_email(text)
    if text.startswith("tg:"):
        chat = text[3:]
        return f"tg:{'x' * max(len(chat) - 3, 0)}{chat[-3:]}"
    return mask_phone(text)


__all__ = ["redact_url", "mask_phone", "mask_email", "mask_recipient", "MASK"]
