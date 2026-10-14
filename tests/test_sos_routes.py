"""/api/sos/* (track T-SOS): thin wrappers over the SOS service, the owner
view everywhere the household looks, CSRF on LC posts, PIN statuses, and the
pre-portal behaviour with the service off."""

from __future__ import annotations

import pytest

from sentrix import services
from sentrix.sos import states as S
from sentrix.storage import requests

from test_sos_engine import (DURESS_PIN, SOS_PIN, Channels, Clock, FakePipeline,  # noqa: E501
                             fire_trigger, make_profile)
from test_sos_engine import STANDALONE

PASSWORD = "correct horse"


def lc_app():
    """The LC routers with what T-WEB's hardening does to tunnel identities."""
    from fastapi import FastAPI
    from sentrix.link import tunnel_auth
    from sentrix.web import extensions, sos_routes

    app = FastAPI()
    app.include_router(sos_routes.router)

    @app.middleware("http")
    async def hardening(request, call_next):
        identity = tunnel_auth.verify(request.headers)
        if identity is not None:
            request.scope[tunnel_auth.SCOPE_KEY] = identity
        return await call_next(request)

    extensions._install_error_handler(app)
    return app


@pytest.fixture
def world(temp_db, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient
    from sentrix.pipeline.state import SystemState
    from sentrix.sos import pins
    from sentrix.sos.service import SosEngine
    from sentrix.web import auth
    from sentrix.web.security import generate_token

    monkeypatch.setenv("SENTRIX_PASSWORD", PASSWORD)
    clock = Clock()
    channels = Channels(clock, allow=set())
    holder = {"profile": make_profile(), "reach": STANDALONE}
    engine = SosEngine(clock=clock, state=SystemState(), channels=lambda: channels,
                       pipeline=lambda: FakePipeline(), profile=lambda: holder["profile"],
                       publish=lambda *a, **k: None, reach=lambda: holder["reach"],
                       limiter=pins.PinLimiter(5, 600, clock=clock), snapshot_dir=tmp_path)
    engine._sweeper.start = lambda: None
    engine.start()
    services.registry.register(engine)
    from sentrix.link import profile as profile_module
    monkeypatch.setattr(profile_module, "effective", lambda: holder["profile"])
    client = TestClient(lc_app())
    cookie = generate_token(PASSWORD)
    client.cookies.set("lc_session", cookie)
    w = SimpleNamespace(engine=engine, clock=clock, client=client, holder=holder,
                        csrf={"X-CSRF-Token": auth.csrf_for_session(cookie)})
    yield w
    engine.stop()


def tunnel(role="owner", user="u1"):
    from sentrix.link import tunnel_auth
    return tunnel_auth.build_headers(user_id=user, user_name="Riya", role=role,
                                     base="/d/dev-1", csrf="portal-csrf")


def open_request(w):
    fire_trigger(w)
    return requests.list_requests(1)[0]


class TestReads:
    def test_signed_out_gets_401_json(self, world):
        from fastapi.testclient import TestClient
        response = TestClient(lc_app()).get("/api/sos")
        assert response.status_code == 401 and response.json()["code"] == "unauthorised"

    def test_current_and_recent(self, world):
        assert world.client.get("/api/sos").json()["current"] is None
        req = open_request(world)
        body = world.client.get("/api/sos").json()
        assert body["current"]["id"] == req["id"] and body["current"]["state"] == S.PENDING
        assert body["recent"][0]["id"] == req["id"]
        assert body["status"]["active"] and body["status"]["actions"][0] == "call_112"
        for hidden in ("session_id", "snapshot_path", "version", "authority_side"):
            assert hidden not in body["current"]
        listing = world.client.get("/api/sos/requests?limit=5&offset=0").json()
        assert [r["id"] for r in listing["requests"]] == [req["id"]]
        assert world.client.get("/api/sos/requests?limit=999").status_code == 422

    def test_one_request_with_its_audit_trail(self, world):
        req = open_request(world)
        body = world.client.get(f"/api/sos/requests/{req['id']}").json()
        assert body["request"]["id"] == req["id"] and body["chain"] == {"ok": True}
        assert [e["event"] for e in body["events"]] == ["created", "prompted"]
        assert world.client.get("/api/sos/requests/nope").status_code == 404
        assert world.client.get("/api/sos/requests/..%2F..").status_code == 404

    def test_preview_from_the_current_profile(self, world):
        body = world.client.get("/api/sos/preview?incident_type=fire").json()
        assert "FIRE" in body["full"] and body["window_seconds"] == 1800
        assert body["buttons"]["approve"] == "Alert my contacts and the SENTRIX desk"
        assert world.client.get("/api/sos/preview?incident_type=assault").status_code == 400
        assert world.client.get("/api/sos/preview?level=2").status_code == 422


class TestAnswers:
    def test_posts_need_the_csrf_header(self, world):
        req = open_request(world)
        refused = world.client.post(f"/api/sos/requests/{req['id']}/approve",
                                    json={"authority": "POLICE"})
        assert refused.status_code == 403 and refused.json()["code"] == "csrf"
        ok = world.client.post(f"/api/sos/requests/{req['id']}/approve",
                               json={"authority": "POLICE"}, headers=world.csrf)
        assert ok.status_code == 200 and ok.json()["request"]["state"] == S.APPROVED
        assert requests.get_request(req["id"])["responded_by"] == "lc:device_admin"

    def test_tunnelled_owner_needs_no_lc_csrf(self, world):
        req = open_request(world)
        response = world.client.post(f"/api/sos/requests/{req['id']}/checking",
                                     headers=tunnel())
        assert response.status_code == 200
        assert requests.get_request(req["id"])["seen_at"]
        events = requests.events(req["id"])
        assert events[-1]["actor_id"] == "portal:u1" and events[-1]["channel"] == "portal"

    def test_pin_statuses(self, world):
        req = open_request(world)
        url = f"/api/sos/requests/{req['id']}/decline"
        wrong = world.client.post(url, json={"pin": "111111"}, headers=world.csrf)
        assert wrong.status_code == 403 and wrong.json()["code"] == "pin_invalid"
        for _ in range(4):
            world.client.post(url, json={"pin": "111111"}, headers=world.csrf)
        locked = world.client.post(url, json={"pin": SOS_PIN}, headers=world.csrf)
        assert locked.status_code == 429 and locked.json()["code"] == "pin_locked"
        assert int(locked.headers["retry-after"]) > 0

    def test_decline_and_duress_look_the_same(self, world):
        first = open_request(world)
        declined = world.client.post(f"/api/sos/requests/{first['id']}/decline",
                                     json={"pin": SOS_PIN}, headers=world.csrf).json()
        world.clock.now += 3600
        second = open_request(world)
        duress = world.client.post(f"/api/sos/requests/{second['id']}/decline",
                                   json={"pin": DURESS_PIN}, headers=world.csrf).json()
        assert declined["message"] == duress["message"] == "Marked as false alarm"
        assert declined["request"]["state"] == duress["request"]["state"] == S.DECLINED
        assert set(declined["request"]["flags"]) == set(duress["request"]["flags"])
        detail = world.client.get(f"/api/sos/requests/{second['id']}").json()
        assert [e["event"] for e in detail["events"]] == ["created", "prompted", "decline"]
        assert "duress" not in str(detail["events"]) and detail["request"]["flags"] == {}
        assert detail["request"]["authority"] is None
        # Even the incident's label reads FALSE (the Events page looks the same).
        from sentrix.storage import database
        labels = {r["id"]: r["verdict"] for r in database.recent_incidents(10)}
        assert labels[first["incident_id"]] == labels[second["incident_id"]] == "FALSE"
        assert world.client.get("/api/sos").json()["current"] is None
        # The Desk (through the tunnel) sees what happened.
        desk = world.client.get(f"/api/sos/requests/{second['id']}",
                                headers=tunnel("operator", "d1")).json()
        assert desk["request"]["flags"]["duress"] and desk["request"]["duress_banner"]
        assert any(e["restricted"] for e in desk["events"])

    def test_a_silent_duress_request_does_not_exist_for_the_household(self, world):
        from sentrix import mode
        from sentrix.actor import Actor
        mode.set_armed(True, by="test")
        assert world.engine.set_mode(False, pin=DURESS_PIN, actor=Actor.device_admin())["ok"]
        silent = requests.list_requests(1)[0]
        assert silent["flags"]["silent"]
        body = world.client.get("/api/sos").json()
        assert body["current"] is None and body["recent"] == []
        assert world.client.get("/api/sos/requests").json()["requests"] == []
        assert world.client.get(f"/api/sos/requests/{silent['id']}").status_code == 404
        desk = world.client.get(f"/api/sos/requests/{silent['id']}",
                                headers=tunnel("operator", "d1"))
        assert desk.status_code == 200 and desk.json()["request"]["flags"]["duress"]

    def test_safe_on_the_help_path(self, world):
        req = open_request(world)
        world.client.post(f"/api/sos/requests/{req['id']}/approve", json={},
                          headers=world.csrf)
        safe = world.client.post(f"/api/sos/requests/{req['id']}/safe",
                                 json={"pin": SOS_PIN}, headers=world.csrf)
        assert safe.status_code == 200
        assert requests.get_request(req["id"])["flags"]["resident_safe"]

    def test_help_now_after_a_decline(self, world):
        req = open_request(world)
        world.client.post(f"/api/sos/requests/{req['id']}/decline", json={"pin": SOS_PIN},
                          headers=world.csrf)
        help_now = world.client.post(f"/api/sos/requests/{req['id']}/help-now",
                                     json={"authority": "MEDICAL"}, headers=world.csrf).json()
        assert help_now["request"]["parent_request_id"] == req["id"]
        assert help_now["request"]["state"] == S.APPROVED
        assert help_now["request"]["authority"] == "MEDICAL"

    def test_the_desk_cannot_answer_for_the_household(self, world):
        req = open_request(world)
        response = world.client.post(f"/api/sos/requests/{req['id']}/approve",
                                     headers=tunnel("operator", "d1"))
        assert response.status_code == 403 and response.json()["code"] == "forbidden_role"

    def test_demo_controls(self, world):
        trigger = world.client.post("/api/sos/demo/trigger", json={"incident_type": "fire"},
                                    headers=world.csrf)
        assert trigger.status_code == 200 and trigger.json()["message"] == "SIMULATED DETECTION"
        assert requests.list_requests(1)[0]["flags"]["simulated"]
        close = world.client.post("/api/sos/demo/close-incident", headers=world.csrf)
        assert close.status_code == 200


class TestServiceOff:
    def test_reads_work_and_answers_say_unsupported(self, temp_db, monkeypatch):
        from fastapi.testclient import TestClient
        from sentrix.web import auth
        from sentrix.web.security import generate_token
        monkeypatch.setenv("SENTRIX_PASSWORD", PASSWORD)
        services.registry.clear()
        client = TestClient(lc_app())
        cookie = generate_token(PASSWORD)
        client.cookies.set("lc_session", cookie)
        csrf = {"X-CSRF-Token": auth.csrf_for_session(cookie)}
        body = client.get("/api/sos").json()
        assert body == {"current": None, "recent": [], "status": body["status"]}
        assert body["status"]["active"] is False
        refused = client.post("/api/sos/requests/abc/approve", json={}, headers=csrf)
        assert refused.status_code == 400 and refused.json()["code"] == "unsupported"
        assert client.get("/api/sos/preview").status_code == 200
        assert client.post("/api/sos/demo/close-incident",
                           headers=csrf).json()["code"] == "unsupported"
