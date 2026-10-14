"""The channel router and the device's ``channels`` service (docs/7 §8, track T-CH).

:class:`Router` completes ``base.ChannelRouter`` (both sides use it; the
Portal's ``services/notify.py`` builds one over its own table):

* **Outbox**: every message is a row first (``storage.notifications.SqlOutbox``
  on the device), deduplicated by ``request|recipient|channel|step``; a crash
  between "sending" and "sent" leaves the row SENDING, and the next start
  retries it (:meth:`Router.recover_interrupted`; at-least-once).
* **Retries**: ``max_attempts`` tries, waiting ``backoff`` seconds between them
  (30 s, 90 s: "3 tries over 2 min"; further steps grow by the same ratio),
  then FAILED and retried every ``failed_retry_seconds`` until the message is
  ``max_age_seconds`` old (24 h, the help path's stale limit). A retry cannot
  fix a non-retryable error (bad address, refused by the provider): FAILED,
  final. The send guard's refusal is SUPPRESSED, final, and never counts as
  delivered.
* **Status**: QUEUED -> SENDING -> SENT (provider accepted) -> DELIVERED
  (Telegram on send; the modem on a status report: :meth:`confirm_delivery`),
  or FAILED / SUPPRESSED. Listeners hear every final change.
* **Cooldowns** of the pre-portal notifier, for the legacy path only: one
  ``legacy.l4_sms`` per recipient and channel every ``sms_cooldown_seconds``
  (300 s), one ``legacy.l5_call`` every ``call_cooldown_seconds`` (600 s).
* :meth:`send_now`: queue and deliver at once on the caller's thread (the
  dispatch "Send now" and ``POST /api/channels/test`` answer with the result).
* :meth:`cancel`: the SOS engine withdraws a closed request's queued messages.

:class:`DeviceChannels` is the ``channels`` service (``build_service``; off
unless ``SENTRIX_CHANNELS_ENABLED=1``). It registers the device's channels —
``modem_sms`` / ``modem_call`` (``SENTRIX_MODEM_ENABLED``), ``twilio_sms`` /
``twilio_call`` (when ``TWILIO_*`` are set), and ``telegram`` / ``email`` only
in standalone mode (no link configured: the Portal sends those when it
exists; docs/7 §8.3) — runs the delivery worker thread, publishes the
``state.channels`` block and answers the SOS engine, the link's ``sms.send``,
the legacy notifier and ``/api/channels``.
"""

from __future__ import annotations

import copy
import logging
import math
import os
import re
import sys
import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sentrix.alerting import guard
from sentrix.alerting.channels.base import (DELIVERED, FAILED, QUEUED, SENDING, SENT,
                                            SUPPRESSED, Channel, ChannelRouter, Message,
                                            OutboxItem, Recipient, SendResult)
from sentrix.utils.redact import mask_email, mask_phone, mask_recipient

log = logging.getLogger("sentrix.channels.router")

#: Phone channels in preference order: the modem first (no per-message cost,
#: works without the Internet), then Twilio.
SMS_CHANNELS = ("modem_sms", "twilio_sms")
CALL_CHANNELS = ("modem_call", "twilio_call")
#: Templates of the pre-portal notifier path (docs/7 §8.2).
LEGACY_SMS, LEGACY_CALL, LEGACY_DISPATCH = "legacy.l4_sms", "legacy.l5_call", "legacy.dispatch"
CHANNEL_TEST = "channel.test"
FINAL_STATUSES = (SENT, DELIVERED, FAILED, SUPPRESSED)
PUMP_LIMIT = 50
#: "+91 98123-45610" style numbers, or 10-15 bare digits (not dates or codes).
_PHONE = re.compile(r"(?<![\w+])\+\d[\d\s-]{6,16}\d|(?<![\w.])\d{10,15}(?![\w.])")
_EMAIL = re.compile(r"[^@\s,;:]+@[^@\s,;:]+\.[A-Za-z]{2,}")


def scrub(text: str | None) -> str | None:
    """Mask phone numbers and e-mail addresses in provider errors (they end up
    in the status block, the UI and the logs)."""
    if not text:
        return text
    text = _EMAIL.sub(lambda m: mask_email(m.group(0)), str(text))
    return _PHONE.sub(lambda m: mask_phone(re.sub(r"[\s-]", "", m.group(0))), text)[:300]


def transport_state(transport: Any) -> tuple[bool, str]:
    """(available, detail) from a transport's optional ``state()``."""
    check = getattr(transport, "state", None)
    if check is None:
        return True, ""
    try:
        available, detail = check()
        return bool(available), str(detail or "")
    except Exception as exc:
        return False, f"state check failed: {exc}"


def _iso(ts: float | None) -> str | None:
    if ts is None or math.isinf(ts):
        return None
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=ts)).isoformat()


def notification_payload(item: OutboxItem) -> dict[str, Any]:
    """The ``notification.status`` outbox event (docs/7 §5.5): masked."""
    return {"id": item.id, "request_id": item.message.request_id,
            "channel": item.recipient.channel, "recipient_ref": item.recipient.ref,
            "recipient_masked": mask_recipient(item.recipient.address),
            "template": item.message.template, "step": item.message.step,
            "status": item.status, "error": scrub(item.error), "attempts": item.attempts,
            "sent_at": _iso(item.sent_at)}


class _MemoryOps:
    """The router extensions for an outbox that only has ``items()``
    (``base.MemoryOutbox``)."""

    def __init__(self, outbox: Any) -> None:
        self.outbox = outbox

    def _items(self) -> list[OutboxItem]:
        items = getattr(self.outbox, "items", None)
        return list(items()) if items is not None else []

    def get(self, item_id: str) -> OutboxItem | None:
        return next((replace(i) for i in self._items() if i.id == item_id), None)

    def recover_interrupted(self, now: float) -> list[OutboxItem]:
        out = []
        for item in self._items():
            if item.status == SENDING:
                item.status, item.next_attempt_at = FAILED, now
                item.error = "interrupted while sending (restart); retrying"
                self.outbox.update(item)
                out.append(replace(item))
        return out

    def cancel(self, request_id: str, reason: str,
               steps: tuple[str, ...] | None = None) -> list[OutboxItem]:
        out = []
        for item in self._items():
            if (item.message.request_id == request_id and item.status in (QUEUED, FAILED)
                    and (not steps or item.message.step in steps)):
                item.status, item.error = SUPPRESSED, f"cancelled: {reason}"
                item.next_attempt_at = math.inf
                self.outbox.update(item)
                out.append(replace(item))
        return out

    def sent_since(self, channel: str, since: float) -> list[OutboxItem]:
        found = [replace(i) for i in self._items()
                 if i.recipient.channel == channel and i.status in (SENT, DELIVERED)
                 and (i.sent_at or 0) >= since]
        return sorted(found, key=lambda i: i.sent_at or 0, reverse=True)

    def counts(self) -> dict[str, int]:
        items = self._items()
        counts = {s: sum(1 for i in items if i.status == s)
                  for s in (QUEUED, SENDING, SENT, DELIVERED, FAILED, SUPPRESSED)}
        counts["depth"] = counts[QUEUED] + counts[SENDING] + sum(
            1 for i in items if i.status == FAILED and not math.isinf(i.next_attempt_at))
        return counts

    def recent(self, limit: int = 20, request_id: str | None = None) -> list[dict]:
        items = [i for i in self._items()
                 if not request_id or i.message.request_id == request_id]
        items.sort(key=lambda i: i.message.created_at, reverse=True)
        return [{**notification_payload(i), "provider_id": i.provider_id}
                for i in items[:limit]]


class _Ops:
    """Dispatch to the outbox's own implementation, else the memory one."""

    def __init__(self, outbox: Any) -> None:
        self.outbox = outbox
        self._fallback = _MemoryOps(outbox)

    def __getattr__(self, name: str) -> Callable[..., Any]:
        native = getattr(self.outbox, name, None)
        return native if native is not None else getattr(self._fallback, name)


class Router(ChannelRouter):
    """``base.ChannelRouter`` with claims, receipts, cooldowns, give-up age,
    ``send_now``, ``cancel`` and per-channel status (see the module doc)."""

    def __init__(self, channels: dict[str, Channel] | None = None, outbox: Any = None, *,
                 max_attempts: int = 3, backoff: tuple = (30.0, 90.0),
                 failed_retry_seconds: float = 120.0, max_age_seconds: float = 86400.0,
                 cooldowns: dict[str, float] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        super().__init__(channels, outbox, max_attempts=max_attempts, backoff=backoff,
                         failed_retry_seconds=failed_retry_seconds, clock=clock)
        self.max_age_seconds = max_age_seconds
        self.cooldowns = dict(cooldowns or {})
        self._ops = _Ops(self.outbox)
        self._last_enqueued: dict[tuple[str, str, str], float] = {}
        self._side_lock = threading.Lock()
        self._stats: dict[str, dict[str, Any]] = {}

    def with_outbox(self, outbox: Any) -> "Router":
        """The same router (channels, listeners, cooldowns, stats, lock) over
        another outbox: the Portal binds one to a request's session."""
        view = copy.copy(self)
        view.outbox = outbox
        view._ops = _Ops(outbox)
        return view

    # ── enqueue ──────────────────────────────────────────────────────────────
    def _cooldown_ok(self, recipient: Recipient, message: Message) -> bool:
        seconds = self.cooldowns.get(message.template)
        if not seconds:
            return True
        key = (message.template, recipient.ref, recipient.channel)
        now = self._clock()
        with self._side_lock:
            last = self._last_enqueued.get(key)
            if last is not None and now - last < seconds:
                return False
            self._last_enqueued[key] = now
            return True

    def _new_item(self, recipient: Recipient, message: Message) -> OutboxItem | None:
        if recipient.channel not in self.channels:
            log.warning("no %s channel here: %s to %s not queued", recipient.channel,
                        message.template, mask_recipient(recipient.address))
            return None
        if not self._cooldown_ok(recipient, message):
            log.info("%s to %s on %s held back by its %.0f s cooldown", message.template,
                     recipient.ref, recipient.channel, self.cooldowns[message.template])
            return None
        item = OutboxItem(id=str(uuid.uuid4()), recipient=recipient, message=message,
                          next_attempt_at=self._clock())
        if not self.outbox.add(item):
            return None                                   # same request|ref|channel|step
        return item

    def enqueue(self, recipient: Recipient, message: Message) -> str | None:
        item = self._new_item(recipient, message)
        return item.id if item is not None else None

    def send_now(self, recipient: Recipient, message: Message) -> OutboxItem | None:
        """Queue and deliver on this thread; returns the item after its
        attempt (None: duplicate key, cooldown or unknown channel)."""
        with self._lock:
            item = self._new_item(recipient, message)
            if item is None:
                return None
            self._deliver(item, self._clock())
            return replace(item)

    # ── delivery ─────────────────────────────────────────────────────────────
    def retry_delay(self, attempts: int) -> float:
        """Seconds before the next try after ``attempts`` failed ones."""
        if attempts >= self.max_attempts or not self.backoff:
            return float(self.failed_retry_seconds)
        steps = [float(s) for s in self.backoff]
        index = attempts - 1
        if index < len(steps):
            return steps[index]
        ratio = steps[-1] / steps[-2] if len(steps) >= 2 and steps[-2] > 0 else 2.0
        return min(steps[-1] * ratio ** (index - len(steps) + 1),
                   max(float(self.failed_retry_seconds), steps[-1]))

    def pump(self, now: float | None = None, limit: int = PUMP_LIMIT) -> int:
        now = self._clock() if now is None else now
        attempts = 0
        with self._lock:
            for item in self.outbox.due(now, limit):
                if self._deliver(item, now):
                    attempts += 1
        return attempts

    def _deliver(self, item: OutboxItem, now: float) -> bool:
        """One attempt (True), or a give-up without one (False)."""
        channel = self.channels.get(item.recipient.channel)
        if channel is None:
            # Queued before a restart without that channel: wait for it to come
            # back (until max age); listeners hear the first FAILED only.
            first = item.status != FAILED
            item.status = FAILED
            item.error = f"channel {item.recipient.channel} is not available here"
            item.next_attempt_at = now + self.failed_retry_seconds
            if self.max_age_seconds and now - item.message.created_at > self.max_age_seconds:
                item.next_attempt_at = math.inf
                first = True
            if first:
                self._finish(item)
            else:
                self.outbox.update(item)
            return False
        if self.max_age_seconds and now - item.message.created_at > self.max_age_seconds:
            item.status, item.next_attempt_at = FAILED, math.inf
            item.error = (f"gave up after {item.attempts} attempts: older than "
                          f"{self.max_age_seconds:.0f} s ({item.error or 'not sent'})")
            self._finish(item)
            return False
        item.status = SENDING
        item.attempts += 1
        self.outbox.update(item)
        try:
            result = channel.deliver(item.recipient, item.message)
        except Exception as exc:                          # Channel.deliver should not raise
            result = SendResult(False, error=f"delivery error: {exc}")
        self._apply(item, result, now, channel)
        self._finish(item)
        return True

    def _apply(self, item: OutboxItem, result: SendResult, now: float,
               channel: Channel) -> None:
        if result.ok:
            on_send = getattr(channel.transport, "delivers_on_send", False)
            item.status = DELIVERED if on_send else SENT
            item.sent_at, item.provider_id, item.error = now, result.provider_id, None
            item.next_attempt_at = math.inf
        elif result.suppressed:
            item.status, item.error = SUPPRESSED, scrub(result.error)
            item.next_attempt_at = math.inf
        else:
            item.status, item.error = FAILED, scrub(result.error)
            delay = self.retry_delay(item.attempts)
            wait = getattr(result, "retry_after", None)
            if wait is not None and not math.isinf(delay):
                delay = max(delay, float(wait))      # the provider's floor
            item.next_attempt_at = now + delay if result.retryable else math.inf

    def _finish(self, item: OutboxItem) -> None:
        self.outbox.update(item)
        self._record(item)
        self._notify(item)

    def _record(self, item: OutboxItem) -> None:
        with self._side_lock:
            stats = self._stats.setdefault(item.recipient.channel, {
                "sent": 0, "delivered": 0, "failed": 0, "suppressed": 0,
                "last_status": None, "last_error": None, "last_at": None})
            key = {SENT: "sent", DELIVERED: "delivered", FAILED: "failed",
                   SUPPRESSED: "suppressed"}.get(item.status)
            if key:
                stats[key] += 1
            stats["last_status"] = item.status
            stats["last_at"] = _iso(self._clock())
            if item.status in (FAILED, SUPPRESSED):
                stats["last_error"] = scrub(item.error)

    # ── after the fact ───────────────────────────────────────────────────────
    def confirm_delivery(self, channel: str, matches: Callable[[OutboxItem], bool], *,
                         delivered: bool = True, error: str | None = None,
                         within_seconds: float = 172800.0) -> OutboxItem | None:
        """A delivery receipt: the newest SENT item of ``channel`` that
        ``matches`` becomes DELIVERED, or FAILED (final) when the network gave
        up. Returns it, or None when nothing matched."""
        now = self._clock()
        with self._lock:
            for item in self._ops.sent_since(channel, now - within_seconds):
                if item.status != SENT or not matches(item):
                    continue
                if delivered:
                    item.status = DELIVERED
                else:
                    item.status, item.next_attempt_at = FAILED, math.inf
                    item.error = scrub(error) or "delivery report: not delivered"
                self._finish(item)
                return replace(item)
        return None

    def cancel(self, request_id: str, reason: str = "request closed",
               steps: tuple[str, ...] | None = None) -> int:
        """Queued and retrying messages of ``request_id`` (optionally only
        ``steps``) become SUPPRESSED with "cancelled: <reason>"."""
        with self._lock:
            items = self._ops.cancel(request_id, reason, tuple(steps) if steps else None)
        for item in items:
            self._record(item)
            self._notify(item)
        return len(items)

    def recover_interrupted(self) -> int:
        """Rows left SENDING by a crash are retried now."""
        with self._lock:
            items = self._ops.recover_interrupted(self._clock())
        for item in items:
            log.warning("%s to %s was interrupted mid-send: retrying", item.message.template,
                        mask_recipient(item.recipient.address))
        return len(items)

    # ── status ───────────────────────────────────────────────────────────────
    def ready(self, channel: str) -> bool:
        """Registered, both send switches on, and the transport can send now
        (configured; the modem present and registered)."""
        if not super().ready(channel):
            return False
        return transport_state(self.channels[channel].transport)[0]

    def channel_status(self) -> dict[str, dict[str, Any]]:
        out = {}
        for name in sorted(self.channels):
            channel = self.channels[name]
            available, detail = transport_state(channel.transport)
            live = channel._policy_factory().channel_live(name)
            with self._side_lock:
                stats = dict(self._stats.get(name, {}))
            out[name] = {"live": live, "available": available, "ready": live and available,
                         "detail": detail, **stats}
        return out

    def counts(self) -> dict[str, int]:
        return self._ops.counts()

    def recent(self, limit: int = 20, request_id: str | None = None) -> list[dict]:
        return self._ops.recent(limit, request_id)

    def status(self) -> dict[str, Any]:
        return {"channels": self.channel_status(), **guard.status()}


# ── the device service ────────────────────────────────────────────────────────
def legacy_cooldowns(settings: Any) -> dict[str, float]:
    alert = settings.alert
    return {LEGACY_SMS: float(alert.sms_cooldown_seconds),
            LEGACY_CALL: float(alert.call_cooldown_seconds)}


def standalone(settings: Any) -> bool:
    """No Portal link configured: the device sends Telegram/e-mail itself."""
    return not (settings.link.enabled and settings.link.portal_url)


def _local_midnight(now: float | None = None) -> float:
    t = time.localtime(now if now is not None else time.time())
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def _audit(event: str, **detail: Any) -> None:
    """A row in the live pipeline's event log, if one runs (never builds one)."""
    engine = sys.modules.get("sentrix.pipeline.engine")
    pipeline = getattr(engine, "_pipeline", None) if engine else None
    if pipeline is not None:
        try:
            pipeline.events.audit(event, **detail)
        except Exception:
            pass


class DeviceChannels:
    """The ``channels`` service (``ChannelsService``, docs/7 §8.2)."""

    name = "channels"

    def __init__(self, router: Router, *, settings: Any, state: Any = None,
                 modem: Any = None, sms_counter: Any = None, poll_seconds: float = 1.0,
                 status_seconds: float = 5.0) -> None:
        self.router = router
        self.settings = settings
        self.state = state
        self.modem = modem
        self.sms_counter = sms_counter
        self.poll_seconds = poll_seconds
        self.status_seconds = status_seconds
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._counts: dict[str, int] = {}
        self._last_error: str | None = None
        self._dirty = True
        self._running = False
        router.add_listener(self._on_status)

    # ── ChannelsService ──────────────────────────────────────────────────────
    def enqueue(self, recipient: Recipient, message: Message) -> str | None:
        item_id = self.router.enqueue(recipient, message)
        if item_id is not None:
            self._wake.set()
        return item_id

    def ready(self, channel: str) -> bool:
        return self.router.ready(channel)

    def add_listener(self, callback: Callable[[OutboxItem], None]) -> None:
        self.router.add_listener(callback)

    def remove_listener(self, callback: Callable[[OutboxItem], None]) -> None:
        self.router.remove_listener(callback)

    def status(self) -> dict[str, Any]:
        """The ``state.channels`` block (docs/7 §9.4)."""
        channels = self.router.channel_status()
        policy = guard.SendPolicy.from_env()
        outbound = any(c["ready"] for c in channels.values())
        modem = None
        if self.modem is not None:
            modem = self.modem.status()
            if self.sms_counter is not None:
                modem["sms_today"] = self.sms_counter.used
                modem["sms_daily_cap"] = self.sms_counter.cap
        return {"enabled": self._running, "outbound_ready": outbound,
                "live": policy.live_alerts, "test_mode": policy.test_mode,
                "test_recipients": len(policy.allowlist), "channels": channels,
                "outbox_depth": self._counts.get("depth", 0), "outbox": dict(self._counts),
                "last_error": self._last_error, "modem": modem,
                "legacy_twilio": bool(channels.get("twilio_sms", {}).get("ready")),
                "warn": bool(self.settings.sos.enabled and not outbound)}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        recovered = self.router.recover_interrupted()
        if recovered:
            log.warning("%d message(s) interrupted by the last shutdown are retried", recovered)
        if self.modem is not None:
            self._seed_sms_counter()
            self.modem.start()
        self._stop.clear()
        self._running = True
        self._refresh_counts()
        self._publish()
        self._thread = threading.Thread(target=self._run, daemon=True, name="sentrix-channels")
        self._thread.start()
        log.info("channels: %s (%s)", ", ".join(sorted(self.router.channels)) or "none",
                 "test mode" if guard.test_mode() else "LIVE MODE")

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=10.0)
            self._thread = None
        if self.modem is not None:
            self.modem.stop()
        self._running = False
        self._publish()

    def health(self) -> dict[str, Any]:
        block = self.status()
        return {"outbound_ready": block["outbound_ready"],
                "ready": {name: c["ready"] for name, c in block["channels"].items()},
                "outbox_depth": block["outbox_depth"], "last_error": block["last_error"],
                "modem": ({k: block["modem"].get(k) for k in
                           ("present", "registered", "signal_dbm", "sms_today")}
                          if block["modem"] else None),
                "worker_alive": bool(self._thread and self._thread.is_alive())}

    # ── extras for the SOS engine, the notifier and the routes ───────────────
    def send_now(self, recipient: Recipient, message: Message) -> OutboxItem | None:
        item = self.router.send_now(recipient, message)
        self._dirty = True
        return item

    def cancel(self, request_id: str, reason: str = "request closed",
               steps: tuple[str, ...] | None = None) -> int:
        return self.router.cancel(request_id, reason, steps)

    def recent(self, limit: int = 20, request_id: str | None = None) -> list[dict]:
        return self.router.recent(limit, request_id)

    def channels_for(self, kind: str) -> list[str]:
        """Registered phone channels of ``kind`` ("sms" | "call"): ready ones
        first, each group in preference order (modem, then Twilio)."""
        names = [n for n in (SMS_CHANNELS if kind == "sms" else CALL_CHANNELS)
                 if n in self.router.channels]
        ready = [n for n in names if self.router.ready(n)]
        return ready + [n for n in names if n not in ready]

    @staticmethod
    def owner_target() -> tuple[str, str] | None:
        """(name, phone) of the owner: the profile's owner phone, else
        ``ALERT_PHONE_NUMBER`` (the pre-portal target)."""
        from sentrix.link import profile as link_profile
        try:
            current = link_profile.effective()
        except Exception as exc:
            log.warning("profile unreadable (%s): using the standalone profile", exc)
            current = link_profile.standalone_profile()
        owner = current.get("owner") or {}
        phone = (owner.get("phone") or os.getenv("ALERT_PHONE_NUMBER", "")).strip()
        if not phone:
            return None
        return owner.get("name") or "Owner", phone

    def send_test(self, channel: str | None = None, *, by: str = "lc:device_admin") -> dict:
        """A guarded test SMS to the owner number (``POST /api/channels/test``)."""
        choices = self.channels_for("sms")
        if channel is None:
            if not choices:
                raise LookupError("no SMS channel is registered (modem or Twilio)")
            channel = choices[0]
        elif channel not in SMS_CHANNELS:
            raise ValueError(f"the test message goes by SMS: {', '.join(SMS_CHANNELS)}")
        elif channel not in self.router.channels:
            raise LookupError(f"{channel} is not registered on this device")
        target = self.owner_target()
        if target is None:
            raise LookupError("no owner number: set ALERT_PHONE_NUMBER or finish onboarding")
        name, phone = target
        message = Message(template=CHANNEL_TEST, step=f"test:{int(time.time() * 1000)}",
                          text="SENTRIX channel test: if you read this, SMS alerts from "
                               "this device reach you. No action needed.")
        item = self.send_now(Recipient("owner", name, channel, phone, "owner"), message)
        if item is None:
            raise LookupError("the test message was not queued")
        _audit("channel_test", channel=channel, status=item.status, by=by)
        return {"id": item.id, "channel": channel, "status": item.status,
                "error": scrub(item.error), "recipient": mask_recipient(phone),
                "attempts": item.attempts, "text": item.message.text}

    # ── worker ───────────────────────────────────────────────────────────────
    def _run(self) -> None:
        next_status = 0.0
        while not self._stop.is_set():
            busy = False
            try:
                busy = self.router.pump() >= PUMP_LIMIT
                self._apply_reports()
            except Exception as exc:
                self._last_error = scrub(f"worker: {exc}")
                log.error("channels worker: %s", exc, exc_info=True)
            now = time.monotonic()
            if self._dirty or now >= next_status:
                self._dirty = False
                self._refresh_counts()
                self._publish()
                next_status = now + self.status_seconds
            if not busy:
                self._wake.wait(self.poll_seconds)
                self._wake.clear()

    def step(self) -> int:
        """One worker iteration on the caller's thread (tests, tools)."""
        attempts = self.router.pump()
        self._apply_reports()
        self._refresh_counts()
        self._publish()
        return attempts

    def _apply_reports(self) -> None:
        if self.modem is None:
            return
        from sentrix.alerting.channels.modem import references_of, report_outcome
        for reference, status in self.modem.take_reports():
            outcome = report_outcome(status)
            if outcome == "pending":
                continue

            def matches(item: OutboxItem, ref: int = reference) -> bool:
                return ref in references_of(item.provider_id)

            item = self.router.confirm_delivery(
                "modem_sms", matches, delivered=outcome == "delivered",
                error=f"delivery report: status {status:#04x}")
            if item is None:
                log.debug("modem: report for reference %d matches no SENT SMS", reference)

    def _seed_sms_counter(self) -> None:
        if self.sms_counter is None:
            return
        from sentrix.alerting.channels.modem import segments_of
        try:
            sent = self.router._ops.sent_since("modem_sms", _local_midnight())
            self.sms_counter.seed(sum(segments_of(i.provider_id) for i in sent))
        except Exception as exc:
            log.warning("modem: cannot count today's SMS (%s); the cap starts at 0", exc)

    def _refresh_counts(self) -> None:
        try:
            self._counts = self.router.counts()
        except Exception as exc:
            self._last_error = scrub(f"outbox: {exc}")

    def _publish(self) -> None:
        if self.state is None:
            return
        try:
            self.state.update(channels=self.status())
        except Exception as exc:
            log.debug("channels state not published: %s", exc)

    def _on_status(self, item: OutboxItem) -> None:
        """Every final status change (on the worker's thread): log, audit a
        suppressed message, keep the last error, republish the block."""
        self._dirty = True
        masked = mask_recipient(item.recipient.address)
        if item.status == SUPPRESSED:
            log.info("%s %s to %s suppressed: %s", item.recipient.channel,
                     item.message.template, masked, item.error)
            _audit("channel_suppressed", channel=item.recipient.channel, recipient=masked,
                   template=item.message.template, step=item.message.step,
                   request_id=item.message.request_id, reason=item.error)
        elif item.status == FAILED:
            self._last_error = f"{item.recipient.channel}: {item.error}"
            log.warning("%s %s to %s failed (attempt %d): %s", item.recipient.channel,
                        item.message.template, masked, item.attempts, item.error)
        else:
            log.info("%s %s to %s %s", item.recipient.channel, item.message.template,
                     masked, item.status)


# ── factory ───────────────────────────────────────────────────────────────────
def _twilio_configured() -> bool:
    from sentrix.alerting.channels.twilio import TwilioCredentials
    return TwilioCredentials.from_env().complete


def build_router(settings: Any, *, outbox: Any = None,
                 clock: Callable[[], float] = time.time) -> Router:
    cfg = settings.channels
    if outbox is None:
        from sentrix.storage.notifications import SqlOutbox
        outbox = SqlOutbox()
    return Router(outbox=outbox, max_attempts=cfg.max_attempts,
                  backoff=tuple(cfg.retry_backoff_seconds),
                  failed_retry_seconds=cfg.failed_retry_seconds,
                  max_age_seconds=settings.sos.stale_close_seconds,
                  cooldowns=legacy_cooldowns(settings), clock=clock)


def build_device_channels(settings: Any, *, state: Any = None, outbox: Any = None,
                          modem_worker: Any = None, transports: dict[str, Any] | None = None,
                          clock: Callable[[], float] = time.time) -> DeviceChannels:
    """The service with every channel this device has. ``transports``
    (tests) replaces the real transport of a channel by name."""
    cfg = settings.channels
    router = build_router(settings, outbox=outbox, clock=clock)
    overrides = dict(transports or {})
    modem = counter = None

    def register(name: str, factory: Callable[[], Any]) -> None:
        router.register(Channel(name, overrides.pop(name, None) or factory()))

    if cfg.modem_enabled:
        from sentrix.alerting.channels import modem as modem_module
        modem = modem_worker or modem_module.build_worker(cfg.modem_port, cfg.modem_baud)
        counter = modem_module.DailyCounter(settings.link.sms_daily_cap)
        register("modem_sms", lambda: modem_module.ModemSmsTransport(modem, counter=counter))
        register("modem_call", lambda: modem_module.ModemCallTransport(modem))
    if _twilio_configured() or {"twilio_sms", "twilio_call"} & set(overrides):
        from sentrix.alerting.channels import twilio
        register("twilio_sms", twilio.TwilioSmsTransport)
        register("twilio_call", twilio.TwilioCallTransport)
    if standalone(settings):
        from sentrix.alerting.channels import email, telegram
        if telegram.bot_token() or "telegram" in overrides:
            register("telegram", telegram.TelegramTransport)
        if email.provider_from_env(cfg.email_provider) is not None or "email" in overrides:
            register("email", lambda: email.EmailTransport(
                email.provider_from_env(cfg.email_provider)))
    for name, transport in overrides.items():           # any other fake a test supplies
        router.register(Channel(name, transport))
    return DeviceChannels(router, settings=settings, state=state, modem=modem,
                          sms_counter=counter)


def build_service(ctx: Any) -> DeviceChannels | None:
    """``services`` factory: the router and its worker, or None when
    ``SENTRIX_CHANNELS_ENABLED`` is off (the legacy notifier then reports its
    own readiness)."""
    if not ctx.settings.channels.enabled:
        return None
    return build_device_channels(ctx.settings, state=ctx.state)


def current_service() -> DeviceChannels | None:
    """The running ``channels`` service, or None."""
    from sentrix import services
    service = services.registry.get("channels")
    if service is None or "channels" not in services.registry.running():
        return None
    return service


# ── doctor ────────────────────────────────────────────────────────────────────
def preflight(settings: Any) -> list[dict[str, str]]:
    """Local checks only (environment and imports; no network, no devices)."""
    cfg = settings.channels
    policy = guard.SendPolicy.from_env()
    rows: list[dict[str, str]] = []

    def row(status: str, name: str, detail: str) -> None:
        rows.append({"status": status, "name": name, "detail": detail})

    if not cfg.enabled:
        row("ok", "channels", "router off (SENTRIX_CHANNELS_ENABLED=0): the legacy "
                              "notifier (guarded Twilio) is the only outbound path")
    else:
        row("ok", "channels", "router on")
    if policy.test_mode:
        row("ok", "test mode", f"on: only {len(policy.allowlist)} allowlisted "
                               "recipient(s) (SENTRIX_TEST_RECIPIENTS) can receive")
    else:
        row("warn", "test mode", "OFF: live alerts reach anyone the profile names")
    live = [ch for ch in guard.CHANNEL_SWITCHES if policy.channel_live(ch)]
    if not policy.live_alerts:
        row("ok", "send guard", "dry run (SENTRIX_LIVE_ALERTS=0): every message is "
                                "recorded as SUPPRESSED")
    else:
        row("warn" if live else "ok", "send guard",
            f"live channels: {', '.join(live) or 'none (every channel switch is off)'}")
        if policy.test_mode and live and not policy.allowlist:
            row("warn", "send guard", "test mode with live channels but an empty "
                                      "SENTRIX_TEST_RECIPIENTS: nothing can be delivered")
    if cfg.modem_enabled:
        try:
            import serial  # noqa: F401
            port = cfg.modem_port or "probe the serial ports"
            row("ok", "modem", f"enabled ({port}, {cfg.modem_baud} baud)")
        except ImportError:
            if cfg.modem_port.lower().startswith("mmcli"):
                row("ok", "modem", "enabled through ModemManager (mmcli)")
            else:
                row("fail", "modem", "pyserial is not installed (pip install pyserial)")
    elif cfg.enabled:
        row("ok", "modem", "off (SENTRIX_MODEM_ENABLED=0): no SMS while offline")
    if _twilio_configured():
        row("ok", "twilio", "credentials set; sends only with SENTRIX_TWILIO_ENABLED=1 "
                            "and SENTRIX_LIVE_ALERTS=1")
    if cfg.enabled and standalone(settings):
        from sentrix.alerting.channels import email, telegram
        if telegram.bot_token():
            row("ok", "telegram", "bot token set (standalone: the device sends)")
        provider = email.provider_from_env(cfg.email_provider)
        if provider is not None:
            ok, detail = provider.configured()
            row("ok" if ok else "warn", "email", detail)
        elif cfg.email_provider:
            row("warn", "email", f"unknown SENTRIX_EMAIL_PROVIDER {cfg.email_provider!r}")
    if not os.getenv("ALERT_PHONE_NUMBER", "").strip():
        row("warn", "owner number", "ALERT_PHONE_NUMBER not set: without a Portal "
                                    "profile there is no number for SMS and calls")
    return rows


__all__ = ["Router", "DeviceChannels", "build_service", "build_router",
           "build_device_channels", "current_service", "preflight",
           "notification_payload", "transport_state", "legacy_cooldowns", "standalone",
           "scrub", "SMS_CHANNELS", "CALL_CHANNELS", "LEGACY_SMS", "LEGACY_CALL",
           "LEGACY_DISPATCH", "CHANNEL_TEST"]
