"""The Portal when delivery fails (2026-11-20, a false weapon L3 on the demo
Pi): the device went offline while the Portal woke up, the offline rule paged
the contact on Telegram, every Telegram send from the Portal timed out, and

* the owner's decline then queued "resolved by the resident" for a contact
  the alert never reached, retried every 2 minutes;
* every one of those failing retries was mirrored to the device as its own
  ``notification`` event;
* the Telegram poll failed without a word in the log or in ``health()``.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import timedelta

import pytest

from test_link_pages import open_request  # noqa: F401
from test_link_support import PIN
from test_portal_sos_resync import real_notify_portal  # noqa: F401


def _contact_alerts(app, seeded, rid):
    """The device goes offline during the open request: the cloud pages the
    contact (D9). Returns {channel: notification id} of those alerts."""
    from sentrix_portal.models import Device, Notification
    from sentrix_portal.services import sos as portal_sos
    with app.state.db.session() as db:
        device = db.get(Device, seeded.device_id)
        device.online = False
        portal_sos.on_device_offline(db, device)
    with app.state.db.session() as db:
        rows = (db.query(Notification)
                .filter(Notification.request_id == rid,
                        Notification.recipient_ref == f"contact:{seeded.contact_id}").all())
        return {row.channel: row.id for row in rows}


def _set(app, note_id, status, error=None, retrying=False):
    from sentrix_portal.models import Notification, utcnow
    with app.state.db.session() as db:
        row = db.get(Notification, note_id)
        row.status, row.error = status, error
        row.attempts = 3 if status == "FAILED" else 1
        row.next_attempt_at = utcnow() + timedelta(minutes=2) if retrying else None


@pytest.mark.parametrize("outcome, told", [
    ({"telegram": ("FAILED", "telegram sendMessage: network ReadTimeout", True),
      "email": ("FAILED", "smtp: timed out", True)}, set()),
    ({"telegram": ("FAILED", "telegram sendMessage: network ReadTimeout", True),
      "email": ("DELIVERED", None, False)}, {"email"}),
    ({"telegram": ("DELIVERED", None, False),
      "email": ("SUPPRESSED", "SENTRIX_EMAIL_LIVE is off", False)}, {"telegram", "email"}),
])
def test_the_end_of_an_alert_goes_only_where_the_alert_arrived(real_notify_portal,
                                                                outcome, told):
    """A dry run (SUPPRESSED by the guard) counts as arrived: it records what
    would have gone out. A failing or withdrawn alert does not."""
    from sentrix.actor import Actor
    from sentrix_portal.models import Notification
    from sentrix_portal.services import sos as portal_sos
    app, seeded = real_notify_portal
    rid, _ = open_request(app, seeded)
    alerts = _contact_alerts(app, seeded, rid)
    assert set(alerts) == {"telegram", "email"}, alerts
    for channel, (status, error, retrying) in outcome.items():
        _set(app, alerts[channel], status, error, retrying)
    owner = Actor("owner", id=seeded.owner_id, name="Riya").to_dict()
    with app.state.db.session() as db:
        result = asyncio.run(portal_sos.respond(db, rid, "decline", actor=owner,
                                                channel="portal", pin=PIN))
    assert result["ok"], result
    with app.state.db.session() as db:
        ends = {row.channel for row in db.query(Notification).filter(
            Notification.request_id == rid, Notification.step == "resolved",
            Notification.recipient_ref == f"contact:{seeded.contact_id}")}
        withdrawn = {row.channel for row in db.query(Notification).filter(
            Notification.id.in_(list(alerts.values())),
            Notification.error.like("cancelled:%"))}
    assert ends == told
    retrying = {c for c, (_, _, again) in outcome.items() if again}
    assert withdrawn == retrying, "the failing alert itself is withdrawn at the decline"


def _item(status, attempts, next_attempt_at):
    from sentrix.alerting.channels.base import Message, OutboxItem, Recipient
    return OutboxItem("n1", Recipient("contact:c1", "Arjun", "telegram", "tg:12345", "contact"),
                      Message("sos.resolved", "resolved", "text", request_id="r1"),
                      status=status, attempts=attempts, next_attempt_at=next_attempt_at,
                      error="telegram sendMessage: network ReadTimeout")


def test_a_retry_failing_again_is_not_mirrored_to_the_device(monkeypatch):
    from types import SimpleNamespace
    from contextlib import contextmanager
    from sentrix_portal.services import notify
    from sentrix_portal.services import sos as portal_sos
    calls = []
    monkeypatch.setattr(portal_sos, "notification_status",
                        lambda db, **kw: calls.append((kw["status"], kw["step"])))

    @contextmanager
    def session():
        yield None
    app = SimpleNamespace(state=SimpleNamespace(db=SimpleNamespace(session=session)))
    worker = notify.NotifyWorker(app)
    soon = time.time() + 120
    for item in (_item("FAILED", 1, soon),          # the first failure: reported
                 _item("FAILED", 2, soon),          # failing again: not
                 _item("FAILED", 7, soon),          # not
                 _item("FAILED", 720, math.inf),    # gave up: reported
                 _item("DELIVERED", 3, math.inf)):  # a late success: reported
        worker.sos_feedback(item)
    assert calls == [("FAILED", "resolved"), ("FAILED", "resolved"),
                     ("DELIVERED", "resolved")]


def test_telegram_polling_failures_are_logged_and_shown(monkeypatch, caplog):
    from types import SimpleNamespace
    from sentrix.alerting.channels import telegram
    from sentrix_portal.services import notify
    app = SimpleNamespace(state=SimpleNamespace())
    worker = notify.NotifyWorker(app)
    seen = []

    class FailingTwice:
        calls = 0

        async def get_updates(self, offset, timeout=25):
            self.calls += 1
            if self.calls <= 2:
                raise telegram.TelegramError("getUpdates", None,
                                             "ReadTimeout: The read operation timed out")
            seen.append(worker.health()["telegram_error"])
            worker._stopping = True
            return []

    monkeypatch.setattr(notify, "_api", lambda app: FailingTwice())

    async def no_wait(seconds):
        return None
    monkeypatch.setattr(notify.asyncio, "sleep", no_wait)
    with caplog.at_level(logging.INFO, logger="sentrix_portal.notify"):
        asyncio.run(worker._poll_telegram())
    assert seen and "ReadTimeout" in seen[0], "health() showed the failure while it lasted"
    assert worker.health()["telegram_error"] is None, "cleared once a poll succeeds"
    failures = [r for r in caplog.records if "telegram polling failed" in r.getMessage()]
    assert len(failures) == 1, "logged once, not on every retry"
    assert any("recovered" in r.getMessage() for r in caplog.records)
