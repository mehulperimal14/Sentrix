"""Track T-WEB, after the switch to the device app (D22): the Local Console's
shell, capabilities, test-mode call button, SOS wording and device data.

The console's pages are the React device app (``frontend/apps/device``,
built into ``sentrix/web/static/app``). Here: the shell is served on the LAN
for ``/`` and every app path, never through the tunnel; it and its hashed
assets load under the strict CSP; each role gets only its capabilities from
``/api/session``; the "Call 112 now" button never dials 112 in test mode;
the SOS contract's wording (docs/7 §7.10) is in the apps' sources; the data
the old pages rendered comes back from the JSON API with the owner view and
URL-encoded links. No pipeline, camera or network: a fake pipeline, a
temporary database and temporary media directories.

The fixtures here are shared by the other ``test_web_*`` and
``test_device_api_v2_*`` modules.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

PASSWORD = "correct horse battery"
BASE = "/d/abc"
XSS = "\"'><script>alert(1)</script><img src=x onerror=alert(2)>"
ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "sentrix" / "web" / "static" / "app"
APP_SOURCES = (ROOT / "frontend" / "apps" / "device" / "src",
               ROOT / "frontend" / "apps" / "portal" / "src")
#: Paths of the device app (its router), which the shell answers on the LAN.
APP_PATHS = ("/", "/login", "/setup/wifi", "/admin", "/admin/recordings",
             "/admin/activity", "/admin/faces", "/dashboard", "/events", "/sos")


# ── a pipeline without models or a camera ────────────────────────────────────
class _Identity:
    def __init__(self):
        self.enrolled_files: set[str] = set()
        self.enrolled: dict[str, object] = {}
        self.backend: str | None = "insightface"
        self.removed: list[str] = []

    def assess(self, image, person):
        return SimpleNamespace(ok=True, reason="ok", embedding=np.zeros(4))

    def enrol(self, filename, embedding):
        self.enrolled_files.add(filename)
        self.enrolled[filename] = embedding
        return Path(filename).stem

    def remove(self, filename):
        self.removed.append(filename)
        self.enrolled_files.discard(filename)


class _Events:
    def __init__(self):
        self.rows: list[tuple[str, dict]] = []

    def audit(self, event, **detail):
        self.rows.append((event, detail))


class FakePipeline:
    session_id = "sess-test"
    preview_seq = 0

    def __init__(self):
        self.identity = _Identity()
        self.events = _Events()
        self.source = SimpleNamespace(online=True,
                                      name="capture:rtsp://admin:hunter2@10.0.0.2/s1")
        self.detector = SimpleNamespace(available=True)
        self.scene = SimpleNamespace(weights_name="scene.pt", device="cpu", available=True,
                                     thresholds={"weapon": 0.5})
        self.violence = None
        self.audio = None
        self.tci = SimpleNamespace(model_source="rules")
        self.frame = np.zeros((48, 64, 3), np.uint8)

    def latest_raw(self):
        return self.frame

    def latest_jpeg(self, quality=70):
        return 0, None


# ── fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture
def console(temp_db, tmp_path, monkeypatch):
    """The LC app with a fake pipeline, temporary media and a known password."""
    from sentrix.app import app
    from sentrix.evidence.vault import EvidenceVault
    from sentrix.pipeline.state import state
    from sentrix.web import routes, setup_routes, streaming
    from sentrix.web.security import RateLimiter, login_limiter

    pipeline = FakePipeline()
    snapshots, faces = tmp_path / "snapshots", tmp_path / "faces"
    snapshots.mkdir()
    faces.mkdir()
    monkeypatch.setenv("SENTRIX_PASSWORD", PASSWORD)
    monkeypatch.setattr(routes, "get_pipeline", lambda: pipeline)
    monkeypatch.setattr(streaming, "get_pipeline", lambda: pipeline)
    monkeypatch.setattr(routes, "SNAPSHOT_DIR", snapshots)
    monkeypatch.setattr(routes, "FACES_DIR", faces)
    monkeypatch.setattr(routes, "vault", EvidenceVault(tmp_path / "evidence"))
    monkeypatch.setattr(routes, "capture_limiter", RateLimiter(max_attempts=20, window_seconds=60.0))
    setup_routes.setup_limiter.reset("testclient")
    login_limiter.reset("testclient")
    state.reset()
    yield SimpleNamespace(app=app, pipeline=pipeline, snapshots=snapshots, faces=faces,
                          tmp=tmp_path, vault=routes.vault)
    state.reset()


def lc_client(console, **kwargs):
    """A signed-in device-admin browser: (client, csrf token)."""
    from fastapi.testclient import TestClient
    from sentrix.web import auth
    from sentrix.web.security import generate_token
    client = TestClient(console.app, **kwargs)
    cookie = generate_token(PASSWORD)
    client.cookies.set("lc_session", cookie)
    return client, auth.csrf_for_session(cookie)


@pytest.fixture(autouse=True)
def consented_household(monkeypatch):
    """Tunnelled users of a household that consented to remote viewing and
    face recognition (the console hides what the path table would refuse
    without them; ``TestConsents`` covers that)."""
    from sentrix.link import policy
    monkeypatch.setattr(policy, "_profile_consents",
                        lambda: {"remote_view": True, "face_recognition": True})


def tunnel_client(console, role="owner", **kwargs):
    """A Portal user replayed in-process by the link (``/d/abc``)."""
    from fastapi.testclient import TestClient
    from sentrix.link import tunnel_auth
    client = TestClient(console.app, client=tunnel_auth.TUNNEL_CLIENT, **kwargs)
    client.headers.update(tunnel_auth.build_headers(
        user_id="u-1", user_name="Riya Sharma", role=role, base=BASE,
        step_up_at=None, csrf="portal-csrf-token"))
    return client


def add_incident(**fields):
    """An incident row with every field the pages show."""
    from sentrix.storage import database
    from sentrix.storage.models import IncidentRecord
    values = dict(id=fields.pop("id", "inc-1"), session_id="s1",
                  started_at=datetime(2026, 10, 14, 9, 0, 0), ended_at=None,
                  duration_seconds=4.0, peak_level=3, peak_tci=0.61, incident_type="weapon",
                  reason="scene weapon 0.71 confirmed", frames=12, evidence_count=1,
                  snapshot_path="snap_1.jpg", evidence_bundle="L3_bundle_1",
                  authorized=False, camera_id="CAM_0")
    values.update(fields)
    with database.session_scope() as session:
        session.add(IncidentRecord(**values))
    return values["id"]


def add_package(pid="pkg-1", incident_id="inc-1", status="PENDING", **fields):
    from sentrix.alerting.dispatch import DispatchPackage
    from sentrix.storage import database
    values = dict(id=pid, created_at="2026-10-14T09:00:05+00:00", incident_id=incident_id,
                  incident_type="weapon", level=3, tci=0.61, user_name="Riya",
                  user_address="12 Model Town", user_phone="", camera_location="Hall",
                  evidence_bundle=None, evidence_hash=None, authority="POLICE",
                  status=status, payload_json="{}")
    request_id = fields.pop("request_id", None)
    values.update(fields)
    package = DispatchPackage(**values)
    package.request_id = request_id          # set by the SOS engine on request packages
    database.save_dispatch(package)
    return pid


def add_evidence(console, bundle="L3_bundle_1", detections=None):
    record = {"bundle_id": bundle, "file": f"{bundle}.enc", "timestamp": "2026-10-14T09:00:02Z",
              "incident_id": "inc-1", "camera_id": "CAM_0", "tci": 0.61, "level": 3,
              "status": "ELEVATED", "incident_type": "weapon", "reason": "r",
              "detections": detections if detections is not None else
              [{"label": "pistol", "conf": 0.8213, "bbox": [1, 2, 3, 4], "track_id": 7}],
              "sha256": "ab" * 32}
    (console.vault.dir / f"{bundle}.json").write_text(json.dumps(record), encoding="utf-8")


def populate(console):
    """One of everything a page links to."""
    add_incident()
    add_package()
    add_evidence(console)
    (console.snapshots / "snap_1.jpg").write_bytes(b"\xff\xd8\xff\xe0jpeg")
    (console.faces / "alice.jpg").write_bytes(b"\xff\xd8\xff\xe0jpeg")
    console.pipeline.identity.enrolled_files.add("alice.jpg")


# ── the shell's markup ────────────────────────────────────────────────────────
class _Collector(HTMLParser):
    URL_ATTRS = ("href", "src")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []
        self.handlers: list[str] = []
        self.styles: list[str] = []
        self.scripts: list[dict] = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name.startswith("on"):
                self.handlers.append(f"<{tag} {name}>")
            if name == "style":
                self.styles.append(f"<{tag} style={value!r}>")
            if name in self.URL_ATTRS and value is not None:
                self.urls.append(value)
        if tag == "script":
            self.scripts.append(dict(attrs))
        if tag == "style":
            self.styles.append("<style> element")


def inspect_html(text: str) -> _Collector:
    collector = _Collector()
    collector.feed(text)
    return collector


def shell_urls() -> list[str]:
    """Every URL the built ``index.html`` references."""
    return inspect_html((APP_DIR / "index.html").read_text(encoding="utf-8")).urls


def asset_file(url: str) -> Path:
    return APP_DIR / url.split("?")[0][len("/static/app/"):]


def app_sources(*apps: str) -> str:
    """The apps' TypeScript sources (not their tests) as one text, JSX
    entities decoded."""
    roots = [r for r in APP_SOURCES if not apps or r.parent.name in apps]
    files = [f for root in roots for f in sorted(root.rglob("*.ts*"))
             if f.suffix in (".ts", ".tsx") and ".test." not in f.name
             and "__tests__" not in f.parts]
    assert files, f"no sources under {roots}"
    text = "\n".join(f.read_text(encoding="utf-8") for f in files)
    return text.replace("&apos;", "'").replace("&rsquo;", "’")


# ── tests: the build ──────────────────────────────────────────────────────────
class TestTheBuild:
    def test_index_and_every_asset_it_references_exist(self):
        index = APP_DIR / "index.html"
        assert index.is_file(), "build it: cd frontend && npm run build -w @sentrix/device"
        urls = shell_urls()
        assert any(u.endswith(".js") for u in urls) and any(u.endswith(".css") for u in urls)
        for url in urls:
            assert url.startswith("/static/app/"), f"{url}: not a file of this build"
            assert asset_file(url).is_file(), url

    def test_chunks_and_fonts_the_entry_loads_exist(self):
        assets = APP_DIR / "assets"
        for path in assets.glob("*.css"):
            for url in re.findall(r"url\(([^)]+)\)", path.read_text(encoding="utf-8")):
                url = url.strip("'\"")
                if url.startswith("data:"):
                    continue
                assert url.startswith("/static/app/"), (path.name, url)
                assert asset_file(url).is_file(), (path.name, url)
        for path in assets.glob("*.js"):
            text = path.read_text(encoding="utf-8")
            for name in re.findall(r"\"\./([\w.-]+\.(?:js|css))\"", text):
                assert (assets / name).is_file(), (path.name, name)
            for name in re.findall(r"\"assets/([\w.-]+\.(?:js|css))\"", text):
                assert (assets / name).is_file(), (path.name, name)

    def test_assets_are_content_hashed(self):
        """Replaces the old ``?v=`` stamps: a new build changes every name."""
        for url in shell_urls():
            if url.endswith((".js", ".css")):
                assert re.search(r"-[\w-]{8}\.(js|css)$", url), url

    def test_the_build_fits_the_strict_csp(self):
        found = inspect_html((APP_DIR / "index.html").read_text(encoding="utf-8"))
        assert found.handlers == [] and found.styles == []
        assert found.scripts, "the app loads its entry script"
        for script in found.scripts:
            assert script.get("src", "").startswith("/static/app/"), "no inline script"
        for path in [APP_DIR / "index.html", *(APP_DIR / "assets").glob("*.css")]:
            text = path.read_text(encoding="utf-8")
            assert "fonts.googleapis" not in text and "@import url(http" not in text, path.name

    def test_no_app_holds_an_emergency_number(self):
        """"Call 112 now" is a link the device builds (the Desk in test mode):
        no source and no built file holds a tel: or sms: link to 112."""
        dial = re.compile(r"(tel|sms):\s*\+?112\b")
        assert not dial.search(app_sources())
        for path in (APP_DIR / "assets").glob("*.js"):
            assert not dial.search(path.read_text(encoding="utf-8")), path.name


# ── tests: the shell ──────────────────────────────────────────────────────────
class TestTheShell:
    @pytest.mark.parametrize("path", APP_PATHS)
    def test_the_lan_gets_the_app_on_every_app_path(self, console, path):
        from fastapi.testclient import TestClient
        index = (APP_DIR / "index.html").read_bytes()
        for client in (TestClient(console.app), lc_client(console)[0]):
            response = client.get(path)
            assert response.status_code == 200, path
            assert response.headers["content-type"].startswith("text/html")
            assert response.content == index, "the shell holds no data, signed in or not"

    @pytest.mark.parametrize("path", APP_PATHS)
    def test_never_through_the_tunnel(self, console, path):
        from fastapi.testclient import TestClient
        from sentrix.link import tunnel_auth
        for client in (tunnel_client(console), tunnel_client(console, "member"),
                       tunnel_client(console, "operator"),
                       TestClient(console.app, client=tunnel_auth.TUNNEL_CLIENT)):
            response = client.get(path)
            assert response.status_code == 404, path
            assert response.json()["code"] == "not_found"
            assert b'id="root"' not in response.content

    def test_server_paths_are_never_the_app(self, console):
        client, _ = lc_client(console)
        for path in ("/api/no-such-thing", "/media/nothing", "/ws/nothing", "/openapi.json",
                     "/docs", "/redoc", "/favicon.ico", "/static/app/assets/missing.js"):
            response = client.get(path)
            assert response.status_code == 404, path
            assert not response.headers.get("content-type", "").startswith("text/html"), path
        session = client.get("/api/session")
        assert session.status_code == 200 and session.json()["signed_in"] is True

    def test_the_shell_carries_the_csp_and_security_headers(self, console):
        from fastapi.testclient import TestClient
        response = TestClient(console.app).get("/admin/recordings")
        csp = response.headers["content-security-policy"]
        directives = dict(part.strip().split(" ", 1) for part in csp.split(";"))
        assert directives["default-src"] == "'self'"
        assert directives["script-src"] == "'self'" and directives["style-src"] == "'self'"
        assert directives["font-src"] == "'self'" and directives["object-src"] == "'none'"
        assert directives["frame-ancestors"] == "'none'" and directives["base-uri"] == "'none'"
        assert "ws://testserver" in directives["connect-src"]
        for loose in ("unsafe-inline", "unsafe-eval", "nonce-", "https:", "*"):
            assert loose not in csp, loose
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "same-origin"
        assert "camera=()" in response.headers["permissions-policy"]
        assert response.headers["cache-control"] == "no-cache, must-revalidate", \
            "a new build is picked up at the next load"

    def test_the_assets_load_from_this_origin_under_that_csp(self, console):
        from fastapi.testclient import TestClient
        client = TestClient(console.app)
        for url in shell_urls():
            response = client.get(url)
            assert response.status_code == 200, url
            kind = response.headers["content-type"]
            if url.endswith(".js"):
                assert "javascript" in kind, (url, kind)
            elif url.endswith(".css"):
                assert kind.startswith("text/css"), (url, kind)
            assert response.headers.get("cache-control") != "no-store", "hashed: cacheable"

    def test_the_old_server_rendered_ui_is_gone(self, console):
        import sentrix.paths
        web = ROOT / "sentrix" / "web"
        assert not (web / "templates").exists()
        assert not (web / "static" / "js").exists() and not (web / "static" / "css").exists()
        assert not (web / "static" / "sounds").exists(), "no siren sound (D25)"
        assert APP_DIR.is_dir()
        assert not hasattr(sentrix.paths, "TEMPLATES_DIR")
        client, csrf = lc_client(console)
        assert client.get("/static/js/app.js").status_code == 404
        for path in ("/login", "/logout", "/setup/wifi", "/setup/wifi/scan"):
            refused = client.post(path, headers={"X-CSRF-Token": csrf},
                                  data={"password": PASSWORD, "code": "x"})
            assert refused.status_code == 404, (path, refused.status_code)
            assert refused.json()["code"] == "not_found"
            assert "set-cookie" not in refused.headers, path

    def test_a_write_to_no_route_is_a_json_404(self, console):
        """Never the app, never a 405 from its GET-only catch-all."""
        client, csrf = lc_client(console)
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            for path in ("/api/no-such-thing", "/api/node/buzzer-test", "/admin/faces"):
                response = client.request(method, path, headers={"X-CSRF-Token": csrf})
                assert response.status_code == 404, (method, path)
                assert response.json()["code"] == "not_found"


# ── tests: what each role may use ─────────────────────────────────────────────
def capabilities_of(client) -> set[str]:
    return set(client.get("/api/session").json()["capabilities"])


class TestCapabilities:
    def test_the_device_admin_uses_everything_on_the_lan(self, console):
        body = lc_client(console)[0].get("/api/session").json()
        assert {"media", "gallery_view", "gallery_write", "verdict", "sos", "dispatch", "mode",
                "record"} <= set(body["capabilities"])
        assert "siren_test" not in body["capabilities"], "no siren (D25)"
        assert body["lan_admin"] is True and body["sos"]["demo_trigger"] is True

    def test_the_owner_edits_the_gallery_and_records(self, console):
        body = tunnel_client(console, "owner").get("/api/session").json()
        assert {"gallery_write", "record", "verdict", "sos", "mode"} <= set(
            body["capabilities"])
        assert body["lan_admin"] is False, "the admin's views stay on the LAN"
        assert body["sos"]["demo_trigger"] is False

    def test_a_member_sees_the_gallery_read_only(self, console):
        caps = capabilities_of(tunnel_client(console, "member"))
        assert {"gallery_view", "verdict", "sos", "dispatch", "mode"} <= caps
        assert not caps & {"gallery_write", "record"}

    def test_the_desk_operator_gets_nothing_to_act_on(self, console):
        body = tunnel_client(console, "operator").get("/api/session").json()
        assert body["capabilities"] == [] and body["lan_admin"] is False
        assert body["media"] == {"snapshots": False, "evidence": False, "faces": False}
        assert body["sos"]["demo_close_incident"] is False

    def test_portal_users_sign_out_on_the_portal(self, console):
        owner = tunnel_client(console)
        body = owner.get("/api/session").json()
        assert body["via"] == "portal" and body["user"]["name"] == "Riya Sharma"
        assert owner.post("/api/logout").status_code == 404

    def test_the_scripted_trigger_offers_what_the_service_accepts(self, console, monkeypatch):
        """An intrusion opens a request only with SENTRIX_SOS_INTRUSION=1, so
        the app offers it only then (D8, D15)."""
        import dataclasses
        from sentrix.web import api_routes
        client, _ = lc_client(console)
        off = dataclasses.replace(api_routes.settings, sos=dataclasses.replace(
            api_routes.settings.sos, intrusion_requests=False))
        monkeypatch.setattr(api_routes, "settings", off)
        assert client.get("/api/session").json()["sos"]["intrusion_requests"] is False
        on = dataclasses.replace(off, sos=dataclasses.replace(off.sos, intrusion_requests=True))
        monkeypatch.setattr(api_routes, "settings", on)
        assert client.get("/api/session").json()["sos"]["intrusion_requests"] is True


# ── tests: test mode and the call button ──────────────────────────────────────
class TestTestModeAndTheCallButton:
    def test_the_session_says_test_mode(self, console, monkeypatch):
        from sentrix.web import routes
        client, _ = lc_client(console)
        assert client.get("/api/session").json()["test_mode"] is True
        monkeypatch.setattr(routes, "_test_mode", lambda: False)
        assert client.get("/api/session").json()["test_mode"] is False

    def test_call_112_never_dials_112_in_test_mode(self, console, monkeypatch):
        monkeypatch.setenv("SENTRIX_DESK_NUMBER", "+919812345678")
        for client in (lc_client(console)[0], tunnel_client(console),
                       tunnel_client(console, "member")):
            response = client.get("/api/session")
            call = response.json()["call"]
            assert call["href"] == "tel:+919812345678" and call["desk"] is True
            assert "tel:112" not in response.text and "sms:112" not in response.text
        monkeypatch.setenv("SENTRIX_DESK_NUMBER", "")
        call = lc_client(console)[0].get("/api/session").json()["call"]
        assert call["enabled"] is False and call["href"] is None and call["number"] is None

    def test_an_emergency_number_is_never_the_desk_number(self, console, monkeypatch):
        monkeypatch.setenv("SENTRIX_DESK_NUMBER", "112")
        response = lc_client(console)[0].get("/api/session")
        assert response.json()["call"]["enabled"] is False and "tel:112" not in response.text

    def test_call_112_outside_test_mode(self, console, monkeypatch):
        from sentrix.web import routes
        monkeypatch.setattr(routes, "_test_mode", lambda: False)
        call = lc_client(console)[0].get("/api/session").json()["call"]
        assert call["href"] == "tel:112" and call["desk"] is False


# ── tests: the SOS contract's wording in the apps (docs/7 §7.10) ──────────────
SOS_CONTRACT = ("Alert my contacts and the SENTRIX desk", "Call 112 now", "I'm checking",
                "False alarm (PIN)", "I'm safe now (PIN)", "Request help now",
                "SENTRIX does not contact 112 itself.")


class TestSosWording:
    @pytest.mark.parametrize("app", ["device", "portal"])
    def test_the_contract_labels_are_in_the_app(self, app):
        text = app_sources(app)
        for words in SOS_CONTRACT:
            assert words in text, (app, words)
        lowered = text.lower()
        assert "possible (one camera check)" in lowered, app
        assert "confirmed by two different models" in lowered, app

    def test_the_portal_alert_screen_has_every_answer(self):
        screen = (APP_SOURCES[1] / "features" / "alert" / "AlertScreen.tsx").read_text(
            encoding="utf-8").replace("&apos;", "'")
        for words in SOS_CONTRACT:
            if words != "Call 112 now":           # its label comes from lib/data.ts
                assert words in screen, words

    def test_the_device_alert_takeover_has_every_answer(self):
        takeover = (APP_SOURCES[0] / "components" / "AlertTakeover.tsx").read_text(
            encoding="utf-8").replace("&apos;", "'")
        for words in SOS_CONTRACT:
            if words != "Call 112 now":           # its label comes from lib/api.ts
                assert words in takeover, words


# ── tests: live indicators ────────────────────────────────────────────────────
class TestLiveIndicators:
    def test_remote_viewers_and_the_channels_warning_follow_the_state(self, console):
        from sentrix.pipeline.state import CHANNELS_IDLE, state
        client, _ = lc_client(console)
        live = client.get("/api/state").json()
        assert live["remote_viewers"] == 0 and live["channels"]["warn"] is False
        state.set_remote_viewers(2)
        state.update(channels={**CHANNELS_IDLE, "warn": True})
        live = client.get("/api/state").json()
        assert live["remote_viewers"] == 2 and live["channels"]["warn"] is True
        assert client.get("/api/session").json()["link"]["remote_viewers"] == 2

    def test_the_sos_block_reaches_owner_and_member_alike(self, console):
        from sentrix.pipeline.state import SOS_IDLE, state
        state.update(sos={**SOS_IDLE, "active": True, "request_id": "r1", "short_id": "SX-R1",
                          "state": "PENDING", "label": "Waiting for your answer",
                          "actions": ["call_112", "approve", "checking", "decline"]})
        for client in (lc_client(console)[0], tunnel_client(console, "member")):
            sos = client.get("/api/state").json()["sos"]
            assert sos["active"] is True and sos["actions"][0] == "call_112"


# ── tests: device data comes back as data ─────────────────────────────────────
class TestDeviceDataIsInert:
    def test_incident_reason_and_note_are_plain_json_strings(self, console):
        add_incident(id="inc-x", reason=XSS, incident_type="weapon", verdict="FALSE",
                     verdict_note=XSS, verdict_by="portal:" + XSS[:20])
        for client in (lc_client(console)[0], tunnel_client(console)):
            response = client.get("/api/activity")
            assert response.headers["content-type"].startswith("application/json")
            assert response.headers["x-content-type-options"] == "nosniff"
            row = response.json()["items"][0]
            assert row["reason"] == XSS and row["verdict_note"] == XSS

    def test_face_file_names_become_encoded_urls(self, console):
        name = "x' onerror='alert(1).jpg"
        (console.faces / name).write_bytes(b"\xff\xd8\xff")
        item = lc_client(console)[0].get("/api/authorized").json()["items"][0]
        assert item["filename"] == name
        assert item["url"] == "/media/face/x%27%20onerror%3D%27alert%281%29.jpg"

    def test_snapshot_names_become_encoded_urls(self, console):
        (console.snapshots / "a b'c.jpg").write_bytes(b"\xff\xd8\xff")
        item = lc_client(console)[0].get("/api/snapshots").json()["items"][0]
        assert item["url"] == "/media/snapshot/a%20b%27c.jpg"

    def test_package_and_evidence_fields_stay_strings(self, console):
        add_incident()
        add_package(user_address=XSS, incident_type="weapon")
        add_evidence(console, detections=[{"label": XSS, "conf": 0.5}, XSS])
        client, _ = lc_client(console)
        assert client.get("/api/dispatch").json()["items"][0]["location"] == XSS
        detections = client.get("/api/evidence").json()["items"][0]["detections"]
        assert detections[0]["label"] == XSS[:40]


# ── tests: what the old pages showed, from the API ────────────────────────────
class TestActivityData:
    def test_stats_note_and_who_when(self, console):
        add_incident(id="inc-a", verdict="TRUE", verdict_note="seen it",
                     verdict_by="lc:device_admin", verdict_at=datetime(2026, 10, 14, 9, 5))
        add_incident(id="inc-b", started_at=datetime(2026, 10, 14, 10, 0), verdict="FALSE")
        add_incident(id="inc-c", started_at=datetime(2026, 10, 14, 11, 0))
        body = lc_client(console)[0].get("/api/activity").json()
        assert body["stats"] == {"total": 3, "confirmed": 1, "false_alarms": 1,
                                 "unreviewed": 1, "false_alarm_rate": 0.5}
        reviewed = next(row for row in body["items"] if row["id"] == "inc-a")
        assert reviewed["verdict"] == "TRUE" and reviewed["verdict_note"] == "seen it"
        assert reviewed["verdict_by"] == "device admin"
        assert reviewed["verdict_at"] == "2026-10-14T09:05:00Z"

    def test_evidence_chain_verified(self, console, monkeypatch):
        monkeypatch.setattr(console.vault, "verify_chain",
                            lambda: {"ok": True, "entries": 3, "problems": []})
        chain = lc_client(console)[0].get("/api/evidence").json()["chain"]
        assert chain["ok"] is True and chain["entries"] == 3 and chain["problem_count"] == 0

    def test_dispatch_statuses_and_what_can_still_be_sent(self, console):
        add_incident()
        for pid, status in (("p-pend", "PENDING"), ("p-sent", "SENT"), ("p-canc", "CANCELLED"),
                            ("p-exp", "EXPIRED"), ("p-fail", "FAILED")):
            add_package(pid, status=status)
        body = lc_client(console)[0].get("/api/dispatch").json()
        shown = {item["id"]: item for item in body["items"]}
        assert {pid: item["status"] for pid, item in shown.items()} == {
            "p-pend": "PENDING", "p-sent": "SENT", "p-canc": "CANCELLED", "p-exp": "EXPIRED",
            "p-fail": "FAILED"}
        assert {pid for pid, item in shown.items() if item["sendable"]} == {"p-pend", "p-fail"}
        assert shown["p-pend"]["created_at"] == "2026-10-14T09:00:05Z"
        assert "MEDICAL" in body["authorities"]

    def test_dispatch_names_a_lapsed_request_for_request_help_now(self, console):
        from sentrix.storage import requests
        add_incident(request_id="req-exp")
        requests.create_request("req-exp", incident_id="inc-1", incident_type="weapon", level=3,
                                evidence_state="confirmed", policy={}, expires_at=None,
                                test_mode=True)
        requests.transition("req-exp", "prompted")
        requests.transition("req-exp", "expire")
        add_package("p-exp", status="EXPIRED", request_id="req-exp")
        item = lc_client(console)[0].get("/api/dispatch").json()["items"][0]
        assert item["request"] == {"id": "req-exp", "short_id": "SX-REQ-EX",
                                   "state": "EXPIRED", "label": "Unanswered"}

    def test_the_sos_panel_shows_a_duress_package_as_cancelled(self, console):
        """Duress parity (docs/7 §7.8): the alert screen's package of a duress
        request reads like a decline's; a silent request's is never shown."""
        from sentrix.storage import database, requests
        from sentrix.storage.models import DispatchRecord
        add_incident(request_id="req-dur")
        requests.create_request("req-dur", incident_id="inc-1", incident_type="weapon",
                                level=3, evidence_state="confirmed", policy={},
                                expires_at=None, test_mode=True, flags={"duress": True})
        requests.create_request("req-sil", incident_id=None, incident_type="duress",
                                level=3, evidence_state="confirmed", policy={},
                                expires_at=None, test_mode=True,
                                flags={"duress": True, "silent": True})
        add_package("p-dur", status="SENT", request_id="req-dur")
        add_package("p-sil", status="PENDING", request_id="req-sil", incident_type="duress",
                    incident_id=None)
        with database.session_scope() as session:
            row = session.get(DispatchRecord, "p-dur")
            row.chosen_authority, row.sent_by = "POLICE", "lc:admin"
            row.sent_to_json = json.dumps(["SENTRIX Desk", "Aman 98xxxxx611"])
        for client in (lc_client(console)[0], tunnel_client(console, "member")):
            assert client.get("/api/dispatch/latest").json() == {}, "silent: never shown"
            mine = client.get("/api/dispatch/latest?request_id=req-dur")
            assert mine.json()["status"] == "CANCELLED" and mine.json()["sent_to"] == []
            assert "Aman" not in mine.text

    def test_the_sos_panel_gets_its_package_in_any_status(self, console):
        from sentrix.storage import requests
        add_incident(request_id="req-ok")
        requests.create_request("req-ok", incident_id="inc-1", incident_type="weapon",
                                level=3, evidence_state="confirmed", policy={},
                                expires_at=None, test_mode=True)
        add_package("p-ok", status="SENT", request_id="req-ok")
        client = lc_client(console)[0]
        assert client.get("/api/dispatch/latest").json() == {}
        package = client.get("/api/dispatch/latest?request_id=req-ok").json()
        assert package["id"] == "p-ok" and package["user_address"] == "12 Model Town"
