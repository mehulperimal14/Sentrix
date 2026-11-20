"""LC Node API (T-HW, docs/7 §9.2): ``GET /api/node``. A fake pipeline (fake
camera) stands in; no lifespan runs, so no camera, model or service starts.
The device has no siren (D25), buzzer or speaker (D19)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sentrix.pipeline.state import NODE_IDLE, state
from sentrix.web import node_routes

PASSWORD = "node-test-password"


@pytest.fixture
def fake_pipeline(monkeypatch):
    pipeline = SimpleNamespace(
        source=SimpleNamespace(name="picamera:0", online=True,
                               status=lambda: {"name": "picamera:0", "online": True,
                                               "lores_format": "RGB888"}))
    monkeypatch.setattr(node_routes, "_pipeline", lambda: pipeline)
    yield pipeline
    state.update(node=dict(NODE_IDLE))


@pytest.fixture
def admin(monkeypatch):
    from sentrix.app import app
    from sentrix.web import auth
    from sentrix.web.security import generate_token
    monkeypatch.setenv("SENTRIX_PASSWORD", PASSWORD)
    client = TestClient(app)                  # no lifespan: no pipeline, no camera
    cookie = generate_token(PASSWORD)
    client.cookies.set("lc_session", cookie)
    # What every state-changing console fetch sends (T-WEB's hardening checks it).
    client.headers["X-CSRF-Token"] = auth.csrf_for_session(cookie)
    return client


class TestNodeStatus:
    def test_idle_block_plus_camera_and_services(self, admin, fake_pipeline):
        body = admin.get("/api/node").json()
        assert set(NODE_IDLE) <= set(body)
        assert body["camera"]["lores_format"] == "RGB888"
        assert body["services"] == {"node": "off", "gpio": "off"}
        assert body["microphone"] is None, "no audio engine: no microphone"

    def test_reports_the_microphone(self, admin, fake_pipeline):
        mic = {"available": True, "device": "USB PnP Sound Device", "rate": 48000,
               "channels": 1, "last_error": None}
        fake_pipeline.audio = SimpleNamespace(status=lambda: dict(mic))
        assert admin.get("/api/node").json()["microphone"] == mic
        fake_pipeline.audio = SimpleNamespace(status=lambda: 1 / 0)
        assert admin.get("/api/node").json()["microphone"] is None, "a failing engine"

    def test_reports_what_the_services_wrote(self, admin, fake_pipeline):
        from sentrix.io.node import merge_node_block
        merge_node_block(state, cpu_temp_c=51.2, mains=False, door="open",
                         gpio={"leds": {"green": "solid", "amber": "fast", "red": "off"},
                               "pir": False, "door": "open", "mains": False})
        body = admin.get("/api/node").json()
        assert body["cpu_temp_c"] == 51.2 and body["mains"] is False
        assert body["gpio"]["leds"]["amber"] == "fast"

    def test_needs_a_principal(self, fake_pipeline, monkeypatch):
        from sentrix.app import app
        monkeypatch.setenv("SENTRIX_PASSWORD", PASSWORD)
        anonymous = TestClient(app)
        response = anonymous.get("/api/node")
        assert response.status_code == 401 and response.json()["code"] == "unauthorised"


class TestTunnelledCallers:
    """Who may read the node is the tunnel path table's rule
    (sentrix/link/policy.py); the route needs only a principal."""

    @pytest.fixture
    def tunnel_app(self):
        from fastapi import FastAPI
        from sentrix.link import tunnel_auth
        from sentrix.web import extensions

        app = FastAPI()
        app.include_router(node_routes.router)

        @app.middleware("http")
        async def fake_hardening(request, call_next):
            identity = tunnel_auth.verify(request.headers)
            if identity is not None:
                request.scope[tunnel_auth.SCOPE_KEY] = identity
            return await call_next(request)

        extensions._install_error_handler(app)
        return app

    def test_owner_through_the_tunnel(self, tunnel_app, fake_pipeline, monkeypatch):
        from sentrix.link import tunnel_auth
        monkeypatch.setenv("SENTRIX_PASSWORD", PASSWORD)
        headers = tunnel_auth.build_headers(user_id="u7", user_name="Riya", role="owner",
                                            base="/d/dev-1", step_up_at=None, csrf="c")
        assert TestClient(tunnel_app).get("/api/node", headers=headers).status_code == 200

    def test_forged_bridge_headers_are_refused(self, tunnel_app, fake_pipeline, monkeypatch):
        from sentrix.link import tunnel_auth
        monkeypatch.setenv("SENTRIX_PASSWORD", PASSWORD)
        forged = {tunnel_auth.HEADER_SECRET: "guess", tunnel_auth.HEADER_ROLE: "owner",
                  tunnel_auth.HEADER_USER_ID: "x", tunnel_auth.HEADER_BASE: "/d/x"}
        assert TestClient(tunnel_app).get("/api/node", headers=forged).status_code == 401


class TestRemovedHardware:
    """D19: no buzzer and no talk-down speaker; D25: no siren."""

    def test_no_siren_buzzer_or_speaker(self, admin, fake_pipeline):
        assert admin.post("/api/node/siren-test").status_code == 404
        assert admin.post("/api/node/buzzer-test").status_code == 404
        assert admin.get("/api/speaker").status_code == 404
        assert admin.post("/api/speaker/say", json={"text": "hello"}).status_code == 404
        body = admin.get("/api/node").json()
        assert not {"siren", "siren_armed", "siren_arming", "siren_test", "speaker"} & set(body)
        assert not {"siren", "buzzer"} & set(body.get("gpio") or {})
        assert admin.get("/static/sounds/siren.wav").status_code == 404
