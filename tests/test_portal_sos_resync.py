"""Portal SOS edge cases found by the 2026-10-28 audit: a device that kept its
own timers while the cloud took over, and withdrawing messages on the cloud
path (no SQLite lock stall)."""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import timedelta

import pytest

from test_link_pages import open_request, portal  # noqa: F401
from test_link_support import PIN, seed_portal


def _device_chain(seeded, rid):
    """The device's own hash chain for one request, as its outbox sends it."""
    from sentrix.sos import states as S
    from sentrix_portal.models import utcnow
    from sentrix_portal.services import sos as portal_sos
    now = utcnow()
    request = {"id": rid, "state": S.DRAFTED, "version": 1, "incident_type": "weapon",
               "incident_ids": [], "flags": {}, "trigger_level": 3, "peak_level": 3,
               "evidence_state": "confirmed", "test_mode": True,
               "created_at": now.isoformat(),
               "expires_at": (now + timedelta(seconds=1800)).isoformat(),
               "policy": {"window_seconds": 1800, "safety_defaults": True,
                          "contacts": [{"id": seeded.contact_id,
                                        "consent_status": "CONFIRMED"}]}}
    state = {"prev": "0" * 64, "seq": 0, "outbox": 0}

    def event(from_state, to_state, name, detail=None):
        state["seq"] += 1
        fields = {"request_id": rid, "seq": state["seq"], "at": utcnow().isoformat(),
                  "from_state": from_state, "to_state": to_state, "event": name,
                  "actor_type": "system", "actor_id": None, "channel": "system",
                  "detail": detail or {}, "restricted": False, "origin": "device"}
        digest = portal_sos.chain_hash(state["prev"], fields)
        ev = {**fields, "prev_hash": state["prev"], "hash": digest}
        state["prev"] = digest
        req = dict(request, state=to_state, version=state["seq"])
        kind = "sos.created" if name == S.CREATED else (
            "sos.transition" if from_state != to_state else "sos.event")
        payload = ({"request": req, "event": ev} if name == S.CREATED else
                   {"request_id": rid, "version": state["seq"], "request": req, "event": ev})
        state["outbox"] += 1
        return state["outbox"], kind, payload
    return event


def _ingest(app, seeded, item):
    from sentrix_portal.models import Device, DeviceEvent, utcnow
    from sentrix_portal.services import sos as portal_sos
    seq, kind, payload = item
    with app.state.db.session() as db:
        device = db.get(Device, seeded.device_id)
        db.add(DeviceEvent(device_id=device.id, seq=seq, kind=kind, received_at=utcnow(),
                           payload=payload))
        db.flush()
        return portal_sos.ingest_device_event(db, device, kind, payload)


def test_stale_device_timers_after_a_cloud_dispatch_send_nothing(portal, monkeypatch):
    """The device was cut off, the cloud took over and the owner asked for
    help; on reconnect the device's own reminder, lapse warning and expiry
    arrive. They are kept in the chain but must not remind the owner, tell
    the contacts "lapsed unanswered" or withdraw the help messages."""
    from sentrix.actor import Actor
    from sentrix.sos import states as S
    from sentrix_portal.models import AssistanceRequest, Device
    from sentrix_portal.services import notify
    from sentrix_portal.services import sos as portal_sos
    app, seeded, outbox = portal
    cancels = []
    monkeypatch.setattr(notify, "cancel", lambda db, rid, reason, steps=None:
                        cancels.append((reason, steps)) or 0)
    rid = str(uuid.uuid4())
    ev = _device_chain(seeded, rid)
    _ingest(app, seeded, ev(None, S.DRAFTED, S.CREATED))
    _ingest(app, seeded, ev(S.DRAFTED, S.PENDING, S.PROMPTED))
    with app.state.db.session() as db:
        device = db.get(Device, seeded.device_id)
        device.online = False
        portal_sos.on_device_offline(db, device)
    owner = Actor("owner", id=seeded.owner_id, name="Riya").to_dict()
    with app.state.db.session() as db:
        assert asyncio.run(portal_sos.respond(db, rid, "approve", actor=owner,
                                              channel="portal"))["ok"]
    with app.state.db.session() as db:
        assert db.get(AssistanceRequest, rid).state == S.DISPATCHED
        portal_sos.sync_messages(db, seeded.device_id)       # the device reconnects
    before = len(outbox.messages)
    cancels.clear()
    _ingest(app, seeded, ev(S.PENDING, S.PENDING, S.REMINDER, {"step": "r600"}))
    _ingest(app, seeded, ev(S.PENDING, S.PENDING, S.LAPSE_WARNING, {"step": "lapse"}))
    _ingest(app, seeded, ev(S.PENDING, S.EXPIRED, S.EXPIRE, {"step": "lapsed"}))
    with app.state.db.session() as db:
        assert db.get(AssistanceRequest, rid).state == S.DISPATCHED
    sent = [(r.ref, m.step) for r, m in outbox.messages[before:]]
    assert sent == [], sent
    assert cancels == [], "the help request's messages are not withdrawn"


@pytest.fixture
def real_notify_portal(tmp_path):
    from sentrix_portal.app import create_app
    from sentrix_portal.services import hub, notify
    from sentrix_portal.services import sos as portal_sos
    from sentrix_portal.settings import PortalSettings
    hub.reset()
    notify.reset()
    portal_sos.reset_pin_limiter()
    app = create_app(PortalSettings.for_tests(tmp_path))
    seeded = seed_portal(app)
    worker = notify.NotifyWorker(app)
    notify.add_listener(worker.sos_feedback)          # as NotifyWorker.start does
    yield app, seeded
    notify.reset()
    app.state.db.dispose()
    hub.reset()


def test_withdrawing_on_the_cloud_path_does_not_stall_on_sqlite(real_notify_portal):
    """The withdrawal used to fan out to the SOS feedback listener, which
    waited out SQLite's 5 s lock per message while the answer held it."""
    from sentrix.actor import Actor
    from sentrix_portal.models import Notification
    from sentrix_portal.services import sos as portal_sos
    app, seeded = real_notify_portal
    rid, _ = open_request(app, seeded)                  # cloud request, prompt queued
    with app.state.db.session() as db:
        assert db.query(Notification).filter_by(request_id=rid, status="QUEUED").count()
    owner = Actor("owner", id=seeded.owner_id, name="Riya").to_dict()
    started = time.monotonic()
    with app.state.db.session() as db:
        result = asyncio.run(portal_sos.respond(db, rid, "decline", actor=owner,
                                                channel="portal", pin=PIN))
    assert result["ok"], result
    assert time.monotonic() - started < 3.0
    with app.state.db.session() as db:
        statuses = {n.step: (n.status, n.error) for n in
                    db.query(Notification).filter_by(request_id=rid, step="t0")}
    assert statuses and all(status == "SUPPRESSED" and error.startswith("cancelled:")
                            for status, error in statuses.values()), statuses


def test_help_now_under_duress_pages_the_contacts_each_time(portal):
    """After a duress PIN the request looks declined; "Request help now"
    must still page the contacts (with the duress banner), each press anew."""
    from sentrix.actor import Actor
    from test_link_support import DURESS_PIN
    from sentrix_portal.services import sos as portal_sos
    app, seeded, outbox = portal
    rid, _ = open_request(app, seeded)
    owner = Actor("owner", id=seeded.owner_id, name="Riya").to_dict()
    with app.state.db.session() as db:
        assert asyncio.run(portal_sos.respond(db, rid, "decline", actor=owner,
                                              channel="portal", pin=DURESS_PIN))["ok"]
    for press in (1, 2):
        before = len(outbox.messages)
        with app.state.db.session() as db:
            assert asyncio.run(portal_sos.respond(db, rid, "help_now", actor=owner,
                                                  channel="portal"))["ok"]
        paged = [(r.ref, m.step, m.text) for r, m in outbox.messages[before:]]
        assert paged and all(ref.startswith("contact:") and step == f"help_now{press}"
                             and "DURESS" in text.upper() for ref, step, text in paged), paged


def test_the_offline_l2_notice_names_the_owner_for_erasure_and_export(portal):
    """No request to find the household by: the notice is stored for the
    owner's user, so erasure and the data export find it."""
    from sentrix_portal.models import Device
    from sentrix_portal.services import sos as portal_sos
    app, seeded, outbox = portal
    with app.state.db.session() as db:
        device = db.get(Device, seeded.device_id)
        device.last_heartbeat = {"alarm": {"level": 2, "incident_type": "intrusion",
                                           "incident_id": "inc-l2"}}
        before = len(outbox.messages)
        assert portal_sos.on_device_offline(db, device) is None, "an L2 opens no request"
    notices = [(r.ref, m.template) for r, m in outbox.messages[before:]]
    assert notices and all(ref == f"user:{seeded.owner_id}" and template == "device.offline"
                           for ref, template in notices), notices
