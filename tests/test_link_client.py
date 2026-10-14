"""T-LINK: the device link client against the real Portal hub (docs/7 §5).

The device's ``LinkService`` and the Portal's ``LinkHub`` talk over an
in-memory pipe on the service's own loop (``MemoryNetwork``), so the full
protocol runs: bootstrap -> claim -> ``token.issue`` -> bearer reconnect ->
welcome -> profile push -> outbox with acks -> heartbeats -> commands ->
tunnel -> state and frames -> unpair (4003). The device database is
``temp_db``; the Portal has its own; nothing leaves the process.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from sentrix.actor import Actor
from sentrix.link import client as link_client
from sentrix.link import protocol

from test_link_support import (MemoryNetwork, lc_app, make_device,
                               profile_for, run_on, seed_portal, wait_for)

OWNER_ACTOR = Actor("owner", id="u1", name="Riya", step_up_at=time.time()).to_dict()


def fast_settings(**link):
    """The device settings the hub reads, with test-speed link timings."""
    from dataclasses import replace
    from sentrix.config import settings
    return replace(settings, link=replace(settings.link, heartbeat_seconds=0.3, **link))


@pytest.fixture
def portal(portal_app, monkeypatch):
    from sentrix_portal.services import accounts, hub
    hub.reset()
    monkeypatch.setattr(hub, "_cfg", fast_settings)
    seeded = seed_portal(portal_app, claimed=False)
    monkeypatch.setattr(accounts, "build_profile",
                        lambda db, household_id, device_id: profile_for(seeded))
    yield portal_app, seeded
    hub.reset()


@pytest.fixture
def rig(portal, tmp_path, monkeypatch, temp_db):
    app, seeded = portal
    network = MemoryNetwork(app)
    device = make_device(tmp_path, monkeypatch, connector=network, device_id=seeded.device_id,
                         app=lc_app(monkeypatch))
    # the identity's bootstrap secret matches the Portal's digest
    identity = json.loads((device.identity_dir / "identity.json").read_text())
    identity["bootstrap_secret"] = seeded.bootstrap_secret
    (device.identity_dir / "identity.json").write_text(json.dumps(identity))
    device.network = network
    device.app = app
    device.seeded = seeded
    yield device
    device.service.stop()


def device_row(app, device_id):
    from sentrix_portal.models import Device
    with app.state.db.session() as db:
        row = db.get(Device, device_id)
        db.expunge(row)
        return row


def claim(rig):
    """What T-ACCT's claim flow does: CLAIMED, then ``hub.issue_token``."""
    from sentrix_portal.models import Device, utcnow
    from sentrix_portal.services import hub

    async def go():
        with rig.app.state.db.session() as db:
            device = db.get(Device, rig.seeded.device_id)
            device.state = "CLAIMED"
            device.claimed_at = utcnow()
            await hub.issue_token(db, device.id)
    run_on(rig.service._loop, go())


def online(rig):
    from sentrix_portal.services import hub
    return hub.is_online(rig.seeded.device_id)


def claim_offline(rig):
    """Claimed before the device ever connects: its token is already on disk."""
    from sentrix_portal import security
    from sentrix_portal.models import Device
    token = "pre-issued-" + "t" * 40
    link_client.save_token(rig.identity_dir, token)
    with rig.app.state.db.session() as db:
        device = db.get(Device, rig.seeded.device_id)
        device.state, device.token_hash = "CLAIMED", security.token_digest(token)


def start_claimed(rig):
    rig.service.start()
    wait_for(lambda: rig.network.hub.sessions.get(rig.seeded.device_id), what="bootstrap session")
    claim(rig)
    wait_for(lambda: online(rig), what="bearer session")


class TestPairing:
    def test_unclaimed_device_gets_a_limited_session(self, rig):
        from sentrix import link
        from sentrix.storage import link_store
        rig.service.start()
        session = wait_for(lambda: rig.network.hub.sessions.get(rig.seeded.device_id),
                           what="session")
        assert session.auth == "bootstrap" and not session.full
        assert rig.network.headers[0]["Authorization"].startswith("Bootstrap ")
        wait_for(lambda: device_row(rig.app, rig.seeded.device_id).online, what="online")
        row = device_row(rig.app, rig.seeded.device_id)
        assert row.last_hello["serial"] == "SX5-000123" and row.last_hello["auth"] == "bootstrap"
        link.publish("incident.upsert", {"id": "4f1d3b52-2b6e-4a43-9a0a-1c2d3e4f5a6b"})
        time.sleep(0.5)
        assert link_store.depth() == 1, "nothing leaves before the claim"
        assert not online(rig), "an unclaimed device takes no commands"
        assert rig.state.get()["link"]["state"] == "connected"
        assert rig.state.get()["link"]["claimed"] is False

    def test_claim_issues_a_token_and_the_device_reconnects_with_it(self, rig):
        from sentrix.storage import link_store
        from sentrix_portal import security
        start_claimed(rig)
        token = link_client.load_token(rig.identity_dir)
        assert token and len(token) > 30
        assert rig.network.headers[-1]["Authorization"] == f"Bearer {token}"
        row = device_row(rig.app, rig.seeded.device_id)
        assert row.token_hash == security.token_digest(token) and row.token_hash != token
        assert row.last_hello["token_confirmed"] is True
        assert (rig.identity_dir / "token.json").exists()
        wait_for(lambda: link_store.depth() == 0, what="outbox drained")
        assert rig.state.get()["link"]["claimed"] is True

    def test_bootstrap_is_revoked_once_the_token_worked(self, rig):
        start_claimed(rig)
        result = rig.network.hub._authenticate(
            f"Bootstrap {rig.seeded.device_id}:{rig.seeded.bootstrap_secret}")
        assert result is None
        assert rig.network.hub._authenticate("Bearer not-a-token") is None
        assert rig.network.hub._authenticate("Basic abc") is None

    def test_profile_is_pushed_on_connect_and_stored_encrypted(self, rig):
        from sentrix.link import profile
        from sentrix.storage import link_store
        start_claimed(rig)
        wait_for(lambda: profile.current_version() == 1, what="profile stored")
        assert profile.current()["owner"]["name"] == "Riya Sharma (demo)"
        blob = link_store.active_profile()["blob"]
        assert b"Riya" not in blob, "the cache is encrypted"
        wait_for(lambda: device_row(rig.app, rig.seeded.device_id).profile_version_acked == 1,
                 what="profile ack")

    def test_unpair_closes_4003_and_the_device_wipes_token_and_profile(self, rig):
        from sentrix.link import profile
        from sentrix_portal.models import Device
        from sentrix_portal.services import hub
        start_claimed(rig)
        wait_for(lambda: profile.current_version() == 1, what="profile")
        actor = Actor("owner", id=rig.seeded.owner_id, step_up_at=time.time()).to_dict()

        async def go():
            with rig.app.state.db.session() as db:
                return await hub.unpair(db, rig.seeded.device_id, actor)
        assert run_on(rig.service._loop, go())["ok"]
        wait_for(lambda: link_client.load_token(rig.identity_dir) is None, what="token wiped")
        assert profile.current() is None
        assert device_row(rig.app, rig.seeded.device_id).state == "ASSIGNED"
        # the device comes back on its bootstrap secret, unclaimed
        wait_for(lambda: (rig.network.hub.sessions.get(rig.seeded.device_id) or
                          type("x", (), {"auth": None})).auth == "bootstrap",
                 what="bootstrap again")
        with rig.app.state.db.session() as db:
            assert db.get(Device, rig.seeded.device_id).token_hash is None

    def test_a_rejected_token_backs_off_to_the_maximum_then_tries_bootstrap(self, rig):
        link_client.save_token(rig.identity_dir, "stale-token-" + "x" * 30)
        rig.service.start()
        wait_for(lambda: len(rig.network.headers) >= 2, what="second attempt")
        assert rig.network.headers[0]["Authorization"].startswith("Bearer stale-token")
        assert rig.network.headers[1]["Authorization"].startswith("Bootstrap ")
        assert rig.service.reconnects >= 1
        # unclaimed on the bootstrap secret: the stale token is wiped
        wait_for(lambda: link_client.load_token(rig.identity_dir) is None, what="stale wiped")


class TestSession:
    def test_heartbeats_reach_the_portal_and_are_acked(self, rig):
        start_claimed(rig)
        rig.state.update(camera_online=True, ai_hz=5.5, video_fps=29.7)
        wait_for(lambda: (device_row(rig.app, rig.seeded.device_id).last_heartbeat or {})
                 .get("ai_hz") == 5.5, what="heartbeat")
        beat = device_row(rig.app, rig.seeded.device_id).last_heartbeat
        assert beat["camera_online"] is True and "outbox_depth" in beat
        assert set(beat) >= {"alarm", "node", "modem", "channels", "uptime_s", "audio"}

    def test_hello_carries_the_redacted_health(self, rig):
        rig.pipeline.source.name = "capture:rtsp://admin:hunter2@10.0.0.9/s1"
        start_claimed(rig)
        hello = device_row(rig.app, rig.seeded.device_id).last_hello
        assert hello["health"]["camera_source"] == "capture:rtsp://***@10.0.0.9/s1"
        assert "audio" in hello["health"] and "hunter2" not in json.dumps(hello)
        assert hello["outbox"]["tail_seq"] >= 0 and hello["capabilities"]

    def test_outbox_events_are_mirrored_and_acked(self, rig):
        from sentrix import link
        from sentrix.storage import link_store
        from sentrix_portal.models import DeviceEvent, IncidentMirror
        start_claimed(rig)
        incident = "4f1d3b52-2b6e-4a43-9a0a-1c2d3e4f5a6b"
        link.publish("incident.upsert", {"id": incident, "started_at": "2026-10-14T09:00:00+00:00",
                                         "peak_level": 3, "peak_tci": 0.62,
                                         "incident_type": "weapon", "reason": "handgun",
                                         "ongoing": True})
        wait_for(lambda: link_store.depth() == 0, what="acked")
        with rig.app.state.db.session() as db:
            mirror = db.get(IncidentMirror, incident)
            assert mirror.peak_level == 3 and mirror.incident_type == "weapon"
            assert db.query(DeviceEvent).count() >= 1

    def test_sos_events_jump_the_queue(self, rig):
        from sentrix import link
        from sentrix.storage import link_store
        claim_offline(rig)
        for n in range(5):
            link.publish("incident.upsert", {"id": f"4f1d3b52-2b6e-4a43-9a0a-1c2d3e4f5a{n:02d}"})
        sos_seq = link.publish("sos.event", {"request_id": "r", "event": {}},
                               dedupe_key=protocol.sos_dedupe_key("r", "device", 1))
        rig.service.start()
        wait_for(lambda: online(rig), what="online")
        wait_for(lambda: link_store.depth() == 0, what="drained")
        sent = [json.loads(m) for m in rig.network.pipes[-1].device.sent if isinstance(m, str)]
        events = [m for m in sent if m["type"] == "event"]
        assert events[0]["seq"] == sos_seq and events[0]["priority"] == 0
        assert [e["seq"] for e in events[1:]] == sorted(e["seq"] for e in events[1:])

    def test_listeners_turn_writes_into_events(self, rig):
        from sentrix import listeners
        from sentrix.storage import link_store
        start_claimed(rig)
        listeners.emit("mode", {"armed": True, "by": "portal:u1", "at": "x", "source": "test"})
        listeners.emit("audit", {"event": "gallery_enrolled", "person": "Riya",
                                 "file": "riya.jpg", "via": "camera", "by": "lc:device_admin"})
        listeners.emit("audit", {"event": "login", "who": "ignored"})
        listeners.emit("snapshot", {"name": "a.jpg", "incident_id": None,
                                    "created_at": "2026-10-14T09:00:00+00:00"})
        listeners.emit("snapshot", {"name": "a.jpg", "incident_id": None,
                                    "created_at": "2026-10-14T09:00:00+00:00"})
        wait_for(lambda: link_store.depth() == 0, what="acked")
        from sentrix_portal.models import AuditLog, DeviceEvent, SnapshotMirror
        with rig.app.state.db.session() as db:
            kinds = sorted(e.kind for e in db.query(DeviceEvent))
            assert kinds.count("snapshot.meta") == 1, "deduplicated on the device"
            assert "gallery.audit" in kinds and "mode.changed" in kinds
            assert db.query(SnapshotMirror).count() == 1
            events = {a.event for a in db.query(AuditLog)}
            assert {"gallery.audit", "mode.set", "device.online"} <= events

    def test_commands_round_trip_and_queue_while_offline(self, rig):
        from sentrix_portal.models import DeviceCommand
        from sentrix_portal.services import hub
        start_claimed(rig)
        ping = run_on(rig.service._loop, hub.send_command(rig.seeded.device_id, "diag.ping", {},
                                                          OWNER_ACTOR, timeout=5))
        assert ping["ok"] and ping["result"]["pong"] and ping["command_id"]
        refused = run_on(rig.service._loop, hub.send_command(
            rig.seeded.device_id, "siren.test", {}, Actor("member", id="m").to_dict()))
        assert refused["code"] == "forbidden_role" and refused["status"] == 403
        rig.service.stop()
        wait_for(lambda: not online(rig), what="offline")
        queued = asyncio.run(hub.send_command(rig.seeded.device_id, "mode.set",
                                              {"armed": True, "pin": "123456"}, OWNER_ACTOR))
        assert queued["queued"] and queued["code"] == "device_offline"
        with rig.app.state.db.session() as db:
            row = db.get(DeviceCommand, queued["command_id"])
            assert row.status == "QUEUED" and "pin" not in row.args, "never store a PIN"

    def test_queued_commands_go_out_after_the_welcome(self, rig):
        from sentrix import mode
        from sentrix_portal.models import DeviceCommand
        from sentrix_portal.services import hub
        hub.hub_for(rig.app)
        queued = asyncio.run(hub.send_command(rig.seeded.device_id, "mode.set",
                                              {"armed": True}, OWNER_ACTOR))
        assert queued["queued"]
        start_claimed(rig)
        wait_for(lambda: mode.is_armed(), what="queued command applied")

        def done():
            with rig.app.state.db.session() as db:
                return db.get(DeviceCommand, queued["command_id"]).status == "DONE"
        wait_for(done, what="command DONE")

    def test_tunnel_requests_are_served_by_the_console(self, rig):
        from sentrix_portal.services import hub
        start_claimed(rig)
        actor = Actor("owner", id=rig.seeded.owner_id, name="Riya").to_dict()
        reply = run_on(rig.service._loop, rig.network.hub.tunnel(
            rig.seeded.device_id, method="GET", path="/api/test-whoami", query="",
            headers=[["accept", "application/json"]], body=b"", actor=actor,
            base=f"/d/{rig.seeded.device_id}", csrf="c", timeout=5))
        assert reply.status == 200
        assert json.loads(reply.body)["actor"] == f"portal:{rig.seeded.owner_id}"
        big = b"\xff\xd8" + b"y" * (700 * 1024)
        reply = run_on(rig.service._loop, rig.network.hub.tunnel(
            rig.seeded.device_id, method="POST", path="/api/sos/test-echo", query="",
            headers=[["content-type", "image/jpeg"]], body=big, actor=actor,
            base=f"/d/{rig.seeded.device_id}", csrf="c", timeout=5))
        assert reply.status == 200 and json.loads(reply.body)["size"] == len(big)

    def test_large_responses_come_back_in_chunks(self, rig):
        from fastapi.responses import Response
        app = rig.ctx.extras["app"]
        payload = bytes(range(256)) * 4000          # ~1 MB: four BODY chunks

        @app.get("/api/test-large")
        async def large():
            return Response(payload, media_type="application/octet-stream")

        start_claimed(rig)
        actor = Actor("owner", id=rig.seeded.owner_id).to_dict()
        reply = run_on(rig.service._loop, rig.network.hub.tunnel(
            rig.seeded.device_id, method="GET", path="/api/test-large", query="", headers=[],
            body=b"", actor=actor, base=f"/d/{rig.seeded.device_id}", csrf=None, timeout=5))
        assert reply.status == 200 and reply.body == payload

    def test_state_is_pushed_while_subscribed(self, rig):
        from sentrix_portal.services.hub import Viewer
        start_claimed(rig)
        rig.state.update(tci=0.42)
        viewer = Viewer(key="s1", role="owner", actor=OWNER_ACTOR)
        run_on(rig.service._loop, rig.network.hub.add_state_viewer(rig.seeded.device_id, viewer))

        async def fresh():                 # the cached payload may predate the update
            while True:
                payload = await asyncio.wait_for(viewer.queue.get(), 5)
                if payload.get("tci") == 0.42:
                    return payload
        payload = run_on(rig.service._loop, fresh())
        assert "sos" in payload and "link" in payload and "alarm" in payload
        wait_for(lambda: rig.state.get()["remote_viewers"] == 1, what="viewer count on the LC")
        times = []

        async def collect():
            for _ in range(3):
                await asyncio.wait_for(viewer.queue.get(), 5)
                times.append(time.monotonic())
        run_on(rig.service._loop, collect())
        assert times[-1] - times[0] < 2.5, "2 Hz while subscribed"
        run_on(rig.service._loop, rig.network.hub.remove_state_viewer(rig.seeded.device_id,
                                                                     viewer))
        wait_for(lambda: rig.state.get()["remote_viewers"] == 0, what="viewer gone")

    def test_frames_flow_while_a_viewer_watches(self, rig):
        from sentrix.link import profile
        from sentrix_portal.services.hub import Viewer
        start_claimed(rig)
        wait_for(lambda: profile.current_version() == 1, what="profile with consents")
        viewer = Viewer(key="s1", role="owner", actor=OWNER_ACTOR)
        refusal = run_on(rig.service._loop,
                         rig.network.hub.add_video_viewer(rig.seeded.device_id, viewer))
        assert refusal is None

        async def frames(n):
            out = []
            for _ in range(n):
                out.append(await asyncio.wait_for(viewer.queue.get(), 5))
            return out
        got = run_on(rig.service._loop, frames(3))
        assert all(jpeg.startswith(b"\xff\xd8") for _seq, _ts, jpeg in got)
        assert len({seq for seq, _ts, _j in got}) == 3
        assert set(rig.pipeline.jpeg_quality) == {70}, "the MJPEG stream's cached quality"
        session = rig.network.hub.sessions[rig.seeded.device_id]
        assert session.delay_ms is not None
        from sentrix.storage import link_store
        assert any(r["kind"] == "video" and r["allowed"] for r in link_store.recent_remote(20))

    def test_video_is_refused_without_the_consent(self, rig, monkeypatch):
        from sentrix.link import profile
        from sentrix_portal.services import accounts
        from sentrix_portal.services.hub import Viewer
        doc = profile_for(rig.seeded, consents={"remote_view": False, "desk_live_view": False})
        monkeypatch.setattr(accounts, "build_profile", lambda db, h, d: doc)
        start_claimed(rig)
        wait_for(lambda: profile.current_version() == 1, what="profile")
        viewer = Viewer(key="s1", role="owner", actor=OWNER_ACTOR)
        refusal = run_on(rig.service._loop,
                         rig.network.hub.add_video_viewer(rig.seeded.device_id, viewer))
        assert refusal and refusal["code"] == "consent_required"
        assert not rig.network.hub.video_viewers.get(rig.seeded.device_id)

    def test_a_silent_portal_is_left_and_redialled(self, rig, monkeypatch):
        rig.service.cfg = rig.service.cfg.__class__(**{**rig.service.cfg.__dict__,
                                                       "offline_after_seconds": 0.6})
        start_claimed(rig)
        count = len(rig.network.pipes)
        hub = rig.network.hub

        async def mute():                    # the Portal stops answering heartbeats
            async def silent(session, message):
                return None
            hub._on_heartbeat = silent
        run_on(rig.service._loop, mute())
        wait_for(lambda: len(rig.network.pipes) > count, timeout=10, what="redial")
        assert rig.network.pipes[count - 1].code == protocol.CLOSE_GOING_AWAY,             "the device left the silent connection itself"

    def test_reconnect_now_and_api_link(self, rig, monkeypatch):
        from fastapi.testclient import TestClient
        from sentrix import services
        from sentrix.web.security import generate_token
        start_claimed(rig)
        services.registry.register(rig.service, "link")
        monkeypatch.setenv("SENTRIX_PASSWORD", "pw")
        from sentrix.pipeline import state as state_module
        monkeypatch.setattr(state_module, "state", rig.state)
        import sentrix.web.link_routes as link_routes
        monkeypatch.setattr(link_routes, "state", rig.state)
        client = TestClient(rig.ctx.extras["app"])
        client.cookies.set("lc_session", generate_token("pw"))
        body = client.get("/api/link").json()
        assert body["connected"] and body["device_id"] == rig.seeded.device_id
        assert body["service"]["state"] == "connected" and body["remote_viewers"] == 0
        rows = client.get("/api/link/remote-audit?limit=5").json()["rows"]
        assert isinstance(rows, list)
        count = len(rig.network.pipes)
        from sentrix.web import auth
        csrf = {"X-CSRF-Token": auth.csrf_for_session(generate_token("pw"))}
        assert client.post("/api/link/reconnect", headers=csrf).json() == {"status": "reconnecting"}
        wait_for(lambda: len(rig.network.pipes) > count and online(rig), what="redial")
        from sentrix.link import tunnel_auth
        tunnelled = TestClient(rig.ctx.extras["app"])
        headers = tunnel_auth.build_headers(user_id="u1", user_name="R", role="owner",
                                            base=f"/d/{rig.seeded.device_id}")
        refused = tunnelled.post("/api/link/reconnect", headers=headers)
        assert refused.status_code == 403 and refused.json()["code"] == "forbidden_role"
        services.registry._services.pop("link", None)


# ── pieces ────────────────────────────────────────────────────────────────────
class TestPieces:
    def test_link_url(self):
        assert link_client.link_url("https://sentrix.example.in") == \
            "wss://sentrix.example.in/device/v1/link"
        assert link_client.link_url("http://portal.localhost:8100/") == \
            "ws://portal.localhost:8100/device/v1/link"
        assert link_client.link_url("ftp://x") is None and link_client.link_url("") is None

    def test_identity_validation(self, tmp_path):
        assert link_client.DeviceIdentity.load(tmp_path) is None
        (tmp_path / "identity.json").write_text('{"device_id": "dev-1", "bootstrap_secret": "s"}')
        assert link_client.DeviceIdentity.load(tmp_path) is None, "UUID4 required"
        (tmp_path / "identity.json").write_text("{nope")
        assert link_client.DeviceIdentity.load(tmp_path) is None

    def test_token_file_is_private_and_atomic(self, tmp_path):
        link_client.save_token(tmp_path, "t" * 40, "now")
        assert link_client.load_token(tmp_path) == "t" * 40
        assert not list(tmp_path.glob(".*.tmp"))
        assert link_client.wipe_token(tmp_path) and not link_client.wipe_token(tmp_path)

    def test_adaptive_rate(self):
        rate = link_client.AdaptiveRate(2, 15)
        assert rate.interval() == pytest.approx(1 / 15)
        for _ in range(20):
            rate.on_sent(40_000, 0.001)              # a fast uplink: the cap
        assert rate.fps == 15
        for _ in range(30):
            rate.on_sent(40_000, 0.2)                # 200 kB/s: ~4 fps at 80 %
        assert 2 <= rate.fps <= 5
        for _ in range(30):
            rate.on_sent(40_000, 5.0)                # a congested link: the floor
        assert rate.fps == 2 and rate.interval() == pytest.approx(0.5)
        assert rate.interval(requested_fps=1) == pytest.approx(0.5)

    def test_backoff_rules(self, tmp_path):
        from types import SimpleNamespace
        from test_link_support import link_cfg
        service = link_client.LinkService(
            SimpleNamespace(settings=None, state=None, extras={}, registry=None,
                            get_pipeline=lambda: None),
            connector=None, identity_dir=tmp_path,
            cfg=link_cfg(reconnect_min_seconds=1, reconnect_max_seconds=60))

        async def after(code, auth="bearer"):
            return await service._after_close(code, auth, None)
        assert asyncio.run(after(protocol.CLOSE_UNAUTHORISED)) == 60.0
        assert service._force_bootstrap is True
        assert asyncio.run(after(protocol.CLOSE_UNSUPPORTED)) == 60.0
        assert asyncio.run(after(1006)) is None, "the normal full jitter"

    def test_build_service_is_off_by_default_and_preflight(self, tmp_path, monkeypatch):
        from types import SimpleNamespace
        from sentrix.config import settings
        from test_link_support import link_cfg, write_identity
        assert link_client.build_service(SimpleNamespace(settings=settings)) is None
        assert link_client.preflight(settings)[0]["detail"].startswith("off")
        monkeypatch.setenv("SENTRIX_DEVICE_IDENTITY_DIR", str(tmp_path))
        from sentrix.link import profile
        monkeypatch.setattr(profile, "identity_dir", lambda: tmp_path)
        on = SimpleNamespace(link=link_cfg("https://portal.example.in"), test_mode=True)
        rows = link_client.preflight(on)
        assert rows[0]["status"] == "ok" and rows[1]["status"] == "fail"
        write_identity(tmp_path)
        rows = link_client.preflight(on)
        assert rows[1]["status"] == "ok" and "bootstrap only" in rows[1]["detail"]
        bad = SimpleNamespace(link=link_cfg("portal.example.in"), test_mode=True)
        assert link_client.preflight(bad)[0]["status"] == "fail"

    def test_measurement_refuses_a_running_link(self, tmp_path):
        from sentrix import services
        from types import SimpleNamespace
        fake = SimpleNamespace(name="link", start=lambda: None, stop=lambda: None,
                               health=lambda: {})
        services.registry.register(fake)
        services.registry.start_all()
        with pytest.raises(services.MeasurementUnsafe):
            services.assert_measurement_safe()
