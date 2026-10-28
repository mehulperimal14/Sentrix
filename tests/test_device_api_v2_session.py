"""Device app API (docs/9 §4): who is calling, JSON sign-in and sign-out, the
one-shot state snapshot, the error envelope, CSRF and the OpenAPI contract.

The LC app with a fake pipeline, a temporary database and temporary media
(``test_web_console.console``); nothing opens a camera or the network.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from test_web_console import (BASE, PASSWORD, console, lc_client,  # noqa: F401
                              tunnel_client)

DEVICE_ID = "0b6f9d4e-58a1-4c2a-9a34-1f2e3d4c5b6a"
BOOTSTRAP = "bootstrap-secret-never-shown-9f2c"
DIGEST = "d" * 64


def anonymous(console, **kwargs) -> TestClient:
    return TestClient(console.app, **kwargs)


def envelope(response, status: int, code: str) -> dict:
    """The docs/9 error body: ``{"error": {"code", "message", ...}}``."""
    assert response.status_code == status, (response.status_code, response.text[:300])
    body = response.json()
    assert set(body) == {"error"}, body
    assert body["error"]["code"] == code, body
    assert body["error"]["message"], "every error says something to the user"
    return body["error"]


@pytest.fixture
def identity(tmp_path, monkeypatch):
    """A provisioned device: identity.json with its secrets."""
    from sentrix.link import profile
    directory = tmp_path / "identity"
    directory.mkdir()
    (directory / "identity.json").write_text(json.dumps({
        "device_id": DEVICE_ID, "serial": "SX5-000001", "bootstrap_secret": BOOTSTRAP,
        "setup_code_digest": DIGEST, "provisioned_at": "2026-10-20T10:00:00Z"}),
        encoding="utf-8")
    monkeypatch.setattr(profile, "identity_dir", lambda: directory)
    return directory


def with_consents(monkeypatch, **consents):
    from sentrix.link import profile
    base = profile.standalone_profile()
    doc = {**base, "consents": {**base["consents"], **consents}}
    monkeypatch.setattr(profile, "effective", lambda: doc)
    return doc


# ── GET /api/session ──────────────────────────────────────────────────────────
class TestSession:
    def test_signed_out_says_only_what_the_sign_in_screen_needs(self, console, identity):
        response = anonymous(console).get("/api/session")
        assert response.status_code == 200
        body = response.json()
        assert body["signed_in"] is False and body["role"] is None
        assert body["capabilities"] == [] and body["csrf_token"] is None
        for private in ("device", "link", "call", "media", "sos", "user", "claimed"):
            assert body[private] is None, private
        assert body["via"] == "lan" and body["base"] == ""
        assert body["test_mode"] is True and body["password_configured"] is True
        assert body["setup_mode"] is False and body["csrf_header"] == "X-CSRF-Token"
        assert body["time_zone"] == "Asia/Kolkata"
        assert body["server_time"].endswith("Z")
        assert "SX5-000001" not in response.text, "the serial only after sign-in"

    def test_no_password_is_reported(self, console, monkeypatch):
        monkeypatch.delenv("SENTRIX_PASSWORD")
        assert anonymous(console).get("/api/session").json()["password_configured"] is False

    def test_the_device_admin(self, console, identity, monkeypatch):
        from sentrix.config import settings
        from sentrix.web.routes import ROLE_CAPABILITIES
        monkeypatch.setenv("SENTRIX_CAMERA_LOCATION", "Hall")
        client, csrf = lc_client(console)
        response = client.get("/api/session")
        body = response.json()
        assert body["signed_in"] is True and body["role"] == "device_admin"
        assert body["via"] == "lan" and body["lan_admin"] is True and body["base"] == ""
        assert body["csrf_token"] == csrf
        assert body["capabilities"] == sorted(ROLE_CAPABILITIES["device_admin"])
        assert body["user"] == {"id": None, "name": "Device admin"}
        assert body["step_up_at"] is None
        assert body["device"] == {"id": DEVICE_ID, "serial": "SX5-000001", "name": None,
                                  "room": "Hall", "profile": settings.node.profile}
        assert body["claimed"] is False
        assert body["link"] == {"enabled": False, "state": "disabled", "connected": False,
                                "claimed": False, "remote_viewers": 0}
        assert body["media"] == {"snapshots": True, "evidence": True, "faces": True}
        assert body["sos"] == {"enabled": False,
                               "intrusion_requests": settings.sos.intrusion_requests,
                               "demo_close_incident": True, "demo_trigger": True}
        for secret in (BOOTSTRAP, DIGEST, PASSWORD, client.cookies.get("lc_session")):
            assert secret not in response.text

    def test_the_call_button_never_dials_112_in_test_mode(self, console, monkeypatch):
        client, _ = lc_client(console)
        monkeypatch.delenv("SENTRIX_DESK_NUMBER", raising=False)
        call = client.get("/api/session").json()["call"]
        assert call["enabled"] is False and call["href"] is None and call["desk"] is False
        assert "Desk" in call["note"] and "SENTRIX_" not in call["note"]
        monkeypatch.setenv("SENTRIX_DESK_NUMBER", "+919800000001")
        call = client.get("/api/session").json()["call"]
        assert call == {"enabled": True, "href": "tel:+919800000001", "number": "+919800000001",
                        "desk": True,
                        "note": "Test mode: this calls the SENTRIX Desk (+919800000001), not 112."}
        from sentrix.web import routes
        monkeypatch.setattr(routes, "_test_mode", lambda: False)
        live = client.get("/api/session").json()["call"]
        assert live == {"enabled": True, "href": "tel:112", "number": "112", "desk": False,
                        "note": ""}

    def test_a_tunnelled_owner(self, console):
        response = tunnel_client(console).get("/api/session")
        body = response.json()
        assert body["signed_in"] is True and body["role"] == "owner"
        assert body["via"] == "portal" and body["base"] == BASE and body["lan_admin"] is False
        assert body["user"] == {"id": "u-1", "name": "Riya Sharma"}
        assert body["csrf_token"] is None, "the Portal app uses the Portal's own token"
        assert "portal-csrf-token" not in response.text
        assert body["setup_mode"] is False
        assert body["media"] == {"snapshots": False, "evidence": False, "faces": False}, \
            "standalone profile: nothing consented"
        assert body["sos"]["demo_trigger"] is False, "the scripted trigger is the admin's"

    def test_media_follows_the_households_consents(self, console, monkeypatch):
        with_consents(monkeypatch, remote_view=True)
        assert tunnel_client(console).get("/api/session").json()["media"] == {
            "snapshots": True, "evidence": True, "faces": False}
        with_consents(monkeypatch, remote_view=True, face_recognition=True)
        assert tunnel_client(console, role="member").get("/api/session").json()["media"] == {
            "snapshots": True, "evidence": True, "faces": True}
        desk = tunnel_client(console, role="operator").get("/api/session").json()
        assert desk["media"] == {"snapshots": False, "evidence": False, "faces": False}
        assert desk["capabilities"] == []

    def test_step_up_time_and_profile_time_zone(self, console, monkeypatch):
        from sentrix.link import tunnel_auth
        doc = with_consents(monkeypatch)
        doc["time_zone"] = "Asia/Dubai"
        client = TestClient(console.app, client=tunnel_auth.TUNNEL_CLIENT)
        client.headers.update(tunnel_auth.build_headers(
            user_id="u-1", user_name="Riya", role="owner", base=BASE,
            step_up_at=1_760_430_000.0, csrf="c"))
        body = client.get("/api/session").json()
        assert body["step_up_at"] == "2025-10-14T08:20:00Z"
        assert body["time_zone"] == "Asia/Dubai"
        doc["time_zone"] = "../../etc/passwd"
        assert client.get("/api/session").json()["time_zone"] == "Asia/Kolkata"

    def test_setup_mode_is_reported_on_the_lan_only(self, console, monkeypatch):
        from sentrix.web import setup_routes
        monkeypatch.setattr(setup_routes, "setup_mode_enabled", lambda: True)
        assert anonymous(console).get("/api/session").json()["setup_mode"] is True
        assert tunnel_client(console).get("/api/session").json()["setup_mode"] is False


# ── POST /api/login, /api/logout ──────────────────────────────────────────────
class TestLogin:
    def test_sign_in_sets_the_html_forms_cookie_and_answers_the_session(self, console):
        from sentrix.web import auth
        from sentrix.web.security import verify_token
        client = anonymous(console)
        response = client.post("/api/login", json={"password": PASSWORD})
        assert response.status_code == 200
        cookie = client.cookies.get("lc_session")
        assert cookie and verify_token(cookie, PASSWORD)
        header = ";".join(response.headers.get_list("set-cookie")).lower()
        assert "httponly" in header and "samesite=lax" in header and "max-age=43200" in header
        assert 'session_token=""' in header, "the pre-M10 cookie is deleted, as /login does"
        body = response.json()
        assert body["signed_in"] is True and body["role"] == "device_admin"
        assert body["csrf_token"] == auth.csrf_for_session(cookie)
        assert PASSWORD not in response.text and cookie not in response.text
        again = client.get("/api/session").json()
        assert again["signed_in"] is True and again["csrf_token"] == body["csrf_token"]

    def test_a_wrong_password(self, console):
        client = anonymous(console)
        response = client.post("/api/login", json={"password": "guess"})
        error = envelope(response, 401, "wrong_password")
        assert "guess" not in response.text and "set-cookie" not in response.headers
        assert error["message"] == "That password is not right."

    def test_validation_names_the_field(self, console):
        client = anonymous(console)
        for body in ({}, {"password": ""}, {"password": 7}):
            error = envelope(client.post("/api/login", json=body), 422, "validation")
            assert set(error["fields"]) == {"password"}, error
        error = envelope(client.post("/api/login", content=b"password=x",
                                     headers={"content-type": "application/json"}),
                         422, "validation")
        assert error["fields"]

    def test_the_rate_limit_is_shared_with_the_html_form(self, console):
        client = anonymous(console, follow_redirects=False)
        for _ in range(4):
            assert client.post("/login", data={"password": "x"}).status_code == 401
        for _ in range(4):
            envelope(client.post("/api/login", json={"password": "x"}), 401, "wrong_password")
        limited = client.post("/api/login", json={"password": PASSWORD})
        error = envelope(limited, 429, "rate_limited")
        assert error["retry_after"] == 300 and limited.headers["retry-after"] == "300"
        assert "lc_session" not in client.cookies

    def test_no_password_configured(self, console, monkeypatch):
        monkeypatch.delenv("SENTRIX_PASSWORD")
        error = envelope(anonymous(console).post("/api/login", json={"password": "admin"}),
                         503, "not_configured")
        assert "SENTRIX_PASSWORD" not in error["message"] and ".env" not in error["message"]

    def test_login_csrf(self, console):
        cross = anonymous(console).post("/api/login", json={"password": PASSWORD},
                                        headers={"Origin": "https://evil.example"})
        assert cross.status_code == 403 and cross.json()["code"] == "csrf"
        client, csrf = lc_client(console)
        assert client.post("/api/login", json={"password": PASSWORD}).status_code == 403, \
            "a signed-in browser sends its CSRF token, as for every POST"
        assert client.post("/api/login", json={"password": PASSWORD},
                           headers={"X-CSRF-Token": csrf}).status_code == 200

    def test_never_through_the_tunnel(self, console):
        owner = tunnel_client(console)
        assert owner.post("/api/login", json={"password": PASSWORD}).status_code == 404
        assert owner.post("/api/logout").status_code == 404
        assert "lc_session" not in owner.cookies


class TestLogout:
    def test_needs_the_csrf_header_and_clears_the_cookie(self, console):
        client = anonymous(console)
        csrf = client.post("/api/login", json={"password": PASSWORD}).json()["csrf_token"]
        refused = client.post("/api/logout")
        assert refused.status_code == 403 and refused.json()["code"] == "csrf"
        response = client.post("/api/logout", headers={"X-CSRF-Token": csrf})
        assert response.status_code == 200 and response.json() == {"ok": True}
        assert 'lc_session=""' in ";".join(response.headers.get_list("set-cookie"))
        assert client.get("/api/session").json()["signed_in"] is False

    def test_signed_out_is_the_same_answer(self, console):
        assert anonymous(console).post("/api/logout").json() == {"ok": True}


# ── GET /api/state ────────────────────────────────────────────────────────────
class TestState:
    def test_is_the_websocket_message(self, console):
        from sentrix.pipeline.state import state
        from sentrix.web.api_models import LiveState
        state.update(reason="camera rtsp://admin:hunter2@10.0.0.2/s1 lost",
                     alarm={"level": 3, "status": "ELEVATED", "since_s": 4.0,
                            "incident_id": "inc-1", "incident_type": "weapon",
                            "headline": "weapon confirmed", "peak_tci": 0.61,
                            "deescalate_in": None})
        client, _ = lc_client(console)
        response = client.get("/api/state")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert response.headers["cache-control"] == "no-store"
        with client.websocket_connect("/ws/threat") as socket:
            message = socket.receive_json()
        assert response.json() == message
        assert "hunter2" not in response.text
        parsed = LiveState.model_validate(response.json())
        assert parsed.alarm.level == 3 and parsed.sos.active is False

    def test_the_idle_state_fits_the_documented_model(self, console):
        from sentrix.web.api_models import LiveState
        body = tunnel_client(console, role="member").get("/api/state").json()
        state = LiveState.model_validate(body)
        assert state.level == 1 and state.mode.armed is False and state.link.enabled is False

    def test_needs_a_principal(self, console):
        envelope(anonymous(console).get("/api/state"), 401, "not_signed_in")
        page = anonymous(console, follow_redirects=False).get(
            "/api/state", headers={"accept": "text/html"})
        assert page.status_code == 401, "an API never redirects to the login page"


# ── the contract ──────────────────────────────────────────────────────────────
NEW_ENDPOINTS = (
    ("get", "/api/session", None, "SessionInfo"),
    ("post", "/api/login", "LoginBody", "SessionInfo"),
    ("post", "/api/logout", None, "OkResponse"),
    ("get", "/api/state", None, "LiveState"),
    ("get", "/api/activity", None, "ActivityPage"),
    ("get", "/api/snapshots", None, "SnapshotPage"),
    ("get", "/api/evidence", None, "EvidencePage"),
    ("get", "/api/dispatch", None, "DispatchPage"),
    ("get", "/api/authorized", None, "FaceGallery"),
    ("get", "/api/setup/wifi", None, "SetupWifiInfo"),
    ("post", "/api/setup/wifi/scan", "SetupCodeBody", "WifiScanResult"),
    ("post", "/api/setup/wifi/connect", "WifiConnectBody", "WifiConnectResult"),
)


class TestContract:
    @pytest.mark.parametrize("method, path, request_model, response_model", NEW_ENDPOINTS)
    def test_openapi_describes_every_new_endpoint(self, console, method, path,
                                                  request_model, response_model):
        spec = console.app.openapi()
        operation = spec["paths"][path][method]
        ok = operation["responses"]["200"]["content"]["application/json"]["schema"]
        assert ok["$ref"].endswith(f"/{response_model}"), ok
        if request_model:
            body = operation["requestBody"]["content"]["application/json"]["schema"]
            assert body["$ref"].endswith(f"/{request_model}"), body
        for status in ("401", "403", "422"):
            schema = operation["responses"][status]["content"]["application/json"]["schema"]
            assert schema["$ref"].endswith("/ApiErrorEnvelope"), (status, schema)
        assert response_model in spec["components"]["schemas"]

    def test_existing_api_errors_keep_their_shape(self, console):
        """Only the new routes use the envelope; the older ones are unchanged."""
        legacy = anonymous(console).get("/api/incidents")
        assert legacy.status_code == 401 and legacy.json() == {"detail": "not signed in",
                                                               "code": "unauthorised"}

    def test_a_forbidden_role_is_forbidden(self, console):
        error = envelope(tunnel_client(console).get("/api/evidence"), 403, "forbidden")
        assert "owner" not in error["message"], "a sentence for the user, not a role dump"

    def test_the_old_pages_still_render(self, console):
        client, _ = lc_client(console)
        for path in ("/dashboard", "/events", "/evidence", "/dispatch", "/alerts",
                     "/authorized", "/sos", "/node"):
            assert client.get(path).status_code == 200, path

    def test_websocket_still_refuses_a_signed_out_page(self, console):
        with anonymous(console).websocket_connect("/ws/threat") as socket:
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_text()
        assert closed.value.code == 1008
