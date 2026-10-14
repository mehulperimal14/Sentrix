"""Track T-CH: ``GET /api/channels`` and ``POST /api/channels/test`` on the LC."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sentrix.alerting import guard
from sentrix.alerting.channels import router as R
from sentrix.alerting.channels.base import SENT, SUPPRESSED, FakeTransport

TEAM = "+919812345610"


def lc_app() -> FastAPI:
    from sentrix.link import tunnel_auth
    from sentrix.web import channel_routes, extensions
    app = FastAPI()
    app.include_router(channel_routes.router)

    @app.middleware("http")
    async def fake_hardening(request, call_next):
        identity = tunnel_auth.verify(request.headers)
        if identity is not None:
            request.scope[tunnel_auth.SCOPE_KEY] = identity
        return await call_next(request)

    extensions._install_error_handler(app)
    return app


@pytest.fixture
def client(monkeypatch):
    from sentrix.web import channel_routes
    from sentrix.web.security import generate_token
    monkeypatch.setenv("SENTRIX_PASSWORD", "pw")
    monkeypatch.setattr(channel_routes, "test_limiter",
                        channel_routes.RateLimiter(max_attempts=3, window_seconds=600))
    test_client = TestClient(lc_app())
    test_client.cookies.set("lc_session", generate_token("pw"))
    return test_client


@pytest.fixture
def service(temp_db, monkeypatch):
    from sentrix.config import settings
    monkeypatch.setenv("ALERT_PHONE_NUMBER", TEAM)
    fake = FakeTransport("twilio_sms")
    built = R.build_device_channels(
        replace(settings, channels=replace(settings.channels, enabled=True)),
        transports={"twilio_sms": fake})
    built.fake = fake
    monkeypatch.setattr(R, "current_service", lambda: built)
    return built


def tunnel_headers(role="owner"):
    from sentrix.link import tunnel_auth
    return tunnel_auth.build_headers(user_id="u1", user_name="Riya", role=role,
                                     step_up_at=None, request_id="t1",
                                     base="/d/0b5e7c1e-0000-4000-8000-000000000001",
                                     csrf="c")


class TestGetChannels:
    def test_needs_a_principal(self):
        assert TestClient(lc_app()).get("/api/channels").status_code in (401, 307)

    def test_without_the_router_the_lifespan_block_is_shown(self, client):
        body = client.get("/api/channels").json()
        assert body["enabled"] is False and body["recent"] == []
        assert set(body) >= {"outbound_ready", "legacy_twilio", "warn", "modem"}

    def test_with_the_router(self, client, service):
        service.enqueue(R.Recipient("owner", "R", "twilio_sms", TEAM), R.Message(
            template="sos.prompt", step="t0", text="x", request_id="r1"))
        service.step()
        body = client.get("/api/channels").json()
        assert body["channels"]["twilio_sms"]["live"] is False
        assert body["recent"][0]["status"] == SUPPRESSED
        assert body["recent"][0]["recipient_masked"] == "+91 98xxxxxx10"
        assert TEAM not in str(body)

    def test_tunnelled_owners_may_read(self, service):
        response = TestClient(lc_app()).get("/api/channels", headers=tunnel_headers())
        assert response.status_code == 200


class TestChannelTest:
    def test_device_admin_only(self, service):
        response = TestClient(lc_app()).post("/api/channels/test", headers=tunnel_headers())
        assert response.status_code == 403 and response.json()["code"] == "forbidden_role"

    def test_router_off_is_unsupported(self, client):
        response = client.post("/api/channels/test")
        assert response.status_code == 400 and response.json()["code"] == "unsupported"

    def test_dry_run_answers_suppressed(self, client, service):
        body = client.post("/api/channels/test").json()
        assert body["status"] == SUPPRESSED and body["channel"] == "twilio_sms"
        assert "dry run" in body["error"] and service.fake.attempts == 0

    def test_live_and_allowlisted_sends_with_the_banner(self, client, service, monkeypatch):
        monkeypatch.setenv("SENTRIX_LIVE_ALERTS", "1")
        monkeypatch.setenv("SENTRIX_TWILIO_ENABLED", "1")
        monkeypatch.setenv("SENTRIX_TEST_RECIPIENTS", TEAM)
        body = client.post("/api/channels/test", json={"channel": "twilio_sms"}).json()
        assert body["status"] == SENT and body["recipient"] == "+91 98xxxxxx10"
        assert service.fake.sent[0][1].text.startswith(guard.TEST_PREFIX_GSM)

    def test_bad_requests(self, client, service, monkeypatch):
        assert client.post("/api/channels/test", content=b"not json",
                           headers={"content-type": "application/json"}).json()["code"] == \
            "invalid_args"
        assert client.post("/api/channels/test", json={"channel": "telegram"}).status_code == 400
        assert client.post("/api/channels/test",
                           json={"channel": "modem_sms"}).status_code == 422
        monkeypatch.delenv("ALERT_PHONE_NUMBER")
        response = client.post("/api/channels/test")
        assert response.status_code == 422 and "owner number" in response.json()["detail"]

    def test_rate_limited(self, client, service):
        for _ in range(3):
            assert client.post("/api/channels/test").status_code == 200
        response = client.post("/api/channels/test")
        assert response.status_code == 429 and response.headers["retry-after"] == "600"


def test_the_router_is_registered_as_an_lc_extension():
    from sentrix.web import extensions
    assert "sentrix.web.channel_routes" in extensions.ROUTER_MODULES
