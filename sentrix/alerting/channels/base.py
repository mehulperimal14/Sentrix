"""Channel interfaces: Message, Recipient, Transport, Channel, ChannelRouter.

Contract for track T-CH (docs/7 §8). Every outbound message on the device *and*
on the Portal is a :class:`Message` to a :class:`Recipient`, handed to a
:class:`ChannelRouter`, which keeps it in an outbox (deduplicated by
``request|recipient|channel|step``), and delivers it through a
:class:`Channel` whose :class:`Transport` does the network I/O.

The send guard sits inside :meth:`Channel.deliver`, in front of every
transport: a refused message is recorded as SUPPRESSED with the guard's reason
(and the rendered text, so a dry run shows what would have been sent); it never
reaches the transport. Tests inject :class:`FakeTransport`; nothing in this
module opens a socket.

Stdlib only (the Portal imports it).
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Callable, Protocol, runtime_checkable

from sentrix.alerting import guard

log = logging.getLogger("sentrix.channels")

#: Message lifecycle in an outbox.
QUEUED, SENDING, SENT, DELIVERED, FAILED, SUPPRESSED = (
    "QUEUED", "SENDING", "SENT", "DELIVERED", "FAILED", "SUPPRESSED")
STATUSES = (QUEUED, SENDING, SENT, DELIVERED, FAILED, SUPPRESSED)


@dataclass(frozen=True)
class Recipient:
    """Who gets a message, on which channel, at which address."""

    ref: str            # owner | member:<id> | contact:<id> | desk | user:<id>
    name: str
    channel: str        # a key of guard.CHANNEL_SWITCHES
    address: str        # E.164 phone, e-mail, or tg:<chat id>
    role: str = "owner"  # owner | member | contact | desk | operator


@dataclass(frozen=True)
class Button:
    """An action offered with the message (Telegram inline button, e-mail link).

    ``action`` is an SOS action name (approve | checking | acknowledge |
    decline_page | open). E-mail/SMS links always open a confirmation page;
    the action runs only on a POST with CSRF (docs/7 §8.4). A Telegram button
    has either ``url`` (e.g. "False alarm" opens the Portal PIN page: a PIN
    typed in a chat stays in its history) or ``callback_data`` from
    :func:`callback_data` (``"<nonce>:<letter>"``, at most 64 bytes)."""

    label: str
    action: str
    url: str | None = None
    callback_data: str | None = None


# ── Telegram callback data (docs/7 §8.4) ──────────────────────────────────────
#: Telegram caps ``callback_data`` at 64 bytes, so a button carries only a
#: nonce and a one-letter action; the Portal finds the request, the recipient
#: and the expiry from the nonce (``action_tokens.nonce``).
CALLBACK_DATA_MAX_BYTES = 64
#: ``secrets.token_urlsafe(12)``: 16 URL-safe characters (96 bits).
CALLBACK_NONCE_BYTES = 12
CALLBACK_ACTIONS = {"a": "approve", "c": "checking", "k": "acknowledge"}
_CALLBACK_LETTERS = {v: k for k, v in CALLBACK_ACTIONS.items()}


def new_callback_nonce() -> str:
    return secrets.token_urlsafe(CALLBACK_NONCE_BYTES)


def callback_data(nonce: str, action: str) -> str:
    """``"<nonce>:<letter>"`` for an inline button; raises ``ValueError`` for
    an unknown action or a value over 64 bytes."""
    letter = _CALLBACK_LETTERS.get(action)
    if letter is None:
        raise ValueError(f"no Telegram callback for action {action!r}")
    data = f"{nonce}:{letter}"
    if len(data.encode("utf-8")) > CALLBACK_DATA_MAX_BYTES or ":" in nonce:
        raise ValueError("callback data must be at most 64 bytes, nonce without ':'")
    return data


def parse_callback_data(data: str | None) -> tuple[str, str] | None:
    """(nonce, action) or None for anything malformed."""
    if not data or len(data.encode("utf-8")) > CALLBACK_DATA_MAX_BYTES:
        return None
    nonce, sep, letter = data.rpartition(":")
    action = CALLBACK_ACTIONS.get(letter)
    if not sep or not nonce or action is None:
        return None
    return nonce, action


@dataclass(frozen=True)
class Message:
    """One message, already rendered. Channels may shorten it (SMS)."""

    #: sos.prompt | sos.reminder | sos.lapse_warning | sos.lapsed | sos.full |
    #: sos.duress | sos.resolved | sos.failed | sos.unacknowledged |
    #: sos.acknowledged | sos.sms; legacy.l4_sms | legacy.l5_call |
    #: legacy.dispatch; telegram.linked | telegram.link_failed | telegram.help;
    #: account.activation | account.reset | account.verify_email |
    #: account.security | contact.consent.
    template: str
    #: Dedupe step. SOS messages use ``sentrix.sos.states`` step names (t0,
    #: r120, r300, r600, r1200, lapse, lapsed, rise4, rise5, contacts_t0,
    #: desk_t0, repage1..n, resolved, duress) plus failed<n>, undelivered,
    #: unacknowledged, acknowledged, help_now<n> (unique per request); other
    #: messages a unique one: "activation:<code id>", "verify:<token id>",
    #: "consent:<token id>", "password:<unix s>", "newip:<audit id>:<ip
    #: digest>", "test:<unix ms>", "l4:<unix ms>", "l5:<unix ms>",
    #: "dispatch:<unix ms>", "tglink:<token id>", "tgbad:<update id>",
    #: "tghelp:<update id>".
    step: str
    text: str
    request_id: str | None = None
    subject: str | None = None       # e-mail
    html: str | None = None          # e-mail
    buttons: tuple = ()
    urgency: str = "NORMAL"
    created_at: float = field(default_factory=time.time)

    def dedupe_key(self, recipient: Recipient) -> str:
        return f"{self.request_id or '-'}|{recipient.ref}|{recipient.channel}|{self.step}"


@dataclass(frozen=True)
class SendResult:
    ok: bool
    provider_id: str | None = None
    error: str | None = None
    #: False for errors a retry cannot fix (bad address, refused by the guard).
    retryable: bool = True
    suppressed: bool = False
    #: The provider asked to wait at least this many seconds before a retry
    #: (Telegram 429 ``retry_after``); the router never retries sooner.
    retry_after: float | None = None


@runtime_checkable
class Transport(Protocol):
    """Network I/O for one channel. ``send`` must not raise.

    Optional conventions the router reads (T-CH): ``state() -> (available,
    detail)`` for readiness, and ``delivers_on_send = True`` when a
    successful send already means DELIVERED (Telegram)."""

    name: str

    def send(self, recipient: Recipient, message: Message) -> SendResult: ...


class FakeTransport:
    """Records instead of sending. The only transport tests may use."""

    def __init__(self, name: str = "fake", fail_times: int = 0,
                 error: str = "simulated failure") -> None:
        self.name = name
        self.sent: list[tuple[Recipient, Message]] = []
        self.attempts = 0
        self._fail_times = fail_times
        self._error = error

    def send(self, recipient: Recipient, message: Message) -> SendResult:
        self.attempts += 1
        if self._fail_times > 0:
            self._fail_times -= 1
            return SendResult(False, error=self._error, retryable=True)
        self.sent.append((recipient, message))
        return SendResult(True, provider_id=f"fake-{len(self.sent)}")


class Channel:
    """A named channel: send guard, test banner, then the transport."""

    def __init__(self, name: str, transport: Transport, *,
                 policy_factory=guard.SendPolicy.from_env) -> None:
        if name not in guard.CHANNEL_SWITCHES:
            raise ValueError(f"unknown channel {name!r}")
        self.name = name
        self.transport = transport
        self._policy_factory = policy_factory

    def deliver(self, recipient: Recipient, message: Message) -> SendResult:
        policy = self._policy_factory()
        decision = guard.may_send(self.name, recipient.address, policy=policy)
        if not decision:
            return SendResult(False, error=decision.reason, retryable=False,
                              suppressed=True)
        text = guard.prepare_text(self.name, message.text, policy=policy)
        try:
            return self.transport.send(recipient, replace(message, text=text))
        except Exception as exc:          # transports must not raise; be safe
            return SendResult(False, error=f"transport error: {exc}")


@dataclass
class OutboxItem:
    id: str
    recipient: Recipient
    message: Message
    status: str = QUEUED
    attempts: int = 0
    next_attempt_at: float = 0.0
    provider_id: str | None = None
    error: str | None = None
    sent_at: float | None = None

    @property
    def dedupe_key(self) -> str:
        return self.message.dedupe_key(self.recipient)


def message_meta(recipient: Recipient, message: Message) -> dict[str, Any]:
    """What an outbox stores beside text and address so ``due`` rebuilds the
    exact message: recipient name and role, HTML, buttons, urgency."""
    return {"recipient_name": recipient.name, "recipient_role": recipient.role,
            "html": message.html, "urgency": message.urgency,
            "buttons": [asdict(b) for b in message.buttons]}


def rebuild(ref: str, channel: str, address: str, meta: dict | None, *,
            template: str, step: str, text: str, request_id: str | None,
            subject: str | None, created_at: float) -> tuple[Recipient, Message]:
    """Inverse of :func:`message_meta` for an outbox row."""
    meta = meta or {}
    recipient = Recipient(ref=ref, name=meta.get("recipient_name") or ref,
                          channel=channel, address=address,
                          role=meta.get("recipient_role") or "owner")
    buttons = tuple(Button(**b) for b in meta.get("buttons") or ())
    message = Message(template=template, step=step, text=text, request_id=request_id,
                      subject=subject, html=meta.get("html"), buttons=buttons,
                      urgency=meta.get("urgency") or "NORMAL", created_at=created_at)
    return recipient, message


@runtime_checkable
class Outbox(Protocol):
    """Persistent queue behind the router (device: SQLite ``notifications``;
    Portal: its ``notifications`` table; tests: :class:`MemoryOutbox`)."""

    def add(self, item: OutboxItem) -> bool: ...          # False on a duplicate key

    def due(self, now: float, limit: int = 50) -> list[OutboxItem]: ...

    def update(self, item: OutboxItem) -> None: ...


class MemoryOutbox:
    def __init__(self) -> None:
        self._items: dict[str, OutboxItem] = {}
        self._keys: set[str] = set()
        self._lock = threading.Lock()

    def add(self, item: OutboxItem) -> bool:
        with self._lock:
            if item.dedupe_key in self._keys:
                return False
            self._keys.add(item.dedupe_key)
            self._items[item.id] = item
            return True

    def due(self, now: float, limit: int = 50) -> list[OutboxItem]:
        with self._lock:
            ready = [i for i in self._items.values()
                     if i.status in (QUEUED, FAILED) and i.next_attempt_at <= now]
            return sorted(ready, key=lambda i: (i.next_attempt_at, i.id))[:limit]

    def update(self, item: OutboxItem) -> None:
        with self._lock:
            self._items[item.id] = item

    def items(self) -> list[OutboxItem]:
        with self._lock:
            return list(self._items.values())


@runtime_checkable
class ChannelsService(Protocol):
    """The ``channels`` service (device: ``registry.get("channels")``, built by
    T-CH's ``router.build_service``; Portal: ``services/notify.py``). The SOS
    engine and the link's ``sms.send`` reach every channel only through it.

    * ``enqueue`` -> outbox id, or None (duplicate dedupe key, unknown channel).
    * ``ready(channel)`` -> a message on that channel could leave now (the
      channel is registered and both send switches are on; the recipient
      allowlist is checked per message).
    * ``status()`` -> the ``state.channels`` block (docs/7 §9.4).
    * ``add_listener(fn)`` -> ``fn(OutboxItem)`` after every status change
      (SENT, FAILED, SUPPRESSED, DELIVERED), on the worker's thread: enqueue
      only. The SOS engine derives ``delivered`` / ``delivery_failed`` /
      ``undelivered`` from it; the link client publishes
      ``notification.status``. SUPPRESSED (a dry run) never counts as
      delivered.
    """

    name: str

    def enqueue(self, recipient: Recipient, message: Message) -> str | None: ...

    def ready(self, channel: str) -> bool: ...

    def status(self) -> dict[str, Any]: ...

    def add_listener(self, callback: Callable[[OutboxItem], None]) -> None: ...

    def remove_listener(self, callback: Callable[[OutboxItem], None]) -> None: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def health(self) -> dict[str, Any]: ...


class ChannelRouter:
    """Queue, deduplicate, deliver and retry (skeleton; T-CH completes it).

    ``max_attempts`` tries per message, waiting ``backoff`` seconds between
    them; after that the item stays FAILED and is retried every
    ``failed_retry_seconds`` (the SOS engine decides when FAILED means
    "SOS NOT DELIVERED"). SUPPRESSED (send guard) is final. Listeners
    (:meth:`add_listener`) hear every status change :meth:`pump` makes.
    """

    def __init__(self, channels: dict[str, Channel] | None = None,
                 outbox: Outbox | None = None, *, max_attempts: int = 3,
                 backoff: tuple = (30.0, 90.0), failed_retry_seconds: float = 120.0,
                 clock=time.time) -> None:
        self.channels: dict[str, Channel] = dict(channels or {})
        self.outbox: Outbox = outbox if outbox is not None else MemoryOutbox()
        self.max_attempts = max_attempts
        self.backoff = tuple(backoff)
        self.failed_retry_seconds = failed_retry_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._listeners: list[Callable[[OutboxItem], None]] = []

    def register(self, channel: Channel) -> None:
        self.channels[channel.name] = channel

    def add_listener(self, callback: Callable[[OutboxItem], None]) -> None:
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[OutboxItem], None]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _notify(self, item: OutboxItem) -> None:
        for callback in list(self._listeners):
            try:
                callback(replace(item))
            except Exception as exc:           # a listener never stops delivery
                log.error("outbox listener failed: %s", exc)

    def ready(self, channel: str) -> bool:
        """Registered, and ``SENTRIX_LIVE_ALERTS`` plus its own switch are on."""
        found = self.channels.get(channel)
        if found is None:
            return False
        return found._policy_factory().channel_live(channel)

    def enqueue(self, recipient: Recipient, message: Message) -> str | None:
        """Queue a message; returns its id, or None for a duplicate key or an
        unknown channel."""
        if recipient.channel not in self.channels:
            return None
        item = OutboxItem(id=str(uuid.uuid4()), recipient=recipient, message=message,
                          next_attempt_at=self._clock())
        return item.id if self.outbox.add(item) else None

    def pump(self, now: float | None = None, limit: int = 50) -> int:
        """Deliver everything due; returns the number of attempts made."""
        now = self._clock() if now is None else now
        attempts = 0
        with self._lock:
            for item in self.outbox.due(now, limit):
                channel = self.channels.get(item.recipient.channel)
                if channel is None:
                    continue
                attempts += 1
                item.attempts += 1
                result = channel.deliver(item.recipient, item.message)
                if result.ok:
                    item.status, item.sent_at = SENT, now
                    item.provider_id, item.error = result.provider_id, None
                elif result.suppressed:
                    item.status, item.error = SUPPRESSED, result.error
                else:
                    item.status, item.error = FAILED, result.error
                    if not result.retryable:
                        item.next_attempt_at = float("inf")
                    elif item.attempts < self.max_attempts:
                        wait = self.backoff[min(item.attempts - 1, len(self.backoff) - 1)]
                        item.next_attempt_at = now + wait
                    else:
                        item.next_attempt_at = now + self.failed_retry_seconds
                self.outbox.update(item)
                self._notify(item)
        return attempts

    def status(self) -> dict[str, Any]:
        return {"channels": sorted(self.channels), **guard.status()}


__all__ = ["Recipient", "Button", "Message", "SendResult", "Transport",
           "FakeTransport", "Channel", "OutboxItem", "Outbox", "MemoryOutbox",
           "ChannelRouter", "ChannelsService", "STATUSES", "QUEUED", "SENDING", "SENT",
           "DELIVERED", "FAILED", "SUPPRESSED", "CALLBACK_ACTIONS",
           "CALLBACK_DATA_MAX_BYTES", "CALLBACK_NONCE_BYTES", "new_callback_nonce",
           "callback_data", "parse_callback_data", "message_meta", "rebuild"]
