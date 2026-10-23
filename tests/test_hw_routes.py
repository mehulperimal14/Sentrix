"""LC Node API (T-HW, docs/7 §9.2): ``GET /api/node`` and ``POST
/api/node/siren-test``. A fake pipeline (mock-pin siren, fake camera) stands
in; no lifespan runs, so no camera, model or service starts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from gpiozero.pins.mock import MockFactory

from sentrix.io import actuators, gpio
from sentrix.io.actuators import GpioSiren
from sentrix.pipeline.state import NODE_IDLE, state
from sentrix.web import node_routes

PASSWORD = "node-test-password"


class Events:
    def __init__(self) -> None:
        self.rows = []

    def audit(self, event, **detail):
        self.rows.append((event, detail))


@pytest.fixture
def fake_pipeline(monkeypatch):
    factory = MockFactory(revision=gpio.MOCK_REVISION)
    siren = GpioSiren(pin_factory=factory)
    pipeline = SimpleNamespace(
        siren=siren, events=Events(),
        source=SimpleNamespace(name="picamera:0", online=True,
                               status=lambda: {"name": "picamera:0", "online": True,
                                               "lores_format": "RGB888"}))
    monkeypatch.setattr(node_routes, "_pipeline", lambda: pipeline)
    monkeypatch.delenv("SENTRIX_SIREN_ARMED", raising=False)
    actuators.clear_arming_source()
    yield pipeline
    siren.close()
    factory.reset()
    actuators.clear_arming_source()
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
    def test_idle_block_plus_siren_camera_and_services(self, admin, fake_pipeline):
        body = admin.get("/api/node").json()
        assert set(NODE_IDLE) <= set(body)
        assert body["siren"]["backend"] == "gpio" and body["siren"]["real"] is False
        assert body["siren"]["armed"] is True and body["siren_armed"] is True
        assert body["siren_arming"] == {"armed": True, "source": "flag"}
        assert body["siren_test"] == {"seconds": 1.0, "min_interval_seconds": 60.0,
                                      "retry_after": None}
        assert body["camera"]["lores_format"] == "RGB888"
        assert body["services"] == {"node": "off", "gpio": "off"}

    def test_reports_what_the_services_wrote(self, admin, fake_pipeline):
        from sentrix.io.node import merge_node_block
        merge_node_block(state, cpu_temp_c=51.2, mains=False, door="open",
                         gpio={"leds": {"green": "solid", "amber": "fast", "red": "off"},
                               "buzzer": False, "pir": False, "door": "open", "mains": False,
                               "siren": {"on": False}})
        body = admin.get("/api/node").json()
        assert body["cpu_temp_c"] == 51.2 and body["mains"] is False
        assert body["gpio"]["leds"]["amber"] == "fast"

    def test_needs_a_principal(self, fake_pipeline, monkeypatch):
        from sentrix.app import app
        monkeypatch.setenv("SENTRIX_PASSWORD", PASSWORD)
        anonymous = TestClient(app)
        response = anonymous.get("/api/node")
        assert response.status_code == 401 and response.json()["code"] == "unauthorised"
        assert anonymous.post("/api/node/siren-test").status_code == 401


class TestSirenTestRoute:
    def test_one_second_then_rate_limited(self, admin, fake_pipeline, monkeypatch):
        from sentrix.pipeline import engine
        monkeypatch.setattr(engine, "_pipeline", fake_pipeline)
        first = admin.post("/api/node/siren-test")
        assert first.status_code == 200
        assert first.json()["ok"] and first.json()["seconds"] == 1.0
        assert fake_pipeline.siren.is_on and fake_pipeline.siren.activations == 1
        assert fake_pipeline.events.rows == [("siren_test", {"by": "lc:device_admin",
                                                              "backend": "gpio",
                                                              "seconds": 1.0})]
        again = admin.post("/api/node/siren-test")
        assert again.status_code == 429 and again.json()["code"] == "rate_limited"
        assert 1 <= int(again.headers["Retry-After"]) <= 60
        assert fake_pipeline.siren.activations == 1
        assert admin.get("/api/node").json()["siren_test"]["retry_after"] > 0

    def test_refused_while_the_siren_is_disarmed(self, admin, fake_pipeline, monkeypatch):
        monkeypatch.setenv("SENTRIX_SIREN_ARMED", "0")
        refused = admin.post("/api/node/siren-test")
        assert refused.status_code == 409 and refused.json()["code"] == "conflict"
        assert "disarmed" in refused.json()["detail"]
        assert fake_pipeline.siren.activations == 0
        monkeypatch.setenv("SENTRIX_SIREN_ARMED", "1")
        assert admin.post("/api/node/siren-test").status_code == 200, \
            "a refusal does not use the minute's slot"

    def test_the_arming_key_input_decides(self, admin, fake_pipeline):
        actuators.set_arming_source(lambda: False)
        assert admin.post("/api/node/siren-test").status_code == 409
        assert admin.get("/api/node").json()["siren_arming"] == {"armed": False,
                                                                 "source": "input"}


class TestTunnelledCallers:
    """Who may test is the tunnel path table's rule (sentrix/link/policy.py);
    the route needs only a principal and never restates it."""

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
        client = TestClient(tunnel_app)
        assert client.get("/api/node", headers=headers).status_code == 200
        result = client.post("/api/node/siren-test", headers=headers)
        assert result.status_code == 200 and result.json()["ok"]

    def test_forged_bridge_headers_are_refused(self, tunnel_app, fake_pipeline, monkeypatch):
        from sentrix.link import tunnel_auth
        monkeypatch.setenv("SENTRIX_PASSWORD", PASSWORD)
        forged = {tunnel_auth.HEADER_SECRET: "guess", tunnel_auth.HEADER_ROLE: "owner",
                  tunnel_auth.HEADER_USER_ID: "x", tunnel_auth.HEADER_BASE: "/d/x"}
        response = TestClient(tunnel_app).post("/api/node/siren-test", headers=forged)
        assert response.status_code == 401
        assert fake_pipeline.siren.activations == 0

class TestBuzzerTestRoute:
    """``POST /api/node/buzzer-test``: two short chirps through the running
    GPIO service (mock pins), device admin only."""

    @pytest.fixture
    def gpio_service(self):
        from sentrix import services
        from sentrix.config import settings
        from sentrix.pipeline.state import SystemState

        factory = MockFactory(revision=gpio.MOCK_REVISION)
        rows = Events()
        ctx = SimpleNamespace(settings=SimpleNamespace(gpio=settings.gpio, web=settings.web,
                                                       node=settings.node),
                              state=SystemState(), registry=None, extras={},
                              get_pipeline=lambda: pytest.fail("never build a pipeline"))
        service = gpio.GpioService(ctx, pin_factory=factory, audit=rows.audit,
                                   publish=lambda kind, payload: None,
                                   siren=lambda: None, run_thread=False)
        service.rows = rows
        services.registry.register(service)
        service.start()
        yield service, factory
        service.stop()
        services.registry.clear()
        factory.reset()

    def test_two_chirps_then_a_short_wait(self, admin, gpio_service):
        import time
        service, factory = gpio_service
        first = admin.post("/api/node/buzzer-test")
        assert first.status_code == 200 and first.json()["ok"] and first.json()["pin"] == 26
        buzzer = factory.pin(26)
        deadline = time.monotonic() + 3.0
        while len(buzzer.states) < 5 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert [st.state for st in buzzer.states[:5]] == [False, True, False, True, False]
        assert ("buzzer_test", {"by": "lc:device_admin", "pin": 26,
                                "active_high": True}) in service.rows.rows
        again = admin.post("/api/node/buzzer-test")
        assert again.status_code == 429 and again.json()["code"] == "rate_limited"
        assert int(again.headers["Retry-After"]) >= 1

    def test_without_the_gpio_service(self, admin):
        refused = admin.post("/api/node/buzzer-test")
        assert refused.status_code == 409 and "GPIO service is off" in refused.json()["detail"]

    def test_not_through_the_tunnel(self, gpio_service, monkeypatch):
        from fastapi import FastAPI
        from sentrix.link import tunnel_auth
        from sentrix.web import extensions

        monkeypatch.setenv("SENTRIX_PASSWORD", PASSWORD)
        app = FastAPI()
        app.include_router(node_routes.router)

        @app.middleware("http")
        async def fake_hardening(request, call_next):
            identity = tunnel_auth.verify(request.headers)
            if identity is not None:
                request.scope[tunnel_auth.SCOPE_KEY] = identity
            return await call_next(request)

        extensions._install_error_handler(app)
        headers = tunnel_auth.build_headers(user_id="u7", user_name="Riya", role="owner",
                                            base="/d/dev-1", step_up_at=None, csrf="c")
        response = TestClient(app).post("/api/node/buzzer-test", headers=headers)
        assert response.status_code == 403
        service, factory = gpio_service
        assert len(factory.pin(26).states) == 1, "no chirp"
