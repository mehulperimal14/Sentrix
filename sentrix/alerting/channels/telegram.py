"""Telegram Bot API: transport, inline buttons and inbound updates (docs/7 §8.4).

Outbound (``telegram`` channel): ``sendMessage`` with a short text and inline
buttons, nothing else: never a photo, an address or medical notes (portal
review: Telegram is a processor outside India; contacts see the snapshot only
on their token page). Plain text (no ``parse_mode``, so nothing in a message
is interpreted as markup) and link previews off (a preview would make
Telegram's servers open the Portal link).

Buttons carry either ``callback_data`` = ``"<nonce>:<letter>"`` (64 bytes at
most; ``channels.base.callback_data``) for approve / checking / acknowledge,
or a URL ("False alarm" opens the Portal PIN page: a PIN typed in a chat would
stay in its history). When Telegram refuses a URL button (a development
``http://portal.localhost`` address), the message is sent again with the
links written into the text.

Inbound (the Portal, ``sentrix_portal.services.notify``): :func:`parse_update`
turns an update into an :class:`Inbound` (callback, ``/start <code>``, the bot
added to a group); the Portal accepts callbacks only from private chats whose
sender is the linked chat of the token's subject. Development uses
``getUpdates`` long polling (:meth:`TelegramApi.get_updates`); the VM uses a
webhook registered with a ``secret_token`` (:meth:`TelegramApi.set_webhook`,
checked by :func:`webhook_secret_ok` on ``POST /hooks/telegram``).

The bot token is read from ``TELEGRAM_BOT_TOKEN`` at use time and never
appears in an error, a log line or a status: httpx logs request URLs, and the
Bot API puts the token in the URL, so a filter masks it on the ``httpx``
logger.

Stdlib + httpx only: the Portal imports this module.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

from sentrix.alerting.channels.base import Button, Message, Recipient, SendResult

log = logging.getLogger("sentrix.channels.telegram")

API_BASE = "https://api.telegram.org"
#: Telegram's limit for one message.
MAX_TEXT = 4096
ALLOWED_UPDATES = ("message", "callback_query", "my_chat_member")
#: ``/start`` deep-link payloads: 1-64 of ``A-Z a-z 0-9 _ -``.
START_CODE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
#: Webhook ``secret_token``: 1-256 of the same alphabet.
WEBHOOK_SECRET = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
PRIVATE = "private"
_TOKEN_IN_URL = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")


class _MaskBotToken(logging.Filter):
    """Mask ``/bot<token>/`` in httpx's request log lines."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        if "/bot" in message:
            record.msg, record.args = _TOKEN_IN_URL.sub("/bot***", message), ()
        return True


if not any(isinstance(f, _MaskBotToken) for f in logging.getLogger("httpx").filters):
    logging.getLogger("httpx").addFilter(_MaskBotToken())


def bot_token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def webhook_secret() -> str:
    return os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()


def webhook_secret_ok(supplied: str | None) -> bool:
    """The ``X-Telegram-Bot-Api-Secret-Token`` header matches (constant
    time). False when no secret is configured: then no webhook is accepted."""
    secret = webhook_secret()
    if not secret or not supplied:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), secret.encode("utf-8"))


class TelegramError(RuntimeError):
    """A Bot API call failed. ``status`` is the HTTP status (None: network)."""

    def __init__(self, method: str, status: int | None, description: str, *,
                 retry_after: float | None = None) -> None:
        super().__init__(f"telegram {method}: {status or 'network'} {description}")
        self.method = method
        self.status = status
        self.description = description
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        """Network errors, 429 and 5xx; not 400 (bad request), 401 (bad
        token), 403 (the user blocked the bot) or 404."""
        return self.status is None or self.status == 429 or self.status >= 500

    @property
    def button_refused(self) -> bool:
        text = self.description.lower()
        return self.status == 400 and ("button" in text or "url" in text)


def _scrub(text: str) -> str:
    token = bot_token()
    text = _TOKEN_IN_URL.sub("/bot***", text)
    return text.replace(token, "***") if token else text


def _result(method: str, response: httpx.Response) -> Any:
    try:
        data = response.json()
    except ValueError:
        raise TelegramError(method, response.status_code, "response is not JSON") from None
    if response.status_code == 200 and data.get("ok"):
        return data.get("result")
    parameters = data.get("parameters") or {}
    raise TelegramError(method, response.status_code,
                        _scrub(str(data.get("description") or response.reason_phrase)),
                        retry_after=parameters.get("retry_after"))


def _network_error(method: str, exc: Exception) -> TelegramError:
    return TelegramError(method, None, _scrub(f"{type(exc).__name__}: {exc}"))


# ── payloads (shared by the sync and async clients) ───────────────────────────
def inline_keyboard(buttons: tuple[Button, ...] | list[Button],
                    *, with_urls: bool = True) -> dict | None:
    """One button per row. Callback buttons need valid ``callback_data``;
    URL buttons an http(s) URL. Others are dropped."""
    rows = []
    for button in buttons:
        if button.callback_data:
            if len(button.callback_data.encode("utf-8")) > 64:
                log.warning("telegram: callback data over 64 bytes dropped (%s)", button.action)
                continue
            rows.append([{"text": button.label[:64], "callback_data": button.callback_data}])
        elif with_urls and button.url and button.url.startswith(("https://", "http://")):
            rows.append([{"text": button.label[:64], "url": button.url}])
    return {"inline_keyboard": rows} if rows else None


def fold_links(text: str, buttons: tuple[Button, ...] | list[Button]) -> str:
    """The text with every URL button written out as "Label: URL"."""
    links = [f"{b.label}: {b.url}" for b in buttons
             if b.url and not b.callback_data and b.url.startswith(("https://", "http://"))]
    if not links:
        return text
    return f"{text}\n\n" + "\n".join(links)


def message_payload(chat_id: int, text: str, buttons=(), *, with_urls: bool = True) -> dict:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text[:MAX_TEXT],
                               "link_preview_options": {"is_disabled": True},
                               "disable_web_page_preview": True}
    keyboard = inline_keyboard(tuple(buttons), with_urls=with_urls)
    if keyboard:
        payload["reply_markup"] = keyboard
    return payload


def _updates_payload(offset: int | None, timeout: int) -> dict:
    payload: dict[str, Any] = {"timeout": int(timeout), "allowed_updates": list(ALLOWED_UPDATES)}
    if offset is not None:
        payload["offset"] = int(offset)
    return payload


def _webhook_payload(url: str, secret_token: str, drop_pending: bool) -> dict:
    if not url.startswith("https://"):
        raise ValueError("a Telegram webhook needs an https:// URL")
    if not WEBHOOK_SECRET.match(secret_token or ""):
        raise ValueError("TELEGRAM_WEBHOOK_SECRET must be 1-256 of A-Z a-z 0-9 _ -")
    return {"url": url, "secret_token": secret_token,
            "allowed_updates": list(ALLOWED_UPDATES), "drop_pending_updates": drop_pending}


class TelegramApi:
    """Synchronous Bot API client (the router's worker thread)."""

    def __init__(self, token: str | None = None, *, client: httpx.Client | None = None,
                 timeout: float = 15.0, base_url: str = API_BASE) -> None:
        self._token = token
        self._client = client
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")

    def _url(self, method: str) -> str:
        token = self._token or bot_token()
        if not token:
            raise TelegramError(method, 401, "TELEGRAM_BOT_TOKEN is not set")
        return f"{self.base_url}/bot{token}/{method}"

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def call(self, method: str, payload: dict | None = None, *,
             timeout: float | None = None) -> Any:
        url = self._url(method)
        try:
            response = self._http().post(url, json=payload or {},
                                         timeout=timeout or self.timeout)
        except httpx.HTTPError as exc:
            raise _network_error(method, exc) from None
        return _result(method, response)

    def send_message(self, chat_id: int, text: str, buttons=(), *,
                     with_urls: bool = True) -> dict:
        return self.call("sendMessage", message_payload(chat_id, text, buttons,
                                                        with_urls=with_urls))

    def answer_callback(self, callback_id: str, text: str | None = None, *,
                        show_alert: bool = False) -> Any:
        payload: dict[str, Any] = {"callback_query_id": callback_id, "show_alert": show_alert}
        if text:
            payload["text"] = text[:200]
        return self.call("answerCallbackQuery", payload)

    def get_updates(self, offset: int | None = None, timeout: int = 25) -> list[dict]:
        return self.call("getUpdates", _updates_payload(offset, timeout),
                         timeout=timeout + self.timeout) or []

    def set_webhook(self, url: str, secret_token: str, *, drop_pending: bool = False) -> Any:
        return self.call("setWebhook", _webhook_payload(url, secret_token, drop_pending))

    def delete_webhook(self, *, drop_pending: bool = False) -> Any:
        return self.call("deleteWebhook", {"drop_pending_updates": drop_pending})

    def get_me(self) -> dict:
        return self.call("getMe")

    def leave_chat(self, chat_id: int) -> Any:
        return self.call("leaveChat", {"chat_id": chat_id})

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


class AsyncTelegramApi:
    """The same calls for the Portal's event loop (polling, callbacks)."""

    def __init__(self, token: str | None = None, *, client: httpx.AsyncClient | None = None,
                 timeout: float = 15.0, base_url: str = API_BASE) -> None:
        self._token = token
        self._client = client
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")

    _url = TelegramApi._url

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def call(self, method: str, payload: dict | None = None, *,
                   timeout: float | None = None) -> Any:
        url = self._url(method)
        try:
            response = await self._http().post(url, json=payload or {},
                                               timeout=timeout or self.timeout)
        except httpx.HTTPError as exc:
            raise _network_error(method, exc) from None
        return _result(method, response)

    async def send_message(self, chat_id: int, text: str, buttons=(), *,
                           with_urls: bool = True) -> dict:
        return await self.call("sendMessage", message_payload(chat_id, text, buttons,
                                                              with_urls=with_urls))

    async def answer_callback(self, callback_id: str, text: str | None = None, *,
                              show_alert: bool = False) -> Any:
        payload: dict[str, Any] = {"callback_query_id": callback_id, "show_alert": show_alert}
        if text:
            payload["text"] = text[:200]
        return await self.call("answerCallbackQuery", payload)

    async def get_updates(self, offset: int | None = None, timeout: int = 25) -> list[dict]:
        return await self.call("getUpdates", _updates_payload(offset, timeout),
                               timeout=timeout + self.timeout) or []

    async def set_webhook(self, url: str, secret_token: str, *,
                          drop_pending: bool = False) -> Any:
        return await self.call("setWebhook", _webhook_payload(url, secret_token, drop_pending))

    async def leave_chat(self, chat_id: int) -> Any:
        return await self.call("leaveChat", {"chat_id": chat_id})

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# ── outbound transport ────────────────────────────────────────────────────────
def chat_id_of(address: str) -> int | None:
    """``tg:<id>`` -> the id of a **private** chat (positive), else None.
    Group and channel ids are negative and are never a recipient."""
    match = re.fullmatch(r"tg:(\d{1,20})", (address or "").strip())
    if not match:
        return None
    value = int(match.group(1))
    return value if value > 0 else None


class TelegramTransport:
    """``telegram``: ``sendMessage`` only. Telegram returns the message once it
    is in the recipient's chat, so success counts as DELIVERED."""

    name = "telegram"
    delivers_on_send = True

    def __init__(self, api: TelegramApi | None = None) -> None:
        self.api = api or TelegramApi()

    def state(self) -> tuple[bool, str]:
        if not (self.api._token or bot_token()):
            return False, "TELEGRAM_BOT_TOKEN not set"
        return True, "bot configured"

    def send(self, recipient: Recipient, message: Message) -> SendResult:
        chat_id = chat_id_of(recipient.address)
        if chat_id is None:
            return SendResult(False, error="not a private Telegram chat", retryable=False)
        try:
            sent = self.api.send_message(chat_id, message.text, message.buttons)
        except TelegramError as exc:
            if not (exc.button_refused and any(b.url for b in message.buttons)):
                return SendResult(False, error=str(exc), retryable=exc.retryable,
                                  retry_after=_seconds(exc.retry_after))
            log.info("telegram refused a URL button (%s): links go in the text", exc.description)
            try:
                sent = self.api.send_message(chat_id, fold_links(message.text, message.buttons),
                                             message.buttons, with_urls=False)
            except TelegramError as again:
                return SendResult(False, error=str(again), retryable=again.retryable,
                                  retry_after=_seconds(again.retry_after))
        return SendResult(True, provider_id=f"tg:{chat_id}:{(sent or {}).get('message_id')}")


def _seconds(value) -> float | None:
    try:
        return max(0.0, float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


# ── inbound updates ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Inbound:
    """One update, reduced to what the Portal acts on.

    ``kind``: ``callback`` (a button), ``start`` (``/start [code]``),
    ``group`` (the bot was added to a group or channel: leave it) or
    ``other`` (ignored)."""

    kind: str
    update_id: int | None
    chat_id: int | None = None
    chat_type: str | None = None
    from_id: int | None = None
    from_name: str = ""
    callback_id: str | None = None
    data: str | None = None
    code: str | None = None

    @property
    def private(self) -> bool:
        return self.chat_type == PRIVATE


def _chat(node: dict | None) -> tuple[int | None, str | None]:
    chat = (node or {}).get("chat") or {}
    try:
        chat_id = int(chat["id"]) if "id" in chat else None
    except (TypeError, ValueError):
        chat_id = None
    return chat_id, chat.get("type")


def _sender(node: dict | None) -> tuple[int | None, str]:
    sender = (node or {}).get("from") or {}
    try:
        sender_id = int(sender["id"]) if "id" in sender else None
    except (TypeError, ValueError):
        sender_id = None
    name = " ".join(x for x in (sender.get("first_name"), sender.get("last_name")) if x)
    return sender_id, name[:100]


def parse_update(update: dict) -> Inbound:
    """Never raises: malformed input is ``other``."""
    if not isinstance(update, dict):
        return Inbound("other", None)
    update_id = update.get("update_id") if isinstance(update.get("update_id"), int) else None
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        chat_id, chat_type = _chat(callback.get("message"))
        from_id, from_name = _sender(callback)
        data = callback.get("data")
        return Inbound("callback", update_id, chat_id, chat_type, from_id, from_name,
                       callback_id=str(callback.get("id") or "") or None,
                       data=data if isinstance(data, str) else None)
    message = update.get("message")
    if isinstance(message, dict):
        chat_id, chat_type = _chat(message)
        from_id, from_name = _sender(message)
        text = message.get("text") if isinstance(message.get("text"), str) else ""
        words = text.strip().split(maxsplit=1)
        if words and (words[0] == "/start" or words[0].startswith("/start@")):
            code = words[1].strip() if len(words) > 1 else None
            if code is not None and not START_CODE.match(code):
                code = ""                                   # present but malformed
            return Inbound("start", update_id, chat_id, chat_type, from_id, from_name, code=code)
        return Inbound("other", update_id, chat_id, chat_type, from_id, from_name)
    member = update.get("my_chat_member")
    if isinstance(member, dict):
        chat_id, chat_type = _chat(member)
        from_id, from_name = _sender(member)
        status = ((member.get("new_chat_member") or {}).get("status"))
        if chat_type != PRIVATE and status in ("member", "administrator"):
            return Inbound("group", update_id, chat_id, chat_type, from_id, from_name)
        return Inbound("other", update_id, chat_id, chat_type, from_id, from_name)
    return Inbound("other", update_id)


def deep_link(bot_name: str, code: str) -> str:
    """``https://t.me/<bot>?start=<code>`` for a one-time link code."""
    bot = (bot_name or "").lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,64}", bot):
        raise ValueError("invalid bot name")
    if not START_CODE.match(code or ""):
        raise ValueError("link codes are 1-64 of A-Z a-z 0-9 _ -")
    return f"https://t.me/{bot}?start={code}"


__all__ = ["TelegramApi", "AsyncTelegramApi", "TelegramError", "TelegramTransport",
           "Inbound", "parse_update", "inline_keyboard", "fold_links", "message_payload",
           "chat_id_of", "deep_link", "webhook_secret_ok", "webhook_secret", "bot_token",
           "API_BASE", "ALLOWED_UPDATES", "SECRET_HEADER", "MAX_TEXT", "START_CODE"]
