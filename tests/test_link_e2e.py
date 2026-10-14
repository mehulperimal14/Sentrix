"""T-LINK end to end over real loopback sockets (docs/7 §15.2, the T-LINK part).

The Portal runs under uvicorn on 127.0.0.1 (its own temporary database); the
device runs the real ``LinkService`` with the ``websockets`` client and the
Local Console app in-process (stand-in hardening, fake pipeline, a test SOS
service on the real request store). A browser is ``httpx`` plus a
``websockets`` client. The story:

claim -> connect -> the tunnelled dashboard renders with the /d/{id} base ->
state and frames relayed -> an SOS request is mirrored and prompts the owner
-> the owner approves on the Portal -> the device applies it and the Desk
queue delivers it -> the Desk acknowledges -> the Desk resolves.

Nothing leaves the machine: the socket guard allows loopback only, messages
are captured from ``notify.enqueue``, the send guard is in its dry run.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import httpx
import pytest

from test_link_support import (FakeSosService, Outbox, lc_app, login, make_device,
                               profile_for, run_on, seed_portal, wait_for)


class PortalServer:
    """uvicorn on 127.0.0.1:<free port> in a thread with its own loop."""

    def __init__(self, app) -> None:
        import uvicorn
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0,
                                                    log_level="warning", lifespan="on"))
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True, name="portal-uvicorn")

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.server.serve())

    def __enter__(self) -> "PortalServer":
        self.thread.start()
        wait_for(lambda: self.server.started, timeout=15, what="uvicorn")
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        return self

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def fast_settings():
    from dataclasses import replace
    from sentrix.config import settings
    return replace(settings, link=replace(settings.link, heartbeat_seconds=0.5))


@pytest.fixture
def world(tmp_path, monkeypatch, temp_db):
    from sentrix import services
    from sentrix_portal.app import create_app
    from sentrix_portal.services import accounts, hub, notify
    from sentrix_portal.settings import PortalSettings
    hub.reset()
    monkeypatch.setattr(hub, "_cfg", fast_settings)
    app = create_app(PortalSettings.for_tests(tmp_path / "portal",
                                              desk_number="+919812345699"))
    seeded = seed_portal(app, claimed=False)
    monkeypatch.setattr(accounts, "build_profile",
                        lambda db, household_id, device_id: profile_for(seeded))
    outbox = Outbox()
    monkeypatch.setattr(notify, "enqueue", outbox)
    sos = FakeSosService()
    services.registry.register(sos, "sos")
    with PortalServer(app) as server:
        device = make_device(tmp_path, monkeypatch, connector=None,
                             portal_url=server.url, device_id=seeded.device_id,
                             app=lc_app(monkeypatch))
        identity = json.loads((device.identity_dir / "identity.json").read_text())
        identity["bootstrap_secret"] = seeded.bootstrap_secret
        (device.identity_dir / "identity.json").write_text(json.dumps(identity))
        from sentrix.link.client import websockets_connect
        device.service.connector = websockets_connect
        try:
            yield app, seeded, server, device, sos, outbox
        finally:
            device.service.stop()
    app.state.db.dispose()
    hub.reset()


def mirror_state(app, request_id):
    from sentrix_portal.models import AssistanceRequest
    with app.state.db.session() as db:
        row = db.get(AssistanceRequest, request_id)
        return row.state if row else None


def test_claim_tunnel_relays_sos_desk_end_to_end(world):
    from sentrix import mode
    from sentrix.link import client as link_client
    from sentrix.link import profile
    from sentrix.storage import link_store, requests
    from sentrix_portal.models import AssistanceRequest, Device, utcnow
    from sentrix_portal.services import hub
    app, seeded, server, device, sos, outbox = world
    device_id = seeded.device_id

    # ── 1. the unclaimed device dials in on its bootstrap secret ──────────────
    device.service.start()
    session = wait_for(lambda: hub.current_hub() and hub.current_hub().sessions.get(device_id),
                       timeout=15, what="bootstrap session")
    assert session.auth == "bootstrap" and not hub.is_online(device_id)

    # ── 2. claim (what T-ACCT's flow does) -> token.issue -> bearer reconnect ─
    async def claim():
        with app.state.db.session() as db:
            row = db.get(Device, device_id)
            row.state, row.claimed_at = "CLAIMED", utcnow()
            await hub.issue_token(db, device_id)
    run_on(server.loop, claim())
    wait_for(lambda: hub.is_online(device_id), timeout=15, what="claimed session")
    assert link_client.load_token(device.identity_dir)
    wait_for(lambda: profile.current_version() == 1, what="profile pushed")

    owner_raw, owner_csrf = login(app, seeded.owner_id)
    browser = httpx.Client(base_url=server.url, cookies={"portal_session": owner_raw},
                           timeout=15, follow_redirects=False)

    # ── 3. the tunnelled console ──────────────────────────────────────────────
    page = browser.get(f"/d/{device_id}/dashboard", headers={"accept": "text/html"})
    assert page.status_code == 200, page.text[:300]
    for dom_id in ("incident-badge", "gauge-svg", "level-badge", "live-feed"):
        assert f'id="{dom_id}"' in page.text
    assert "x-frame-options" not in page.headers, "the console's own headers pass through"
    who = browser.get(f"/d/{device_id}/api/test-whoami").json()
    assert who == {"role": "owner", "actor": f"portal:{seeded.owner_id}", "csrf": owner_csrf,
                   "base": f"/d/{device_id}", "cookies": [], "forwarded_for": None}
    moved = browser.get(f"/d/{device_id}/api/test-redirect")
    assert moved.status_code == 302 and moved.headers["location"] == f"/d/{device_id}/dashboard"
    assert "set-cookie" not in moved.headers
    assert browser.get(f"/d/{device_id}/api/health").headers["cache-control"] == "no-store"
    assert browser.post(f"/d/{device_id}/api/mode", json={"armed": True}).status_code == 403
    armed = browser.post(f"/d/{device_id}/api/mode", json={"armed": True},
                         headers={"X-CSRF-Token": owner_csrf})
    assert armed.status_code == 200 and armed.json()["mode"]["armed"] and mode.is_armed()

    def portal_armed():
        with app.state.db.session() as db:
            return db.get(Device, device_id).armed
    wait_for(portal_armed, what="mode.changed mirrored")

    # ── 4. state relay ────────────────────────────────────────────────────────
    from websockets.sync.client import connect as ws_connect
    device.state.update(tci=0.37)
    with ws_connect(f"ws://127.0.0.1:{server.port}/d/{device_id}/ws/threat",
                    additional_headers={"Cookie": f"portal_session={owner_raw}",
                                        "Origin": server.url}, open_timeout=10) as ws:
        deadline = time.monotonic() + 10
        payload = {}
        while time.monotonic() < deadline and payload.get("tci") != 0.37:
            payload = json.loads(ws.recv(timeout=5))
        assert payload.get("tci") == 0.37 and "sos" in payload and "alarm" in payload
        wait_for(lambda: device.state.get()["remote_viewers"] >= 1, what="REMOTE VIEW on the LC")

    # ── 5. video relay ────────────────────────────────────────────────────────
    with browser.stream("GET", f"/d/{device_id}/video") as video:
        assert video.status_code == 200
        assert video.headers["content-type"].startswith("multipart/x-mixed-replace")
        seen = b""
        for chunk in video.iter_bytes():
            seen += chunk
            if seen.count(b"--frame") >= 2:
                break
        assert b"Content-Type: image/jpeg" in seen and b"\xff\xd8" in seen
    assert any(r["kind"] == "video" and r["allowed"] for r in link_store.recent_remote(50))

    # ── 6. an SOS request is mirrored and prompts the owner ───────────────────
    request = sos.open(incident_type="weapon", level=3)
    rid = request["id"]
    wait_for(lambda: mirror_state(app, rid) == "PENDING", timeout=15, what="PENDING mirrored")
    wait_for(lambda: ("owner", "telegram", "t0") in outbox.steps("owner"), what="owner prompt")
    sos_page = browser.get(f"/sos/{rid}")
    assert sos_page.status_code == 200 and "Do you need assistance?" in sos_page.text

    # ── 7. the owner approves on the Portal: the device decides ───────────────
    approved = browser.post(f"/sos/{rid}/approve", data={"csrf_token": owner_csrf,
                                                         "authority": "POLICE"})
    assert approved.status_code == 303 and approved.headers["location"].endswith("m=ok")
    wait_for(lambda: mirror_state(app, rid) == "DISPATCHED", timeout=15,
             what="approve applied on the device, delivered by the Desk queue")
    assert ("respond", rid, "approve", "owner", "portal", "POLICE", False) in sos.calls
    assert any(c[0] == "cloud_event" and c[2] == "delivered" for c in sos.calls)
    assert requests.get_request(rid)["state"] == "DISPATCHED"

    # ── 8. the Desk acknowledges (and may watch: help path + consent) ─────────
    desk_raw, desk_csrf = login(app, seeded.operator_id, totp=True)
    desk = httpx.Client(base_url=server.url, cookies={"portal_session": desk_raw}, timeout=15,
                        follow_redirects=False)
    queue = desk.get("/desk/api/queue").json()
    assert [c["id"] for c in queue["requests"]] == [rid] and queue["unacknowledged"] == 1
    assert desk.get(f"/d/{device_id}/live", headers={"accept": "text/html"}).status_code == 200
    assert desk.get(f"/d/{device_id}/events").status_code in (403, 404), \
        "the Desk sees the live console only"
    ack = desk.post(f"/desk/requests/{rid}/acknowledge", data={"csrf_token": desk_csrf})
    assert ack.status_code == 303 and ack.headers["location"].endswith("m=ok")
    wait_for(lambda: mirror_state(app, rid) == "ACKNOWLEDGED", timeout=15, what="acknowledged")
    with app.state.db.session() as db:
        row = db.get(AssistanceRequest, rid)
        assert row.desk_operator_id == seeded.operator_id and row.desk_acknowledged_at
    wait_for(lambda: ("owner", "telegram", "acknowledged") in outbox.steps("owner"),
             what="owner told")

    # ── 9. the Desk resolves: the outcome becomes the verdict on the device ───
    resolved = desk.post(f"/desk/requests/{rid}/resolve",
                         data={"csrf_token": desk_csrf, "outcome": "help_arrived",
                               "note": "simulated"})
    assert resolved.headers["location"].endswith("m=ok")
    wait_for(lambda: mirror_state(app, rid) == "CLOSED", timeout=15, what="closed")
    device_row = requests.get_request(rid)
    assert device_row["state"] == "CLOSED" and device_row["outcome"] == "help_arrived"
    with app.state.db.session() as db:
        from sentrix_portal.services import sos as portal_sos
        chains = portal_sos.verify_chains(db, rid)
        assert chains["ok"] and chains["origins"]["device"] >= 6
    assert requests.verify_chain(rid)["ok"]

    # ── what the device recorded about the cloud ──────────────────────────────
    rows = link_store.recent_remote(200)
    targets = {r["target"] for r in rows}
    assert {"GET /dashboard", "GET /api/test-whoami", "POST /api/mode", "sos.respond",
            "sos.update"} <= targets
    commands = [r for r in rows if r["kind"] == "command"]
    assert all(r["role"] for r in commands), "every command carries its actor"
    assert all(r["user_id"] for r in commands if r["role"] != "cloud"),         "and a person's commands carry the Portal user id"
    assert {r["role"] for r in commands} >= {"owner", "operator", "cloud"}
    browser.close()
    desk.close()
