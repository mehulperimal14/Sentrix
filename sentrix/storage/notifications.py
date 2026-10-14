"""SQLite outbox for the device's channel router (``notifications`` table).

Implements ``sentrix.alerting.channels.base.Outbox``. Minimal version from the
foundation; track T-CH owns this module from here on.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

from sentrix.alerting import guard
from sentrix.alerting.channels.base import (FAILED, QUEUED, OutboxItem, message_meta,
                                            rebuild)
from sentrix.storage import database
from sentrix.storage.models import NotificationRecord
from sentrix.utils.redact import mask_recipient

#: "Never again" retry time, stored as a far-future date.
_NEVER = datetime(9999, 1, 1, tzinfo=timezone.utc)


def _dt(ts: float | None) -> datetime | None:
    if ts is None:
        return None
    if math.isinf(ts):
        return _NEVER
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _ts(value: datetime | None) -> float:
    if value is None:
        return 0.0
    value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return float("inf") if value.year >= 9999 else value.timestamp()


class SqlOutbox:
    """Outbox rows in ``var/sentrix.db`` (or the test's temporary database)."""

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
                    .order_by(NotificationRecord.next_attempt_at)
                    .limit(limit).all())
            items = []
            for row in rows:
                recipient, message = rebuild(
                    row.recipient_ref, row.channel, row.address,
                    json.loads(row.meta_json) if row.meta_json else None,
                    template=row.template, step=row.step, text=row.body,
                    request_id=row.request_id, subject=row.subject,
                    created_at=_ts(row.created_at))
                items.append(OutboxItem(
                    id=row.id, recipient=recipient, message=message,
                    status=row.status, attempts=row.attempts or 0,
                    next_attempt_at=_ts(row.next_attempt_at),
                    provider_id=row.provider_id, error=row.error))
            return items

    def update(self, item: OutboxItem) -> None:
        with database.session_scope() as session:
            row = session.get(NotificationRecord, item.id)
            if row is None:
                return
            row.status, row.attempts = item.status, item.attempts
            row.next_attempt_at = _dt(item.next_attempt_at)
            row.provider_id, row.error = item.provider_id, item.error
            row.sent_at = _dt(item.sent_at)


__all__ = ["SqlOutbox"]
