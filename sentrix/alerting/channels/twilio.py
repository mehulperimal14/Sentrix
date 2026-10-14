"""Twilio SMS and voice behind the channel router (``twilio_sms``, ``twilio_call``).

The pre-portal L4 SMS, L5 call and dispatch "Send now" used Twilio directly
(``sentrix/alerting/notifier.py``). With the channel router running they go
through these transports instead, so they share the outbox, retries, status
and the send guard: nothing is sent unless ``SENTRIX_LIVE_ALERTS=1`` *and*
``SENTRIX_TWILIO_ENABLED=1`` and, in test mode (the default), the number is in
``SENTRIX_TEST_RECIPIENTS`` (decision D13). Keys alone send nothing.

The Twilio client is built lazily on the first allowed send (``.env`` may
hold live keys; importing this module touches nothing). Credentials are read
from the environment at use time and never stored in config or logged.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable
from xml.sax.saxutils import escape

from sentrix.alerting import guard
from sentrix.alerting.channels.base import Message, Recipient, SendResult

log = logging.getLogger("sentrix.channels.twilio")

#: Twilio error codes a retry cannot fix (invalid / unreachable / unverified
#: numbers, a number that opted out, a geographic permission refusal).
PERMANENT_ERRORS = frozenset({21211, 21214, 21217, 21219, 21408, 21610, 21612, 21614,
                              13224, 13227})


@dataclass(frozen=True)
class TwilioCredentials:
    sid: str = ""
    token: str = ""
    from_number: str = ""

    @classmethod
    def from_env(cls) -> "TwilioCredentials":
        return cls(sid=os.getenv("TWILIO_ACCOUNT_SID", "").strip(),
                   token=os.getenv("TWILIO_AUTH_TOKEN", "").strip(),
                   from_number=os.getenv("TWILIO_PHONE_NUMBER", "").strip())

    @property
    def complete(self) -> bool:
        return all((self.sid, self.token, self.from_number))


def twiml_say(text: str) -> str:
    """A TwiML ``<Say>``; the text is free (incident reasons), so escaped."""
    return f'<Response><Say voice="alice">{escape(text)}</Say></Response>'


def _default_client(credentials: TwilioCredentials) -> Any:
    from twilio.rest import Client                       # imported only to send
    return Client(credentials.sid, credentials.token)


def _error_result(exc: Exception) -> SendResult:
    code = getattr(exc, "code", None)
    status = getattr(exc, "status", None)
    message = getattr(exc, "msg", None) or str(exc)
    permanent = code in PERMANENT_ERRORS or status in (400, 401, 403, 404)
    if status == 429:
        permanent = False
    return SendResult(False, error=f"twilio {status or ''} {code or ''}: {message}".strip(),
                      retryable=not permanent)


class _TwilioTransport:
    name = "twilio"

    def __init__(self, *, credentials: Callable[[], TwilioCredentials] = TwilioCredentials.from_env,
                 client_factory: Callable[[TwilioCredentials], Any] = _default_client) -> None:
        self._credentials = credentials
        self._client_factory = client_factory
        self._client = None
        self._client_sid = None

    def state(self) -> tuple[bool, str]:
        credentials = self._credentials()
        if not credentials.complete:
            return False, "TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_PHONE_NUMBER not set"
        return True, "Twilio"

    def _client_for(self, credentials: TwilioCredentials) -> Any:
        if self._client is None or self._client_sid != credentials.sid:
            self._client = self._client_factory(credentials)
            self._client_sid = credentials.sid
        return self._client

    def _send(self, recipient: Recipient, message: Message,
              call: Callable[[Any, TwilioCredentials, str], Any]) -> SendResult:
        credentials = self._credentials()
        if not credentials.complete:
            return SendResult(False, error="Twilio is not configured", retryable=False)
        to = guard.normalize_address(self.name, recipient.address)
        try:
            created = call(self._client_for(credentials), credentials, to)
        except ImportError:
            return SendResult(False, error="twilio is not installed", retryable=False)
        except Exception as exc:                          # TwilioRestException, network
            return _error_result(exc)
        return SendResult(True, provider_id=getattr(created, "sid", None))


class TwilioSmsTransport(_TwilioTransport):
    name = "twilio_sms"

    def send(self, recipient: Recipient, message: Message) -> SendResult:
        return self._send(recipient, message, lambda client, creds, to: client.messages.create(
            body=message.text, from_=creds.from_number, to=to))


class TwilioCallTransport(_TwilioTransport):
    """A voice call that reads the message aloud (TwiML ``<Say>``)."""

    name = "twilio_call"

    def send(self, recipient: Recipient, message: Message) -> SendResult:
        return self._send(recipient, message, lambda client, creds, to: client.calls.create(
            twiml=twiml_say(message.text), from_=creds.from_number, to=to))


__all__ = ["TwilioCredentials", "TwilioSmsTransport", "TwilioCallTransport", "twiml_say",
           "PERMANENT_ERRORS"]
