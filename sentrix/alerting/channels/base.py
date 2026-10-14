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

import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable

from sentrix.alerting import guard

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

    ``action`` is an SOS action name (approve | checking | decline_page |
    open). E-mail/SMS links always open a confirmation page; the action runs
    only on a POST with CSRF (docs/7 §8.4)."""

    label: str
    action: str
    url: str | None = None
    callback_data: str | None = None    # Telegram: "<request id>:<nonce>:<action>"


@dataclass(frozen=True)
class Message:
    """One message, already rendered. Channels may shorten it (SMS)."""

    template: str                    # sos.prompt | sos.reminder | sos.full | ...
    step: str                        # dedupe step: t0 | r120 | lapse | repage1 | ...
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


@runtime_checkable
class Transport(Protocol):
    """Network I/O for one channel. ``send`` must not raise."""

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


class ChannelRouter:
    """Queue, deduplicate, deliver and retry (skeleton; T-CH completes it).

    ``max_attempts`` tries per message, waiting ``backoff`` seconds between
    them; after that the item stays FAILED and is retried every
    ``failed_retry_seconds`` (the SOS engine decides when FAILED means
    "SOS NOT DELIVERED"). SUPPRESSED (send guard) is final.
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

    def register(self, channel: Channel) -> None:
        self.channels[channel.name] = channel

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
        return attempts

    def status(self) -> dict[str, Any]:
        return {"channels": sorted(self.channels), **guard.status()}


__all__ = ["Recipient", "Button", "Message", "SendResult", "Transport",
           "FakeTransport", "Channel", "OutboxItem", "Outbox", "MemoryOutbox",
           "ChannelRouter", "STATUSES", "QUEUED", "SENDING", "SENT",
           "DELIVERED", "FAILED", "SUPPRESSED"]
