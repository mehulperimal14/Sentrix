"""Track T-WEB: the Local Console pages on the LAN and through the Portal tunnel.

Every page is rendered twice: as the device admin (no prefix) and as a
tunnelled Portal user under ``/d/abc``. The pages must build every link,
image, form, fetch and websocket URL under that prefix, carry the CSP nonce
on every script, contain no inline handler, inline script or inline style,
render device-supplied strings inert, and show each role only what it may
use (docs/7 §9, §10). No pipeline, camera or network: a fake pipeline, a
temporary database and temporary media directories.

The fixtures here are shared by the other ``test_web_*`` modules.
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
PAGES = ("/dashboard", "/live", "/events", "/alerts", "/evidence", "/dispatch",
         "/authorized", "/sos", "/node")
STATIC_JS = Path(__file__).resolve().parents[1] / "sentrix" / "web" / "static" / "js"
TEMPLATES = Path(__file__).resolve().parents[1] / "sentrix" / "web" / "templates"


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
        self.siren = SimpleNamespace(name="console", is_real=False)
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


# ── HTML inspection ───────────────────────────────────────────────────────────
class _Collector(HTMLParser):
    URL_ATTRS = ("href", "src", "action", "formaction", "data-lightbox")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls: list[tuple[str, str, str]] = []
        self.handlers: list[str] = []
        self.styles: list[str] = []
        self.scripts: list[dict] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        for name, value in attrs:
            if name.startswith("on"):
                self.handlers.append(f"<{tag} {name}>")
            if name == "style":
                self.styles.append(f"<{tag} style={value!r}>")
            if name in self.URL_ATTRS and value is not None:
                self.urls.append((tag, name, value))
        if tag == "script":
            self.scripts.append(values)
        if tag == "style":
            self.styles.append("<style> element")


def inspect_html(text: str) -> _Collector:
    collector = _Collector()
    collector.feed(text)
    return collector


#: Absolute Portal pages the tunnelled console links to on purpose.
PORTAL_LINKS = frozenset({"/home", "/desk", "/account"})


def assert_prefixed(text: str, base: str) -> None:
    """Every URL the page holds starts with the base (or is a tel: link)."""
    found = inspect_html(text)
    assert found.urls, "the page has no links at all"
    for tag, attr, value in found.urls:
        if value.startswith("tel:"):
            continue
        if base and value in PORTAL_LINKS:
            continue                    # the tunnelled nav's way back to the Portal
        assert value.startswith(base + "/") and not value.startswith("//"), \
            f"<{tag} {attr}={value!r}> is not under {base or '/'}"
        assert "://" not in value, f"external URL {value!r}"


# ── tests: rendering under both prefixes ──────────────────────────────────────
class TestPagesUnderBothPrefixes:
    @pytest.mark.parametrize("path", PAGES)
    def test_lan_pages_keep_root_urls(self, console, path):
        populate(console)
        client, _ = lc_client(console)
        page = client.get(path)
        assert page.status_code == 200, page.text[:300]
        assert_prefixed(page.text, "")
        assert 'data-base=""' in page.text

    @pytest.mark.parametrize("path", PAGES)
    def test_tunnel_pages_prefix_every_url(self, console, path):
        populate(console)
        page = tunnel_client(console).get(path)
        assert page.status_code == 200, page.text[:300]
        assert_prefixed(page.text, BASE)
        assert f'data-base="{BASE}"' in page.text
        # The CSRF token the Portal bridged is what page scripts send back.
        assert '<meta name="csrf-token" content="portal-csrf-token">' in page.text

    @pytest.mark.parametrize("path,key", [("/dashboard", "dashboard"), ("/events", "events"),
                                          ("/sos", "sos"), ("/node", "node")])
    def test_nav_highlight_follows_the_page_not_the_path(self, console, path, key):
        page = tunnel_client(console).get(path)
        active = re.findall(r'<a href="([^"]+)" class="nav-link active"', page.text)
        assert active == [f"{BASE}{path}"]
        assert f'data-page="{key}"' in page.text

    def test_lc_session_csrf_token_is_in_the_meta_tag(self, console):
        client, csrf = lc_client(console)
        page = client.get("/dashboard")
        assert f'<meta name="csrf-token" content="{csrf}">' in page.text

    def test_logout_confirm_page_on_the_lan(self, console):
        client, _ = lc_client(console)
        page = client.get("/logout")
        assert page.status_code == 200 and 'id="logout-confirm"' in page.text
        assert_prefixed(page.text, "")

    def test_signed_out_pages_redirect_and_tunnel_gets_json(self, console):
        from fastapi.testclient import TestClient
        from sentrix.link import tunnel_auth
        page = TestClient(console.app, follow_redirects=False).get(
            "/dashboard", headers={"accept": "text/html"})
        assert page.status_code == 307 and page.headers["location"] == "/login"
        api = TestClient(console.app).get("/api/incidents")
        assert api.status_code == 401 and api.json()["code"] == "unauthorised"
        unverified = TestClient(console.app, client=tunnel_auth.TUNNEL_CLIENT,
                                follow_redirects=False).get("/events")
        assert unverified.status_code == 401


# ── tests: CSP-ready markup ───────────────────────────────────────────────────
class TestStrictCspMarkup:
    @pytest.mark.parametrize("path", PAGES + ("/logout",))
    def test_no_inline_handlers_scripts_or_styles(self, console, path):
        populate(console)
        client, _ = lc_client(console)
        page = client.get(path)
        found = inspect_html(page.text)
        assert found.handlers == [], found.handlers
        assert found.styles == [], found.styles
        nonce = re.search(r"script-src 'self' 'nonce-([^']+)'",
                          page.headers["content-security-policy"]).group(1)
        assert found.scripts, "every page loads its scripts"
        for script in found.scripts:
            assert script.get("src", "").startswith("/static/js/"), script
            assert script.get("nonce") == nonce, script

    def test_the_nonce_changes_on_every_response(self, console):
        client, _ = lc_client(console)
        first = client.get("/dashboard").headers["content-security-policy"]
        second = client.get("/dashboard").headers["content-security-policy"]
        assert first != second

    def test_templates_have_no_inline_code(self):
        for template in TEMPLATES.glob("*.html"):
            text = template.read_text(encoding="utf-8")
            assert not re.search(r"\son[a-z]+\s*=", text), f"inline handler in {template.name}"
            assert not re.search(r"<script(?![^>]*\ssrc=)", text), f"inline script in {template.name}"
            assert " style=" not in text, f"inline style in {template.name}"
            assert "fonts.googleapis" not in text and "https://" not in text, template.name

    def test_scripts_use_no_html_sinks_and_build_every_url_from_the_base(self):
        sinks = re.compile(r"\.innerHTML\b|\.outerHTML\s*=|insertAdjacentHTML|document\.write|"
                           r"\beval\(|new Function\(")
        literal = re.compile(r"""(["'`])/[A-Za-z]""")
        allowed = re.compile(r"(?:\burl|\bapi)\(\s*$")
        for script in STATIC_JS.glob("*.js"):
            text = script.read_text(encoding="utf-8")
            assert not sinks.search(text), f"HTML sink in {script.name}"
            for match in literal.finditer(text):
                before = text[max(0, match.start() - 40):match.start()]
                line = text[:match.start()].count("\n") + 1
                assert allowed.search(before), \
                    f"{script.name}:{line}: a path literal not built with url()/api()"
            assert "WebSocket(`${scheme}//${window.location.host}${url(" in text \
                or "WebSocket" not in text, f"websocket URL in {script.name} ignores the base"

    def test_no_script_holds_an_emergency_number(self):
        # "Call 112 now" is a link the server builds (the Desk in test mode);
        # no script builds a tel:/sms: link or holds the number itself.
        for script in STATIC_JS.glob("*.js"):
            text = script.read_text(encoding="utf-8")
            assert not re.search(r"(tel|sms):", text), script.name
            code = re.sub(r'("[^"\n]*"|`[^`]*`)', "", text)      # prose in messages
            assert "112" not in code, script.name


# ── tests: device data renders inert ──────────────────────────────────────────
class TestXssPayloadsAreInert:
    def test_incident_reason_note_and_type_are_escaped(self, console):
        add_incident(id="inc-x", reason=XSS, incident_type="weapon", verdict="FALSE",
                     verdict_note=XSS, verdict_by="portal:" + XSS[:20])
        for client in (lc_client(console)[0], tunnel_client(console)):
            page = client.get("/events")
            assert page.status_code == 200
            assert "<script>alert(1)" not in page.text
            assert "onerror=alert" not in page.text.replace("onerror=alert(2)&gt;", "")
            assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
            assert not inspect_html(page.text).handlers

    def test_face_file_names_are_inert(self, console):
        name = "x' onerror='alert(1).jpg"
        (console.faces / name).write_bytes(b"\xff\xd8\xff")
        page = lc_client(console)[0].get("/authorized")
        assert page.status_code == 200
        assert not inspect_html(page.text).handlers
        # The name is an attribute value and a URL-encoded path, never markup or code.
        assert "data-delete-face=\"x&#39; onerror=&#39;alert(1).jpg\"" in page.text
        assert "/media/face/x%27%20onerror%3D%27alert%281%29.jpg" in page.text

    def test_package_and_evidence_fields_are_escaped(self, console):
        add_incident()
        add_package(user_address=XSS, incident_type="weapon")
        add_evidence(console, detections=[{"label": XSS, "conf": 0.5}, XSS])
        client = tunnel_client(console)
        for path in ("/dispatch", "/evidence"):
            page = client.get(path)
            assert "<script>alert(1)" not in page.text, path
            assert not inspect_html(page.text).handlers, path

    def test_snapshot_names_are_url_encoded(self, console):
        (console.snapshots / "a b'c.jpg").write_bytes(b"\xff\xd8\xff")
        page = tunnel_client(console).get("/alerts")
        assert f'data-lightbox="{BASE}/media/snapshot/a%20b%27c.jpg"' in page.text


# ── tests: roles ──────────────────────────────────────────────────────────────
class TestRoles:
    def test_owner_and_device_admin_may_edit_the_gallery(self, console):
        populate(console)
        for client in (lc_client(console)[0], tunnel_client(console, "owner")):
            page = client.get("/authorized")
            assert 'id="capture-toggle"' in page.text and "data-delete-face=" in page.text

    def test_member_sees_the_gallery_read_only(self, console):
        populate(console)
        page = tunnel_client(console, "member").get("/authorized")
        assert page.status_code == 200
        assert 'id="capture-toggle"' not in page.text and "data-delete-face=" not in page.text
        assert 'id="gallery-view-only"' in page.text
        assert 'id="siren-test-btn"' not in tunnel_client(console, "member").get("/node").text

    def test_owner_may_test_the_siren(self, console):
        assert 'id="siren-test-btn"' in tunnel_client(console, "owner").get("/node").text
        assert 'id="siren-test-btn"' in lc_client(console)[0].get("/node").text

    def test_desk_operator_gets_the_live_views_only(self, console):
        page = tunnel_client(console, "operator").get("/dashboard")
        assert page.status_code == 200
        nav = re.findall(r'<a href="([^"]+)" class="nav-link', page.text)
        assert [n for n in nav if n not in PORTAL_LINKS] == [f"{BASE}/dashboard", f"{BASE}/live"]
        assert "/desk" in nav, "the operator's way back to the Desk"
        assert 'data-caps=""' in page.text
        assert "data-dispatch-authority" not in page.text
        assert 'data-sos-action="approve"' not in page.text

    def test_the_device_admin_and_the_owner_record_for_training(self, console):
        """D20 as amended on 2026-10-28: the card and its script are the
        device admin's and the owner's (who starts and stops through the
        Portal; the path table keeps the files on the LAN); members and the
        Desk never see it."""
        lan = lc_client(console)[0].get("/dashboard").text
        owner = tunnel_client(console, "owner").get("/dashboard").text
        assert 'id="record-card"' in lan and "/static/js/recording.js" in lan
        assert 'id="record-card"' in owner and f"{BASE}/static/js/recording.js" in owner
        for page in (lan, owner):
            assert "record" in re.search(r'data-caps="([^"]*)"', page).group(1).split()
        for role in ("member", "operator"):
            page = tunnel_client(console, role).get("/dashboard").text
            assert 'id="record-card"' not in page and "recording.js" not in page, role
            assert "data-record-" not in page, role
            assert "record" not in re.search(r'data-caps="([^"]*)"', page).group(1).split()

    def test_portal_users_have_no_console_logout(self, console):
        page = tunnel_client(console).get("/dashboard")
        assert 'id="logout-link"' not in page.text and "Riya Sharma" in page.text
        assert 'id="logout-link"' in lc_client(console)[0].get("/dashboard").text


# ── tests: banners, indicators and the SOS prompt ─────────────────────────────
class TestBannersAndSos:
    def test_test_mode_banner_on_every_page(self, console):
        client, _ = lc_client(console)
        for path in PAGES:
            assert 'id="test-mode-banner"' in client.get(path).text, path

    def test_no_test_mode_banner_outside_test_mode(self, console, monkeypatch):
        from sentrix.web import routes
        monkeypatch.setattr(routes, "_test_mode", lambda: False)
        assert 'id="test-mode-banner"' not in lc_client(console)[0].get("/dashboard").text

    def test_call_112_never_dials_112_in_test_mode(self, console, monkeypatch):
        monkeypatch.setenv("SENTRIX_DESK_NUMBER", "+919812345678")
        for path in ("/dashboard", "/sos", "/live"):
            text = lc_client(console)[0].get(path).text
            assert 'href="tel:+919812345678"' in text, path
            assert "tel:112" not in text and "sms:112" not in text, path
        monkeypatch.setenv("SENTRIX_DESK_NUMBER", "")
        text = lc_client(console)[0].get("/dashboard").text
        assert "tel:" not in text and 'aria-disabled="true" data-sos-call' in text

    def test_call_112_outside_test_mode(self, console, monkeypatch):
        from sentrix.web import routes
        monkeypatch.setattr(routes, "_test_mode", lambda: False)
        assert 'href="tel:112"' in lc_client(console)[0].get("/dashboard").text

    def test_an_emergency_number_is_never_the_desk_number(self, console, monkeypatch):
        monkeypatch.setenv("SENTRIX_DESK_NUMBER", "112")
        assert "tel:112" not in lc_client(console)[0].get("/dashboard").text

    def test_prompt_banner_chip_and_panel_on_every_page(self, console):
        client, _ = lc_client(console)
        for path in PAGES:
            text = client.get(path).text
            for marker in ('id="sos-prompt"', 'id="sos-banner"', 'id="sos-chip"',
                           'id="sos-prompt-minimise"', 'id="channels-banner"',
                           'id="remote-view"', 'id="dot-link"', 'id="dot-modem"'):
                assert marker in text, (path, marker)
        dashboard = client.get("/dashboard").text
        for marker in ('id="dispatch-panel"', 'id="dp-incident"', 'id="dp-tci"',
                       'id="dp-address"', 'id="dp-authority"', 'id="dp-dismiss"',
                       'data-dispatch-authority="POLICE"', 'data-dispatch-authority="FIRE"',
                       'data-dispatch-authority="MEDICAL"', 'id="dp-countdown"',
                       'id="video-overlay" data-sos-countdown',
                       'data-sos-pin-for="decline"', 'data-sos-action="checking"'):
            assert marker in dashboard, marker

    def test_prompt_wording(self, console):
        text = lc_client(console)[0].get("/dashboard").text
        for words in ("Alert my contacts and the SENTRIX desk", "Call 112 now",
                      "I'm checking", "False alarm (PIN)", "SENTRIX does not contact 112 itself"):
            assert words.replace("'", "&#39;") in text or words in text, words

    def test_remote_view_and_channel_banner_follow_the_state(self, console):
        from sentrix.pipeline.state import CHANNELS_IDLE, state
        client, _ = lc_client(console)
        text = client.get("/dashboard").text
        assert 'class="nav-remote hidden"' in text and 'class="alert-banner hidden"' in text
        state.set_remote_viewers(2)
        state.update(channels={**CHANNELS_IDLE, "warn": True})
        text = client.get("/dashboard").text
        assert 'class="nav-remote" id="remote-view"' in text
        assert '<span id="remote-view-count">2</span>' in text
        assert 'class="alert-banner" id="channels-banner"' in text

    def test_engine_scores_show_text_and_label_missing_models(self, console):
        text = lc_client(console)[0].get("/dashboard").text
        assert 'id="score-weapon-value">0%' in text
        assert 'id="score-theft-value"' in text and "n/a (no model)" in text
        assert 'id="score-is_night"' in text


# ── tests: the parity fixes on each page ──────────────────────────────────────
class TestPageParityFixes:
    def test_events_stats_note_and_who_when(self, console):
        add_incident(id="inc-a", verdict="TRUE", verdict_note="seen it", verdict_by="lc:device_admin",
                     verdict_at=datetime(2026, 10, 14, 9, 5))
        add_incident(id="inc-b", started_at=datetime(2026, 10, 14, 10, 0), verdict="FALSE")
        add_incident(id="inc-c", started_at=datetime(2026, 10, 14, 11, 0))
        text = lc_client(console)[0].get("/events").text
        assert 'id="events-stats"' in text
        assert re.search(r"Confirmed real</div><div class=\"stat-value\">1<", text)
        assert re.search(r"False alarms</div><div class=\"stat-value\">1<", text)
        assert re.search(r"Unreviewed</div><div class=\"stat-value\">1<", text)
        assert ">50%<" in text
        assert 'value="seen it" data-verdict-note' in text
        assert "Real · device admin" in text
        assert 'data-iso="2026-10-14T09:05:00+00:00"' in text

    def test_events_filters_and_paging(self, console):
        for n in range(5):
            add_incident(id=f"inc-{n}", started_at=datetime(2026, 10, 10 + n, 12, 0),
                         incident_type="fire" if n % 2 else "weapon", peak_level=3 + n % 3)
        client, _ = lc_client(console)
        page = client.get("/events", params={"incident_type": "fire"})
        assert page.text.count('data-incident="') == 2 and "2 incidents match" in page.text
        page = client.get("/events", params={"level": 5})
        assert page.text.count('data-incident="') == 1
        page = client.get("/events", params={"limit": 2})
        assert page.text.count('data-incident="') == 2
        assert 'id="events-next"' in page.text and 'id="events-prev"' not in page.text
        assert 'href="/events?limit=2&amp;offset=2"' in page.text
        page = client.get("/events", params={"limit": 2, "offset": 4})
        assert page.text.count('data-incident="') == 1 and 'id="events-prev"' in page.text
        page = tunnel_client(console).get("/events", params={"limit": 2, "incident_type": "weapon"})
        assert f'href="{BASE}/events?incident_type=weapon&amp;limit=2&amp;offset=2"' in page.text

    def test_events_show_the_request_in_the_owner_view(self, console):
        from sentrix.storage import requests
        add_incident(id="inc-r", request_id="req-duress")
        requests.create_request("req-duress", incident_id="inc-r", incident_type="weapon",
                                level=3, evidence_state="confirmed", policy={},
                                expires_at=None, test_mode=True, flags={"duress": True})
        requests.transition("req-duress", "approve", actor_type="owner")
        text = lc_client(console)[0].get("/events").text
        assert "SX-REQ-DU" in text
        assert "Marked as false alarm" in text, "duress must look like a decline"
        assert "Help requested" not in text
        assert 'href="/sos?request=req-duress"' in text

    def test_evidence_chain_badge_objects_and_view_link(self, console):
        add_evidence(console)
        text = tunnel_client(console).get("/evidence").text
        assert "Chain: no manifest yet" in text
        assert "pistol 0.82" in text and "{&#39;label&#39;" not in text
        assert f'href="{BASE}/media/evidence/L3_bundle_1"' in text

    def test_evidence_chain_verified(self, console, monkeypatch):
        monkeypatch.setattr(console.vault, "verify_chain",
                            lambda: {"ok": True, "entries": 3, "problems": []})
        assert "Chain verified: 3 entries, 0 problems" in lc_client(console)[0].get("/evidence").text

    def test_evidence_chain_problems_are_shown(self, console, monkeypatch):
        monkeypatch.setattr(console.vault, "verify_chain", lambda: {
            "ok": False, "entries": 3, "problems": [{"line": 2, "bundle": "b", "error": "bad signature"}]})
        text = lc_client(console)[0].get("/evidence").text
        assert "Chain check failed: 3 entries, 1 problem" in text and "bad signature" in text

    def test_alerts_empty_state_says_level_3(self, console):
        text = lc_client(console)[0].get("/alerts").text
        assert "Level 3+" in text and "Level 2+" not in text

    def test_dispatch_statuses_local_time_and_actions(self, console):
        add_incident()
        add_package("p-pend", status="PENDING")
        add_package("p-sent", status="SENT")
        add_package("p-canc", status="CANCELLED")
        add_package("p-exp", status="EXPIRED")
        add_package("p-fail", status="FAILED")
        text = lc_client(console)[0].get("/dispatch").text
        assert "Dispatch &amp; SOS" in text
        for status in ("PENDING", "SENT", "CANCELLED", "EXPIRED", "FAILED"):
            assert f'class="status-tag {status.lower()}">{status}<' in text
        assert 'data-send-package="p-pend"' in text and 'data-send-package="p-fail"' in text
        for refused in ("p-sent", "p-canc", "p-exp"):
            assert f'data-send-package="{refused}"' not in text
        assert ">Cancelled<" in text and "Dispatched" not in text
        assert 'data-iso="2026-10-14T09:00:05+00:00"' in text
        assert 'value="MEDICAL"' in text
        assert "Level 3+" not in text or "assistance request" in text

    def test_dispatch_offers_help_now_on_a_lapsed_request(self, console):
        from sentrix.storage import requests
        add_incident(request_id="req-exp")
        requests.create_request("req-exp", incident_id="inc-1", incident_type="weapon", level=3,
                                evidence_state="confirmed", policy={}, expires_at=None,
                                test_mode=True)
        requests.transition("req-exp", "prompted")
        requests.transition("req-exp", "expire")
        add_package("p-exp", status="EXPIRED", request_id="req-exp")
        text = lc_client(console)[0].get("/dispatch").text
        assert 'data-help-now="req-exp"' in text and "Unanswered" in text

    def test_dispatch_hides_a_duress_package_and_drops_a_silent_one(self, console):
        import json as _json
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
            row.sent_to_json = _json.dumps(["SENTRIX Desk", "Aman 98xxxxx611"])
        client = lc_client(console)[0]
        text = client.get("/dispatch").text
        assert 'class="status-tag sent">SENT<' not in text
        assert "Aman 98" not in text and "DURESS" not in text and "p-sil" not in text
        assert 'class="status-tag cancelled">CANCELLED<' in text
        latest = client.get("/api/dispatch/latest").json()
        assert latest == {}, "a silent request's PENDING package is never shown"
        mine = client.get("/api/dispatch/latest?request_id=req-dur").json()
        assert mine["status"] == "CANCELLED" and mine["sent_to"] == []

    def test_the_tunnelled_console_links_back_to_the_portal(self, console):
        owner = tunnel_client(console).get("/dashboard").text
        assert 'href="/home"' in owner and 'href="/account"' in owner
        desk = tunnel_client(console, role="operator").get("/live")
        assert desk.status_code != 200 or 'href="/desk"' in desk.text
        assert 'href="/home"' not in lc_client(console)[0].get("/dashboard").text

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

    def test_access_page_shows_the_identity_backend(self, console):
        client, _ = lc_client(console)
        assert "Face recognition: insightface" in client.get("/authorized").text
        console.pipeline.identity.backend = None
        assert "identity fails closed" in client.get("/authorized").text

    def test_live_page_uses_the_shared_socket(self, console):
        text = lc_client(console)[0].get("/live").text
        assert 'id="ls-status">SECURE<' in text
        assert "new WebSocket" not in text and "/static/js/live.js" in text
        assert "WebSocket" not in (STATIC_JS / "live.js").read_text(encoding="utf-8")

    def test_sos_page_and_selection(self, console):
        text = tunnel_client(console).get("/sos", params={"request": "req-1"}).text
        assert 'data-selected="req-1"' in text and 'id="sos-history"' in text
        assert 'id="sos-preview"' in text and 'id="sos-audit-card"' in text
        assert "The SOS service is off" in text
        assert 'data-selected=""' in lc_client(console)[0].get(
            "/sos", params={"request": "<x>"}).text

    def test_static_assets_are_versioned(self, console):
        text = lc_client(console)[0].get("/dashboard").text
        assert re.search(r'href="/static/css/style\.css\?v=[0-9a-f]{10}"', text)
        assert re.search(r'src="/static/js/dashboard\.js\?v=[0-9a-f]{10}"', text)
        assert lc_client(console)[0].get("/static/js/app.js").status_code == 200
