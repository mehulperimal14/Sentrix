"""Track T-WEB: the console's hardening middleware (docs/7 §5.8, §9.1).

Tunnel identity accepted only with the in-process secret (bridge headers from
the network are stripped), CSRF on every state-changing request of an LC
session, cross-site requests refused, paths the tunnel never carries refused
for tunnel identities, the strict CSP with a per-response nonce and the other
security headers, the websocket Origin check, the ``lc_session`` cookie and
the POST logout.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from test_web_console import (BASE, PASSWORD, console, lc_client,  # noqa: F401 (fixture)
                              tunnel_client)


def _probe_app():
    """A bare app with the hardening and one echo route."""
    from sentrix.link import tunnel_auth
    from sentrix.web import extensions, hardening

    app = FastAPI()

    @app.api_route("/echo", methods=["GET", "POST"])
    async def echo(request: Request):
        identity = tunnel_auth.identity_of(request)
        return {"identity": identity.actor if identity else None,
                "base": tunnel_auth.base_for(request),
                "bridge": sorted(k for k in request.headers.keys()
                                 if k in tunnel_auth.BRIDGE_HEADERS),
                "nonce": request.state.csp_nonce}

    extensions._install_error_handler(app)
    hardening.install(app)
    return app


# ── tunnel identity ───────────────────────────────────────────────────────────
class TestTunnelBridge:
    def test_verified_identity_is_stored_and_bridge_headers_kept(self):
        from sentrix.link import tunnel_auth
        headers = tunnel_auth.build_headers(user_id="u9", user_name="Asha", role="member",
                                            base="/d/dev-9", csrf="c")
        body = TestClient(_probe_app(), client=tunnel_auth.TUNNEL_CLIENT).get(
            "/echo", headers=headers).json()
        assert body["identity"] == "portal:u9" and body["base"] == "/d/dev-9"

    def test_forged_bridge_headers_are_stripped(self):
        from sentrix.link import tunnel_auth
        forged = tunnel_auth.build_headers(user_id="u9", user_name="Asha", role="owner",
                                           base="/d/dev-9", csrf="c")
        forged[tunnel_auth.HEADER_SECRET] = "guessed"
        body = TestClient(_probe_app()).get("/echo", headers=forged).json()
        assert body == {"identity": None, "base": "", "bridge": [], "nonce": body["nonce"]}

    def test_forged_identity_never_reaches_a_console_route(self, console):
        from sentrix.link import tunnel_auth
        forged = tunnel_auth.build_headers(user_id="u9", user_name="x", role="owner", base=BASE)
        forged[tunnel_auth.HEADER_SECRET] = "nope"
        client = TestClient(console.app, follow_redirects=False)
        assert client.get("/api/incidents", headers=forged).status_code == 401
        assert client.get("/events", headers={**forged, "accept": "text/html"}).status_code == 307

    @pytest.mark.parametrize("method,path", [("GET", "/login"), ("POST", "/login"),
                                             ("GET", "/logout"), ("POST", "/logout"),
                                             ("GET", "/setup/wifi"), ("POST", "/setup/wifi"),
                                             ("GET", "/video"), ("GET", "/docs"),
                                             ("GET", "/openapi.json")])
    def test_paths_the_tunnel_never_carries_are_refused(self, console, method, path):
        response = tunnel_client(console).request(method, path)
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"

    def test_the_websocket_is_refused_to_a_tunnel_identity(self, console):
        with pytest.raises(WebSocketDisconnect):
            with tunnel_client(console).websocket_connect("/ws/threat") as socket:
                socket.receive_text()


# ── CSRF ──────────────────────────────────────────────────────────────────────
class TestCsrf:
    def test_lc_session_needs_the_header(self, console):
        client, csrf = lc_client(console)
        refused = client.post("/api/mode", json={"armed": True})
        assert refused.status_code == 403 and refused.json()["code"] == "csrf"
        wrong = client.post("/api/mode", json={"armed": True}, headers={"X-CSRF-Token": "x" * 64})
        assert wrong.status_code == 403
        ok = client.post("/api/mode", json={"armed": True}, headers={"X-CSRF-Token": csrf})
        assert ok.status_code == 200 and ok.json()["mode"]["armed"] is True

    def test_every_state_changing_method_is_checked(self, console):
        (console.faces / "bob.jpg").write_bytes(b"\xff\xd8\xff")
        client, csrf = lc_client(console)
        assert client.delete("/api/authorized/bob.jpg").status_code == 403
        assert (console.faces / "bob.jpg").exists()
        assert client.post("/api/incidents/i/verdict", data={"verdict": "TRUE"}).status_code == 403
        assert client.post("/api/dispatch/p/send/POLICE").status_code == 403
        assert client.post("/logout").status_code == 403
        assert client.delete("/api/authorized/bob.jpg",
                             headers={"X-CSRF-Token": csrf}).status_code == 200

    def test_the_token_is_bound_to_the_session(self, console):
        from sentrix.web import auth
        from sentrix.web.security import generate_token
        client, _ = lc_client(console)
        other = auth.csrf_for_session(generate_token(PASSWORD) + "0")
        assert client.post("/api/mode", json={"armed": True},
                           headers={"X-CSRF-Token": other}).status_code == 403

    def test_cross_site_requests_are_refused_even_with_a_token(self, console):
        client, csrf = lc_client(console)
        for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"},
                        {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
                        {"Referer": "https://evil.example/page"}):
            response = client.post("/api/mode", json={"armed": True},
                                   headers={**headers, "X-CSRF-Token": csrf})
            assert response.status_code == 403, headers
        same = client.post("/api/mode", json={"armed": True},
                           headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin",
                                    "X-CSRF-Token": csrf})
        assert same.status_code == 200

    def test_login_from_another_site_is_refused(self, console):
        client = TestClient(console.app, follow_redirects=False)
        forged = client.post("/login", data={"password": PASSWORD},
                             headers={"Origin": "https://evil.example"})
        assert forged.status_code == 403
        assert client.post("/login", data={"password": PASSWORD},
                           headers={"Origin": "http://testserver"}).status_code == 302

    def test_legacy_cookie_is_not_a_session(self, console):
        # Retired at integration: it used to skip the CSRF header.
        from sentrix.web import auth
        from sentrix.web.security import generate_token
        client = TestClient(console.app)
        cookie = generate_token(PASSWORD)
        client.cookies.set("session_token", cookie)
        assert client.post("/api/mode", json={"armed": True}).status_code == 401
        assert client.post("/api/mode", json={"armed": True},
                           headers={"X-CSRF-Token": auth.csrf_for_session(cookie)}).status_code == 401

    def test_tunnelled_requests_were_checked_by_the_portal(self, console):
        client = tunnel_client(console)
        assert client.post("/api/mode", json={"armed": True}).status_code == 200
        # The browser's origin is the Portal's, never this console's host.
        assert client.post("/api/mode", json={"armed": True},
                           headers={"Origin": "https://portal.example"}).status_code == 200

    def test_signed_out_writes_get_401_not_csrf(self, console):
        response = TestClient(console.app).post("/api/mode", json={"armed": True})
        assert response.status_code == 401 and response.json()["code"] == "unauthorised"


# ── response headers ──────────────────────────────────────────────────────────
class TestHeaders:
    def test_pages_and_apis_carry_the_security_headers(self, console):
        client, _ = lc_client(console)
        for path in ("/dashboard", "/api/incidents", "/login"):
            headers = client.get(path).headers
            csp = headers["content-security-policy"]
            assert "script-src 'self' 'nonce-" in csp and "'unsafe-inline'" not in csp
            assert "style-src 'self' 'nonce-" in csp and "frame-ancestors 'none'" in csp
            assert "object-src 'none'" in csp and "base-uri 'none'" in csp
            assert "connect-src 'self' ws://testserver wss://testserver" in csp
            assert headers["x-frame-options"] == "DENY"
            assert headers["x-content-type-options"] == "nosniff"
            assert headers["referrer-policy"] == "same-origin"
            assert "camera=()" in headers["permissions-policy"]
            assert headers["cache-control"] == "no-store"

    def test_static_files_stay_cacheable(self, console):
        response = lc_client(console)[0].get("/static/css/style.css")
        assert response.status_code == 200 and "no-store" not in response.headers.get(
            "cache-control", "")

    def test_tunnelled_csp_relies_on_self_only(self, console):
        csp = tunnel_client(console).get("/dashboard").headers["content-security-policy"]
        assert "connect-src 'self';" in csp

    def test_csp_can_be_switched_off(self, console, monkeypatch):
        from sentrix.web import hardening
        monkeypatch.setattr(hardening, "csp_enabled", lambda: False)
        headers = lc_client(console)[0].get("/dashboard").headers
        assert "content-security-policy" not in headers and headers["x-frame-options"] == "DENY"

    def test_a_route_keeps_its_own_cache_header(self, console):
        response = lc_client(console)[0].get("/api/authorized/preview.jpg")
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"

    def test_refusals_carry_the_headers_too(self, console):
        refused = lc_client(console)[0].post("/api/mode", json={"armed": True})
        assert refused.status_code == 403 and "content-security-policy" in refused.headers

    def test_the_hardening_is_installed_on_the_console(self):
        from sentrix.app import app
        from sentrix.web.hardening import ConsoleHardening
        assert any(m.cls is ConsoleHardening for m in app.user_middleware)


# ── websocket ─────────────────────────────────────────────────────────────────
class TestWebsocket:
    def test_session_receives_the_state(self, console):
        client, _ = lc_client(console)
        with client.websocket_connect("/ws/threat") as socket:
            payload = socket.receive_json()
        assert payload["level"] == 1 and "sos" in payload

    def test_same_origin_is_accepted_and_a_foreign_origin_refused(self, console):
        client, _ = lc_client(console)
        with client.websocket_connect("/ws/threat",
                                      headers={"Origin": "http://testserver"}) as socket:
            assert "tci" in socket.receive_json()
        with pytest.raises(WebSocketDisconnect) as refused:
            with client.websocket_connect("/ws/threat",
                                          headers={"Origin": "https://evil.example"}) as socket:
                socket.receive_text()
        assert refused.value.code == 1008

    def test_without_a_session_the_page_is_told_to_sign_in(self, console):
        client = TestClient(console.app)
        with client.websocket_connect("/ws/threat") as socket:
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_text()
        assert closed.value.code == 1008

    def test_payload_is_redacted(self, console):
        from sentrix.pipeline.state import state
        state.update(reason="camera rtsp://admin:hunter2@10.0.0.2/s1 lost")
        client, _ = lc_client(console)
        with client.websocket_connect("/ws/threat") as socket:
            payload = socket.receive_json()
        assert "hunter2" not in payload["reason"] and "rtsp://***@10.0.0.2" in payload["reason"]


# ── cookies, login and logout ─────────────────────────────────────────────────
class TestSessionCookies:
    def test_login_sets_lc_session_and_drops_the_legacy_cookie(self, console):
        client = TestClient(console.app, follow_redirects=False)
        client.cookies.set("session_token", "old")
        response = client.post("/login", data={"password": PASSWORD})
        assert response.status_code == 302 and response.headers["location"] == "/dashboard"
        cookies = response.headers.get_list("set-cookie")
        session = next(c for c in cookies if c.startswith("lc_session="))
        assert "HttpOnly" in session and "SameSite=lax" in session
        assert any(c.startswith("session_token=") and "Max-Age=0" in c for c in cookies)

    def test_wrong_password_and_missing_password(self, console, monkeypatch):
        client = TestClient(console.app)
        assert client.post("/login", data={"password": "nope"}).status_code == 401
        monkeypatch.setenv("SENTRIX_PASSWORD", "")
        page = client.get("/login")
        assert "Set SENTRIX_PASSWORD in .env" in page.text

    def test_logout_is_a_post_with_csrf(self, console):
        client, csrf = lc_client(console)
        response = client.post("/logout", headers={"X-CSRF-Token": csrf})
        assert response.status_code == 200 and response.json()["ok"] is True
        cookies = response.headers.get_list("set-cookie")
        assert any(c.startswith("lc_session=") and "Max-Age=0" in c for c in cookies)
        assert any(c.startswith("session_token=") and "Max-Age=0" in c for c in cookies)

    def test_logout_page_needs_a_session(self, console):
        page = TestClient(console.app, follow_redirects=False).get("/logout")
        assert page.status_code == 307 and page.headers["location"] == "/login"

    def test_video_needs_a_session(self, console):
        response = TestClient(console.app, follow_redirects=False).get("/video")
        assert response.status_code == 307 and response.headers["location"] == "/login"
