"""SQLite outbox for the device's channel router (``notifications`` table).

Implements ``sentrix.alerting.channels.base.Outbox`` (``add``, ``due``,
``update``) plus what the full router needs (docs/7 §8.2, track T-CH):

* ``recover_interrupted`` — rows left SENDING by a crash or a power cut go back
  to FAILED and are retried at once (at-least-once: a duplicate SMS is better
  than a lost SOS);
* ``cancel`` — the SOS engine withdraws a closed request's queued messages;
* ``sent_since`` / ``get`` — delivery receipts (modem status reports) find
  the row they confirm; the modem's daily cap is seeded from today's rows;
* ``counts`` / ``recent`` — the ``state.channels`` block and ``/api/channels``.

Addresses stay in the device database (it already holds the profile); every
row also keeps a masked copy for the UI and logs.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from sentrix.alerting import guard
from sentrix.alerting.channels.base import (DELIVERED, FAILED, QUEUED, SENDING, SENT,
                                            SUPPRESSED, OutboxItem, message_meta, rebuild)
from sentrix.storage import database
from sentrix.storage.models import NotificationRecord
from sentrix.utils.redact import mask_recipient

#: "Never again" retry time, stored as a far-future date.
_NEVER = datetime(9999, 1, 1, tzinfo=timezone.utc)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


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
    return float("inf") if value.year >= 9999 else value.timestamp()


def _item(row: NotificationRecord) -> OutboxItem:
    recipient, message = rebuild(
        row.recipient_ref, row.channel, row.address,
        json.loads(row.meta_json) if row.meta_json else None,
        template=row.template, step=row.step, text=row.body,
        request_id=row.request_id, subject=row.subject,
        created_at=_ts(row.created_at))
    return OutboxItem(id=row.id, recipient=recipient, message=message,
                      status=row.status, attempts=row.attempts or 0,
                      next_attempt_at=_ts(row.next_attempt_at),
                      provider_id=row.provider_id, error=row.error,
                      sent_at=_ts(row.sent_at) if row.sent_at else None)


def row_dict(row: NotificationRecord) -> dict:
    """A row for the UI: masked recipient, no address, no body."""
    def iso(value: datetime | None) -> str | None:
        if value is None or value.year >= 9999:
            return None
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()

    return {"id": row.id, "request_id": row.request_id, "channel": row.channel,
            "recipient_ref": row.recipient_ref, "recipient_masked": row.recipient_masked,
            "template": row.template, "step": row.step, "status": row.status,
            "attempts": row.attempts or 0, "error": row.error,
            "provider_id": row.provider_id, "test_mode": bool(row.test_mode),
            "created_at": iso(row.created_at), "sent_at": iso(row.sent_at),
            "next_attempt_at": iso(row.next_attempt_at)}


class SqlOutbox:
    """Outbox rows in ``var/sentrix.db`` (or the test's temporary database)."""

    # ── base.Outbox ───────────────────────────────────────────────────────────
    def add(self, item: OutboxItem) -> bool:
        r, m = item.recipient, item.message
        try:
            with database.session_scope() as session:
                session.add(NotificationRecord(
                    id=item.id, request_id=m.request_id, recipient_ref=r.ref,
                    recipient_masked=mask_recipient(r.address), address=r.address,
                    channel=r.channel, template=m.template, step=m.step,
                    dedupe_key=item.dedupe_key, status=item.status,
                    attempts=item.attempts, next_attempt_at=_dt(item.next_attempt_at),
                    subject=m.subject, body=m.text,
                    meta_json=json.dumps(message_meta(r, m), default=str),
                    test_mode=guard.test_mode(), created_at=_dt(m.created_at)))
            return True
        except IntegrityError:
            return False

    def due(self, now: float, limit: int = 50) -> list[OutboxItem]:
        with database.session_scope() as session:
            rows = (session.query(NotificationRecord)
                    .filter(NotificationRecord.status.in_((QUEUED, FAILED)),
                            NotificationRecord.next_attempt_at <= _dt(now))
                    .order_by(NotificationRecord.next_attempt_at,
                              NotificationRecord.created_at)
                    .limit(limit).all())
            return [_item(row) for row in rows]

    def update(self, item: OutboxItem) -> None:
        with database.session_scope() as session:
            row = session.get(NotificationRecord, item.id)
            if row is None:
                return
            row.status, row.attempts = item.status, item.attempts
            row.next_attempt_at = _dt(item.next_attempt_at)
            row.provider_id, row.error = item.provider_id, item.error
            row.sent_at = _dt(item.sent_at)

    # ── router extensions ─────────────────────────────────────────────────────
    def get(self, item_id: str) -> OutboxItem | None:
        with database.session_scope() as session:
            row = session.get(NotificationRecord, item_id)
            return _item(row) if row is not None else None

    def recover_interrupted(self, now: float) -> list[OutboxItem]:
        """SENDING rows (the process died mid-send) become FAILED, due now."""
        with database.session_scope() as session:
            rows = session.query(NotificationRecord).filter_by(status=SENDING).all()
            for row in rows:
                row.status = FAILED
                row.error = "interrupted while sending (restart); retrying"
                row.next_attempt_at = _dt(now)
            return [_item(row) for row in rows]

    def cancel(self, request_id: str, reason: str,
               steps: tuple[str, ...] | None = None) -> list[OutboxItem]:
        """Queued or retrying messages of a request become SUPPRESSED (final)."""
        with database.session_scope() as session:
            query = (session.query(NotificationRecord)
                     .filter(NotificationRecord.request_id == request_id,
                             NotificationRecord.status.in_((QUEUED, FAILED))))
            if steps:
                query = query.filter(NotificationRecord.step.in_(tuple(steps)))
            rows = query.all()
            for row in rows:
                row.status = SUPPRESSED
                row.error = f"cancelled: {reason}"
                row.next_attempt_at = _NEVER
            return [_item(row) for row in rows]

    def sent_since(self, channel: str, since: float) -> list[OutboxItem]:
        """SENT and DELIVERED rows of a channel sent at or after ``since``."""
        with database.session_scope() as session:
            rows = (session.query(NotificationRecord)
                    .filter(NotificationRecord.channel == channel,
                            NotificationRecord.status.in_((SENT, DELIVERED)),
                            NotificationRecord.sent_at >= _dt(since))
                    .order_by(NotificationRecord.sent_at.desc()).all())
            return [_item(row) for row in rows]

    def counts(self) -> dict[str, int]:
        """Rows per status, plus ``depth``: what is still waiting to leave
        (queued, being sent, or failed with a retry scheduled)."""
        with database.session_scope() as session:
            per_status = dict(session.query(NotificationRecord.status,
                                            func.count(NotificationRecord.id))
                              .group_by(NotificationRecord.status).all())
            retrying = (session.query(func.count(NotificationRecord.id))
                        .filter(NotificationRecord.status == FAILED,
                                NotificationRecord.next_attempt_at < _NEVER).scalar())
        counts = {status: int(per_status.get(status, 0))
                  for status in (QUEUED, SENDING, SENT, DELIVERED, FAILED, SUPPRESSED)}
        counts["depth"] = counts[QUEUED] + counts[SENDING] + int(retrying or 0)
        return counts

    def recent(self, limit: int = 20, request_id: str | None = None) -> list[dict]:
        """Newest first, masked (``row_dict``)."""
        limit = max(1, min(int(limit), 200))
        with database.session_scope() as session:
            query = session.query(NotificationRecord)
            if request_id:
                query = query.filter_by(request_id=request_id)
            rows = query.order_by(NotificationRecord.created_at.desc()).limit(limit).all()
            return [row_dict(row) for row in rows]


__all__ = ["SqlOutbox", "row_dict"]
