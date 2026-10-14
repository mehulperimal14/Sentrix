"""T-LINK: the Portal side of the link (docs/7 §5, §7.6, §7.11).

The hub's protocol handling (auth, hello, close codes, errors), the outbox
ingest (dedupe, watermark, factory reset), the SOS mirror (per-request order,
gaps, chain checks, snapshot consent), the heartbeat watcher and tamper
signal, the cloud authority (timers, answers, PINs, duress) and the
reconnect sync, and 100 forced disconnects without losing an event. Messages
the Portal would send are captured from ``notify.enqueue``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import random
import threading
import time
import uuid
from datetime import timedelta

import pytest

from sentrix.actor import Actor
from sentrix.link import protocol

from test_link_support import (DURESS_PIN, PIN, FakeSosService, MemoryNetwork, MemoryPipe,
                               Outbox, lc_app, make_device, profile_for, run_on,
                               seed_portal, wait_for)


def fast_settings(**link):
    from dataclasses import replace
    from sentrix.config import settings
    return replace(settings, link=replace(settings.link, heartbeat_seconds=0.3, **link))


@pytest.fixture
def portal(portal_app, monkeypatch):
    from sentrix_portal.services import accounts, hub, notify
    from sentrix_portal.services import sos as portal_sos
    hub.reset()
    portal_sos.reset_pin_limiter()
    monkeypatch.setattr(hub, "_cfg", fast_settings)
    seeded = seed_portal(portal_app, claimed=True)
    monkeypatch.setattr(accounts, "build_profile",
                        lambda db, household_id, device_id: profile_for(seeded))
    outbox = Outbox()
    monkeypatch.setattr(notify, "enqueue", outbox)
    yield portal_app, seeded, outbox
    hub.reset()


TOKEN = "device-token-" + "k" * 40


def pair(app, seeded, token=TOKEN):
    from sentrix_portal import security
    from sentrix_portal.models import Device
    with app.state.db.session() as db:
        device = db.get(Device, seeded.device_id)
        device.token_hash = security.token_digest(token)
        device.last_hello = {"token_confirmed": True}


@pytest.fixture
def rig(portal, tmp_path, monkeypatch, temp_db):
    from sentrix import services
    from sentrix.link import client as link_client
    app, seeded, outbox = portal
    pair(app, seeded)
    network = MemoryNetwork(app)
    device = make_device(tmp_path, monkeypatch, connector=network, device_id=seeded.device_id,
                         app=lc_app(monkeypatch))
    link_client.save_token(device.identity_dir, TOKEN)
    sos = FakeSosService()
    services.registry.register(sos, "sos")
    device.sos, device.network, device.app, device.seeded, device.outbox = (
        sos, network, app, seeded, outbox)
    yield device
    device.service.stop()


def mirror(app, request_id):
    from sentrix_portal.models import AssistanceRequest
    with app.state.db.session() as db:
        row = db.get(AssistanceRequest, request_id)
        if row is not None:
            db.expunge(row)
        return row


def connect(rig):
    from sentrix_portal.services import hub
    rig.service.start()
    wait_for(lambda: hub.is_online(rig.seeded.device_id), what="online")


# ── protocol ──────────────────────────────────────────────────────────────────
async def _hand_device(app, auth, hello_fields=None):
    """Open a pipe to the hub and say hello by hand; returns (pipe, welcome|None, task)."""
    from sentrix_portal.services.hub import hub_for
    pipe = MemoryPipe()
    task = asyncio.ensure_future(hub_for(app).serve(pipe.portal, auth))
    hello = protocol.envelope(protocol.HELLO, protocol=1, outbox={}, **(hello_fields or {}))
    await pipe.device.send_text(protocol.encode(hello))
    try:
        reply = await asyncio.wait_for(pipe.device.receive(), 3)
    except Exception:
        reply = None
    return pipe, (json.loads(reply) if isinstance(reply, str) else None), task


class TestProtocol:
    def test_bad_credentials_close_4001(self, portal):
        app, seeded, _ = portal

        async def go():
            pipe, welcome, task = await _hand_device(app, "Bearer nope",
                                                     {"device_id": seeded.device_id})
            await task
            return pipe.code, welcome
        assert asyncio.run(go()) == (protocol.CLOSE_UNAUTHORISED, None)

    def test_hello_for_another_device_and_wrong_protocol(self, portal):
        app, seeded, _ = portal
        pair(app, seeded)

        async def other():
            pipe, _, task = await _hand_device(app, f"Bearer {TOKEN}",
                                               {"device_id": str(uuid.uuid4())})
            await task
            return pipe.code

        async def version():
            from sentrix_portal.services.hub import hub_for
            pipe = MemoryPipe()
            task = asyncio.ensure_future(hub_for(app).serve(pipe.portal, f"Bearer {TOKEN}"))
            hello = protocol.envelope(protocol.HELLO, device_id=seeded.device_id, protocol=2)
            await pipe.device.send_text(protocol.encode(hello))
            await task
            return pipe.code
        assert asyncio.run(other()) == protocol.CLOSE_UNAUTHORISED
        assert asyncio.run(version()) == protocol.CLOSE_UNSUPPORTED

    def test_welcome_errors_and_three_strikes(self, portal):
        app, seeded, _ = portal
        pair(app, seeded)

        async def go():
            pipe, welcome, task = await _hand_device(app, f"Bearer {TOKEN}",
                                                     {"device_id": seeded.device_id})
            assert welcome["type"] == "welcome" and welcome["claimed"] is True
            assert welcome["resend_from_seq"] == 0 and welcome["profile_version"] == 1
            await pipe.device.send_text("{not json")
            first = json.loads(await asyncio.wait_for(pipe.device.receive(), 3))
            while first["type"] != "error":
                first = json.loads(await asyncio.wait_for(pipe.device.receive(), 3))
            await pipe.device.send_text(protocol.encode(protocol.envelope("bogus")))
            await pipe.device.send_text(protocol.encode(protocol.envelope(
                protocol.EVENT, seq=0, kind="incident.upsert", payload={})))
            await asyncio.wait_for(task, 5)
            return first, pipe.code
        first, code = asyncio.run(go())
        assert first["code"] == "invalid_args" and code == 1008

    def test_a_newer_connection_replaces_the_old_one(self, portal):
        app, seeded, _ = portal
        pair(app, seeded)

        async def go():
            old, w1, t1 = await _hand_device(app, f"Bearer {TOKEN}", {"device_id": seeded.device_id})
            new, w2, t2 = await _hand_device(app, f"Bearer {TOKEN}", {"device_id": seeded.device_id})
            await asyncio.wait_for(t1, 3)
            code = old.code
            new.drop()
            await asyncio.wait_for(t2, 3)
            return code, w1["type"], w2["type"]
        assert asyncio.run(go()) == (protocol.CLOSE_REPLACED, "welcome", "welcome")

    def test_a_bootstrap_session_may_only_say_hello_and_heartbeat(self, portal):
        app, seeded, _ = portal
        from sentrix_portal.models import Device
        with app.state.db.session() as db:
            db.get(Device, seeded.device_id).state = "ASSIGNED"

        async def go():
            pipe, welcome, task = await _hand_device(
                app, f"Bootstrap {seeded.device_id}:{seeded.bootstrap_secret}",
                {"device_id": seeded.device_id})
            await pipe.device.send_text(protocol.encode(protocol.envelope(
                protocol.EVENT, seq=1, kind="incident.upsert", payload={})))
            error = json.loads(await asyncio.wait_for(pipe.device.receive(), 3))
            pipe.drop()
            await task
            return welcome, error
        welcome, error = asyncio.run(go())
        assert welcome["claimed"] is False and error["code"] == "unauthorised"


# ── ingest ────────────────────────────────────────────────────────────────────
class TestIngest:
    def test_duplicates_are_reacked_not_reapplied_and_the_watermark_is_contiguous(self, portal):
        from sentrix_portal.models import AuditLog, Device, DeviceEvent
        from sentrix_portal.services.hub import DeviceSession, hub_for
        app, seeded, _ = portal
        hub = hub_for(app)
        session = DeviceSession(seeded.device_id, None, "bearer", True, seeded.household_id)
        mode = {"armed": True, "by": "portal:u1", "at": "2026-10-14T09:00:00+00:00"}
        hub._ingest(session, 2, "mode.changed", mode, None)
        assert session.watermark == 0, "seq 1 is missing"
        hub._ingest(session, 1, "mode.changed", mode, None)
        assert session.watermark == 2
        hub._ingest(session, 2, "mode.changed", mode, None)
        with app.state.db.session() as db:
            assert db.query(DeviceEvent).count() == 2
            assert db.query(AuditLog).filter_by(event="mode.set").count() == 2
            assert db.get(Device, seeded.device_id).last_acked_seq == 2

    def test_a_failing_effect_still_keeps_the_raw_event(self, portal):
        from sentrix_portal.models import AuditLog, DeviceEvent
        from sentrix_portal.services.hub import DeviceSession, hub_for
        app, seeded, _ = portal
        session = DeviceSession(seeded.device_id, None, "bearer", True, seeded.household_id)
        hub_for(app)._ingest(session, 1, "incident.upsert", {"id": "not-a-uuid"}, None)
        with app.state.db.session() as db:
            assert db.get(DeviceEvent, (seeded.device_id, 1)) is not None
            assert db.query(AuditLog).filter_by(event="device.event_failed").count() == 1
        assert session.watermark == 1

    def test_strings_are_revalidated_at_ingest(self, portal):
        from sentrix_portal.models import DispatchMirror, IncidentMirror
        from sentrix_portal.services.hub import DeviceSession, hub_for
        app, seeded, _ = portal
        session = DeviceSession(seeded.device_id, None, "bearer", True, seeded.household_id)
        incident, package = str(uuid.uuid4()), str(uuid.uuid4())
        hub = hub_for(app)
        hub._ingest(session, 1, "incident.upsert", {
            "id": incident, "incident_type": "<script>", "reason": "x" * 999,
            "snapshot_path": "rtsp://admin:pw@cam/s1", "peak_level": 3}, None)
        hub._ingest(session, 2, "dispatch.upsert", {
            "id": package, "incident_id": incident, "status": "PENDING",
            "request_id": "../../etc"}, None)
        with app.state.db.session() as db:
            row = db.get(IncidentMirror, incident)
            assert row.incident_type == "unknown" and len(row.reason) == 255
            assert row.data["snapshot_path"] == "rtsp://***@cam/s1"
            assert db.get(DispatchMirror, package).request_id is None

    def test_a_factory_reset_device_is_not_deduplicated_against_its_old_numbers(self, rig):
        from sentrix import link
        from sentrix.storage import link_store
        from sentrix_portal.models import AuditLog, Device, DeviceEvent
        connect(rig)
        incident = str(uuid.uuid4())
        link.publish("incident.upsert", {"id": incident, "peak_level": 1})
        wait_for(lambda: link_store.depth() == 0, what="acked")
        rig.service.stop()
        with rig.app.state.db.session() as db:
            device = db.get(Device, rig.seeded.device_id)
            device.last_acked_seq = 50                 # the old database went further
            hello = dict(device.last_hello)
            hello["outbox_epoch"] = "2020-01-01T00:00:00+00:00"
            device.last_hello = hello
        rig.service._future = None
        rig.service.start()
        wait_for(lambda: rig.network.hub.online(rig.seeded.device_id), what="back")
        with rig.app.state.db.session() as db:
            assert db.query(AuditLog).filter_by(event="device.outbox_reset").count() == 1
            assert db.get(Device, rig.seeded.device_id).last_acked_seq <= 1


# ── the SOS mirror ────────────────────────────────────────────────────────────
class TestSosMirror:
    def test_a_device_request_is_mirrored_with_its_chain_and_prompts_the_owner(self, rig):
        from sentrix_portal.models import ActionToken
        from sentrix_portal.services import sos as portal_sos
        connect(rig)
        request = rig.sos.open()
        row = wait_for(lambda: (r := mirror(rig.app, request["id"])) and r.state == "PENDING"
                       and r, what="PENDING mirrored")
        assert row.household_id == rig.seeded.household_id and row.device_id == rig.seeded.device_id
        with rig.app.state.db.session() as db:
            chains = portal_sos.verify_chains(db, request["id"])
            assert chains["ok"] and chains["origins"] == {"device": 2}
        steps = rig.outbox.steps("owner")
        assert ("owner", "telegram", "t0") in steps and ("owner", "email", "t0") in steps
        telegram = next(m for r, m in rig.outbox.messages if r.channel == "telegram")
        assert [b.action for b in telegram.buttons] == ["approve", "checking", "decline_page"]
        nonce = telegram.buttons[0].callback_data.split(":")[0]
        with rig.app.state.db.session() as db:
            token = db.query(ActionToken).filter_by(nonce=nonce).one()
            assert token.request_id == request["id"] and token.purpose == "action_link"
        email = next(m for r, m in rig.outbox.messages if r.channel == "email")
        assert "/a/" in email.text and "SENTRIX does not contact 112 itself." in email.text

    def test_events_are_applied_in_order_and_a_gap_waits(self, portal, temp_db, monkeypatch):
        from sentrix import link
        from sentrix.storage import link_store
        from sentrix_portal.services.hub import DeviceSession, hub_for
        app, seeded, _ = portal
        monkeypatch.setattr(link, "link_enabled", lambda: True)
        sos = FakeSosService()
        request = sos.open()
        sos.respond(request["id"], "approve", actor=Actor("owner", id="u1"), channel="lc")
        rows = sorted(link_store.pending(10), key=lambda r: r["seq"])
        assert [r["kind"] for r in rows] == ["sos.created", "sos.transition", "sos.transition"]
        session = DeviceSession(seeded.device_id, None, "bearer", True, seeded.household_id)
        hub = hub_for(app)
        hub._ingest(session, rows[2]["seq"], rows[2]["kind"], rows[2]["payload"], None)
        hub._ingest(session, rows[1]["seq"], rows[1]["kind"], rows[1]["payload"], None)
        assert mirror(app, request["id"]) is None, "nothing applies before the created event"
        followups = hub._ingest(session, rows[0]["seq"], rows[0]["kind"], rows[0]["payload"],
                                None)
        assert mirror(app, request["id"]).state == "APPROVED"
        assert followups == [("sos.update", {"request_id": request["id"], "event": "delivered",
                                             "detail": {"target": "desk"}})]

    def test_a_tampered_event_stops_the_chain(self, portal, temp_db, monkeypatch):
        from sentrix import link
        from sentrix.storage import link_store
        from sentrix_portal.models import AuditLog
        from sentrix_portal.services.hub import DeviceSession, hub_for
        app, seeded, _ = portal
        monkeypatch.setattr(link, "link_enabled", lambda: True)
        request = FakeSosService().open()
        rows = sorted(link_store.pending(10), key=lambda r: r["seq"])
        forged = json.loads(json.dumps(rows[1]["payload"]))
        forged["event"]["actor_id"] = "portal:attacker"
        session = DeviceSession(seeded.device_id, None, "bearer", True, seeded.household_id)
        hub = hub_for(app)
        hub._ingest(session, rows[0]["seq"], rows[0]["kind"], rows[0]["payload"], None)
        hub._ingest(session, rows[1]["seq"], rows[1]["kind"], forged, None)
        assert mirror(app, request["id"]).state == "DRAFTED"
        with app.state.db.session() as db:
            assert db.query(AuditLog).filter_by(event="sos.chain_invalid").count() == 1

    def test_the_snapshot_needs_consent_and_a_matching_hash(self, portal, temp_db, monkeypatch):
        from sentrix import link
        from sentrix.storage import link_store
        from sentrix_portal.models import Consent, SosSnapshot
        from sentrix_portal.services.hub import DeviceSession, hub_for
        from test_link_support import _tiny_jpeg
        app, seeded, _ = portal
        monkeypatch.setattr(link, "link_enabled", lambda: True)
        request = FakeSosService().open()
        jpeg = _tiny_jpeg(7)
        good = {"request_id": request["id"], "content_type": "image/jpeg",
                "sha256": hashlib.sha256(jpeg).hexdigest(), "width": 32, "height": 24,
                "data_b64": base64.b64encode(jpeg).decode()}
        session = DeviceSession(seeded.device_id, None, "bearer", True, seeded.household_id)
        hub = hub_for(app)
        hub._ingest(session, 90, "sos.snapshot", good, None)       # before the request
        for row in sorted(link_store.pending(10), key=lambda r: r["seq"]):
            hub._ingest(session, row["seq"], row["kind"], row["payload"], None)
        with app.state.db.session() as db:
            stored = db.query(SosSnapshot).filter_by(request_id=request["id"]).one()
            assert stored.data == jpeg and stored.expires_at is not None
        other = FakeSosService().open()
        for row in sorted(link_store.pending(20), key=lambda r: r["seq"]):
            if row["payload"].get("request", {}).get("id") == other["id"]:
                hub._ingest(session, row["seq"], row["kind"], row["payload"], None)
        hub._ingest(session, 91, "sos.snapshot", {**good, "request_id": other["id"],
                                                  "sha256": "0" * 64}, None)
        with app.state.db.session() as db:
            assert db.query(SosSnapshot).filter_by(request_id=other["id"]).first() is None
            for consent in db.query(Consent).filter_by(purpose="sos_snapshot"):
                consent.withdrawn_at = consent.granted_at
        hub._ingest(session, 92, "sos.snapshot", {**good, "request_id": other["id"]}, None)
        with app.state.db.session() as db:
            assert db.query(SosSnapshot).filter_by(request_id=other["id"]).first() is None, \
                "withdrawn consent: no image is kept"

    def test_online_approve_goes_through_the_device_and_the_desk_delivers(self, rig):
        from sentrix_portal.services import sos as portal_sos
        connect(rig)
        request = rig.sos.open()
        wait_for(lambda: (r := mirror(rig.app, request["id"])) and r.state == "PENDING",
                 what="PENDING")
        owner = Actor("owner", id=rig.seeded.owner_id, name="Riya").to_dict()

        async def approve():
            with rig.app.state.db.session() as db:
                return await portal_sos.respond(db, request["id"], "approve", actor=owner,
                                                channel="portal", authority="FIRE")
        result = run_on(rig.service._loop, approve())
        assert result["ok"] and result["request"]["state"] == "APPROVED"
        assert rig.sos.calls[-1][:6] == ("respond", request["id"], "approve", "owner", "portal",
                                         "FIRE")
        wait_for(lambda: mirror(rig.app, request["id"]).state == "DISPATCHED",
                 what="delivered by the Desk queue")
        assert ("contact:" + rig.seeded.contact_id, "telegram", "contacts_t0") in \
            rig.outbox.steps("contact:")

    def test_online_decline_checks_the_pin_on_the_device(self, rig):
        from sentrix_portal.services import sos as portal_sos
        connect(rig)
        request = rig.sos.open()
        wait_for(lambda: (r := mirror(rig.app, request["id"])) and r.state == "PENDING",
                 what="PENDING")
        owner = Actor("owner", id=rig.seeded.owner_id).to_dict()

        async def decline(pin):
            with rig.app.state.db.session() as db:
                return await portal_sos.respond(db, request["id"], "decline", actor=owner,
                                                channel="portal", pin=pin)
        assert run_on(rig.service._loop, decline("000000"))["code"] == "pin_invalid"
        duress = run_on(rig.service._loop, decline(DURESS_PIN))
        assert duress["ok"] and duress["request"]["state"] == "DECLINED", "owner view"
        row = wait_for(lambda: (r := mirror(rig.app, request["id"])) and r.state != "PENDING"
                       and r, what="mirrored")
        assert row.flags.get("duress") and row.state in ("APPROVED", "DISPATCHED")
        with rig.app.state.db.session() as db:
            view = portal_sos.request_view(db.get(type(row), row.id), "owner")
            assert view["state"] == "DECLINED" and "duress" not in view["flags"]
            assert all(not e["restricted"] for e in portal_sos.events_view(db, row.id, "owner"))
            desk = portal_sos.request_view(db.get(type(row), row.id), "desk")
            assert desk["duress"] and desk["banner"] == "Do not go to the home. Call 112."


# ── offline: watcher, tamper, cloud authority, sync ───────────────────────────
class TestCloudAuthority:
    def _offline(self, rig, request_id=None):
        from sentrix_portal.models import utcnow
        rig.service.stop()
        wait_for(lambda: not rig.network.hub.online(rig.seeded.device_id), what="gone")
        later = utcnow() + timedelta(seconds=60)
        lost = run_on_new_loop(rig.network.hub.check_heartbeats(now=later))
        assert lost == [rig.seeded.device_id]

    def test_heartbeat_lost_with_an_open_request_hands_it_to_the_cloud(self, rig):
        from sentrix_portal.models import AuditLog, Device
        connect(rig)
        request = rig.sos.open()
        wait_for(lambda: (r := mirror(rig.app, request["id"])) and r.state == "PENDING",
                 what="PENDING")
        self._offline(rig)
        row = mirror(rig.app, request["id"])
        assert row.authority_side == "cloud" and row.flags["device_offline"]
        assert row.flags["contacts_notified"], "safety defaults: contacts at once"
        with rig.app.state.db.session() as db:
            assert db.get(Device, rig.seeded.device_id).online is False
            events = {a.event for a in db.query(AuditLog)}
            assert {"device.offline", "device.tamper", "sos.device_offline",
                    "sos.contacts_notified"} <= events
        steps = rig.outbox.steps()
        assert ("owner", "telegram", "offline") in steps
        assert ("contact:" + rig.seeded.contact_id, "email", "contacts_t0") in steps
        contact_mail = next(m for r, m in rig.outbox.messages
                            if r.ref.startswith("contact:") and r.channel == "email")
        assert "/c/" in contact_mail.text

    def test_heartbeat_lost_during_an_open_l2_incident_opens_a_cloud_request(self, rig):
        from sentrix_portal.models import AssistanceRequest
        from sentrix_portal.services import sos as portal_sos
        connect(rig)
        incident = str(uuid.uuid4())
        rig.state.update(alarm={"level": 2, "incident_id": incident, "incident_type": "weapon"})
        wait_for(lambda: (row_hb := device_heartbeat(rig)) and row_hb.get("alarm", {})
                 .get("level") == 2, what="L2 heartbeat")
        self._offline(rig)
        with rig.app.state.db.session() as db:
            row = db.query(AssistanceRequest).filter_by(device_id=rig.seeded.device_id).one()
            assert row.state == "PENDING" and row.authority_side == "cloud"
            assert row.flags["device_offline"] and row.incident_ids == [incident]
            assert portal_sos.verify_chains(db, row.id)["origins"] == {"cloud": 3}
            names = [e["event"] for e in portal_sos.events_view(db, row.id, "desk")]
            assert names == ["created", "prompted", "contacts_notified"]
            assert "went offline" in row.sos_text_full

    def test_cloud_timers_remind_warn_and_expire_unanswered(self, rig):
        from sentrix_portal.models import AssistanceRequest, utcnow
        from sentrix_portal.services import sos as portal_sos
        connect(rig)
        request = rig.sos.open(window=1800)
        wait_for(lambda: (r := mirror(rig.app, request["id"])) and r.state == "PENDING",
                 what="PENDING")
        self._offline(rig)
        with rig.app.state.db.session() as db:
            base = portal_sos._aware(db.get(AssistanceRequest, request["id"]).created_at)
            assert portal_sos.run_timers(db, now=base + timedelta(seconds=130)) == 1
            assert portal_sos.run_timers(db, now=base + timedelta(seconds=130)) == 0
            portal_sos.run_timers(db, now=base + timedelta(seconds=1550))
            portal_sos.run_timers(db, now=base + timedelta(seconds=1801))
            row = db.get(AssistanceRequest, request["id"])
            assert row.state == "EXPIRED"
        steps = [s for _r, _c, s in rig.outbox.steps("owner")]
        for step in ("r120", "r300", "r600", "r1200", "lapse", "lapsed"):
            assert step in steps, step

    def test_offline_answers_are_applied_by_the_cloud(self, rig):
        from sentrix_portal.services import sos as portal_sos
        connect(rig)
        request = rig.sos.open()
        wait_for(lambda: (r := mirror(rig.app, request["id"])) and r.state == "PENDING",
                 what="PENDING")
        self._offline(rig)
        owner = Actor("owner", id=rig.seeded.owner_id, name="Riya").to_dict()

        def answer(action, **kw):
            async def go():
                with rig.app.state.db.session() as db:
                    return await portal_sos.respond(db, request["id"], action, actor=owner,
                                                    channel="portal", **kw)
            return asyncio.run(go())
        assert answer("checking")["ok"]
        assert answer("decline", pin="000000")["code"] == "pin_invalid"
        assert answer("resolve")["code"] == "forbidden_role"
        approved = answer("approve", authority="MEDICAL")
        assert approved["ok"] and approved["request"]["state"] == "DISPATCHED"
        row = mirror(rig.app, request["id"])
        assert row.authority == "MEDICAL" and row.responded_by == f"portal:{rig.seeded.owner_id}"

    def test_offline_duress_and_the_pin_back_off(self, rig):
        from sentrix_portal.services import sos as portal_sos
        connect(rig)
        first, second = rig.sos.open(), None
        wait_for(lambda: (r := mirror(rig.app, first["id"])) and r.state == "PENDING",
                 what="PENDING")
        self._offline(rig)
        owner = Actor("owner", id=rig.seeded.owner_id).to_dict()

        async def decline(pin, request_id):
            with rig.app.state.db.session() as db:
                return await portal_sos.respond(db, request_id, "decline", actor=owner,
                                                channel="portal", pin=pin)
        coerced = asyncio.run(decline(DURESS_PIN, first["id"]))
        assert coerced["ok"] and coerced["request"]["state"] == "DECLINED"
        row = mirror(rig.app, first["id"])
        assert row.state == "DISPATCHED" and row.flags["duress"]
        assert any(s == "duress" for _r, _c, s in rig.outbox.steps("contact:"))
        for _ in range(5):
            assert asyncio.run(decline("111111", first["id"]))["code"] in ("pin_invalid",
                                                                          "conflict")
        assert asyncio.run(decline(PIN, first["id"]))["code"] == "pin_locked"

    def test_contacts_may_ask_for_help_only_under_safety_defaults(self, rig):
        from sentrix_portal.services import sos as portal_sos
        connect(rig)
        allowed = rig.sos.open(policy={"window_seconds": 1800, "safety_defaults": True,
                                       "contacts": [{"id": rig.seeded.contact_id,
                                                     "consent_status": "CONFIRMED"}]})
        refused = rig.sos.open(policy={"window_seconds": 1800, "safety_defaults": False,
                                       "contacts": [{"id": rig.seeded.contact_id}]})
        for r in (allowed, refused):
            wait_for(lambda r=r: (m := mirror(rig.app, r["id"])) and m.state == "PENDING",
                     what="PENDING")
        self._offline(rig)
        contact = Actor("contact", id=rig.seeded.contact_id, name="Arjun").to_dict()

        async def approve(request_id):
            with rig.app.state.db.session() as db:
                return await portal_sos.respond(db, request_id, "approve", actor=contact,
                                                channel="contact_page")
        assert asyncio.run(approve(refused["id"]))["code"] == "forbidden_role"
        assert asyncio.run(approve(allowed["id"]))["ok"]

    def test_reconnect_syncs_the_cloud_chain_and_hands_authority_back(self, rig):
        from sentrix.storage import requests
        from sentrix_portal.services import sos as portal_sos
        connect(rig)
        request = rig.sos.open()
        wait_for(lambda: (r := mirror(rig.app, request["id"])) and r.state == "PENDING",
                 what="PENDING")
        self._offline(rig)
        owner = Actor("owner", id=rig.seeded.owner_id).to_dict()

        async def approve():
            with rig.app.state.db.session() as db:
                return await portal_sos.respond(db, request["id"], "approve", actor=owner,
                                                channel="portal")
        assert asyncio.run(approve())["ok"]
        rig.service._future = None
        rig.service.start()
        wait_for(lambda: rig.sos.synced and "codes" in rig.sos.synced[0], what="sos.sync")
        sync = rig.sos.synced[0]
        assert sync["request_id"] == request["id"] and sync["cloud_state"] == "DISPATCHED"
        assert sync["authority_side"] == "cloud"
        names = [e["event"] for e in sync["events"]]
        assert names[:3] == ["device_offline", "contacts_notified", "approve"]
        assert "device_online" in names
        assert sync["codes"] == ["ok"] * len(names), sync["codes"]
        assert requests.verify_chain(request["id"])["origins"]["cloud"] == len(names)
        assert requests.get_request(request["id"])["state"] == "DISPATCHED", "help path wins"
        assert mirror(rig.app, request["id"]).authority_side == "device"


def device_heartbeat(rig):
    from sentrix_portal.models import Device
    with rig.app.state.db.session() as db:
        return dict(db.get(Device, rig.seeded.device_id).last_heartbeat or {})


def run_on_new_loop(coro):
    return asyncio.run(coro)


# ── 100 forced disconnects ────────────────────────────────────────────────────
class TestNoEventLost:
    def test_one_hundred_forced_disconnects_lose_no_outbox_event(self, rig):
        from sentrix import link
        from sentrix.storage import link_store
        from sentrix_portal.models import DeviceEvent, IncidentMirror
        connect(rig)
        rig.network._loop = rig.service._loop
        published: list[int] = []
        stop = threading.Event()

        def produce():
            n = 0
            while not stop.is_set() and n < 400:
                seq = link.publish("incident.upsert", {"id": str(uuid.uuid4()),
                                                       "peak_level": 1 + n % 3})
                if seq is not None:
                    published.append(seq)
                n += 1
                time.sleep(0.004)

        producer = threading.Thread(target=produce)
        producer.start()
        rng = random.Random(7)
        drops = 0
        while drops < 100:
            pipes = len(rig.network.pipes)
            rig.network.drop()
            drops += 1
            wait_for(lambda: len(rig.network.pipes) > pipes and
                     rig.network.hub.online(rig.seeded.device_id), timeout=10,
                     what=f"reconnect after drop {drops}")
            time.sleep(rng.uniform(0.0, 0.03))
        stop.set()
        producer.join()
        wait_for(lambda: link_store.depth() == 0, timeout=30, what="outbox drained")
        with rig.app.state.db.session() as db:
            stored = {seq for (seq,) in db.query(DeviceEvent.seq)
                      .filter(DeviceEvent.device_id == rig.seeded.device_id)}
            mirrored = db.query(IncidentMirror).count()
        assert set(published) <= stored, sorted(set(published) - stored)[:10]
        assert mirrored == len(published), "every event applied exactly once"
        assert drops == 100 and len(rig.network.pipes) >= 101
