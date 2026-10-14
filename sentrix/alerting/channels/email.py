"""E-mail through an HTTPS API: Brevo or Resend (decision D11; never SMTP).

Hosting fallbacks (Render) block outbound SMTP, so the ``email`` channel
talks to the provider's HTTPS API: ``SENTRIX_EMAIL_PROVIDER`` = ``brevo``
(``BREVO_API_KEY``) or ``resend`` (``RESEND_API_KEY``), sender
``SENTRIX_EMAIL_FROM`` (``SENTRIX <alerts@example.in>``). Keys are read at use
time and never logged.

Every message has a plain-text and an HTML body. Links are the message's URL
buttons; they open **confirmation pages** (the action happens only on a POST
with CSRF there, so a mail scanner that fetches every link approves nothing:
portal review). Test messages carry the guard's banner in the subject and on
top of the HTML too.

Stdlib + httpx only: the Portal imports this module.
"""

from __future__ import annotations

import html
import logging
import os
import re
from email.utils import formataddr, parseaddr
from typing import Protocol

import httpx

from sentrix.alerting import guard
from sentrix.alerting.channels.base import Message, Recipient, SendResult

log = logging.getLogger("sentrix.channels.email")

BREVO_URL = "https://api.brevo.com/v3/smtp/email"
RESEND_URL = "https://api.resend.com/emails"
PROVIDERS = ("brevo", "resend")
_URL = re.compile(r"https?://[^\s<>\"']+")


def sender_from_env() -> tuple[str, str]:
    """(name, address) from ``SENTRIX_EMAIL_FROM``; ("", "") when unset."""
    name, address = parseaddr(os.getenv("SENTRIX_EMAIL_FROM", "").strip())
    if not address or "@" not in address:
        return "", ""
    return name or "SENTRIX", address


def _http_error(provider: str, response: httpx.Response) -> SendResult:
    try:
        detail = response.json()
    except ValueError:
        detail = response.reason_phrase
    if isinstance(detail, dict):
        detail = detail.get("message") or detail.get("code") or detail
    status = response.status_code
    retryable = status == 429 or status >= 500
    return SendResult(False, error=f"{provider} {status}: {str(detail)[:200]}",
                      retryable=retryable)


class EmailProvider(Protocol):
    name: str

    def configured(self) -> tuple[bool, str]: ...

    def send(self, to: str, to_name: str, subject: str, text: str, html_body: str, *,
             idempotency_key: str | None = None) -> SendResult: ...


class _HttpProvider:
    name = "http"
    key_env = ""
    url = ""

    def __init__(self, *, client: httpx.Client | None = None, timeout: float = 15.0,
                 api_key: str | None = None, sender: tuple[str, str] | None = None) -> None:
        self._client = client
        self.timeout = timeout
        self._api_key = api_key
        self._sender = sender

    def _key(self) -> str:
        return self._api_key or os.getenv(self.key_env, "").strip()

    def _from(self) -> tuple[str, str]:
        return self._sender or sender_from_env()

    def configured(self) -> tuple[bool, str]:
        if not self._key():
            return False, f"{self.key_env} not set"
        if not self._from()[1]:
            return False, "SENTRIX_EMAIL_FROM not set"
        return True, self.name

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def _post(self, headers: dict, payload: dict) -> httpx.Response | SendResult:
        try:
            return self._http().post(self.url, headers=headers, json=payload,
                                     timeout=self.timeout)
        except httpx.HTTPError as exc:
            return SendResult(False, error=f"{self.name} network: {type(exc).__name__}",
                              retryable=True)


class BrevoProvider(_HttpProvider):
    """Brevo transactional e-mail: ``POST /v3/smtp/email`` -> 201 {messageId}."""

    name = "brevo"
    key_env = "BREVO_API_KEY"
    url = BREVO_URL

    def send(self, to: str, to_name: str, subject: str, text: str, html_body: str, *,
             idempotency_key: str | None = None) -> SendResult:
        ok, detail = self.configured()
        if not ok:
            return SendResult(False, error=detail, retryable=False)
        name, address = self._from()
        payload = {"sender": {"name": name, "email": address},
                   "to": [{"email": to, **({"name": to_name} if to_name else {})}],
                   "subject": subject, "textContent": text, "htmlContent": html_body}
        response = self._post({"api-key": self._key(), "accept": "application/json"}, payload)
        if isinstance(response, SendResult):
            return response
        if response.status_code in (200, 201, 202):
            return SendResult(True, provider_id=str(response.json().get("messageId") or ""))
        return _http_error(self.name, response)


class ResendProvider(_HttpProvider):
    """Resend: ``POST /emails`` -> 200 {id}. Retries carry an
    ``Idempotency-Key`` (the outbox dedupe key), so a timeout never doubles."""

    name = "resend"
    key_env = "RESEND_API_KEY"
    url = RESEND_URL

    def send(self, to: str, to_name: str, subject: str, text: str, html_body: str, *,
             idempotency_key: str | None = None) -> SendResult:
        ok, detail = self.configured()
        if not ok:
            return SendResult(False, error=detail, retryable=False)
        name, address = self._from()
        headers = {"Authorization": f"Bearer {self._key()}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key[:256]
        payload = {"from": formataddr((name, address)), "to": [to], "subject": subject,
                   "text": text, "html": html_body}
        response = self._post(headers, payload)
        if isinstance(response, SendResult):
            return response
        if response.status_code in (200, 201, 202):
            return SendResult(True, provider_id=str(response.json().get("id") or ""))
        return _http_error(self.name, response)


def provider_from_env(name: str | None = None, *,
                      client: httpx.Client | None = None) -> EmailProvider | None:
    """The provider ``SENTRIX_EMAIL_PROVIDER`` names (or ``name``), or None."""
    choice = (name if name is not None else os.getenv("SENTRIX_EMAIL_PROVIDER", "")).strip().lower()
    if choice == "brevo":
        return BrevoProvider(client=client)
    if choice == "resend":
        return ResendProvider(client=client)
    if choice:
        log.warning("unknown SENTRIX_EMAIL_PROVIDER %r (brevo | resend)", choice)
    return None


# ── bodies ────────────────────────────────────────────────────────────────────
def _links(message: Message) -> list[tuple[str, str]]:
    return [(b.label, b.url) for b in message.buttons
            if b.url and b.url.startswith(("https://", "http://"))]


def _subject(message: Message, text: str) -> str:
    banner = text.startswith((guard.TEST_PREFIX, guard.TEST_PREFIX_GSM))
    subject = (message.subject or "").strip()
    if not subject:
        body = text
        if banner:
            body = text.split("]", 1)[-1]
        subject = "SENTRIX: " + (body.strip().splitlines() or ["notification"])[0][:80]
    if banner and not subject.startswith("[SENTRIX DEMO"):
        subject = f"[SENTRIX DEMO - TEST] {subject}"
    return subject[:200]


def _linkify(escaped: str) -> str:
    return _URL.sub(lambda m: f'<a href="{m.group(0)}">{m.group(0)}</a>', escaped)


def render_bodies(message: Message) -> tuple[str, str, str]:
    """(subject, plain text, HTML). The text already carries the test banner
    (the guard adds it); a caller's HTML gets the banner on top as well."""
    text = message.text
    links = _links(message)
    plain = text + ("\n\n" + "\n".join(f"{label}: {url}" for label, url in links)
                    if links else "")
    banner = text.startswith((guard.TEST_PREFIX, guard.TEST_PREFIX_GSM))
    banner_html = (f'<p style="background:#b00020;color:#fff;padding:8px;font-weight:bold">'
                   f'{html.escape(guard.TEST_PREFIX)}</p>') if banner else ""
    if message.html:
        body = message.html
        if banner and guard.TEST_PREFIX not in body:
            body = banner_html + body
    else:
        paragraphs = "".join(f"<p>{_linkify(html.escape(p)).replace(chr(10), '<br>')}</p>"
                             for p in text.split("\n\n") if p.strip())
        body = paragraphs
    buttons = "".join(
        f'<p><a href="{html.escape(url, quote=True)}" style="display:inline-block;'
        f'padding:10px 16px;background:#1a237e;color:#fff;text-decoration:none;'
        f'border-radius:4px">{html.escape(label)}</a></p>' for label, url in links)
    footer = ('<p style="color:#666;font-size:12px">These links open a confirmation page; '
              'nothing happens until you confirm there.</p>') if links else ""
    document = (f'<!doctype html><html><body style="font-family:sans-serif">'
                f'{body}{buttons}{footer}</body></html>')
    return _subject(message, text), plain, document


class EmailTransport:
    """``email``: one provider; SENT means the provider accepted it."""

    name = "email"

    def __init__(self, provider: EmailProvider | None = None) -> None:
        self._provider = provider

    @property
    def provider(self) -> EmailProvider | None:
        return self._provider or provider_from_env()

    def state(self) -> tuple[bool, str]:
        provider = self.provider
        if provider is None:
            return False, "SENTRIX_EMAIL_PROVIDER not set (brevo | resend)"
        return provider.configured()

    def send(self, recipient: Recipient, message: Message) -> SendResult:
        provider = self.provider
        if provider is None:
            return SendResult(False, error="no e-mail provider configured", retryable=False)
        address = guard.normalize_address(self.name, recipient.address)
        subject, plain, document = render_bodies(message)
        name = recipient.name if recipient.name and recipient.name != recipient.ref else ""
        return provider.send(address, name, subject, plain, document,
                             idempotency_key=message.dedupe_key(recipient))


__all__ = ["BrevoProvider", "ResendProvider", "EmailTransport", "EmailProvider",
           "provider_from_env", "render_bodies", "sender_from_env", "PROVIDERS",
           "BREVO_URL", "RESEND_URL"]
