"""The Portal's channel router: Telegram, e-mail, its outbox, worker and the
inbound bot (docs/7 §4.2, §7.11, §8). Owner: track T-CH.

* :func:`router` (``router(db)``) -> ``channels.router.Router`` over the
  Portal's ``notifications`` table, bound to the caller's session: a message
  queued with a state change commits (or rolls back) with it. ``meta``
  round-trips buttons, HTML, urgency and the recipient's name and role;
  addresses are sealed (``address_enc``).
* :func:`enqueue` (``enqueue(db, recipient, message)``) -> outbox id, or None
  (duplicate ``request|recipient|channel|step``, or no such channel).
* Channels: ``telegram`` and ``email`` always (the send guard decides, so a
  dry run still records what would have gone out), ``twilio_sms`` as a
  backup when ``TWILIO_*`` are set. The guard is
  ``SendPolicy.from_env(test_mode_env="PORTAL_TEST_MODE")``.
* :func:`start` (``start(app) -> stop``): the delivery worker (an asyncio task
  that pumps on a thread every second, or at once after an enqueue), the
  recovery of rows left SENDING, and in ``SENTRIX_TELEGRAM_MODE=polling`` the
  ``getUpdates`` long poll; in ``webhook`` mode it registers
  ``<PORTAL_BASE_URL>/hooks/telegram`` with ``TELEGRAM_WEBHOOK_SECRET``.
* :func:`handle_update` — one Telegram update, from polling or the webhook:
  ``/start <code>`` links a private chat to the code's user or contact
  (T-ACCT's ``telegram_link`` token, single use); a button press is accepted
  only from a private chat whose sender is the subject's linked chat, the
  nonce's token not used, revoked or expired, and goes to
  ``services.sos.respond`` (``channel="telegram"``); the bot leaves any group
  it is added to. Every link and callback is audited.
* :func:`add_listener` / :func:`remove_listener` — ``fn(OutboxItem)`` after
  every final status change, on the worker's thread (T-LINK derives
  ``delivered`` / ``undelivered``). SUPPRESSED (dry run) never counts as
  delivered.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator

from sqlalchemy import func
from sqlalchemy.dialects import postgresql as postgresql_dialect
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.exc import IntegrityError

from sentrix.alerting import guard
from sentrix.alerting.channels import telegram
from sentrix.alerting.channels.base import (DELIVERED, FAILED, QUEUED, SENDING, SENT,
                                            SUPPRESSED, Channel, Message, OutboxItem,
                                            Recipient, message_meta, parse_callback_data,
                                            rebuild)
from sentrix.alerting.channels.router import Router, notification_payload
from sentrix.utils.redact import mask_recipient
from sentrix_portal import security
from sentrix_portal.models import (ActionToken, AssistanceRequest, Device, EmergencyContact,
                                   Notification, TelegramLink, User)

log = logging.getLogger("sentrix_portal.notify")

POLL_SECONDS = 1.0
#: Telegram callbacks name a token by its nonce; these purposes may carry one.
CALLBACK_PURPOSES = ("action_link", "contact_page")
#: Answers that use the token up (a second tap says "already answered").
CONSUMING_ACTIONS = ("approve", "acknowledge")
_NEVER = datetime(9999, 1, 1, tzinfo=timezone.utc)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_SEEN_UPDATES = 2048


def _dt(ts: float | None) -> datetime | None:
    if ts is None:
        return None
    if math.isinf(ts):
        return _NEVER
    return _EPOCH + timedelta(seconds=ts)          # Windows: no negative timestamps


def _ts(value: datetime | None) -> float:
    if value is None:
        return 0.0
    value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return math.inf if value.year >= 9999 else value.timestamp()


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def portal_policy() -> guard.SendPolicy:
    return guard.SendPolicy.from_env(test_mode_env="PORTAL_TEST_MODE")


# ── outbox ────────────────────────────────────────────────────────────────────
class PortalOutbox:
    """``base.Outbox`` over ``notifications``. With ``session`` it works in
    the caller's transaction (a duplicate key is skipped by the insert itself,
    so it never rolls the caller back); with ``database`` every call is its
    own short transaction (the worker: SQLite must not stay locked while a
    provider answers)."""

    def __init__(self, *, session: Any = None, database: Any = None) -> None:
        if (session is None) == (database is None):
            raise ValueError("PortalOutbox needs a session or a database")
        self._db = session
        self._database = database

    @contextmanager
    def _session(self) -> Iterator[Any]:
        if self._db is not None:
            yield self._db
            self._db.flush()
        else:
            with self._database.session() as db:
                yield db

    @staticmethod
    def _household(db: Any, recipient: Recipient, request_id: str | None) -> str | None:
        if request_id:
            request = db.get(AssistanceRequest, request_id)
            if request is not None and request.household_id:
                return request.household_id
        kind, _, ident = recipient.ref.partition(":")
        if kind == "contact" and ident:
            contact = db.get(EmergencyContact, ident)
            return contact.household_id if contact is not None else None
        if kind in ("user", "member") and ident:
            user = db.get(User, ident)
            return user.household_id if user is not None else None
        return None

    def add(self, item: OutboxItem) -> bool:
        """Insert unless the dedupe key exists: ``INSERT … ON CONFLICT DO
        NOTHING`` (SQLite, Postgres), atomic under concurrent enqueues and
        harmless to the caller's transaction."""
        r, m = item.recipient, item.message
        with self._session() as db:
            values = dict(
                id=item.id, request_id=m.request_id,
                household_id=self._household(db, r, m.request_id),
                recipient_ref=r.ref, recipient_masked=mask_recipient(r.address),
                address_enc=security.seal(r.address), channel=r.channel,
                template=m.template, step=m.step, dedupe_key=item.dedupe_key,
                status=item.status, attempts=item.attempts,
                next_attempt_at=_dt(item.next_attempt_at), subject=m.subject, body=m.text,
                meta=message_meta(r, m), test_mode=portal_policy().test_mode,
                created_at=_dt(m.created_at))
            dialect = db.get_bind().dialect.name
            if dialect in ("sqlite", "postgresql"):
                module = (sqlite_dialect if dialect == "sqlite" else postgresql_dialect)
                statement = (module.insert(Notification).values(**values)
                             .on_conflict_do_nothing(index_elements=["dedupe_key"]))
                return db.execute(statement).rowcount == 1
            if db.query(Notification.id).filter_by(dedupe_key=item.dedupe_key).first():
                return False
            try:
                with db.begin_nested():
                    db.add(Notification(**values))
            except IntegrityError:
                return False
        return True

    @staticmethod
    def _item(row: Notification) -> OutboxItem:
        try:
            address = security.unseal(row.address_enc) or ""
        except Exception:
            address = ""                  # a changed data key: the guard refuses ""
        recipient, message = rebuild(row.recipient_ref, row.channel, address, row.meta,
                                     template=row.template, step=row.step, text=row.body,
                                     request_id=row.request_id, subject=row.subject,
                                     created_at=_ts(row.created_at))
        return OutboxItem(id=row.id, recipient=recipient, message=message,
                          status=row.status, attempts=row.attempts or 0,
                          next_attempt_at=_ts(row.next_attempt_at),
                          provider_id=row.provider_id, error=row.error,
                          sent_at=_ts(row.sent_at) if row.sent_at else None)

    def due(self, now: float, limit: int = 50) -> list[OutboxItem]:
        with self._session() as db:
            rows = (db.query(Notification)
                    .filter(Notification.status.in_((QUEUED, FAILED)),
                            Notification.next_attempt_at <= _dt(now))
                    .order_by(Notification.next_attempt_at, Notification.created_at)
                    .limit(limit).all())
            return [self._item(row) for row in rows]

    def update(self, item: OutboxItem) -> None:
        with self._session() as db:
            row = db.get(Notification, item.id)
            if row is None:
                return
            row.status, row.attempts = item.status, item.attempts
            row.next_attempt_at = _dt(item.next_attempt_at)
            row.provider_id, row.error = item.provider_id, item.error
            row.sent_at = _dt(item.sent_at)

    def get(self, item_id: str) -> OutboxItem | None:
        with self._session() as db:
            row = db.get(Notification, item_id)
            return self._item(row) if row is not None else None

    def recover_interrupted(self, now: float) -> list[OutboxItem]:
        with self._session() as db:
            rows = db.query(Notification).filter_by(status=SENDING).all()
            for row in rows:
                row.status, row.next_attempt_at = FAILED, _dt(now)
                row.error = "interrupted while sending (restart); retrying"
            return [self._item(row) for row in rows]

    def cancel(self, request_id: str, reason: str,
               steps: tuple[str, ...] | None = None) -> list[OutboxItem]:
        with self._session() as db:
            query = db.query(Notification).filter(Notification.request_id == request_id,
                                                  Notification.status.in_((QUEUED, FAILED)))
            if steps:
                query = query.filter(Notification.step.in_(tuple(steps)))
            rows = query.all()
            for row in rows:
                row.status, row.error = SUPPRESSED, f"cancelled: {reason}"
                row.next_attempt_at = _NEVER
            return [self._item(row) for row in rows]

    def sent_since(self, channel: str, since: float) -> list[OutboxItem]:
        with self._session() as db:
            rows = (db.query(Notification)
                    .filter(Notification.channel == channel,
                            Notification.status.in_((SENT, DELIVERED)),
                            Notification.sent_at >= _dt(since))
                    .order_by(Notification.sent_at.desc()).all())
            return [self._item(row) for row in rows]

    def counts(self) -> dict[str, int]:
        with self._session() as db:
            per_status = dict(db.query(Notification.status, func.count(Notification.id))
                              .group_by(Notification.status).all())
            retrying = (db.query(func.count(Notification.id))
                        .filter(Notification.status == FAILED,
                                Notification.next_attempt_at < _NEVER).scalar())
        counts = {s: int(per_status.get(s, 0))
                  for s in (QUEUED, SENDING, SENT, DELIVERED, FAILED, SUPPRESSED)}
        counts["depth"] = counts[QUEUED] + counts[SENDING] + int(retrying or 0)
        return counts

    def recent(self, limit: int = 20, request_id: str | None = None) -> list[dict]:
        with self._session() as db:
            query = db.query(Notification)
            if request_id:
                query = query.filter_by(request_id=request_id)
            rows = (query.order_by(Notification.created_at.desc())
                    .limit(max(1, min(int(limit), 200))).all())
            return [{**notification_payload(self._item(row)),
                     "household_id": row.household_id} for row in rows]


# ── the router and its listeners ──────────────────────────────────────────────
_listeners: list[Callable[[OutboxItem], None]] = []
_state_lock = threading.Lock()
_router: Router | None = None
_worker: "NotifyWorker | None" = None


def _fanout(item: OutboxItem) -> None:
    for callback in list(_listeners):
        try:
            callback(item)
        except Exception as exc:               # a listener never stops delivery
            log.error("notify listener failed: %s", exc)


def add_listener(callback: Callable[[OutboxItem], None]) -> None:
    with _state_lock:
        if callback not in _listeners:
            _listeners.append(callback)


def remove_listener(callback: Callable[[OutboxItem], None]) -> None:
    with _state_lock:
        if callback in _listeners:
            _listeners.remove(callback)


def build_channels(*, transports: dict[str, Any] | None = None,
                   policy_factory: Callable[[], guard.SendPolicy] = portal_policy
                   ) -> dict[str, Channel]:
    """Telegram and e-mail, plus Twilio SMS when configured. ``transports``
    (tests) replaces a transport by name."""
    from sentrix.alerting.channels import email
    from sentrix.alerting.channels.twilio import TwilioCredentials, TwilioSmsTransport
    chosen = dict(transports or {})
    factories: dict[str, Callable[[], Any]] = {
        "telegram": telegram.TelegramTransport,
        "email": lambda: email.EmailTransport(),
    }
    if TwilioCredentials.from_env().complete:
        factories["twilio_sms"] = TwilioSmsTransport
    channels = {}
    for name, factory in factories.items():
        channels[name] = Channel(name, chosen.pop(name, None) or factory(),
                                 policy_factory=policy_factory)
    for name, transport in chosen.items():
        channels[name] = Channel(name, transport, policy_factory=policy_factory)
    return channels


def configure(*, transports: dict[str, Any] | None = None,
              policy_factory: Callable[[], guard.SendPolicy] = portal_policy,
              clock: Callable[[], float] = time.time) -> Router:
    """(Re)build the process-wide router (tests pass fake transports)."""
    global _router
    from sentrix.config import settings
    cfg = settings.channels
    router_ = Router(build_channels(transports=transports, policy_factory=policy_factory),
                     _NullOutbox(), max_attempts=cfg.max_attempts,
                     backoff=tuple(cfg.retry_backoff_seconds),
                     failed_retry_seconds=cfg.failed_retry_seconds,
                     max_age_seconds=settings.sos.stale_close_seconds, clock=clock)
    router_.add_listener(_fanout)
    with _state_lock:
        _router = router_
    return router_


class _NullOutbox:
    """The shared router's own outbox is never used: every call binds one."""

    def add(self, item: OutboxItem) -> bool:
        raise RuntimeError("bind an outbox: notify.router(db)")

    def due(self, now: float, limit: int = 50) -> list[OutboxItem]:
        return []

    def update(self, item: OutboxItem) -> None:
        raise RuntimeError("bind an outbox: notify.router(db)")


def _shared() -> Router:
    with _state_lock:
        current = _router
    return current if current is not None else configure()


def router(db) -> Router:
    """The Portal router over ``notifications`` in ``db``'s transaction."""
    return _shared().with_outbox(PortalOutbox(session=db))


def worker_router(database) -> Router:
    """The router over its own short transactions (the worker, tools)."""
    return _shared().with_outbox(PortalOutbox(database=database))


def enqueue(db, recipient: Recipient, message: Message) -> str | None:
    """Queue in the caller's transaction; the worker sends it after the commit.
    None for a duplicate key, an unknown channel or a missing argument."""
    if db is None or not isinstance(recipient, Recipient) or not isinstance(message, Message):
        log.warning("notify.enqueue needs a session, a Recipient and a Message")
        return None
    item_id = router(db).enqueue(recipient, message)
    if item_id is not None and _worker is not None:
        _worker.wake()
    return item_id


def cancel(db, request_id: str, reason: str = "request closed",
           steps: tuple[str, ...] | None = None) -> int:
    """Withdraw a request's queued and retrying messages (SUPPRESSED,
    "cancelled: <reason>") in the caller's transaction. Does not wait for the
    worker: a message already being sent may still leave."""
    items = PortalOutbox(session=db).cancel(request_id, reason,
                                            tuple(steps) if steps else None)
    for item in items:
        _fanout(item)
    return len(items)


def ready(channel: str) -> bool:
    return _shared().ready(channel)


def status(database=None) -> dict[str, Any]:
    """Channel readiness and, with a database, the outbox counts."""
    shared = _shared()
    block: dict[str, Any] = {"channels": shared.channel_status(),
                             **guard.status(portal_policy())}
    if database is not None:
        block["outbox"] = worker_router(database).counts()
    block["worker"] = _worker.health() if _worker is not None else {"running": False}
    return block


def reset() -> None:
    """Forget the router and listeners (tests)."""
    global _router
    with _state_lock:
        _router = None
        _listeners.clear()


# ── Telegram inbound ──────────────────────────────────────────────────────────
def _api(app) -> telegram.AsyncTelegramApi:
    api = getattr(app.state, "telegram_api", None)
    if api is None:
        api = telegram.AsyncTelegramApi()
        app.state.telegram_api = api
    return api


def _first_time(app, update_id: int | None) -> bool:
    """Telegram re-delivers an update it thinks we missed; act once."""
    if update_id is None:
        return True
    seen = getattr(app.state, "telegram_seen", None)
    if seen is None:
        seen = app.state.telegram_seen = (deque(maxlen=_SEEN_UPDATES), set())
    order, members = seen
    if update_id in members:
        return False
    if len(order) == order.maxlen:
        members.discard(order[0])
    order.append(update_id)
    members.add(update_id)
    return True


async def _answer(api: telegram.AsyncTelegramApi, callback_id: str | None, text: str) -> None:
    """The toast for the person who pressed (a reply to their own tap, not a
    message to anyone: it does not pass the send guard)."""
    if not callback_id:
        return
    try:
        await api.answer_callback(callback_id, text)
    except telegram.TelegramError as exc:
        log.info("telegram answerCallbackQuery failed: %s", exc)


def _linked_chats(db, subject_type: str | None, subject_id: str | None) -> set[str]:
    if not subject_type or not subject_id:
        return set()
    chats = {link.chat_id for link in db.query(TelegramLink).filter_by(
        subject_type=subject_type, subject_id=subject_id, status="active")
        if link.chat_id}
    if subject_type == "contact":
        contact = db.get(EmergencyContact, subject_id)
        if contact is not None and contact.telegram_chat_id:
            chats.add(contact.telegram_chat_id)
    return chats


def _actor_for(db, token: ActionToken) -> dict[str, Any] | None:
    from sentrix.actor import Actor
    if token.subject_type == "contact":
        contact = db.get(EmergencyContact, token.subject_id)
        if contact is None:
            return None
        return Actor("contact", id=contact.id, name=contact.name).to_dict()
    user = db.get(User, token.subject_id)
    if user is None or user.status != "active" or user.role not in ("owner", "member",
                                                                    "operator"):
        return None
    return Actor(user.role, id=user.id, name=user.display_name or user.username).to_dict()


_ANSWERS = {"approve": "Help requested: the SENTRIX desk and your contacts are being "
                       "alerted.",
            "checking": "Noted: you are checking. Reminders pause for 5 minutes.",
            "acknowledge": "Thank you: you are marked as handling this."}
_REFUSALS = {"not_found": "This request no longer exists.",
             "conflict": "This request was already answered.",
             "forbidden_role": "You cannot answer this request.",
             "device_offline": "The home device is offline; try the Portal page.",
             "unsupported": "Answering from Telegram is not available yet; use the Portal."}


async def _on_callback(app, inbound: telegram.Inbound, api) -> dict[str, Any]:
    if not inbound.private:
        await _answer(api, inbound.callback_id, "Use your private chat with the SENTRIX bot.")
        return {"handled": "refused", "reason": "not a private chat"}
    parsed = parse_callback_data(inbound.data)
    if parsed is None:
        await _answer(api, inbound.callback_id, "This button is not valid.")
        return {"handled": "refused", "reason": "malformed callback data"}
    nonce, action = parsed
    from sentrix_portal.services import sos
    now = datetime.now(timezone.utc)
    with app.state.db.session() as db:
        token = (db.query(ActionToken).filter(ActionToken.nonce == nonce,
                                              ActionToken.purpose.in_(CALLBACK_PURPOSES))
                 .first())
        if token is None:
            await _answer(api, inbound.callback_id, "This button is not valid any more.")
            return {"handled": "refused", "reason": "unknown nonce"}

        def refuse(reason: str, text: str) -> dict[str, Any]:
            security.audit(db, "telegram.callback", actor_type=token.subject_type or "system",
                           actor_id=token.subject_id, household_id=token.household_id,
                           target_type="assistance_request", target_id=token.request_id,
                           detail={"action": action, "ok": False, "reason": reason,
                                   "chat": mask_recipient(f"tg:{inbound.from_id}")})
            return {"handled": "refused", "reason": reason, "text": text}

        outcome: dict[str, Any]
        if token.revoked_at is not None or (_aware(token.expires_at) or now) <= now:
            outcome = refuse("expired or revoked", "This request has closed.")
        elif str(inbound.from_id) not in _linked_chats(db, token.subject_type,
                                                       token.subject_id):
            outcome = refuse("sender is not the linked chat",
                             "This button is not for this Telegram account.")
        elif token.used_at is not None and action in CONSUMING_ACTIONS:
            outcome = refuse("already used", "Already answered.")
        else:
            actor = _actor_for(db, token)
            if actor is None:
                outcome = refuse("no active subject", "This account cannot answer.")
            else:
                token.opened_count = (token.opened_count or 0) + 1
                result = await sos.respond(db, token.request_id, action, actor=actor,
                                           channel="telegram")
                ok = bool(result.get("ok"))
                if ok and action in CONSUMING_ACTIONS:
                    token.used_at = now
                security.audit(db, "telegram.callback", actor_type=actor["type"],
                               actor_id=actor["id"], household_id=token.household_id,
                               target_type="assistance_request", target_id=token.request_id,
                               detail={"action": action, "ok": ok,
                                       "code": result.get("code"),
                                       "chat": mask_recipient(f"tg:{inbound.from_id}")})
                text = (_ANSWERS[action] if ok else
                        _REFUSALS.get(result.get("code"), result.get("message")
                                      or "That did not work; use the Portal."))
                outcome = {"handled": "answered" if ok else "failed", "action": action,
                           "code": result.get("code"), "text": text}
    await _answer(api, inbound.callback_id, outcome["text"])
    return outcome


def _reply(db, ref: str, role: str, chat_id: int, template: str, step: str,
           text: str) -> str | None:
    return enqueue(db, Recipient(ref, ref, "telegram", f"tg:{chat_id}", role),
                   Message(template=template, step=step[:32], text=text))


async def _on_start(app, inbound: telegram.Inbound, api) -> dict[str, Any]:
    if not inbound.private or inbound.chat_id is None:
        return {"handled": "ignored", "reason": "not a private chat"}
    chat_id, stranger = inbound.chat_id, f"chat:{inbound.chat_id}"
    if not inbound.code:
        with app.state.db.session() as db:
            _reply(db, stranger, "contact", chat_id, "telegram.help",
                   f"tghelp:{inbound.update_id}",
                   "This is the SENTRIX alert bot. To connect this chat, open the "
                   "Telegram link in your SENTRIX Portal settings (or the one in your "
                   "contact invitation).")
        return {"handled": "help"}
    now = datetime.now(timezone.utc)
    pushed: list[str] = []
    with app.state.db.session() as db:
        token = (db.query(ActionToken)
                 .filter_by(token_hash=security.token_digest(inbound.code),
                            purpose="telegram_link").first())
        valid = (token is not None and token.used_at is None and token.revoked_at is None
                 and (_aware(token.expires_at) or now) > now
                 and token.subject_type in ("user", "contact") and token.subject_id)
        if not valid:
            _reply(db, stranger, "contact", chat_id, "telegram.link_failed",
                   f"tgbad:{inbound.update_id}",
                   "This link has expired or was already used. Create a new one in "
                   "SENTRIX Portal settings.")
            return {"handled": "refused", "reason": "invalid link code"}
        for link in db.query(TelegramLink).filter_by(subject_type=token.subject_type,
                                                     subject_id=token.subject_id,
                                                     status="active"):
            link.status = "revoked"
        db.add(TelegramLink(subject_type=token.subject_type, subject_id=token.subject_id,
                            household_id=token.household_id, chat_id=str(chat_id),
                            status="active"))
        role = "contact"
        if token.subject_type == "contact":
            contact = db.get(EmergencyContact, token.subject_id)
            if contact is not None:
                contact.telegram_chat_id = str(chat_id)
        else:
            user = db.get(User, token.subject_id)
            role = user.role if user is not None else "owner"
        token.used_at = now
        token.opened_count = (token.opened_count or 0) + 1
        security.audit(db, "telegram.linked", actor_type=token.subject_type,
                       actor_id=token.subject_id, household_id=token.household_id,
                       target_type="telegram_link", target_id=token.subject_id,
                       detail={"chat": mask_recipient(f"tg:{chat_id}")})
        if token.household_id:
            # Contacts' Telegram flag is part of the device profile (§5.11).
            try:
                from sentrix_portal.services import accounts
                accounts.bump_profile_version(db, token.household_id)
                pushed = [d.id for d in db.query(Device).filter_by(
                    household_id=token.household_id, state="CLAIMED")]
            except LookupError:
                pushed = []
        _reply(db, f"{token.subject_type}:{token.subject_id}", role, chat_id,
               "telegram.linked", f"tglink:{token.id[:24]}",
               "Connected: SENTRIX alerts for your household will arrive in this chat. "
               "SENTRIX never sends photos, addresses or medical notes here.")
    if pushed:
        from sentrix_portal.services import hub
        with app.state.db.session() as db:
            for device_id in pushed:
                try:
                    await hub.push_profile(db, device_id)
                except Exception as exc:
                    log.warning("profile push after a Telegram link failed: %s", exc)
    return {"handled": "linked", "subject_type": token.subject_type}


async def handle_update(app, update: dict, *,
                        api: telegram.AsyncTelegramApi | None = None) -> dict[str, Any]:
    """Act on one Telegram update (polling or webhook); returns what happened."""
    inbound = telegram.parse_update(update)
    if not _first_time(app, inbound.update_id):
        return {"handled": "duplicate"}
    api = api or _api(app)
    if inbound.kind == "callback":
        return await _on_callback(app, inbound, api)
    if inbound.kind == "start":
        if not inbound.private and inbound.chat_id is not None:
            await _leave(api, inbound.chat_id)
            return {"handled": "left_group"}
        return await _on_start(app, inbound, api)
    if inbound.kind == "group" and inbound.chat_id is not None:
        await _leave(api, inbound.chat_id)
        return {"handled": "left_group"}
    return {"handled": "ignored"}


async def _leave(api, chat_id: int) -> None:
    """Private chats only (portal review): the bot never stays in a group."""
    try:
        await api.leave_chat(chat_id)
    except telegram.TelegramError as exc:
        log.info("telegram leaveChat failed: %s", exc)


# ── worker ────────────────────────────────────────────────────────────────────
class NotifyWorker:
    """Delivery loop, Telegram polling or webhook registration (``start``)."""

    def __init__(self, app, *, poll_seconds: float = POLL_SECONDS) -> None:
        self.app = app
        self.poll_seconds = poll_seconds
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._tasks: list[asyncio.Task] = []
        self._stopping = False
        self.pumped = 0
        self.last_error: str | None = None
        self.telegram_state = "off"

    def wake(self) -> None:
        loop, event = self._loop, self._wake
        if loop is None or event is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            pass

    def pump_once(self) -> int:
        attempts = worker_router(self.app.state.db).pump()
        self.pumped += attempts
        return attempts

    async def _deliver_loop(self) -> None:
        recovered = await asyncio.to_thread(worker_router(self.app.state.db).recover_interrupted)
        if recovered:
            log.warning("%d Portal message(s) interrupted by the last stop are retried",
                        recovered)
        while not self._stopping:
            try:
                await asyncio.to_thread(self.pump_once)
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)[:200]
                log.error("notify worker: %s", exc, exc_info=True)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def _poll_telegram(self) -> None:
        api = _api(self.app)
        offset: int | None = None
        delay = 1.0
        self.telegram_state = "polling"
        while not self._stopping:
            try:
                updates = await api.get_updates(offset, timeout=25)
                delay = 1.0
            except telegram.TelegramError as exc:
                if exc.status in (401, 404):
                    self.telegram_state = "stopped: bad TELEGRAM_BOT_TOKEN"
                    log.error("telegram polling stopped: %s", exc)
                    return
                if exc.status == 409:
                    self.telegram_state = "paused: a webhook is set (polling and a webhook exclude each other)"
                    log.warning("telegram: %s", self.telegram_state)
                    delay = 60.0
                await asyncio.sleep(delay)
                delay = min(60.0, delay * 2)
                continue
            for update in updates:
                if isinstance(update.get("update_id"), int):
                    offset = max(offset or 0, update["update_id"] + 1)
                try:
                    await handle_update(self.app, update, api=api)
                except Exception as exc:
                    log.error("telegram update failed: %s", exc, exc_info=True)

    async def _register_webhook(self) -> None:
        base = self.app.state.settings.base_url.rstrip("/")
        secret = telegram.webhook_secret()
        try:
            await _api(self.app).set_webhook(f"{base}/hooks/telegram", secret)
            self.telegram_state = "webhook"
            log.info("telegram webhook registered at %s/hooks/telegram", base)
        except (telegram.TelegramError, ValueError) as exc:
            self.telegram_state = f"webhook not registered: {exc}"
            log.error("telegram: %s", self.telegram_state)

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._tasks.append(self._loop.create_task(self._deliver_loop()))
        mode = os.getenv("SENTRIX_TELEGRAM_MODE", "polling").strip().lower() or "polling"
        if not telegram.bot_token():
            self.telegram_state = "off (TELEGRAM_BOT_TOKEN not set)"
        elif mode == "webhook":
            if telegram.webhook_secret():
                self._tasks.append(self._loop.create_task(self._register_webhook()))
            else:
                self.telegram_state = "webhook mode without TELEGRAM_WEBHOOK_SECRET: off"
                log.error("telegram: %s", self.telegram_state)
        else:
            self._tasks.append(self._loop.create_task(self._poll_telegram()))

    async def stop(self) -> None:
        global _worker
        self._stopping = True
        if self._wake is not None:
            self._wake.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        api = getattr(self.app.state, "telegram_api", None)
        if api is not None:
            await api.aclose()
        if _worker is self:
            _worker = None

    def health(self) -> dict[str, Any]:
        return {"running": any(not t.done() for t in self._tasks), "pumped": self.pumped,
                "last_error": self.last_error, "telegram": self.telegram_state}


def start(app):
    """Background module (``app.BACKGROUND_MODULES``): returns ``stop``."""
    global _worker
    worker = NotifyWorker(app)
    worker.start()
    _worker = worker
    app.state.notify = worker
    return worker.stop


__all__ = ["router", "enqueue", "start", "handle_update", "add_listener",
           "remove_listener", "ready", "status", "cancel", "configure", "reset",
           "worker_router",
           "build_channels", "PortalOutbox", "NotifyWorker", "portal_policy"]
