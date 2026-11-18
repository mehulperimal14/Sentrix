"""Device app API (docs/9) through the Portal tunnel, and no secret anywhere.

* The path table (``sentrix/link/policy.py``) classifies every new path:
  owner and member read ``/api/session``, ``/api/state`` and
  ``/api/activity``; the admin lists are LC-only for every role; sign-in and
  Wi-Fi setup are never tunnelled; the Desk keeps exactly its two API paths;
  no page or static file is tunnelled (the device app is the LAN's); the API
  rows are unchanged.
* The real tunnel executor replays requests into the real console app, so the
  path table, the hardening and the routes answer together.
* No response of a new endpoint carries the console password, the session
  cookie or its secret, the bootstrap secret, the setup code's digest, a PIN
  hash, the Portal's CSRF token or a camera credential.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest
from fastapi.testclient import TestClient

from sentrix.actor import Actor
from sentrix.link.policy import DESK_PATHS, TunnelPolicy, classify

from test_device_api_v2_session import BOOTSTRAP, DIGEST, identity, with_consents  # noqa: F401
from test_web_console import (BASE, PASSWORD, add_evidence, console,  # noqa: F401
                              populate, tunnel_client)

NOW = time.time()
OWNER = Actor("owner", id="u1", name="Riya", step_up_at=NOW - 30)
MEMBER = Actor("member", id="m1", name="Dev")
DESK = Actor("operator", id="op1", name="Kartik")
ALL = {"remote_view": True, "face_recognition": True, "desk_live_view": True}

READS = ("/api/session", "/api/state", "/api/activity")
ADMIN_LISTS = ("/api/snapshots", "/api/evidence", "/api/dispatch")
FACE_LIST = "/api/authorized"
NEVER = (("POST", "/api/login"), ("POST", "/api/logout"), ("GET", "/api/setup/wifi"),
         ("POST", "/api/setup/wifi/scan"), ("POST", "/api/setup/wifi/connect"),
         ("GET", "/api/setup"), ("GET", "/api/login"))


def policy(consents=None, help_path=False):
    return TunnelPolicy(media_per_minute=60, consents=lambda: dict(consents or {}),
                        help_path_open=lambda: help_path, clock=lambda: NOW)


# ── the path table ────────────────────────────────────────────────────────────
class TestPathTable:
    @pytest.mark.parametrize("method, path, expected", [
        *[("GET", path, "read_api") for path in READS],
        *[(method, path, "lc_only") for path in ADMIN_LISTS
          for method in ("GET", "HEAD", "POST", "DELETE")],
        *[(method, path, "never") for method, path in NEVER]])
    def test_every_new_path_has_a_class(self, method, path, expected):
        assert classify(method, path) == expected

    def test_existing_rows_are_unchanged(self):
        assert classify("GET", "/api/evidence/verify") == "read_api"
        assert classify("GET", "/api/dispatch/latest") == "read_api"
        assert classify("GET", "/api/incidents") == "read_api"
        assert classify("GET", "/api/metrics") == "read_api"
        assert classify("POST", "/api/dispatch/p/send/POLICE") == "sos_write"
        assert classify("GET", "/api/authorized/preview.jpg") == "media"
        assert classify("POST", "/api/authorized/capture") == "gallery_write"
        assert classify("POST", "/api/authorized/upload") == "gallery_write"
        assert classify("POST", "/authorized/upload") == "gallery_write"
        assert classify("DELETE", "/api/authorized/alice.jpg") == "gallery_delete"
        assert classify("POST", "/api/mode") == "mode"
        assert classify("GET", "/api/recordings") == "record_status"
        assert classify("POST", "/api/recordings/start") == "record"
        assert DESK_PATHS == frozenset({"/api/health", "/api/metrics"})
        for page in ("/", "/dashboard", "/authorized", "/admin/faces", "/static/app/index.html"):
            assert classify("GET", page) == "unknown", page

    def test_owner_and_member_read_the_app_data_the_desk_does_not(self):
        p = policy(ALL, help_path=True)
        for path in READS:
            for actor in (OWNER, MEMBER):
                decision = p.check_request("GET", path, actor)
                assert decision and decision.path_class == "read_api", (actor.type, path)
            refused = p.check_request("GET", path, DESK)
            assert refused.code == "forbidden_role", path
        # Without any consent too: no media in these bodies (only links).
        assert policy({}).check_request("GET", "/api/activity", MEMBER)

    def test_admin_lists_stay_on_the_lan(self):
        p = policy(ALL, help_path=True)
        for path in ADMIN_LISTS:
            for actor in (OWNER, MEMBER, DESK):
                decision = p.check_request("GET", path, actor)
                assert not decision and decision.code == "forbidden_role", (actor.type, path)
                assert decision.path_class == "lc_only" and decision.status == 403

    def test_the_face_list_follows_the_face_pictures(self):
        assert classify("GET", FACE_LIST) == classify("HEAD", FACE_LIST) == "faces"
        p = policy(ALL, help_path=True)
        assert p.check_request("GET", FACE_LIST, OWNER)
        assert p.check_request("GET", FACE_LIST, MEMBER)
        assert p.check_request("GET", FACE_LIST, DESK).code == "forbidden_role"
        no_faces = policy({"remote_view": True})
        assert no_faces.check_request("GET", FACE_LIST, OWNER).code == "consent_required"

    def test_sign_in_and_setup_are_never_tunnelled(self):
        p = policy(ALL, help_path=True)
        for method, path in NEVER:
            for actor in (OWNER, MEMBER, DESK):
                decision = p.check_request(method, path, actor)
                assert decision.code == "not_found" and decision.status == 404, (method, path)


# ── the real executor into the real console ───────────────────────────────────
def tunnel(app, path, method="GET", *, actor=OWNER, consents=ALL, help_path=False,
           body=b"", query=""):
    from sentrix.config import settings
    from sentrix.link.tunnel import TunnelExecutor
    message = {"id": "req-1", "stream": 1, "method": method, "path": path, "query": query,
               "headers": [["accept", "application/json"],
                           ["content-type", "application/json"]],
               "actor": actor.to_dict(), "base": BASE, "csrf": "portal-csrf-token",
               "timeout_ms": 5000, "body_b64": base64.b64encode(body).decode()}

    async def go():
        executor = TunnelExecutor(app, cfg=settings.link,
                                  policy=TunnelPolicy(media_per_minute=60,
                                                      consents=lambda: dict(consents),
                                                      help_path_open=lambda: help_path))
        try:
            return await executor.execute(message, None)
        finally:
            await executor.close()

    return asyncio.run(go())


def body_of(result) -> dict:
    return json.loads(result.body)


class TestThroughTheTunnel:
    def test_the_owner_reads_session_state_and_activity(self, console, monkeypatch):
        with_consents(monkeypatch, remote_view=True)
        populate(console)
        session = tunnel(console.app, "/api/session")
        assert session.status == 200
        body = body_of(session)
        assert body["via"] == "portal" and body["role"] == "owner" and body["base"] == BASE
        assert body["csrf_token"] is None and body["lan_admin"] is False
        assert b"portal-csrf-token" not in session.body
        state = tunnel(console.app, "/api/state", actor=MEMBER)
        assert state.status == 200 and body_of(state)["alarm"]["level"] == 1
        activity = tunnel(console.app, "/api/activity", query="limit=5")
        assert activity.status == 200
        row = body_of(activity)["items"][0]
        assert row["snapshot_url"] == f"{BASE}/media/snapshot/snap_1.jpg"
        image = tunnel(console.app, "/media/snapshot/snap_1.jpg")
        assert image.status == 200, "the link it gave is one the path table serves"

    def test_the_desk_gets_none_of_it(self, console):
        for path in READS + ADMIN_LISTS:
            refused = tunnel(console.app, path, actor=DESK, help_path=True)
            assert refused.status == 403 and body_of(refused)["code"] == "forbidden_role", path
        assert tunnel(console.app, "/api/metrics", actor=DESK, help_path=True).status == 200, \
            "the Desk's own paths still work"

    def test_admin_lists_are_refused_before_the_console(self, console):
        for path in ADMIN_LISTS:
            for actor in (OWNER, MEMBER):
                refused = tunnel(console.app, path, actor=actor)
                assert refused.status == 403, (actor.type, path)
                assert body_of(refused)["code"] == "forbidden_role"

    def test_sign_in_and_setup_are_refused(self, console, monkeypatch):
        from sentrix.web import setup_routes
        monkeypatch.setattr(setup_routes, "setup_mode_enabled", lambda: True)
        login = tunnel(console.app, "/api/login", "POST",
                       body=json.dumps({"password": PASSWORD}).encode())
        assert login.status == 404 and login.header("set-cookie") is None
        assert tunnel(console.app, "/api/logout", "POST").status == 404
        assert tunnel(console.app, "/api/setup/wifi").status == 404
        assert tunnel(console.app, "/api/setup/wifi/scan", "POST",
                      body=b'{"code": "KTRW-BQXL"}').status == 404

    def test_every_tunnelled_request_is_audited(self, console):
        from sentrix.storage import link_store
        assert tunnel(console.app, "/api/session").status == 200
        assert tunnel(console.app, "/api/evidence").status == 403
        rows = link_store.recent_remote(2)
        assert {(r["target"], r["allowed"]) for r in rows} == {
            ("GET /api/session", True), ("GET /api/evidence", False)}

    def test_past_the_path_table_the_routes_still_refuse(self, console):
        """A forged identity that skipped the table (never in production: the
        executor always checks first) still cannot list the admin's data,
        sign in or reach setup mode."""
        owner = tunnel_client(console)
        for path in ADMIN_LISTS:
            assert owner.get(path).status_code == 403, path
        assert owner.post("/api/login", json={"password": PASSWORD}).status_code == 404
        assert owner.get("/api/setup/wifi").status_code == 404


# ── secrets ───────────────────────────────────────────────────────────────────
PIN_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHRzYWx0$pinhashpinhashpinhash"
SESSION_SECRET = "session-secret-for-tests-only-4f1d"


class TestNoSecrets:
    def test_no_response_carries_a_secret(self, console, identity, monkeypatch):
        from sentrix.pipeline.state import state
        monkeypatch.setenv("SESSION_SECRET", SESSION_SECRET)
        monkeypatch.setenv("SENTRIX_SOS_PIN_HASH", PIN_HASH)
        populate(console)
        add_evidence(console, bundle="L3_bundle_2")
        state.update(reason="camera rtsp://admin:hunter2@10.0.0.2/s1 lost",
                     link={"enabled": True, "state": "connected", "connected": True,
                           "claimed": True, "device_id": "d",
                           "portal": "https://dev:linktoken@portal.example/x",
                           "since": None, "last_message_at": None, "outbox_depth": 0,
                           "profile_version": 1})
        anonymous = TestClient(console.app)
        login = anonymous.post("/api/login", json={"password": PASSWORD})
        assert login.status_code == 200
        cookie = anonymous.cookies.get("lc_session")
        csrf = login.json()["csrf_token"]
        secrets = (PASSWORD, cookie, SESSION_SECRET, BOOTSTRAP, DIGEST, PIN_HASH, "hunter2",
                   "linktoken", "portal-csrf-token")
        texts = {"POST /api/login": login.text}
        for path in ("/api/session", "/api/state", "/api/activity", "/api/snapshots",
                     "/api/evidence", "/api/dispatch", "/api/authorized"):
            response = anonymous.get(path)
            assert response.status_code == 200, (path, response.text[:200])
            texts[f"LAN {path}"] = response.text
        owner = tunnel_client(console)
        for path in READS:
            texts[f"owner {path}"] = owner.get(path).text
        texts["POST /api/logout"] = anonymous.post("/api/logout",
                                                   headers={"X-CSRF-Token": csrf}).text
        for where, text in texts.items():
            for secret in secrets:
                assert secret not in text, f"{where} leaks {secret[:12]}..."
        assert csrf and csrf != cookie, "the CSRF token is not the session cookie"
