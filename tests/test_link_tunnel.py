"""T-LINK: the device's tunnel executor (docs/7 §5.7, §5.8).

Requests are replayed in-process into the real Local Console app
(``sentrix.app.create_app``) with ``httpx.ASGITransport``; T-WEB's hardening
is stood in for by a middleware that does what the contract says (verify the
bridge, store the identity). No pipeline is built: the routes used here do not
need one, and ``/api/health`` reads a fake.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import replace

import pytest

from sentrix.actor import Actor
from sentrix.link.policy import TunnelPolicy
from sentrix.link.tunnel import TunnelExecutor

from test_link_support import FakePipeline, lc_app

DEVICE = "0b6f9d4e-58a1-4c2a-9a34-1f2e3d4c5b6a"
BASE = f"/d/{DEVICE}"
OWNER = Actor("owner", id="u1", name="Riya Sharma", step_up_at=None)
ALL = {"remote_view": True, "face_recognition": True, "desk_live_view": True}


@pytest.fixture
def app(monkeypatch, temp_db):
    from sentrix.pipeline import engine
    monkeypatch.setattr(engine, "_pipeline", FakePipeline())
    return lc_app(monkeypatch)


def executor(app, *, consents=None, **cfg):
    from sentrix.config import settings
    return TunnelExecutor(app, cfg=replace(settings.link, **cfg),
                          policy=TunnelPolicy(media_per_minute=60,
                                              consents=lambda: dict(ALL if consents is None else consents),
                                              help_path_open=lambda: False))


def request(path, method="GET", *, actor=OWNER, body=b"", headers=None, query="", **extra):
    message = {"id": "req-1", "stream": 1, "method": method, "path": path, "query": query,
               "headers": headers or [["accept", "application/json"]],
               "actor": actor.to_dict() if isinstance(actor, Actor) else actor,
               "base": BASE, "csrf": "portal-csrf-token", "timeout_ms": 5000,
               "body_b64": base64.b64encode(body).decode()}
    message.update(extra)
    return message


def run(app, message, body=None, **kw):
    async def go():
        ex = executor(app, **kw)
        try:
            return await ex.execute(message, body)
        finally:
            await ex.close()
    return asyncio.run(go())


class TestTunnelExecutor:
    def test_identity_base_and_csrf_reach_the_console(self, app):
        result = run(app, request("/api/test-whoami",
                                  headers=[["accept", "application/json"],
                                           ["cookie", "lc_session=stolen"],
                                           ["x-forwarded-for", "203.0.113.9"]]))
        assert result.status == 200
        body = json.loads(result.body)
        assert body == {"role": "owner", "actor": "portal:u1", "csrf": "portal-csrf-token",
                        "base": BASE, "cookies": [], "forwarded_for": None}

    def test_forged_bridge_headers_from_the_portal_are_dropped(self, app):
        forged = [["x-sentrix-role", "operator"], ["x-sentrix-user-id", "evil"],
                  ["x-sentrix-tunnel", "guess"], ["x-sentrix-base", "/d/other"]]
        body = json.loads(run(app, request("/api/test-whoami", headers=forged)).body)
        assert body["actor"] == "portal:u1" and body["base"] == BASE

    def test_the_dashboard_renders_through_the_tunnel(self, app):
        result = run(app, request("/dashboard", headers=[["accept", "text/html"]]))
        assert result.status == 200
        assert "text/html" in result.header("content-type")
        html = result.body.decode()
        for dom_id in ("incident-badge", "gauge-svg", "level-badge", "live-feed"):
            assert f'id="{dom_id}"' in html

    def test_cookies_and_hop_by_hop_headers_never_leave(self, app):
        result = run(app, request("/api/test-redirect"))
        assert result.status == 302 and result.header("location") == "/dashboard"
        assert result.header("set-cookie") is None
        assert result.header("content-length") is None

    def test_refusals_are_json_and_audited(self, app):
        from sentrix.storage import link_store
        refused = run(app, request("/login"))
        assert refused.status == 404 and json.loads(refused.body)["code"] == "not_found"
        media = run(app, request("/media/snapshot/a.jpg"), consents={})
        assert media.status == 403 and json.loads(media.body)["code"] == "consent_required"
        admin = run(app, request("/api/health", actor={"type": "company_admin", "id": "a"}))
        assert admin.status == 403 and json.loads(admin.body)["code"] == "forbidden_role"
        rows = link_store.recent_remote(10)
        assert [r["allowed"] for r in rows] == [False, False, False]
        assert rows[-1]["target"] == "GET /login" and rows[-1]["status"] == 404
        assert rows[1]["user_id"] == "u1" and rows[1]["reason"]

    def test_served_requests_are_audited_too(self, app):
        from sentrix.storage import link_store
        assert run(app, request("/api/test-whoami")).status == 200
        row = link_store.recent_remote(1)[0]
        assert row["allowed"] and row["target"] == "GET /api/test-whoami"
        assert row["status"] == 200 and row["request_id"] == "req-1" and row["role"] == "owner"

    def test_bodies_inline_and_reassembled(self, app):
        small = run(app, request("/api/sos/test-echo", "POST", body=b"verdict=FALSE",
                                 headers=[["content-type",
                                           "application/x-www-form-urlencoded"]]))
        assert json.loads(small.body) == {"size": 13, "head": "verdict=FALSE",
                                          "content_type": "application/x-www-form-urlencoded"}
        large = b"\xff\xd8" + b"x" * (600 * 1024)
        message = request("/api/sos/test-echo", "POST", body=b"", chunked=True,
                          body_len=len(large), headers=[["content-type", "image/jpeg"]])
        result = run(app, message, body=large)
        assert json.loads(result.body)["size"] == len(large)

    def test_policy_runs_before_the_console(self, app):
        """A refused path is never replayed (the console route never runs)."""
        hits = []

        @app.post("/api/channels/test")
        async def channels_test():
            hits.append(1)
            return {}

        result = run(app, request("/api/channels/test", "POST"))
        assert result.status == 403 and hits == []

    def test_size_concurrency_and_bad_messages(self, app):
        too_big = run(app, request("/api/sos/test-echo", "POST"), body=b"x" * 2048,
                      tunnel_max_request_bytes=1024)
        assert too_big.status == 413 and too_big.code == "too_large"
        bad_base = run(app, request("/api/test-whoami", base="/elsewhere"))
        assert bad_base.status == 400
        bad_body = run(app, request("/api/sos/test-echo", "POST", body_b64="%%%"))
        assert bad_body.status == 400

        async def busy():
            ex = executor(app, tunnel_max_inflight=2)
            ex.inflight = 2
            result = await ex.execute(request("/api/test-whoami"))
            await ex.close()
            return result
        result = asyncio.run(busy())
        assert result.status == 503 and json.loads(result.body)["code"] == "device_busy"
        assert result.header("retry-after") == "1"

    def test_timeouts(self, app):
        @app.get("/api/test-slow")
        async def slow():
            await asyncio.sleep(2)
            return {}

        result = run(app, request("/api/test-slow"), tunnel_timeout_seconds=0.2)
        assert result.status == 504 and json.loads(result.body)["code"] == "timeout"
        ex = executor(app)
        assert ex.timeout_for({"timeout_ms": 1000}, "GET", "/dashboard") == 1.0
        assert ex.timeout_for({}, "GET", "/dashboard") == 20.0
        assert ex.timeout_for({}, "POST", "/api/authorized/capture") == 60.0
        upload = {"headers": [["content-type", "multipart/form-data; boundary=x"]]}
        assert ex.timeout_for(upload, "POST", "/authorized/upload") == 60.0

    def test_cancel_propagates(self, app):
        @app.get("/api/test-forever")
        async def forever():
            await asyncio.sleep(30)
            return {}

        async def go():
            ex = executor(app)
            task = asyncio.ensure_future(ex.execute(request("/api/test-forever")))
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert ex.inflight == 0
            await ex.close()
        asyncio.run(go())

    def test_health_through_the_tunnel_is_redacted(self, app, monkeypatch):
        from sentrix.pipeline import engine
        engine._pipeline.source.name = "capture:rtsp://admin:hunter2@192.168.1.9/s1"
        result = run(app, request("/api/health"))
        assert result.status == 200
        body = json.loads(result.body)
        assert "hunter2" not in result.body.decode()
        assert body["camera_source"].startswith("capture:rtsp://***@")
