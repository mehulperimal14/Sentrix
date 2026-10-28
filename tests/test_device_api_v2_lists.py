"""Device app API (docs/9): the lists the server-rendered pages only showed as
HTML - activity (Events), snapshots (Alerts), evidence, dispatch, faces
(Access) - and the Wi-Fi setup mode as JSON.

The LC app with a fake pipeline, a temporary database and temporary media;
the setup-mode tests use a fake ``sentrix.io.network`` (no Wi-Fi is touched).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from test_device_api_v2_session import envelope, with_consents
from test_web_api import CODE, setup_mode  # noqa: F401
from test_web_console import (BASE, add_evidence, add_incident, add_package,  # noqa: F401
                              console, lc_client, populate, tunnel_client)


@pytest.fixture(autouse=True)
def _room(monkeypatch):
    """The standalone profile's camera label, whatever the developer's .env says."""
    monkeypatch.setenv("SENTRIX_CAMERA_LOCATION", "Hall")


def add_duress_request(incident_id: str, request_id: str = "req-a1"):
    from sentrix.storage import requests
    requests.create_request(request_id, incident_id=incident_id, incident_type="weapon",
                            level=3, evidence_state="confirmed", policy={},
                            expires_at=None, test_mode=True, flags={"duress": True})
    requests.transition(request_id, "approve", actor_type="owner")


# ── GET /api/activity ─────────────────────────────────────────────────────────
class TestActivity:
    def test_rows_carry_their_request_and_media_links(self, console):
        populate(console)
        add_incident(id="inc-r", request_id="req-a1", started_at=datetime(2026, 10, 15, 8),
                     ended_at=datetime(2026, 10, 15, 8, 0, 30), snapshot_path=None,
                     evidence_bundle=None)
        add_duress_request("inc-r")
        client, _ = lc_client(console)
        response = client.get("/api/activity")
        assert response.status_code == 200
        body = response.json()
        assert [row["id"] for row in body["items"]] == ["inc-r", "inc-1"]
        assert body["total"] == 2 and body["next_cursor"] is None and body["cursor"] is None
        assert body["stats"]["total"] == 2 and body["stats"]["unreviewed"] == 2
        newest, first = body["items"]
        assert newest["request"] == {"id": "req-a1", "short_id": "SX-REQ-A1",
                                     "state": "DECLINED", "label": "Marked as false alarm"}
        assert "Help requested" not in response.text and "duress" not in response.text.lower()
        assert newest["ongoing"] is False and newest["ended_at"] == "2026-10-15T08:00:30Z"
        assert newest["has_snapshot"] is False and newest["snapshot_url"] is None
        assert first == {
            "id": "inc-1", "started_at": "2026-10-14T09:00:00Z", "ended_at": None,
            "ongoing": True, "duration_seconds": 4.0, "peak_level": 3, "peak_tci": 0.61,
            "status": "ELEVATED", "incident_type": "weapon",
            "reason": "scene weapon 0.71 confirmed", "authorized": False, "camera_id": "CAM_0",
            "room": "Hall", "verdict": None, "verdict_note": None, "verdict_by": None,
            "verdict_at": None, "request": None, "has_snapshot": True,
            "snapshot_url": "/media/snapshot/snap_1.jpg", "has_evidence": True,
            "evidence_url": "/media/evidence/L3_bundle_1"}

    def test_a_verdict_shows_who_and_when(self, console):
        populate(console)
        client, csrf = lc_client(console)
        assert client.post("/api/incidents/inc-1/verdict", data={"verdict": "TRUE",
                                                                 "note": "replica gun"},
                           headers={"X-CSRF-Token": csrf}).status_code == 200
        row = client.get("/api/activity").json()["items"][0]
        assert row["verdict"] == "TRUE" and row["verdict_note"] == "replica gun"
        assert row["verdict_by"] == "device admin" and row["verdict_at"].endswith("Z")

    def test_paging_with_a_cursor_and_filters(self, console):
        for n in range(7):
            add_incident(id=f"i{n}", started_at=datetime(2026, 10, 1 + n, 8, 0),
                         incident_type="fire" if n % 2 else "weapon", peak_level=3 + n % 3)
        client, _ = lc_client(console)

        def page(**params):
            return client.get("/api/activity", params=params).json()

        first = page(limit=3)
        assert [r["id"] for r in first["items"]] == ["i6", "i5", "i4"]
        assert first["total"] == 7 and first["next_cursor"] == "3"
        second = page(limit=3, cursor=first["next_cursor"])
        assert [r["id"] for r in second["items"]] == ["i3", "i2", "i1"]
        assert second["cursor"] == "3" and second["next_cursor"] == "6"
        last = page(limit=3, cursor="6")
        assert [r["id"] for r in last["items"]] == ["i0"] and last["next_cursor"] is None
        fires = page(incident_type="fire")
        assert [r["id"] for r in fires["items"]] == ["i5", "i3", "i1"]
        assert fires["total"] == 3 and fires["filters"] == {"type": "fire"}
        assert [r["id"] for r in page(level=5)["items"]] == ["i5", "i2"]
        assert page(verdict="none")["total"] == 7
        assert [r["id"] for r in page(since="2026-10-03", until="2026-10-04")["items"]] in (
            ["i3", "i2"], ["i2"], ["i3"]), "device-date bounds"

    @pytest.mark.parametrize("params, field", [
        ({"limit": 0}, "limit"), ({"limit": 201}, "limit"), ({"cursor": "abc"}, "cursor"),
        ({"cursor": "-1"}, "cursor"), ({"level": 9}, "level"), ({"verdict": "maybe"}, "verdict")])
    def test_bad_parameters_are_validation_errors(self, console, params, field):
        client, _ = lc_client(console)
        error = envelope(client.get("/api/activity", params=params), 422, "validation")
        assert field in error["fields"], error

    def test_media_links_through_the_portal_follow_the_consents(self, console, monkeypatch):
        populate(console)
        row = tunnel_client(console).get("/api/activity").json()["items"][0]
        assert row["has_snapshot"] is True and row["snapshot_url"] is None, \
            "no remote_view consent: no link the path table would refuse"
        assert row["evidence_url"] is None
        with_consents(monkeypatch, remote_view=True)
        row = tunnel_client(console, role="member").get("/api/activity").json()["items"][0]
        assert row["snapshot_url"] == f"{BASE}/media/snapshot/snap_1.jpg"
        assert row["evidence_url"] == f"{BASE}/media/evidence/L3_bundle_1"

    def test_signed_out(self, console):
        envelope(TestClient(console.app).get("/api/activity"), 401, "not_signed_in")


# ── GET /api/snapshots ────────────────────────────────────────────────────────
class TestSnapshots:
    def test_newest_first_with_their_incident(self, console):
        populate(console)
        (console.snapshots / "snap_2.jpg").write_bytes(b"\xff\xd8\xff\xe0more")
        (console.snapshots / "notes.txt").write_text("not an image")
        client, _ = lc_client(console)
        body = client.get("/api/snapshots").json()
        assert [item["name"] for item in body["items"]] == ["snap_2.jpg", "snap_1.jpg"]
        assert body["total"] == 2 and body["next_cursor"] is None
        newest, linked = body["items"]
        assert newest["incident"] is None and newest["bytes"] == 8
        assert newest["taken_at"].endswith("Z")
        assert linked["incident"] == {"id": "inc-1", "incident_type": "weapon", "peak_level": 3}
        assert linked["url"] == "/media/snapshot/snap_1.jpg"
        assert client.get(linked["url"]).status_code == 200
        paged = client.get("/api/snapshots", params={"limit": 1}).json()
        assert [i["name"] for i in paged["items"]] == ["snap_2.jpg"]
        assert paged["next_cursor"] == "1"

    def test_the_device_admin_only(self, console):
        envelope(tunnel_client(console).get("/api/snapshots"), 403, "forbidden")
        envelope(TestClient(console.app).get("/api/snapshots"), 401, "not_signed_in")


# ── GET /api/evidence ─────────────────────────────────────────────────────────
class TestEvidence:
    def test_bundles_detections_and_the_chain(self, console):
        add_evidence(console)
        add_evidence(console, bundle="L3_bundle_0",
                     detections=["Detection(label='knife', conf=0.55, bbox=(1, 2, 3, 4))",
                                 "garbage"])
        client, _ = lc_client(console)
        body = client.get("/api/evidence").json()
        assert body["chain"] == {"ok": True, "entries": 0, "note": "no manifest yet",
                                 "problems": [], "problem_count": 0, "error": None}
        assert body["total"] == 2
        newest, oldest = body["items"]
        assert newest == {
            "bundle_id": "L3_bundle_1", "timestamp": "2026-10-14T09:00:02Z",
            "incident_id": "inc-1", "camera_id": "CAM_0", "room": "Hall", "level": 3,
            "tci": 0.61, "status": "ELEVATED", "incident_type": "weapon", "reason": "r",
            "detections": [{"label": "pistol", "conf": 0.8213, "track_id": 7}],
            "sha256": "ab" * 32, "view_url": "/media/evidence/L3_bundle_1"}
        assert oldest["detections"] == [{"label": "knife", "conf": 0.55, "track_id": None}]
        second = client.get("/api/evidence", params={"limit": 1, "cursor": "1"}).json()
        assert [i["bundle_id"] for i in second["items"]] == ["L3_bundle_0"]

    def test_a_broken_chain_without_file_paths(self, console, monkeypatch):
        problems = [{"line": n, "bundle": f"b{n}", "error": "chain break"} for n in range(25)]
        monkeypatch.setattr(console.vault, "verify_chain", lambda: {
            "ok": False, "entries": 30, "problems": problems,
            "error": r"[Errno 13] Permission denied: 'C:\\secret\\var\\manifest.jsonl'"})
        client, _ = lc_client(console)
        response = client.get("/api/evidence")
        chain = response.json()["chain"]
        assert chain["ok"] is False and chain["problem_count"] == 25
        assert len(chain["problems"]) == 20 and chain["problems"][0] == {
            "line": 0, "bundle": "b0", "error": "chain break"}
        assert chain["error"] == "The evidence manifest could not be read."
        assert "secret" not in response.text and "Errno" not in response.text

    def test_the_device_admin_only(self, console):
        envelope(tunnel_client(console).get("/api/evidence"), 403, "forbidden")


# ── GET /api/dispatch ─────────────────────────────────────────────────────────
class TestDispatch:
    def test_owner_view_duress_and_silent(self, console):
        from sentrix.storage import database, requests
        from sentrix.storage.models import DispatchRecord
        add_incident(request_id="req-dur")
        add_duress_request("inc-1", "req-dur")
        requests.create_request("req-sil", incident_id=None, incident_type="duress",
                                level=3, evidence_state="confirmed", policy={},
                                expires_at=None, test_mode=True,
                                flags={"duress": True, "silent": True})
        add_package("p-dur", status="SENT", request_id="req-dur",
                    user_phone="+919812345678")
        add_package("p-sil", status="PENDING", request_id="req-sil", incident_type="duress",
                    incident_id=None, created_at="2026-10-14T09:00:06+00:00")
        add_package("p-new", status="PENDING", created_at="2026-10-14T09:00:07+00:00",
                    incident_type="fire", authority="FIRE")
        with database.session_scope() as session:
            row = session.get(DispatchRecord, "p-dur")
            row.chosen_authority, row.sent_by = "POLICE", "lc:device_admin"
            row.sent_to_json = json.dumps(["SENTRIX Desk", "Aman 98xxxxx611"])
        client, _ = lc_client(console)
        response = client.get("/api/dispatch")
        body = response.json()
        assert [p["id"] for p in body["items"]] == ["p-new", "p-dur"], "silent: absent"
        fresh, duress = body["items"]
        assert fresh["sendable"] is True and fresh["authority"] == "FIRE"
        assert fresh["status"] == "PENDING" and fresh["created_at"] == "2026-10-14T09:00:07Z"
        assert fresh["location"] == "12 Model Town" and fresh["camera_location"] == "Hall"
        assert duress["status"] == "CANCELLED" and duress["sent_to"] == []
        assert duress["chosen_authority"] is None and duress["sent_by"] is None
        assert duress["sendable"] is False
        assert duress["request"] == {"id": "req-dur", "short_id": "SX-REQ-DU",
                                     "state": "DECLINED", "label": "Marked as false alarm"}
        assert body["authorities"] == ["POLICE", "FIRE", "MEDICAL"]
        assert body["sendable_statuses"] == ["PENDING", "FAILED"]
        for hidden in ("Aman", "+919812345678", "user_phone", "p-sil", "DURESS"):
            assert hidden not in response.text, hidden

    def test_sent_packages_name_people_not_audit_strings(self, console):
        from sentrix.storage import database
        from sentrix.storage.models import DispatchRecord
        add_package("p-1", status="SENT")
        with database.session_scope() as session:
            row = session.get(DispatchRecord, "p-1")
            row.sent_by = "portal:0b6f9d4e-58a1"
            row.sent_to_json = json.dumps([{"address": "+919812345678"}, "SENTRIX Desk"])
        response = lc_client(console)[0].get("/api/dispatch")
        item = response.json()["items"][0]
        assert item["sent_by"] == "Portal user 0b6f9d4e"
        assert len(item["sent_to"]) == 2 and "+919812345678" not in response.text

    def test_paging(self, console):
        for n in range(3):
            add_package(f"p{n}", created_at=f"2026-10-14T09:00:0{n}+00:00")
        client, _ = lc_client(console)
        first = client.get("/api/dispatch", params={"limit": 2}).json()
        assert [p["id"] for p in first["items"]] == ["p2", "p1"] and first["next_cursor"] == "2"
        rest = client.get("/api/dispatch", params={"limit": 2, "cursor": "2"}).json()
        assert [p["id"] for p in rest["items"]] == ["p0"] and rest["next_cursor"] is None

    def test_the_device_admin_only(self, console):
        envelope(tunnel_client(console, role="member").get("/api/dispatch"), 403, "forbidden")


# ── GET /api/authorized ───────────────────────────────────────────────────────
class TestFaces:
    def test_the_gallery(self, console):
        populate(console)
        (console.faces / "bob__2.png").write_bytes(b"\x89PNG\r\n\x1a\nxx")
        (console.faces / "readme.txt").write_text("not a face")
        client, _ = lc_client(console)
        body = client.get("/api/authorized").json()
        assert body["backend"] == "insightface" and body["available"] is True
        assert body["items"] == [
            {"filename": "alice.jpg", "person": "alice", "enrolled": True,
             "url": "/media/face/alice.jpg"},
            {"filename": "bob__2.png", "person": "bob", "enrolled": False,
             "url": "/media/face/bob__2.png"}]

    def test_identity_failing_closed(self, console):
        console.pipeline.identity.backend = None
        body = lc_client(console)[0].get("/api/authorized").json()
        assert body == {"backend": None, "available": False, "items": []}

    def test_the_device_admin_only(self, console):
        envelope(tunnel_client(console).get("/api/authorized"), 403, "forbidden")


# ── /api/setup/wifi ───────────────────────────────────────────────────────────
def reset_setup_limiter():
    from sentrix.web import setup_routes
    setup_routes.setup_limiter.reset("testclient")


class TestSetupWifi:
    def test_absent_outside_setup_mode(self, console):
        client = TestClient(console.app)
        envelope(client.get("/api/setup/wifi"), 404, "not_found")
        envelope(client.post("/api/setup/wifi/scan", json={"code": CODE}), 404, "not_found")
        envelope(client.post("/api/setup/wifi/connect", json={"code": CODE, "ssid": "x"}),
                 404, "not_found")
        envelope(client.post("/api/setup/wifi/connect", json={}), 404, "not_found")

    def test_info_and_scan_with_the_code(self, setup_mode, console):
        client = TestClient(console.app)
        assert client.get("/api/setup/wifi").json() == {
            "setup_mode": True, "network_available": True, "test_mode": True}
        for typed in (CODE, "ktrw bqxl", "KTRWBQXL"):
            reset_setup_limiter()
            body = client.post("/api/setup/wifi/scan", json={"code": typed}).json()
            assert body["networks"] == [
                {"ssid": "HomeNet", "signal": 71, "security": "WPA2"},
                {"ssid": "<b>evil</b>", "signal": 10, "security": ""}], typed
        assert body["status"] == {"connected": False, "ssid": None, "ip": None, "via": None}

    def test_wrong_codes_share_the_html_pages_limit(self, setup_mode, console):
        client = TestClient(console.app)
        for _ in range(3):
            assert client.post("/setup/wifi/scan", json={"code": "AAAA-BBBB"}).status_code == 403
        for _ in range(2):
            error = envelope(client.post("/api/setup/wifi/scan", json={"code": "AAAA-BBBB"}),
                             403, "wrong_code")
            assert error["fields"] == {"code": "Not the code on the label"}
        locked = client.post("/api/setup/wifi/scan", json={"code": CODE})
        error = envelope(locked, 429, "rate_limited")
        assert error["retry_after"] == 600 and locked.headers["retry-after"] == "600"

    def test_connect_validates_and_never_echoes_the_passphrase(self, setup_mode, console,
                                                              caplog):
        client = TestClient(console.app)
        caplog.set_level(logging.DEBUG)
        error = envelope(client.post("/api/setup/wifi/connect",
                                     json={"code": CODE, "ssid": "", "psk": "tiny-pw"}),
                         422, "validation")
        assert set(error["fields"]) == {"ssid", "psk"}
        assert error["fields"]["psk"] == "A Wi-Fi password has 8 to 63 characters"
        long_ssid = client.post("/api/setup/wifi/connect", json={"code": CODE, "ssid": "x" * 40})
        assert envelope(long_ssid, 422, "validation")["fields"]["ssid"].startswith("Choose")
        secret = "correct-horse-9"
        ok = client.post("/api/setup/wifi/connect",
                         json={"code": CODE, "ssid": "HomeNet", "psk": secret})
        assert ok.status_code == 200 and ok.json() == {
            "ok": True, "status": {"connected": False, "ssid": None, "ip": None, "via": None}}
        assert setup_mode.calls == [("HomeNet", secret)]
        failed = client.post("/api/setup/wifi/connect",
                             json={"code": CODE, "ssid": "Other", "psk": "12345678"})
        error = envelope(failed, 502, "wifi_failed")
        assert error["reason"] == "no such network"
        bad_psk = client.post("/api/setup/wifi/connect",
                              json={"code": CODE, "ssid": "HomeNet", "psk": "short-secret"[:5]})
        assert "short" not in bad_psk.text
        open_network = client.post("/api/setup/wifi/connect",
                                   json={"code": CODE, "ssid": "HomeNet", "psk": ""})
        assert open_network.status_code == 200 and setup_mode.calls[-1] == ("HomeNet", "")
        assert secret not in caplog.text and secret not in ok.text

    def test_refused_through_the_tunnel(self, setup_mode, console):
        from sentrix.link import tunnel_auth
        owner = tunnel_client(console)
        assert owner.get("/api/setup/wifi").status_code == 404
        assert owner.post("/api/setup/wifi/scan", json={"code": CODE}).status_code == 404
        bare = TestClient(console.app, client=tunnel_auth.TUNNEL_CLIENT)
        envelope(bare.get("/api/setup/wifi"), 404, "not_found")
        envelope(bare.post("/api/setup/wifi/scan", json={"code": CODE}), 404, "not_found")

    def test_without_network_control_or_identity(self, setup_mode, console, monkeypatch,
                                                 tmp_path):
        from sentrix.link import profile
        from sentrix.web import setup_routes
        monkeypatch.setattr(setup_routes, "_network", lambda: None)
        client = TestClient(console.app)
        assert client.get("/api/setup/wifi").json()["network_available"] is False
        envelope(client.post("/api/setup/wifi/scan", json={"code": CODE}), 409, "unsupported")
        monkeypatch.setattr(profile, "identity_dir", lambda: tmp_path / "nowhere")
        response = client.post("/api/setup/wifi/scan", json={"code": CODE})
        error = envelope(response, 409, "unsupported")
        assert "provision" not in error["message"] and "tools/" not in error["message"]
