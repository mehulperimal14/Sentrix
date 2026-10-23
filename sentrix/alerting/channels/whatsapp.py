"""WhatsApp through Meta's Cloud API: transport, quick replies and the inbound
webhook (decision D17).

Outbound (``whatsapp`` channel) goes straight to Meta's Graph API
(``POST /<version>/<phone number id>/messages``), no reseller. WhatsApp lets a
business start a conversation only with an approved *template*; free text and
reply buttons are allowed only inside the 24-hour window after the
recipient's own last message. So SENTRIX keeps three templates
(``tools/whatsapp_templates.py`` creates them; :data:`TEMPLATES`):

* ``sentrix_prompt`` - the owner's "Do you need assistance?" with three quick
  replies (Alert contacts / I'm checking / False alarm);
* ``sentrix_contact`` - a contact's alert with two (I'm handling it / Open the
  SOS page);
* ``sentrix_notice`` - everything else, no buttons.

Each body starts with the test banner (D13: test mode is the only mode this
semester) and carries the message text in ``{{1}}``, flattened: WhatsApp
refuses new lines and tabs in a template parameter.

Buttons: a quick reply's payload is set per message: the button's
``callback_data`` (``"<nonce>:<letter>"``, exactly Telegram's) or, for a link
button, ``"open:<index>"``, which the webhook answers with the link (a PIN
typed into a chat would stay in its history, so "False alarm" still opens the
Portal PIN page).

``WHATSAPP_MODE``: ``template`` (default); ``session`` (free text with reply
buttons, only inside the 24-hour window: a fallback while Meta reviews the
templates); ``auto`` (template, then session when Meta refuses the template).
Replies to a recipient's own message (template ``whatsapp.*``) are always
session messages: the recipient just opened the window.

Inbound: :func:`parse_webhook` reduces a webhook body to :class:`Inbound`
events (a button reply, a text, a delivery status); :func:`signature_ok`
checks ``X-Hub-Signature-256`` (HMAC-SHA256 of the raw body with
``WHATSAPP_APP_SECRET``); :func:`verify_subscription` answers Meta's GET
handshake with ``WHATSAPP_VERIFY_TOKEN``.

Read at use time, never logged: ``WHATSAPP_ACCESS_TOKEN``,
``WHATSAPP_APP_SECRET``, ``WHATSAPP_VERIFY_TOKEN``; also
``WHATSAPP_PHONE_NUMBER_ID``, ``WHATSAPP_API_VERSION`` (v23.0),
``WHATSAPP_TEMPLATE_LANG`` (en) and ``WHATSAPP_TEMPLATE_PROMPT`` /
``_CONTACT`` / ``_NOTICE`` (other template names).

Stdlib + httpx only: the Portal imports this module.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

from sentrix.alerting import guard
from sentrix.alerting.channels.base import Button, Message, Recipient, SendResult

log = logging.getLogger("sentrix.channels.whatsapp")

GRAPH_BASE = "https://graph.facebook.com"
DEFAULT_API_VERSION = "v23.0"
SIGNATURE_HEADER = "X-Hub-Signature-256"
MODES = ("template", "session", "auto")
#: WhatsApp's limits: a session text body, an interactive body, a reply
#: button's title and id; a template parameter is kept well under the body's.
MAX_TEXT = 4096
MAX_INTERACTIVE_BODY = 1024
MAX_BUTTON_TITLE = 20
MAX_PARAM = 700
#: Quick-reply payload of a link button (the webhook answers with the link)
#: and of a template button this message does not use.
OPEN_PREFIX = "open:"
UNUSED = "none"

#: The three templates: button labels in template order, and the SOS action
#: each label stands for (``Button.action``).
PROMPT, CONTACT, NOTICE = "prompt", "contact", "notice"
TEMPLATES: dict[str, dict[str, Any]] = {
    PROMPT: {"name": "sentrix_prompt",
             "body": f"{guard.TEST_PREFIX} {{{{1}}}} Do you need assistance? "
                     "Choose an option below.",
             "buttons": (("Alert contacts", "approve"), ("I'm checking", "checking"),
                         ("False alarm", "decline_page")),
             "example": "Possible weapon seen by the Living room camera at 21:04."},
    CONTACT: {"name": "sentrix_contact",
              "body": f"{guard.TEST_PREFIX} {{{{1}}}} Please let the others know "
                      "if you are handling it.",
              "buttons": (("I'm handling it", "acknowledge"), ("Open the SOS page", "open")),
              "example": "Riya asked for help: possible weapon at H-212, Hostel J."},
    NOTICE: {"name": "sentrix_notice",
             "body": f"{guard.TEST_PREFIX} {{{{1}}}} (SENTRIX home safety)",
             "buttons": (),
             "example": "The SENTRIX request for H-212 was closed: false alarm."},
}
_TEMPLATE_ENV = {PROMPT: "WHATSAPP_TEMPLATE_PROMPT", CONTACT: "WHATSAPP_TEMPLATE_CONTACT",
                 NOTICE: "WHATSAPP_TEMPLATE_NOTICE"}

#: Graph API error codes: what a retry cannot fix, and what means "this
#: template cannot be used" (``auto`` then tries a session message).
TEMPLATE_ERRORS = frozenset({132000, 132001, 132005, 132007, 132012, 132015, 132016,
                             132018})
PERMANENT_ERRORS = frozenset({100, 190, 131008, 131009, 131021, 131026, 131030, 131031,
                              131045, 131047, 131051, 131052}) | TEMPLATE_ERRORS
RATE_ERRORS = frozenset({4, 80007, 130429, 131048, 131056})


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, "").strip() or default


def access_token() -> str:
    return _env("WHATSAPP_ACCESS_TOKEN")


def phone_number_id() -> str:
    return _env("WHATSAPP_PHONE_NUMBER_ID")


def api_version() -> str:
    version = _env("WHATSAPP_API_VERSION", DEFAULT_API_VERSION)
    return version if version.startswith("v") else f"v{version}"


def template_language() -> str:
    return _env("WHATSAPP_TEMPLATE_LANG", "en")


def template_name(kind: str) -> str:
    return _env(_TEMPLATE_ENV[kind], TEMPLATES[kind]["name"])


def mode() -> str:
    chosen = _env("WHATSAPP_MODE", "template").lower()
    return chosen if chosen in MODES else "template"


def configured() -> bool:
    """The token and the sender's phone number id are set (SOS recipients on
    WhatsApp exist only then)."""
    return bool(access_token() and phone_number_id())


def wa_number(address: str | None) -> str:
    """``+919812345610`` -> ``919812345610`` (the Graph API's ``to``)."""
    return re.sub(r"\D", "", address or "")


def from_wa(wa_id: str | None) -> str:
    """A webhook's ``from`` (digits) -> E.164."""
    digits = re.sub(r"\D", "", wa_id or "")
    return f"+{digits}" if digits else ""


def flatten(text: str, limit: int = MAX_PARAM) -> str:
    """One line for a template parameter: no new lines or tabs, no runs of
    spaces, at most ``limit`` characters."""
    parts = [re.sub(r"\s+", " ", line).strip() for line in (text or "").splitlines()]
    flat = " · ".join(p for p in parts if p)
    return flat if len(flat) <= limit else flat[:limit - 1].rstrip() + "…"


def strip_banner(text: str) -> str:
    """The text without the test banner (the template body carries it)."""
    for banner in (guard.TEST_PREFIX, guard.TEST_PREFIX_GSM):
        if text.startswith(banner):
            return text[len(banner):].lstrip()
    return text


def template_kind(buttons: tuple) -> str:
    actions = {b.action for b in buttons}
    if "approve" in actions or "checking" in actions:
        return PROMPT
    if "acknowledge" in actions:
        return CONTACT
    return NOTICE


def button_payload(button: Button, index: int) -> str:
    """What a quick reply sends back: the callback data, or ``open:<index>``."""
    return button.callback_data or f"{OPEN_PREFIX}{index}"


def template_payloads(kind: str, buttons: tuple) -> list[str]:
    """One payload per button of the template, in template order; a template
    button this message has no matching button for answers ``none``."""
    payloads = []
    for _, action in TEMPLATES[kind]["buttons"]:
        found = next(((i, b) for i, b in enumerate(buttons) if b.action == action), None)
        payloads.append(button_payload(found[1], found[0]) if found else UNUSED)
    return payloads


#: Reply-button titles (20 characters at most) by action: the templates' labels.
SHORT_LABELS = {action: label for kind in TEMPLATES.values()
                for label, action in kind["buttons"]}


def short_label(button: Button) -> str:
    return (SHORT_LABELS.get(button.action) or button.label)[:MAX_BUTTON_TITLE]


def fold_links(text: str, buttons: tuple) -> str:
    """The text with each link button written out (session messages: reply
    buttons cannot carry a URL)."""
    links = [f"{b.label}: {b.url}" for b in buttons if b.url and not b.callback_data]
    return text + ("\n\n" + "\n".join(links) if links else "")


class WhatsAppError(Exception):
    """A Graph API refusal; ``code`` is Meta's error code when there is one."""

    def __init__(self, message: str, *, code: int | None = None, status: int | None = None,
                 retryable: bool = True) -> None:
        super().__init__(message)
        self.code, self.status, self.retryable = code, status, retryable

    @property
    def template_problem(self) -> bool:
        return self.code in TEMPLATE_ERRORS


class WhatsAppApi:
    """The few Graph API calls SENTRIX makes. The token goes in a header,
    never in a URL or an error."""

    def __init__(self, token: str | None = None, number_id: str | None = None, *,
                 client: httpx.Client | None = None, timeout: float = 15.0) -> None:
        self._token = token
        self._number_id = number_id
        self._client = client
        self._timeout = timeout

    def _post(self, payload: dict) -> dict:
        token = self._token or access_token()
        number = self._number_id or phone_number_id()
        if not token or not number:
            raise WhatsAppError("WHATSAPP_ACCESS_TOKEN / WHATSAPP_PHONE_NUMBER_ID not set",
                                retryable=False)
        url = f"{GRAPH_BASE}/{api_version()}/{number}/messages"
        headers = {"Authorization": f"Bearer {token}"}
        try:
            if self._client is not None:
                response = self._client.post(url, json=payload, headers=headers)
            else:
                with httpx.Client(timeout=self._timeout) as client:
                    response = client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise WhatsAppError(f"network error: {type(exc).__name__}") from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code < 400 and not body.get("error"):
            return body
        error = body.get("error") or {}
        code = error.get("code")
        details = (error.get("error_data") or {}).get("details") or error.get("message") or ""
        retryable = (response.status_code >= 500 or code in RATE_ERRORS
                     or (code not in PERMANENT_ERRORS and response.status_code == 429))
        raise WhatsAppError(f"graph api {response.status_code} ({code}): {details}"[:300],
                            code=code, status=response.status_code, retryable=retryable)

    def send_template(self, to: str, name: str, language: str, text: str,
                      payloads: list[str]) -> dict:
        components: list[dict] = [{"type": "body",
                                   "parameters": [{"type": "text", "text": text}]}]
        for index, payload in enumerate(payloads):
            components.append({"type": "button", "sub_type": "quick_reply",
                               "index": str(index),
                               "parameters": [{"type": "payload", "payload": payload}]})
        return self._post({"messaging_product": "whatsapp", "to": to, "type": "template",
                           "template": {"name": name, "language": {"code": language},
                                        "components": components}})

    def send_text(self, to: str, text: str) -> dict:
        return self._post({"messaging_product": "whatsapp", "recipient_type": "individual",
                           "to": to, "type": "text",
                           "text": {"body": text[:MAX_TEXT], "preview_url": False}})

    def send_buttons(self, to: str, text: str, buttons: list[tuple[str, str]]) -> dict:
        """Up to three reply buttons ``(id, title)``: session messages only."""
        return self._post({
            "messaging_product": "whatsapp", "recipient_type": "individual", "to": to,
            "type": "interactive",
            "interactive": {"type": "button", "body": {"text": text[:MAX_INTERACTIVE_BODY]},
                            "action": {"buttons": [
                                {"type": "reply", "reply": {"id": i[:256],
                                                            "title": t[:MAX_BUTTON_TITLE]}}
                                for i, t in buttons[:3]]}}})


def _message_id(body: dict) -> str | None:
    messages = body.get("messages") or []
    return (messages[0] or {}).get("id") if messages else None


class WhatsAppTransport:
    """``whatsapp``: Meta accepted the message (SENT); the webhook's delivery
    status makes it DELIVERED or FAILED (``Router.confirm_delivery``)."""

    name = "whatsapp"
    delivers_on_send = False

    def __init__(self, api: WhatsAppApi | None = None) -> None:
        self.api = api or WhatsAppApi()

    def state(self) -> tuple[bool, str]:
        if not (self.api._token or access_token()):
            return False, "WHATSAPP_ACCESS_TOKEN not set"
        if not (self.api._number_id or phone_number_id()):
            return False, "WHATSAPP_PHONE_NUMBER_ID not set"
        return True, f"cloud api, {mode()} messages"

    def send(self, recipient: Recipient, message: Message) -> SendResult:
        to = wa_number(recipient.address)
        if not to:
            return SendResult(False, error="not a phone number", retryable=False)
        session_only = str(message.template).startswith("whatsapp.")
        chosen = "session" if session_only else mode()
        try:
            if chosen == "session":
                sent = self._session(to, message)
            else:
                try:
                    sent = self._template(to, message)
                except WhatsAppError as exc:
                    if chosen != "auto" or not exc.template_problem:
                        raise
                    log.info("whatsapp template refused (%s): trying a session message",
                             exc.code)
                    sent = self._session(to, message)
        except WhatsAppError as exc:
            return SendResult(False, error=str(exc), retryable=exc.retryable)
        return SendResult(True, provider_id=_message_id(sent))

    def _template(self, to: str, message: Message) -> dict:
        kind = template_kind(message.buttons)
        text = flatten(strip_banner(message.text))
        return self.api.send_template(to, template_name(kind), template_language(), text,
                                      template_payloads(kind, message.buttons))

    def _session(self, to: str, message: Message) -> dict:
        replies = [(b.callback_data, short_label(b)) for b in message.buttons
                   if b.callback_data]
        text = fold_links(message.text, message.buttons)
        if replies:
            return self.api.send_buttons(to, text, replies)
        return self.api.send_text(to, text)


# ── inbound ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Inbound:
    """One webhook event, reduced to what the Portal acts on.

    ``kind``: ``button`` (a quick reply or reply button: ``payload``),
    ``text``, ``status`` (``status`` of our ``message_id``: sent, delivered,
    read, failed) or ``other``. ``sender`` is E.164; ``context_id`` is the
    id of our message a button belongs to."""

    kind: str
    message_id: str | None = None
    sender: str = ""
    name: str = ""
    payload: str | None = None
    text: str | None = None
    context_id: str | None = None
    status: str | None = None
    error: str | None = None
    number_id: str | None = None


def _inbound_message(node: dict, value: dict, names: dict[str, str]) -> Inbound:
    sender = from_wa(node.get("from"))
    common = dict(message_id=node.get("id"), sender=sender,
                  name=names.get(node.get("from") or "", ""),
                  context_id=(node.get("context") or {}).get("id"),
                  number_id=(value.get("metadata") or {}).get("phone_number_id"))
    kind = node.get("type")
    if kind == "button":
        button = node.get("button") or {}
        return Inbound("button", payload=button.get("payload") or button.get("text"),
                       **common)
    if kind == "interactive":
        interactive = node.get("interactive") or {}
        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
        return Inbound("button", payload=reply.get("id"), **common)
    if kind == "text":
        return Inbound("text", text=((node.get("text") or {}).get("body") or "")[:1000],
                       **common)
    return Inbound("other", **common)


def parse_webhook(body: dict | None) -> list[Inbound]:
    """Every message and status in a webhook body (Meta batches them)."""
    events: list[Inbound] = []
    if not isinstance(body, dict) or body.get("object") != "whatsapp_business_account":
        return events
    for entry in body.get("entry") or []:
        for change in (entry or {}).get("changes") or []:
            if (change or {}).get("field") != "messages":
                continue
            value = change.get("value") or {}
            names = {c.get("wa_id"): ((c.get("profile") or {}).get("name") or "")
                     for c in value.get("contacts") or [] if isinstance(c, dict)}
            for node in value.get("messages") or []:
                if isinstance(node, dict):
                    events.append(_inbound_message(node, value, names))
            for node in value.get("statuses") or []:
                if not isinstance(node, dict):
                    continue
                errors = node.get("errors") or []
                error = None
                if errors:
                    first = errors[0] or {}
                    error = f"{first.get('code')}: {first.get('title') or first.get('message')}"
                events.append(Inbound(
                    "status", message_id=node.get("id"), sender=from_wa(node.get("recipient_id")),
                    status=node.get("status"), error=error,
                    number_id=(value.get("metadata") or {}).get("phone_number_id")))
    return events


def app_secret() -> str:
    return _env("WHATSAPP_APP_SECRET")


def verify_token() -> str:
    return _env("WHATSAPP_VERIFY_TOKEN")


def signature_ok(body: bytes, header: str | None, secret: str | None = None) -> bool:
    """``X-Hub-Signature-256: sha256=<hex>`` over the raw body, constant time."""
    key = secret if secret is not None else app_secret()
    if not key or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(key.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):].strip().lower())


def verify_subscription(mode_: str | None, token: str | None,
                        challenge: str | None) -> str | None:
    """Meta's GET handshake: the challenge when the token matches, else None."""
    expected = verify_token()
    if (mode_ != "subscribe" or not expected or not token or not challenge
            or not hmac.compare_digest(expected.encode(), token.encode())):
        return None
    return challenge


__all__ = ["WhatsAppApi", "WhatsAppTransport", "WhatsAppError", "Inbound", "TEMPLATES",
           "PROMPT", "CONTACT", "NOTICE", "OPEN_PREFIX", "UNUSED", "MODES",
           "SIGNATURE_HEADER", "configured", "mode", "template_name", "template_kind",
           "template_payloads", "button_payload", "flatten", "strip_banner", "fold_links",
           "wa_number", "from_wa", "parse_webhook", "signature_ok", "verify_subscription",
           "access_token", "phone_number_id", "api_version", "template_language"]
