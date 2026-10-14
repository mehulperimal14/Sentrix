"""Track T-WEB: the console's JSON APIs and the setup-mode Wi-Fi page.

``/api/incidents`` paging and filters, ``/api/level-history``, credential
redaction on every JSON output, verdicts with note and author, the gallery
routes' audit of who changed it and per-actor rate limits, and ``/setup/wifi``
(setup mode only, LAN only, the label's code, a fake ``sentrix.io.network``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import types
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from test_web_console import (BASE, add_incident, add_package, console,  # noqa: F401
                              lc_client, tunnel_client)


# ── incidents ─────────────────────────────────────────────────────────────────
class TestIncidentsApi:
    def test_paging_with_offset_and_limit(self, console):
        for n in range(7):
            add_incident(id=f"i{n}", started_at=datetime(2026, 10, 1 + n, 8, 0))
        client, _ = lc_client(console)
        first = client.get("/api/incidents", params={"limit": 3}).json()
        assert [row["id"] for row in first["incidents"]] == ["i6", "i5", "i4"]
        assert (first["limit"], first["offset"], first["total"]) == (3, 0, 7)
        third = client.get("/api/incidents", params={"limit": 3, "offset": 6}).json()
        assert [row["id"] for row in third["incidents"]] == ["i0"]
        assert client.get("/api/incidents", params={"limit": 9999}).json()["limit"] == 500
        assert client.get("/api/incidents", params={"limit": 0, "offset": -5}).json()["offset"] == 0
        assert first["stats"]["total"] == 7

    def test_filters(self, console):
        add_incident(id="w3", incident_type="weapon", peak_level=3,
                     started_at=datetime(2026, 10, 3, 12, 0), verdict="TRUE")
        add_incident(id="f5", incident_type="fire", peak_level=5,
                     started_at=datetime(2026, 10, 5, 12, 0), verdict="FALSE")
        add_incident(id="w4", incident_type="weapon", peak_level=4,
                     started_at=datetime(2026, 10, 7, 12, 0))
        client, _ = lc_client(console)

        def ids(**params):
            return [row["id"] for row in client.get("/api/incidents", params=params).json()["incidents"]]

        assert ids(incident_type="weapon") == ["w4", "w3"]
        assert ids(level=4) == ["w4", "f5"]
        assert ids(verdict="none") == ["w4"]
        assert ids(verdict="FALSE") == ["f5"]
        assert ids(since="2026-10-04", until="2026-10-06") == ["f5"]
        assert ids(incident_type="weapon", level=4) == ["w4"]
        # Malformed filters are ignored, never an error.
        assert ids(incident_type="<x>", level="high", since="yesterday") == ["w4", "f5", "w3"]
        body = client.get("/api/incidents", params={"incident_type": "weapon"}).json()
        assert body["total"] == 2 and body["filters"] == {"type": "weapon"}

    def test_requires_a_session(self, console):
        assert TestClient(console.app).get("/api/incidents").status_code == 401

    def test_verdict_records_note_and_author(self, console):
        from sentrix.storage import database
        add_incident(id="inc-v")
        client, csrf = lc_client(console)
        response = client.post("/api/incidents/inc-v/verdict", data={"verdict": "TRUE", "note": " real gun "},
                               headers={"X-CSRF-Token": csrf})
        assert response.json() == {"status": "ok", "verdict": "TRUE", "by": "lc:device_admin"}
        row = database.recent_incidents()[0]
        assert (row["verdict"], row["verdict_note"], row["verdict_by"]) == ("TRUE", "real gun", "lc:device_admin")
        assert row["verdict_at"]
        tunnelled = tunnel_client(console, "member").post("/api/incidents/inc-v/verdict",
                                                          data={"verdict": "FALSE"})
        assert tunnelled.json()["by"] == "portal:u-1"
        assert database.recent_incidents()[0]["verdict_by"] == "portal:u-1"

    def test_dispatch_send_errors_are_json_with_a_code(self, console):
        add_incident()
        add_package("p-sent", status="SENT")
        add_package("p-new")
        client, csrf = lc_client(console)
        refused = client.post("/api/dispatch/p-sent/send/POLICE", headers={"X-CSRF-Token": csrf})
        assert refused.status_code == 409 and refused.json()["code"] == "conflict"
        failed = client.post("/api/dispatch/p-new/send/MEDICAL", headers={"X-CSRF-Token": csrf}).json()
        assert failed["status"] == "failed" and failed["detail"]
        bad = client.post("/api/dispatch/p-new/send/ARMY", headers={"X-CSRF-Token": csrf})
        assert bad.status_code == 400

    def test_level_history(self, console):
        from sentrix.pipeline.state import state
        state.record_level_change(3, 1, "ELEVATED", "weapon confirmed", "inc-1", "weapon")
        state.record_level_change(1, 3, "SECURE", "clear", "inc-1", "weapon")
        history = lc_client(console)[0].get("/api/level-history").json()
        assert [(h["previous"], h["level"]) for h in history] == [(1, 3), (3, 1)]
        assert history[0]["reason"] == "weapon confirmed" and history[0]["at"]
        assert TestClient(console.app).get("/api/level-history").status_code == 401


# ── redaction ─────────────────────────────────────────────────────────────────
class _LeakyService:
    name = "leaky"

    def health(self):
        return {"portal": "wss://user:sekrit@portal.example/device/v1/link?token=abc123",
                "nested": [{"url": "rtsp://cam:pw2@10.0.0.9/live"}]}


class TestRedaction:
    def test_health_hides_camera_and_service_credentials(self, console):
        from sentrix import services
        services.registry.register(_LeakyService(), "leaky")
        body = lc_client(console)[0].get("/api/health").json()
        text = json.dumps(body)
        for secret in ("hunter2", "sekrit", "abc123", "pw2"):
            assert secret not in text, secret
        assert body["camera_source"] == "capture:rtsp://***@10.0.0.2/s1"

    def test_metrics_and_config_are_redacted(self, console):
        from sentrix.pipeline.state import state
        state.update(reason="lost rtsp://u:p4ss@cam/s?password=x9")
        client, _ = lc_client(console)
        metrics = client.get("/api/metrics").json()
        assert "p4ss" not in metrics["reason"] and "x9" not in metrics["reason"]
        assert client.get("/api/config").status_code == 200

    def test_redacted_walks_every_container(self):
        from sentrix.web.routes import redacted
        value = {"a": ["rtsp://u:p@h/x", ("http://k:v@y",)], "b": {"c": "plain"}, "n": 3}
        assert redacted(value) == {"a": ["rtsp://***@h/x", ["http://***@y"]],
                                   "b": {"c": "plain"}, "n": 3}


# ── gallery ───────────────────────────────────────────────────────────────────
def _jpeg() -> bytes:
    import cv2
    import numpy as np
    ok, buffer = cv2.imencode(".jpg", np.zeros((32, 32, 3), np.uint8))
    assert ok
    return buffer.tobytes()


class TestGallery:
    def test_upload_uses_the_name_field_or_the_file_name(self, console):
        client, csrf = lc_client(console)
        named = client.post("/authorized/upload", files={"file": ("IMG_001.jpg", _jpeg(), "image/jpeg")},
                            data={"person": "Asha Rao"}, headers={"X-CSRF-Token": csrf})
        assert named.json()["person"].lower().startswith("asha")
        assert (console.faces / named.json()["filename"]).exists()
        stem = client.post("/authorized/upload", files={"file": ("ravi.jpg", _jpeg(), "image/jpeg")},
                           headers={"X-CSRF-Token": csrf})
        assert stem.json()["filename"] == "ravi.jpg"
        event, detail = console.pipeline.events.rows[-1]
        assert event == "gallery_enrolled" and detail["by"] == "lc:device_admin"

    def test_upload_limits(self, console):
        from sentrix.web import routes
        client, csrf = lc_client(console)
        big = b"\xff\xd8\xff" + b"0" * (routes.MAX_UPLOAD_BYTES + 10)
        assert client.post("/authorized/upload", files={"file": ("a.jpg", big, "image/jpeg")},
                           headers={"X-CSRF-Token": csrf}).status_code == 413
        assert client.post("/authorized/upload", files={"file": ("a.gif", b"GIF89a", "image/gif")},
                           headers={"X-CSRF-Token": csrf}).status_code == 400

    def test_capture_and_delete_record_who(self, console):
        client = tunnel_client(console, "owner")
        captured = client.post("/api/authorized/capture", data={"person": "meera"})
        assert captured.status_code == 200, captured.text
        filename = captured.json()["filename"]
        assert console.pipeline.events.rows[-1][1]["by"] == "portal:u-1"
        assert client.delete(f"/api/authorized/{filename}").status_code == 200
        event, detail = console.pipeline.events.rows[-1]
        assert event == "gallery_removed" and detail["by"] == "portal:u-1"
        assert client.delete("/api/authorized/..%2F..%2Fsentrix.db").status_code == 404

    def test_capture_rate_limit_is_per_actor(self, console):
        from sentrix.web import routes
        limiter = routes.capture_limiter
        owner = tunnel_client(console, "owner")
        for _ in range(limiter.max_attempts):
            assert limiter.allow("portal:u-1")
        assert owner.post("/api/authorized/capture", data={"person": "x"}).status_code == 429
        client, csrf = lc_client(console)          # the LAN admin has a bucket of its own
        assert client.post("/api/authorized/capture", data={"person": "x"},
                           headers={"X-CSRF-Token": csrf}).status_code == 200

    def test_media_needs_a_session_and_stays_in_its_folder(self, console):
        (console.snapshots / "s.jpg").write_bytes(b"\xff\xd8\xffdata")
        assert TestClient(console.app).get("/media/snapshot/s.jpg").status_code == 401
        client, _ = lc_client(console)
        assert client.get("/media/snapshot/s.jpg").content == b"\xff\xd8\xffdata"
        assert client.get("/media/snapshot/..%2Fs.jpg").status_code in (200, 404)
        assert client.get("/media/snapshot/missing.jpg").status_code == 404


# ── setup mode ────────────────────────────────────────────────────────────────
CODE = "KTRW-BQXL"


@pytest.fixture
def setup_mode(console, tmp_path, monkeypatch):
    """Setup mode on, an identity file with the label's code, a fake network."""
    from sentrix.link import profile
    from sentrix.web import setup_routes
    identity = tmp_path / "identity"
    identity.mkdir()
    (identity / "identity.json").write_text(json.dumps({
        "device_id": "6f0e8c1a-0000-4000-8000-000000000001", "serial": "SX-0001",
        "setup_code_digest": hashlib.sha256(CODE.encode()).hexdigest()}), encoding="utf-8")
    monkeypatch.setattr(profile, "identity_dir", lambda: identity)
    monkeypatch.setattr(setup_routes, "setup_mode_enabled", lambda: True)
    network = types.SimpleNamespace(
        calls=[],
        scan=lambda: [{"ssid": "HomeNet", "signal": 71, "security": "WPA2"},
                      {"ssid": "<b>evil</b>", "signal": 10, "security": ""}, {"signal": 5}],
        status=lambda: {"connected": False, "ssid": None, "ip": None, "via": None})

    def connect(ssid, psk):
        network.calls.append((ssid, psk))
        return {"ok": ssid == "HomeNet", "error": None if ssid == "HomeNet" else "no such network"}

    network.connect = connect
    monkeypatch.setitem(sys.modules, "sentrix.io.network", network)
    setup_routes.setup_limiter.reset("testclient")
    yield network
    setup_routes.setup_limiter.reset("testclient")


class TestSetupWifi:
    def test_absent_outside_setup_mode(self, console):
        client = TestClient(console.app)
        assert client.get("/setup/wifi").status_code == 404
        assert client.post("/setup/wifi/scan", json={"code": CODE}).status_code == 404
        assert client.post("/setup/wifi", json={"code": CODE, "ssid": "x"}).status_code == 404

    def test_page_and_scan_with_the_code(self, setup_mode, console):
        client = TestClient(console.app)
        page = client.get("/setup/wifi")
        assert page.status_code == 200 and 'id="setup-scan"' in page.text
        assert "/static/js/setup.js" in page.text
        for typed in (CODE, "ktrw bqxl", "KTRWBQXL"):
            setup_routes_reset()
            body = client.post("/setup/wifi/scan", json={"code": typed}).json()
            assert [n["ssid"] for n in body["networks"]] == ["HomeNet", "<b>evil</b>"], typed
        assert body["status"]["connected"] is False

    def test_wrong_codes_are_refused_and_rate_limited(self, setup_mode, console):
        client = TestClient(console.app)
        for _ in range(5):
            wrong = client.post("/setup/wifi/scan", json={"code": "AAAA-BBBB"})
            assert wrong.status_code == 403 and wrong.json()["code"] == "pin_invalid"
        locked = client.post("/setup/wifi/scan", json={"code": CODE})
        assert locked.status_code == 429 and locked.headers["retry-after"] == "600"

    def test_connect_validates_and_never_logs_the_passphrase(self, setup_mode, console, caplog):
        client = TestClient(console.app)
        caplog.set_level(logging.DEBUG)
        assert client.post("/setup/wifi", json={"code": CODE, "ssid": ""}).status_code == 400
        assert client.post("/setup/wifi", json={"code": CODE, "ssid": "HomeNet",
                                                "psk": "short"}).status_code == 400
        ok = client.post("/setup/wifi", json={"code": CODE, "ssid": "HomeNet",
                                              "psk": "correct-horse-9"})
        assert ok.status_code == 200 and ok.json()["ok"] is True
        assert setup_mode.calls == [("HomeNet", "correct-horse-9")]
        failed = client.post("/setup/wifi", json={"code": CODE, "ssid": "Other", "psk": "12345678"})
        assert failed.status_code == 502 and failed.json()["error"] == "no such network"
        assert "correct-horse-9" not in caplog.text

    def test_refused_through_the_tunnel(self, setup_mode, console):
        from sentrix.link import tunnel_auth
        assert tunnel_client(console).get("/setup/wifi").status_code == 404
        bare = TestClient(console.app, client=tunnel_auth.TUNNEL_CLIENT)
        assert bare.get("/setup/wifi").status_code == 404

    def test_without_network_control_or_identity(self, setup_mode, console, monkeypatch, tmp_path):
        from sentrix.link import profile
        from sentrix.web import setup_routes
        monkeypatch.setattr(setup_routes, "_network", lambda: None)
        client = TestClient(console.app)
        page = client.get("/setup/wifi")
        assert 'id="setup-unavailable"' in page.text and "setup.js" not in page.text
        assert client.post("/setup/wifi/scan", json={"code": CODE}).json()["code"] == "unsupported"
        monkeypatch.setattr(profile, "identity_dir", lambda: tmp_path / "nowhere")
        refused = client.post("/setup/wifi/scan", json={"code": CODE})
        assert refused.status_code == 400 and "provision" in refused.json()["detail"]

    def test_code_matching(self):
        from sentrix.web.setup_routes import code_matches
        digest = hashlib.sha256(CODE.encode()).hexdigest()
        assert code_matches("  ktrw-bqxl ", digest) and code_matches("KTRWBQXL", digest)
        assert not code_matches("", digest) and not code_matches("KTRW-BQXM", digest)


def setup_routes_reset():
    from sentrix.web import setup_routes
    setup_routes.setup_limiter.reset("testclient")
