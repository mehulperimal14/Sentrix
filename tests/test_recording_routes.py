"""Training recordings on the Local Console (D20): ``/api/recordings`` and
``/media/recording/*``.

The device admin on the LAN starts, stops, lists, downloads and deletes
recordings; anyone signed out gets 401. Since 2026-10-28 the owner starts and
stops them through the Portal with a fresh step-up and lists them without one;
downloads and deletes are refused to every Portal user before any handler runs
(the link's path table: ``lc_only``) and, if a request got past it, by the
route's own ``require_lc("device_admin")``; members and the Desk get nothing.
Files are served only by their exact finished name, as attachments. The
pipeline is a real ``Pipeline`` without models or a camera (frames are handed
to the recorder directly); the app is the real console with its hardening, and
Portal requests go through the link's real tunnel executor and path table.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from sentrix import paths
from sentrix.actor import Actor
from sentrix.config import settings
from sentrix.io.recorder import RecorderError
from sentrix.io.sources import FrameSource
from sentrix.pipeline.engine import Pipeline
from sentrix.web.security import RateLimiter

from test_web_console import console, lc_client, tunnel_client  # noqa: F401

FPS = 10.0
#: Every consent granted and a request on the help path: the most a Portal
#: user can ever have, so a refusal below is the recording rules' own.
EVERYTHING = {"remote_view": True, "face_recognition": True, "desk_live_view": True}


class Camera(FrameSource):
    name = "capture:rtsp://admin:hunter2@10.0.0.9/s1"

    def read(self):
        return False, None

    def status(self) -> dict:
        return {"name": self.name, "online": True, "fps": FPS}


def make_pipeline(events) -> Pipeline:
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.cfg = settings
    pipeline.source = Camera()
    pipeline.events = events
    pipeline.audio = None
    pipeline.session_id = "sess-rec"
    pipeline._init_live()
    pipeline._raw = np.zeros((48, 64, 3), np.uint8)        # the camera's first frame
    return pipeline


@pytest.fixture
def rec(console, tmp_path, monkeypatch):  # noqa: F811
    """The console with a recording pipeline, a fresh limiter and a signed-in
    device admin (CSRF header set)."""
    from sentrix.io import recorder as recorder_module
    from sentrix.web import recording_routes
    directory = tmp_path / "recordings"
    monkeypatch.setattr(paths, "RECORDINGS_DIR", directory)
    monkeypatch.setattr(recorder_module, "settings", replace(
        settings, recording=replace(settings.recording, min_free_mb=0)))
    monkeypatch.setattr(recording_routes, "control_limiter",
                        RateLimiter(max_attempts=recording_routes.CONTROL_PER_MINUTE,
                                    window_seconds=60.0))
    monkeypatch.setattr(recording_routes, "_pruned_at", None)
    pipeline = make_pipeline(console.pipeline.events)
    monkeypatch.setattr(recording_routes, "_pipeline", lambda: pipeline)
    client, csrf = lc_client(console)
    client.headers["X-CSRF-Token"] = csrf
    yield SimpleNamespace(console=console, pipeline=pipeline, client=client, dir=directory,
                          events=console.pipeline.events)
    pipeline._close_recorder()


def feed(pipeline, frames: int = 10) -> None:
    """What the capture thread hands the recorder."""
    recorder = pipeline.recorder
    start = time.monotonic()
    for seq in range(frames):
        recorder.on_frame(np.full((48, 64, 3), 100, np.uint8), seq + 1, start + seq / FPS)


def record_one(rec, label: str = "take") -> dict:
    """Start, ten frames, stop; returns the finished recording's list item."""
    assert rec.client.post("/api/recordings/start", json={"label": label}).status_code == 200
    feed(rec.pipeline)
    assert rec.client.post("/api/recordings/stop").status_code == 200
    assert rec.pipeline.recorder.wait(10.0)
    return rec.pipeline.recorder.list()[0]


def audits(rec, event: str) -> list[dict]:
    return [detail for name, detail in rec.events.rows if name == event]


def owner(step_up_at: float | None = None) -> Actor:
    return Actor("owner", id="u1", name="Riya", step_up_at=step_up_at)


def through_portal(rec, method: str, path: str, actor: Actor, body: bytes = b""):
    """One request as the Portal forwards it: the link's tunnel executor with
    the real path table (every consent granted), replayed into the console."""
    from sentrix.link.policy import TunnelPolicy
    from sentrix.link.tunnel import TunnelExecutor

    async def go():
        executor = TunnelExecutor(rec.console.app, cfg=settings.link, audit=lambda **_: None,
                                  policy=TunnelPolicy(media_per_minute=60,
                                                      consents=lambda: EVERYTHING,
                                                      help_path_open=lambda: True))
        try:
            return await executor.execute({
                "id": "r1", "method": method, "path": path, "query": "", "headers": [],
                "actor": actor.to_dict(), "base": "/d/abc", "csrf": "c", "timeout_ms": 5000,
                "body_b64": base64.b64encode(body).decode()})
        finally:
            await executor.close()

    return asyncio.run(go())


def stepped_up(client):
    """A tunnelled test client whose Portal user re-entered the password now."""
    from sentrix.link import tunnel_auth
    client.headers[tunnel_auth.HEADER_STEP_UP] = str(int(time.time()))
    return client


class TestDeviceAdmin:
    def test_start_stop_list_download_delete(self, rec):
        listing = rec.client.get("/api/recordings")
        assert listing.status_code == 200 and listing.headers["cache-control"] == "no-store"
        body = listing.json()
        assert {"status", "items", "free_bytes", "ffmpeg", "microphone", "keep_days"} <= set(body)
        assert body["items"] == [] and not body["status"]["recording"]
        assert body["microphone"] is None and body["keep_days"] == settings.recording.keep_days

        started = rec.client.post("/api/recordings/start", json={"label": "Two people"})
        assert started.status_code == 200
        status = started.json()
        assert status["recording"] and status["name"].endswith("_two_people")
        assert status["fps"] == FPS and "microphone" in status
        again = rec.client.post("/api/recordings/start", json={})
        assert again.status_code == 409 and again.json()["code"] == "conflict"
        feed(rec.pipeline)
        stopped = rec.client.post("/api/recordings/stop")
        assert stopped.status_code == 200 and not stopped.json()["recording"]
        assert rec.pipeline.recorder.wait(10.0)
        nothing = rec.client.post("/api/recordings/stop")
        assert nothing.status_code == 409 and nothing.json()["code"] == "conflict"

        items = rec.client.get("/api/recordings").json()["items"]
        assert [item["name"] for item in items] == [status["name"]]
        video = items[0]["files"]["video"]
        download = rec.client.get(f"/media/recording/{video}")
        assert download.status_code == 200 and download.content[:4] == b"RIFF"
        assert download.headers["content-type"] == "video/x-msvideo"
        assert download.headers["content-disposition"].startswith("attachment;")
        assert video in download.headers["content-disposition"]
        assert download.headers["cache-control"] == "no-store"
        assert audits(rec, "recording_downloaded") == [{"file": video, "by": "lc:device_admin"}]
        head = rec.client.head(f"/media/recording/{video}")
        assert head.status_code == 200 and head.content == b""
        part = rec.client.get(f"/media/recording/{video}", headers={"Range": "bytes=8-11"})
        assert part.status_code == 206 and part.content == b"AVI "
        assert len(audits(rec, "recording_downloaded")) == 1, "HEAD and a later range: no row"
        meta = rec.client.get(f"/media/recording/{items[0]['files']['meta']}")
        assert meta.status_code == 200 and meta.json()["name"] == status["name"]
        assert "hunter2" not in meta.text

        deleted = rec.client.delete(f"/api/recordings/{status['name']}")
        assert deleted.status_code == 200 and deleted.json() == {"status": "ok",
                                                                 "name": status["name"]}
        assert rec.client.get("/api/recordings").json()["items"] == []
        gone = rec.client.delete(f"/api/recordings/{status['name']}")
        assert gone.status_code == 404 and gone.json()["code"] == "not_found"
        assert rec.client.get(f"/media/recording/{video}").status_code == 404
        names = [name for name, _ in rec.events.rows]
        assert {"recording_started", "recording_stopped", "recording_deleted"} <= set(names)

    @pytest.mark.parametrize("headers", [
        {"Range": "Bytes=0-"},                          # the unit in another case
        {"Range": "bytes=-1000000"},                    # a suffix range: the whole file
        {"Range": "bytes=5-", "If-Range": '"nope"'},    # If-Range mismatch: the whole file
        {"Range": "bytes=1-,0-0"},                      # merged ranges: the whole file
        {"Range": ",".join(f"bytes={k}-{k}" for k in range(150))},   # too many: whole file
    ])
    def test_every_way_to_get_the_whole_file_is_audited(self, rec, headers):
        item = record_one(rec)
        video = item["files"]["video"]
        size = (rec.dir / video).stat().st_size
        response = rec.client.get(f"/media/recording/{video}", headers=headers)
        assert response.status_code in (200, 206) and len(response.content) == size
        assert audits(rec, "recording_downloaded") == [{"file": video, "by": "lc:device_admin"}]

    def test_a_later_part_of_a_download_is_not_audited_again(self, rec):
        item = record_one(rec)
        video = item["files"]["video"]
        for headers in ({"Range": "bytes=8-11"}, {"Range": "bytes=100-"}):
            assert rec.client.get(f"/media/recording/{video}", headers=headers).status_code == 206
        assert audits(rec, "recording_downloaded") == []

    def test_the_recording_in_progress_cannot_be_deleted_or_downloaded(self, rec):
        name = rec.client.post("/api/recordings/start", json={}).json()["name"]
        feed(rec.pipeline, 3)
        busy = rec.client.delete(f"/api/recordings/{name}")
        assert busy.status_code == 409 and busy.json()["code"] == "conflict"
        assert rec.client.get(f"/media/recording/{name}.part.avi").status_code == 404
        assert rec.client.get(f"/media/recording/{name}.avi").status_code == 404

    def test_an_empty_body_starts_without_a_label(self, rec):
        started = rec.client.post("/api/recordings/start")
        assert started.status_code == 200 and started.json()["label"] == ""


class TestRefusals:
    def test_signed_out_is_401(self, rec):
        from fastapi.testclient import TestClient
        anonymous = TestClient(rec.console.app)
        for method, path in (("GET", "/api/recordings"), ("POST", "/api/recordings/start"),
                             ("POST", "/api/recordings/stop"),
                             ("DELETE", "/api/recordings/rec_20261028_101010"),
                             ("GET", "/media/recording/rec_20261028_101010.avi")):
            response = anonymous.request(method, path)
            assert response.status_code == 401, (method, path)
            assert response.json()["code"] == "unauthorised"
        assert rec.pipeline.recorder is None, "no handler ran"

    def test_the_path_table_keeps_the_files_on_the_lan(self):
        from sentrix.link.policy import TunnelPolicy, classify
        policy = TunnelPolicy(media_per_minute=60, consents=lambda: EVERYTHING,
                              help_path_open=lambda: True)
        member, desk = Actor("member", id="m1"), Actor("operator", id="o1")
        fresh = owner(time.time())
        mine = {("GET", "/api/recordings"): "record_status",
                ("POST", "/api/recordings/start"): "record",
                ("POST", "/api/recordings/stop"): "record_stop"}
        for (method, path), path_class in mine.items():
            assert classify(method, path) == path_class, (method, path)
            assert policy.check_request(method, path, fresh), (method, path)
            for actor in (member, desk):
                decision = policy.check_request(method, path, actor)
                assert not decision and decision.code == "forbidden_role", (actor.type, path)
        for method, path in (("DELETE", "/api/recordings/rec_20261028_101010"),
                             ("GET", "/media/recording/rec_20261028_101010.avi"),
                             ("HEAD", "/media/recording/rec_20261028_101010.wav")):
            assert classify(method, path) == "lc_only", (method, path)
            for actor in (fresh, member, desk):
                decision = policy.check_request(method, path, actor)
                assert not decision and decision.code == "forbidden_role", (method, path)

    def test_a_tunnel_principal_past_the_table_is_still_refused(self, rec):
        """The routes restate the table: if a request got past it, members
        and the Desk get nothing, the owner no file, and no start without a
        fresh step-up (a stop never needs one)."""
        item = record_one(rec)
        files = (("GET", "/media/recording/" + item["files"]["video"]),
                 ("HEAD", "/media/recording/" + item["files"]["meta"]),
                 ("DELETE", "/api/recordings/" + item["name"]))
        control = (("POST", "/api/recordings/start"), ("POST", "/api/recordings/stop"))
        for role in ("member", "operator"):
            client = stepped_up(tunnel_client(rec.console, role))
            for method, path in files + control + (("GET", "/api/recordings"),):
                response = client.request(method, path)
                assert response.status_code == 403, (role, method, path)
                if method != "HEAD":
                    assert response.json()["code"] == "forbidden_role"
        fresh = stepped_up(tunnel_client(rec.console, "owner"))
        for method, path in files:
            response = fresh.request(method, path)
            assert response.status_code == 403, (method, path)
            if method != "HEAD":
                assert response.json()["code"] == "forbidden_role"
        stale = tunnel_client(rec.console, "owner")                 # never stepped up
        response = stale.post("/api/recordings/start")
        assert response.status_code == 403 and response.json()["code"] == "step_up_required"
        stop = stale.post("/api/recordings/stop")
        assert stop.status_code == 409 and stop.json()["code"] == "conflict", \
            "a stop reaches the recorder without a step-up (nothing records: 409)"
        assert stale.get("/api/recordings").status_code == 200, "looking needs no step-up"
        assert audits(rec, "recording_downloaded") == [] and audits(rec, "recording_deleted") == []
        assert not rec.pipeline.recorder.active
        assert [i["name"] for i in rec.pipeline.recorder.list()] == [item["name"]]

    def test_traversal_part_files_and_unknown_names_are_404(self, rec):
        item = record_one(rec)
        (rec.dir.parent / "secret.txt").write_text("top secret", encoding="utf-8")
        (rec.dir / "rec_20261028_101010.part.avi").write_bytes(b"RIFF half written")
        (rec.dir / "notes.txt").write_text("not a recording", encoding="utf-8")
        for name in ("rec_20261028_101010.part.avi", "notes.txt", "..%2Fsecret.txt",
                     "%2e%2e%2fsecret.txt", "..%5Csecret.txt", f"{item['name']}.mp4",
                     f"{item['name']}.AVI", "secret.txt"):
            response = rec.client.get(f"/media/recording/{name}")
            assert response.status_code == 404, name
            assert "top secret" not in response.text and "half written" not in response.text
        for name in ("..%2F..%2Fx", "not-a-recording", "rec_1"):
            assert rec.client.delete(f"/api/recordings/{name}").status_code == 404, name
        assert (rec.dir.parent / "secret.txt").exists()

    @pytest.mark.parametrize("body, content_type", [
        (b"not json", "application/json"),
        (b"[1, 2]", "application/json"),
        (b'{"label": 5}', "application/json"),
        (json.dumps({"label": "x" * 201}).encode(), "application/json"),
        (b"x" * 5000, "application/json"),
    ])
    def test_bad_bodies_are_400(self, rec, body, content_type):
        response = rec.client.post("/api/recordings/start", content=body,
                                   headers={"content-type": content_type})
        assert response.status_code == 400 and response.json()["code"] == "invalid_args"
        assert not rec.pipeline.recorder or not rec.pipeline.recorder.active

    def test_recorder_errors_keep_their_code(self, rec, monkeypatch):
        rec.pipeline._raw = None
        early = rec.client.post("/api/recordings/start", json={})
        assert early.status_code == 409 and early.json()["code"] == "conflict"
        assert "camera" in early.json()["detail"]
        rec.pipeline._raw = np.zeros((48, 64, 3), np.uint8)

        def refuse(**_):
            raise RecorderError("invalid_args", "fps must be a number", 400)

        monkeypatch.setattr(rec.pipeline, "start_recording", refuse)
        bad = rec.client.post("/api/recordings/start", json={})
        assert bad.status_code == 400 and bad.json() == {"detail": "fps must be a number",
                                                         "code": "invalid_args"}

    def test_low_disk_is_a_conflict(self, rec, monkeypatch):
        from sentrix.io import recorder as recorder_module
        monkeypatch.setattr(recorder_module, "settings", replace(
            settings, recording=replace(settings.recording, min_free_mb=10 ** 9)))
        refused = rec.client.post("/api/recordings/start", json={})
        assert refused.status_code == 409 and "MB free" in refused.json()["detail"]

    def test_starts_are_rate_limited(self, rec):
        from sentrix.web import recording_routes
        rec.pipeline._raw = None                     # every start refused: they still count
        for _ in range(recording_routes.CONTROL_PER_MINUTE):
            assert rec.client.post("/api/recordings/start", json={}).status_code == 409
        rec.pipeline._raw = np.zeros((48, 64, 3), np.uint8)
        limited = rec.client.post("/api/recordings/start", json={})
        assert limited.status_code == 429 and limited.json()["code"] == "rate_limited"
        assert limited.headers["retry-after"] == "60"
        assert not rec.pipeline.recorder or not rec.pipeline.recorder.active

    def test_a_stop_is_never_rate_limited(self, rec):
        # The camera and the microphone keep filming the room until a stop
        # gets through: it is never refused with 429, whatever came before.
        from sentrix.web import recording_routes
        for _ in range(3 * recording_routes.CONTROL_PER_MINUTE):
            assert rec.client.post("/api/recordings/stop").status_code == 409
        assert rec.client.post("/api/recordings/start", json={}).status_code == 200
        for _ in range(recording_routes.CONTROL_PER_MINUTE - 1):
            busy = rec.client.post("/api/recordings/start", json={})
            assert busy.status_code == 409 and busy.json()["code"] == "conflict"
        assert rec.client.post("/api/recordings/start", json={}).status_code == 429
        stopped = rec.client.post("/api/recordings/stop")
        assert stopped.status_code == 200, stopped.text
        assert rec.pipeline.recorder.wait(10.0) and not rec.pipeline.recorder.active


class TestThroughThePortal:
    """D20 as amended on 2026-10-28: the owner starts and stops recordings
    through the Portal with a fresh step-up (the siren test's rule) and lists
    them; downloads and deletes stay on the LAN."""

    def test_the_owner_with_a_fresh_step_up_starts_and_stops(self, rec):
        fresh = owner(time.time())
        started = through_portal(rec, "POST", "/api/recordings/start", fresh,
                                 json.dumps({"label": "From the Portal"}).encode())
        assert started.status == 200, started.body
        status = json.loads(started.body)
        assert status["recording"] and status["name"].endswith("_from_the_portal")
        feed(rec.pipeline)
        stopped = through_portal(rec, "POST", "/api/recordings/stop", fresh)
        assert stopped.status == 200 and not json.loads(stopped.body)["recording"]
        assert rec.pipeline.recorder.wait(10.0)
        assert [item["name"] for item in rec.pipeline.recorder.list()] == [status["name"]]
        assert [row["by"] for row in audits(rec, "recording_started")] == ["portal:u1"]
        assert [row["by"] for row in audits(rec, "recording_stopped")] == ["portal:u1"]

    @pytest.mark.parametrize("step_up_at", [None, -3600.0, -301.0])
    def test_without_a_fresh_step_up_nothing_starts(self, rec, step_up_at):
        actor = owner(None if step_up_at is None else time.time() + step_up_at)
        result = through_portal(rec, "POST", "/api/recordings/start", actor)
        assert result.status == 403, step_up_at
        assert json.loads(result.body)["code"] == "step_up_required"
        assert rec.pipeline.recorder is None, "refused before any handler ran"

    def test_the_owner_stops_a_recording_without_a_step_up(self, rec):
        """A step-up lasts 5 min and a recording up to 10: stopping the filming
        of the room is never blocked, whoever started it."""
        assert rec.client.post("/api/recordings/start", json={}).status_code == 200
        stopped = through_portal(rec, "POST", "/api/recordings/stop", owner())
        assert stopped.status == 200, stopped.body
        assert rec.pipeline.recorder.wait(10.0) and not rec.pipeline.recorder.active
        assert [row["by"] for row in audits(rec, "recording_stopped")] == ["portal:u1"]

    def test_downloads_and_deletes_are_refused(self, rec):
        item = record_one(rec)                                   # recorded on the LAN
        for actor in (owner(time.time()), Actor("member", id="m1"),
                      Actor("operator", id="o1")):
            for method, path in (("GET", "/media/recording/" + item["files"]["video"]),
                                 ("HEAD", "/media/recording/" + item["files"]["meta"]),
                                 ("DELETE", "/api/recordings/" + item["name"])):
                result = through_portal(rec, method, path, actor)
                assert result.status == 403, (actor.type, method, path)
                assert json.loads(result.body)["code"] == "forbidden_role"
        assert audits(rec, "recording_downloaded") == [] and audits(rec, "recording_deleted") == []
        assert [i["name"] for i in rec.pipeline.recorder.list()] == [item["name"]]

    def test_members_and_the_desk_get_nothing(self, rec):
        for actor in (Actor("member", id="m1", step_up_at=time.time()),
                      Actor("operator", id="o1", step_up_at=time.time())):
            for method, path in (("GET", "/api/recordings"), ("POST", "/api/recordings/start"),
                                 ("POST", "/api/recordings/stop")):
                result = through_portal(rec, method, path, actor)
                assert result.status == 403, (actor.type, method, path)
                assert json.loads(result.body)["code"] == "forbidden_role"
        assert rec.pipeline.recorder is None, "refused before any handler ran"

    def test_the_listing_says_where_the_files_are(self, rec, monkeypatch):
        from sentrix.web import recording_routes
        monkeypatch.setattr(recording_routes, "_device_address", lambda: "192.168.29.9")
        monkeypatch.setenv("SENTRIX_PORT", "8080")
        item = record_one(rec)
        result = through_portal(rec, "GET", "/api/recordings", owner())   # no step-up needed
        assert result.status == 200, result.body
        assert "hunter2" not in result.body.decode()
        body = json.loads(result.body)
        assert [i["name"] for i in body["items"]] == [item["name"]]
        assert body["via"] == "portal"
        assert body["download_here"] is False and body["can_delete"] is False
        assert body["device_address"] == "192.168.29.9"
        assert body["lan_url"] == "http://192.168.29.9:8080", "the device's port, not the Portal's"
        lan = rec.client.get("/api/recordings").json()
        assert lan["via"] == "lan" and lan["can_delete"] is True and lan["download_here"] is True
        assert lan["device_address"] is None and lan["lan_url"] is None

    def test_an_unknown_address_has_no_url(self, rec, monkeypatch):
        from sentrix.web import recording_routes
        monkeypatch.setattr(recording_routes, "_device_address", lambda: None)
        body = json.loads(through_portal(rec, "GET", "/api/recordings", owner()).body)
        assert body["device_address"] is None and body["lan_url"] is None
        assert body["download_here"] is False and body["can_delete"] is False

    @pytest.mark.parametrize("address, port, url", [
        ("192.168.29.9", None, "http://192.168.29.9:8000"),
        ("192.168.29.9", "9000", "http://192.168.29.9:9000"),
        ("fe80::1", None, "http://[fe80::1]:8000"),
        (None, None, None), ("", "9000", None)])
    def test_lan_url(self, monkeypatch, address, port, url):
        from sentrix.web import recording_routes
        if port is None:
            monkeypatch.delenv("SENTRIX_PORT", raising=False)
        else:
            monkeypatch.setenv("SENTRIX_PORT", port)
        assert recording_routes.lan_url(address) == url


def request_from(client_host, server_host="192.168.29.9"):
    """Just what same_machine reads from a request."""
    return SimpleNamespace(client=SimpleNamespace(host=client_host) if client_host else None,
                           scope={"server": (server_host, 8000)})


class TestNoDownloadOntoTheDevice:
    """A download in a browser on the Pi itself would land on its SD card."""

    @pytest.mark.parametrize("client, same", [
        ("127.0.0.1", True), ("::1", True), ("::ffff:127.0.0.1", True),
        ("192.168.29.9", True),                 # the Pi opening its own address
        ("::ffff:192.168.29.9", True),
        ("192.168.29.28", False),               # the laptop
        ("testclient", False), (None, False)])
    def test_same_machine(self, client, same):
        from sentrix.web import recording_routes
        assert recording_routes.same_machine(request_from(client)) is same

    @pytest.mark.parametrize("mode, on_pi, client, allowed", [
        ("auto", True, "127.0.0.1", False),     # the Pi's own desktop browser
        ("auto", True, "192.168.29.9", False),
        ("auto", True, "192.168.29.28", True),  # the laptop on the same Wi-Fi
        ("auto", False, "127.0.0.1", True),     # a laptop running SENTRIX itself
        ("1", True, "127.0.0.1", True),
        ("0", False, "127.0.0.1", False),
        ("0", True, "192.168.29.28", True)])
    def test_downloads_allowed(self, monkeypatch, mode, on_pi, client, allowed):
        from sentrix.io import sensors
        from sentrix.web import recording_routes
        monkeypatch.setattr(sensors, "is_raspberry_pi", lambda root="/": on_pi)
        cfg = replace(settings.recording, download_on_device=mode)
        assert recording_routes.downloads_allowed(request_from(client), cfg) is allowed

    def test_refused_on_the_device_and_the_card_names_the_laptop_address(self, rec,
                                                                          monkeypatch):
        from sentrix.web import recording_routes
        item = record_one(rec)
        monkeypatch.setattr(recording_routes, "downloads_allowed", lambda request, cfg: False)
        monkeypatch.setattr(recording_routes, "_device_address", lambda: "192.168.29.9")
        refused = rec.client.get("/media/recording/" + item["files"]["video"])
        assert refused.status_code == 409 and refused.json()["code"] == "conflict"
        assert "SD card" in refused.json()["detail"]
        assert rec.client.head("/media/recording/" + item["files"]["video"]).status_code == 409
        assert audits(rec, "recording_downloaded") == [], "nothing left the device"
        body = rec.client.get("/api/recordings").json()
        assert body["download_here"] is False and body["device_address"] == "192.168.29.9"

    def test_other_computers_download_as_before(self, rec):
        item = record_one(rec)
        body = rec.client.get("/api/recordings").json()
        assert body["download_here"] is True and body["device_address"] is None
        assert rec.client.get("/media/recording/" + item["files"]["video"]).status_code == 200


class FakeMic:
    def __init__(self, status):
        self._status = status
        self.available = bool(status.get("available"))

    def status(self):
        return dict(self._status)


MIC = {"available": True, "device": "USB PnP Sound Device", "rate": 48000, "channels": 1}


class TestHealthAndNode:
    def test_health_reports_the_microphone_and_the_recording(self, rec):
        fake = rec.console.pipeline
        body = rec.client.get("/api/health").json()
        assert body["microphone"] is None and body["recording"] == {"active": False}
        fake.audio = FakeMic(MIC)
        fake.recorder = rec.pipeline.get_recorder()
        name = rec.pipeline.start_recording(label="", by="lc:device_admin")["name"]
        body = rec.client.get("/api/health").json()
        assert body["microphone"]["device"] == "USB PnP Sound Device"
        assert body["recording"] == {"active": True}, "no name: it carries the label"
        assert name not in json.dumps(body)
        assert body["audio"] is True, "the existing keys stay"

    def test_node_reports_the_microphone(self, rec, monkeypatch):
        from sentrix.io.actuators import ConsoleSiren
        from sentrix.web import node_routes
        node_pipeline = SimpleNamespace(siren=ConsoleSiren(), audio=None,
                                        source=SimpleNamespace(name="file:x.mp4", online=True))
        monkeypatch.setattr(node_routes, "_pipeline", lambda: node_pipeline)
        assert rec.client.get("/api/node").json()["microphone"] is None
        node_pipeline.audio = FakeMic(MIC)
        assert rec.client.get("/api/node").json()["microphone"]["rate"] == 48000
        del node_pipeline.audio
        assert rec.client.get("/api/node").json()["microphone"] is None


class TestDashboardCard:
    def test_the_device_admin_and_the_owner_get_the_card(self, rec):
        page = rec.client.get("/dashboard").text
        assert 'id="record-card"' in page and "js/recording.js" in page
        assert "Record video" in page and "not through the Portal" in page
        remote = tunnel_client(rec.console, "owner").get("/dashboard")
        assert remote.status_code == 200
        assert 'id="record-card"' in remote.text and "js/recording.js" in remote.text
        assert "Start and stop recordings here" in remote.text
        assert "on the home Wi-Fi" in remote.text and "not through the Portal" not in remote.text
        for role in ("member", "operator"):
            remote = tunnel_client(rec.console, role).get("/dashboard")
            assert remote.status_code == 200
            assert 'id="record-card"' not in remote.text and "recording.js" not in remote.text
