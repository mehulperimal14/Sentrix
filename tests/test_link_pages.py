"""T-LINK: the Portal's own pages (docs/7 §11): SOS, action links, Desk,
contact pages, and the device proxy while the device is offline.

The device is not connected here, so every answer takes the cloud path
(``services.sos.respond`` applies it and syncs later); the online path is
covered by ``test_link_portal.py`` and ``test_link_e2e.py``. Requests are
seeded as the cloud would open them. Messages are captured, never sent.
"""

from __future__ import annotations

import re
import uuid

import pytest
from fastapi.testclient import TestClient

from sentrix.actor import Actor

from test_link_support import DESK_PHONE, DURESS_PIN, PIN, Outbox, login, seed_portal


@pytest.fixture
def portal(tmp_path, monkeypatch):
    from sentrix_portal.app import create_app
    from sentrix_portal.services import hub, notify
    from sentrix_portal.services import sos as portal_sos
    from sentrix_portal.settings import PortalSettings
    hub.reset()
    portal_sos.reset_pin_limiter()
    app = create_app(PortalSettings.for_tests(tmp_path, desk_number=DESK_PHONE))
    seeded = seed_portal(app, other_household=True)
    outbox = Outbox()
    monkeypatch.setattr(notify, "enqueue", outbox)
    yield app, seeded, outbox
    app.state.db.dispose()
    hub.reset()


def open_request(app, seeded, *, incident_type="weapon", level=3, state="PENDING",
                 snapshot=False):
    """A request as the cloud opens it (device offline)."""
    from sentrix_portal.models import Device, IncidentMirror, SosSnapshot, utcnow
    from sentrix_portal.services import sos as portal_sos
    from sentrix.sos import states as S
    with app.state.db.session() as db:
        device = db.get(Device, seeded.device_id)
        incident = str(uuid.uuid4())
        db.add(IncidentMirror(id=incident, device_id=device.id, household_id=device.household_id,
                              started_at=utcnow(), peak_level=level, incident_type=incident_type,
                              reason="handgun"))
        row = portal_sos.create_cloud_request(db, device, seeded.household_id,
                                              incident_type=incident_type, level=level,
                                              incident_ids=[incident])
        portal_sos._cloud_event(db, row, S.PROMPTED)
        if state != "PENDING":
            portal_sos._cloud_event(db, row, S.APPROVE, actor_type="owner",
                                    actor_id=f"portal:{seeded.owner_id}", channel="portal",
                                    authority="POLICE")
            portal_sos._cloud_event(db, row, S.DELIVERED, detail={"target": "desk"})
        if snapshot:
            db.add(SosSnapshot(request_id=row.id, device_id=device.id, data=b"\xff\xd8jpeg",
                               sha256="x", expires_at=utcnow()))
        return row.id, incident


def client_for(app, user_id, *, totp=False):
    raw, csrf = login(app, user_id, totp=totp)
    client = TestClient(app, follow_redirects=False)
    client.cookies.set("portal_session", raw)
    return client, csrf


def state_of(app, request_id):
    from sentrix_portal.models import AssistanceRequest
    with app.state.db.session() as db:
        return db.get(AssistanceRequest, request_id).state


def digest(app, raw: str) -> str:
    from sentrix_portal import security
    return security.token_digest(raw)


def no_inline_script(html: str) -> bool:
    scripts = re.findall(r"<script\b[^>]*>", html)
    return (all("src=" in tag and "nonce=" in tag for tag in scripts)
            and not re.search(r"\son[a-z]+=", html))


# ── owner SOS pages ───────────────────────────────────────────────────────────
class TestSosPages:
    def test_access_rules(self, portal):
        app, seeded, _ = portal
        rid, _ = open_request(app, seeded)
        anonymous = TestClient(app, follow_redirects=False)
        page = anonymous.get("/sos", headers={"accept": "text/html"})
        assert page.status_code == 303 and page.headers["location"] == "/login"
        assert anonymous.get("/sos/api/open").status_code == 401
        desk, _ = client_for(app, seeded.operator_id, totp=True)
        assert desk.get("/sos").status_code == 403
        stranger, _ = client_for(app, seeded.extra["stranger_id"])
        assert stranger.get(f"/sos/{rid}").status_code == 404
        assert stranger.get(f"/sos/api/requests/{rid}").status_code == 404
        assert stranger.get("/sos/api/open").json() == {"requests": []}

    def test_the_prompt_page(self, portal):
        from sentrix_portal.models import AssistanceRequest
        app, seeded, _ = portal
        rid, _ = open_request(app, seeded, snapshot=True)
        owner, _ = client_for(app, seeded.owner_id)
        page = owner.get(f"/sos/{rid}")
        assert page.status_code == 200
        html = page.text
        assert "Do you need assistance?" in html
        assert "Alert my contacts and the SENTRIX desk" in html
        assert "I'm checking" in html and "False alarm (PIN)" in html
        assert f'href="tel:{DESK_PHONE}"' in html and "tel:112" not in html, \
            "test mode: the Desk number, never 112"
        assert "SENTRIX does not contact 112 itself." in html
        assert f'src="/sos/{rid}/snapshot.jpg"' in html
        assert no_inline_script(html)
        assert "content-security-policy" in page.headers
        with app.state.db.session() as db:
            assert db.get(AssistanceRequest, rid).seen_at is not None, "opening it is 'seen'"
        assert owner.get(f"/sos/{rid}/snapshot.jpg").content == b"\xff\xd8jpeg"
        listing = owner.get("/sos").text
        assert f'href="/sos/{rid}"' in listing
        banner = owner.get("/sos/api/open").json()["requests"]
        assert [r["id"] for r in banner] == [rid] and banner[0]["state"] == "PENDING"

    def test_answers_need_csrf_and_take_the_cloud_path_offline(self, portal):
        app, seeded, _ = portal
        rid, _ = open_request(app, seeded)
        owner, csrf = client_for(app, seeded.owner_id)
        assert owner.post(f"/sos/{rid}/approve", data={"authority": "POLICE"}).status_code == 403
        checking = owner.post(f"/sos/{rid}/checking", data={"csrf_token": csrf})
        assert checking.status_code == 303 and checking.headers["location"] == f"/sos/{rid}?m=ok"
        approved = owner.post(f"/sos/{rid}/approve",
                              data={"csrf_token": csrf, "authority": "FIRE"},
                              headers={"x-requested-with": "fetch"})
        assert approved.status_code == 200 and approved.json()["request"]["state"] == "DISPATCHED"
        assert state_of(app, rid) == "DISPATCHED"
        assert owner.post(f"/sos/{rid}/dance", data={"csrf_token": csrf}).status_code == 404

    def test_decline_with_the_pin_and_duress(self, portal):
        app, seeded, outbox = portal
        rid, _ = open_request(app, seeded)
        member, csrf = client_for(app, seeded.member_id)
        wrong = member.post(f"/sos/{rid}/decline", data={"csrf_token": csrf, "pin": "000000"})
        assert wrong.headers["location"] == f"/sos/{rid}?m=pin_invalid"
        assert "That PIN is not right." in member.get(wrong.headers["location"]).text
        coerced = member.post(f"/sos/{rid}/decline", data={"csrf_token": csrf,
                                                           "pin": DURESS_PIN})
        assert coerced.headers["location"] == f"/sos/{rid}?m=ok"
        page = member.get(f"/sos/{rid}").text
        assert "Marked as false alarm" in page and "duress" not in page.lower()
        api = member.get(f"/sos/api/requests/{rid}").json()
        assert api["request"]["state"] == "DECLINED" and "duress" not in api["request"]["flags"]
        assert all(e["event"] != "approve" for e in api["events"]), "the restricted row is hidden"
        assert state_of(app, rid) == "DISPATCHED", "help is on its way"
        rid2, _ = open_request(app, seeded)
        declined = member.post(f"/sos/{rid2}/decline", data={"csrf_token": csrf, "pin": PIN})
        assert declined.headers["location"] == f"/sos/{rid2}?m=ok"
        assert state_of(app, rid2) == "DECLINED"

    def test_request_help_now_opens_a_linked_request(self, portal):
        from sentrix_portal.models import AssistanceRequest
        app, seeded, _ = portal
        rid, _ = open_request(app, seeded)
        owner, csrf = client_for(app, seeded.owner_id)
        owner.post(f"/sos/{rid}/decline", data={"csrf_token": csrf, "pin": PIN})
        later = owner.post(f"/sos/{rid}/help_now", data={"csrf_token": csrf})
        new_id = later.headers["location"].split("/sos/")[1].split("?")[0]
        assert new_id != rid
        with app.state.db.session() as db:
            new = db.get(AssistanceRequest, new_id)
            assert new.parent_request_id == rid and new.state == "DISPATCHED"


# ── action links (/a/) ────────────────────────────────────────────────────────
class TestActionLinks:
    def _token(self, app, rid, user_id):
        from sentrix_portal.models import AssistanceRequest
        from sentrix_portal.services import sos as portal_sos
        with app.state.db.session() as db:
            raw, _ = portal_sos.mint_token(db, purpose="action_link",
                                           row=db.get(AssistanceRequest, rid),
                                           subject_type="user", subject_id=user_id)
            return raw

    def test_get_only_confirms_and_post_acts_once(self, portal):
        from sentrix_portal.models import ActionToken, AuditLog
        app, seeded, _ = portal
        rid, _ = open_request(app, seeded)
        raw = self._token(app, rid, seeded.owner_id)
        browser = TestClient(app, follow_redirects=False)
        page = browser.get(f"/a/{raw}")
        assert page.status_code == 200 and "Alert my contacts and the SENTRIX desk" in page.text
        assert "False alarm" in page.text and 'action="/a/' in page.text
        assert state_of(app, rid) == "PENDING", "a link scanner's GET changes nothing"
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        assert browser.post(f"/a/{raw}", data={"action": "approve"}).status_code == 403
        done = browser.post(f"/a/{raw}", data={"action": "approve", "authority": "POLICE",
                                               "csrf_token": csrf})
        assert done.status_code == 200 and "Thank you" in done.text
        assert state_of(app, rid) == "DISPATCHED"
        again = browser.post(f"/a/{raw}", data={"action": "approve", "csrf_token": csrf})
        assert again.status_code == 404, "single use"
        with app.state.db.session() as db:
            token = db.query(ActionToken).filter_by(token_hash=digest(app, raw)).one()
            assert token.used_at is not None and token.opened_count == 1
            events = {a.event for a in db.query(AuditLog)}
            assert {"action_link.opened", "action_link.used"} <= events

    def test_a_link_cannot_decline_and_bad_tokens_are_404(self, portal):
        app, seeded, _ = portal
        rid, _ = open_request(app, seeded)
        raw = self._token(app, rid, seeded.owner_id)
        browser = TestClient(app, follow_redirects=False)
        page = browser.get(f"/a/{raw}")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        assert browser.post(f"/a/{raw}", data={"action": "decline",
                                               "csrf_token": csrf}).status_code == 400
        assert browser.get("/a/not-a-token").status_code == 404


# ── the Desk ──────────────────────────────────────────────────────────────────
class TestDesk:
    def test_operator_with_totp_only(self, portal):
        app, seeded, _ = portal
        no_totp, _ = client_for(app, seeded.operator_id, totp=False)
        page = no_totp.get("/desk", headers={"accept": "text/html"})
        assert page.status_code == 303 and page.headers["location"] == "/account/2fa"
        owner, _ = client_for(app, seeded.owner_id)
        assert owner.get("/desk").status_code == 403

    def test_queue_only_shows_the_help_path(self, portal):
        app, seeded, _ = portal
        pending, _ = open_request(app, seeded)
        approved, _ = open_request(app, seeded, state="DISPATCHED")
        desk, _ = client_for(app, seeded.operator_id, totp=True)
        queue = desk.get("/desk/api/queue").json()
        assert [c["id"] for c in queue["requests"]] == [approved]
        assert queue["unacknowledged"] == 1 and queue["requests"][0]["city"] == "Patiala"
        assert desk.get(f"/desk/requests/{pending}").status_code == 404
        html = desk.get("/desk").text
        assert f'href="/desk/requests/{approved}"' in html and no_inline_script(html)
        assert '/static/desk/desk.js' in html

    def test_card_acknowledge_called_112_resolve(self, portal):
        from sentrix_portal.models import AssistanceRequest, AuditLog, IncidentMirror
        app, seeded, outbox = portal
        rid, incident = open_request(app, seeded, state="DISPATCHED", snapshot=True)
        desk, csrf = client_for(app, seeded.operator_id, totp=True)
        card = desk.get(f"/desk/requests/{rid}").text
        assert "H.No. 212" in card and "maps.google.com/?q=30.3398,76.3869" in card
        assert "Riya Sharma (demo)" in card and 'href="tel:+919812345610"' in card
        assert f"/desk/requests/{rid}/snapshot.jpg" in card
        assert f"/d/{seeded.device_id}/live" not in card, "the device is offline"
        assert desk.post(f"/desk/requests/{rid}/acknowledge").status_code == 403
        ack = desk.post(f"/desk/requests/{rid}/acknowledge", data={"csrf_token": csrf})
        assert ack.headers["location"] == f"/desk/requests/{rid}?m=ok"
        note = desk.post(f"/desk/requests/{rid}/called-112", data={"csrf_token": csrf})
        assert note.status_code == 303
        bad = desk.post(f"/desk/requests/{rid}/resolve", data={"csrf_token": csrf,
                                                               "outcome": "shrug"})
        assert bad.headers["location"].endswith("m=invalid_args")
        done = desk.post(f"/desk/requests/{rid}/resolve",
                         data={"csrf_token": csrf, "outcome": "help_arrived", "note": "ok"})
        assert done.headers["location"].endswith("m=ok")
        with app.state.db.session() as db:
            row = db.get(AssistanceRequest, rid)
            assert row.state == "CLOSED" and row.outcome == "help_arrived"
            assert row.desk_operator_id == seeded.operator_id and row.desk_acknowledged_at
            assert db.get(IncidentMirror, incident).verdict == "TRUE"
            events = {a.event for a in db.query(AuditLog)}
            assert {"desk.acknowledged", "desk.resolved", "desk.note"} <= events
        assert ("owner", "telegram", "acknowledged") in outbox.steps("owner")
        assert ("owner", "email", "resolved") in outbox.steps("owner")
        assert desk.get(f"/desk/requests/{rid}").status_code == 200, "closed: visible 2 h"

    def test_duress_banner_and_no_call_resident(self, portal):
        from sentrix_portal.models import AssistanceRequest
        app, seeded, _ = portal
        rid, _ = open_request(app, seeded, state="DISPATCHED")
        with app.state.db.session() as db:
            row = db.get(AssistanceRequest, rid)
            row.flags = {**row.flags, "duress": True}
        desk, _ = client_for(app, seeded.operator_id, totp=True)
        card = desk.get(f"/desk/requests/{rid}").text
        assert "Do not go to the home. Call 112." in card
        assert "Call resident" not in card


# ── contact pages (/c/) ───────────────────────────────────────────────────────
class TestContactPages:
    def _token(self, app, rid, contact_id):
        from sentrix_portal.models import AssistanceRequest
        from sentrix_portal.services import sos as portal_sos
        with app.state.db.session() as db:
            raw, _ = portal_sos.mint_token(db, purpose="contact_page",
                                           row=db.get(AssistanceRequest, rid),
                                           subject_type="contact", subject_id=contact_id)
            return raw

    def test_view_open_logged_and_handling_it(self, portal):
        from sentrix_portal.models import ActionToken, AuditLog
        app, seeded, _ = portal
        rid, _ = open_request(app, seeded, state="DISPATCHED", snapshot=True)
        raw = self._token(app, rid, seeded.contact_id)
        browser = TestClient(app, follow_redirects=False)
        page = browser.get(f"/c/{raw}")
        assert page.status_code == 200
        html = page.text
        assert "Arjun (demo)" in html and "H.No. 212" in html and "I'm handling it" in html
        assert f'href="tel:{DESK_PHONE}"' in html and "tel:112" not in html
        assert f'src="/c/{raw}/snapshot.jpg"' in html and no_inline_script(html)
        assert page.headers["referrer-policy"] == "no-referrer"
        assert browser.get(f"/c/{raw}/snapshot.jpg").content == b"\xff\xd8jpeg"
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        acted = browser.post(f"/c/{raw}", data={"action": "acknowledge", "csrf_token": csrf})
        assert acted.headers["location"] == f"/c/{raw}?m=ok"
        assert state_of(app, rid) == "ACKNOWLEDGED"
        with app.state.db.session() as db:
            assert db.query(ActionToken).filter_by(
                token_hash=digest(app, raw)).one().opened_count == 1
            assert db.query(AuditLog).filter_by(event="contact_page.opened").count() == 1

    def test_ask_for_help_only_under_safety_defaults(self, portal):
        app, seeded, _ = portal
        rid, _ = open_request(app, seeded)
        raw = self._token(app, rid, seeded.contact_id)
        browser = TestClient(app, follow_redirects=False)
        page = browser.get(f"/c/{raw}").text
        assert "Alert the SENTRIX desk" in page, "the cloud's policy has safety defaults on"
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        assert browser.post(f"/c/{raw}", data={"action": "decline",
                                               "csrf_token": csrf}).status_code == 400
        browser.post(f"/c/{raw}", data={"action": "approve", "csrf_token": csrf})
        assert state_of(app, rid) == "DISPATCHED"

    def test_decline_revokes_and_close_expires(self, portal):
        from sentrix_portal.models import ActionToken, AssistanceRequest, utcnow
        from sentrix_portal.services import sos as portal_sos
        from datetime import timedelta
        app, seeded, _ = portal
        rid, _ = open_request(app, seeded)
        raw = self._token(app, rid, seeded.contact_id)
        owner, csrf = client_for(app, seeded.owner_id)
        owner.post(f"/sos/{rid}/decline", data={"csrf_token": csrf, "pin": PIN})
        assert TestClient(app).get(f"/c/{raw}").status_code == 404, "revoked on decline"
        rid2, _ = open_request(app, seeded, state="DISPATCHED")
        raw2 = self._token(app, rid2, seeded.contact_id)
        desk, dcsrf = client_for(app, seeded.operator_id, totp=True)
        desk.post(f"/desk/requests/{rid2}/resolve", data={"csrf_token": dcsrf,
                                                          "outcome": "no_action_needed"})
        with app.state.db.session() as db:
            token = db.query(ActionToken).filter_by(token_hash=digest(app, raw2)).one()
            limit = utcnow() + timedelta(seconds=7200)
            assert portal_sos._aware(token.expires_at) <= limit + timedelta(seconds=5)
            token.expires_at = utcnow() - timedelta(seconds=1)
        assert TestClient(app).get(f"/c/{raw2}").status_code == 404
        assert TestClient(app).get("/c/garbage").status_code == 404


# ── the device proxy while the device is offline ──────────────────────────────
class TestOfflineProxy:
    def test_pages_show_the_mirror_and_apis_say_offline(self, portal):
        app, seeded, _ = portal
        rid, incident = open_request(app, seeded)
        owner, csrf = client_for(app, seeded.owner_id)
        page = owner.get(f"/d/{seeded.device_id}/events", headers={"accept": "text/html"})
        assert page.status_code == 503 and "is offline" in page.text
        assert f'data-incident="{incident}"' in page.text and f"/sos/{rid}" in page.text
        assert no_inline_script(page.text)
        api = owner.get(f"/d/{seeded.device_id}/api/health")
        assert api.status_code == 503 and api.json()["code"] == "device_offline"
        assert owner.get(f"/d/{seeded.device_id}/video").status_code == 503
        assert owner.get(f"/d/{seeded.device_id}/login").status_code == 404
        root = owner.get(f"/d/{seeded.device_id}/")
        assert root.headers["location"] == f"/d/{seeded.device_id}/dashboard"

    def test_verdicts_are_queued_without_the_pin(self, portal):
        from sentrix_portal.models import DeviceCommand
        app, seeded, _ = portal
        incident = str(uuid.uuid4())
        owner, csrf = client_for(app, seeded.owner_id)
        url = f"/d/{seeded.device_id}/api/incidents/{incident}/verdict"
        assert owner.post(url, data={"verdict": "FALSE"}).status_code == 403, "Portal CSRF"
        assert owner.post(url, data={"verdict": "FALSE", "csrf_token": csrf}).status_code == 403, \
            "the proxy accepts the header only"
        queued = owner.post(url, data={"verdict": "FALSE", "pin": PIN},
                            headers={"X-CSRF-Token": csrf})
        assert queued.status_code == 202 and queued.json()["status"] == "queued, will sync"
        with app.state.db.session() as db:
            row = db.get(DeviceCommand, queued.json()["command_id"])
            assert row.name == "verdict.set" and row.status == "QUEUED"
            assert row.args["verdict"] == "FALSE" and "pin" not in row.args
            assert row.args["_actor"]["id"] == seeded.owner_id

    def test_tenant_isolation_and_roles(self, portal):
        app, seeded, _ = portal
        stranger, _ = client_for(app, seeded.extra["stranger_id"])
        assert stranger.get(f"/d/{seeded.device_id}/api/health").status_code == 404
        assert stranger.get(f"/d/{uuid.uuid4()}/api/health").status_code == 404
        assert stranger.get("/d/not-a-uuid/api/health").status_code == 404
        desk, _ = client_for(app, seeded.operator_id, totp=True)
        assert desk.get(f"/d/{seeded.device_id}/live").status_code == 403, \
            "the Desk sees a home only while its request is on the help path"
        open_request(app, seeded, state="DISPATCHED")
        assert desk.get(f"/d/{seeded.device_id}/api/health").status_code == 503, \
            "allowed now (and the device is offline)"
        anonymous = TestClient(app, follow_redirects=False)
        bookmark = anonymous.get(f"/d/{seeded.device_id}/dashboard",
                                 headers={"accept": "text/html"})
        assert bookmark.status_code == 303 and bookmark.headers["location"] == "/login"
        assert anonymous.get(f"/d/{seeded.device_id}/api/health").status_code == 401

    def test_the_state_relay_needs_a_session_and_the_same_origin(self, portal):
        from starlette.websockets import WebSocketDisconnect
        app, seeded, _ = portal
        with pytest.raises(WebSocketDisconnect):
            with TestClient(app).websocket_connect(f"/d/{seeded.device_id}/ws/threat") as ws:
                ws.receive_text()
        raw, _ = login(app, seeded.owner_id)
        client = TestClient(app)
        client.cookies.set("portal_session", raw)
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/d/{seeded.device_id}/ws/threat",
                                          headers={"origin": "https://evil.example"}) as ws:
                ws.receive_text()
        with client.websocket_connect(f"/d/{seeded.device_id}/ws/threat",
                                      headers={"origin": "http://testserver"}) as ws:
            first = ws.receive_json()
        assert first["camera_online"] is False and first["ai_hz"] == 0.0
        assert first["link"]["connected"] is False, "offline: the Link dot is red"
