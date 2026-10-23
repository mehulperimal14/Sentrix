"""The Portal on Render or Railway (D18): the port and public URL, the client
address behind the edge, the first admin without a shell, ``render.yaml`` and
``railway.json``, the image and its start script, and the WhatsApp template
tool's definitions. No network, no Docker."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


class TestSettings:
    def test_port_from_the_platform(self, monkeypatch):
        from sentrix_portal.settings import PortalSettings
        monkeypatch.delenv("PORTAL_PORT", raising=False)
        monkeypatch.setenv("PORT", "4321")
        assert PortalSettings.from_env().port == 4321
        monkeypatch.setenv("PORTAL_PORT", "8100")
        assert PortalSettings.from_env().port == 8100, "an explicit PORTAL_PORT wins"
        monkeypatch.delenv("PORT")
        monkeypatch.delenv("PORTAL_PORT")
        assert PortalSettings.from_env().port == 8100

    def test_public_url_from_the_platform(self, monkeypatch):
        from sentrix_portal.settings import PortalSettings
        for name in ("PORTAL_BASE_URL", "RENDER_EXTERNAL_URL", "RAILWAY_PUBLIC_DOMAIN"):
            monkeypatch.delenv(name, raising=False)
        assert PortalSettings.from_env().base_url == "http://portal.localhost:8100"
        monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "sx.up.railway.app")
        assert PortalSettings.from_env().base_url == "https://sx.up.railway.app"
        monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://sx.onrender.com/")
        assert PortalSettings.from_env().base_url == "https://sx.onrender.com"
        monkeypatch.setenv("PORTAL_BASE_URL", "https://sentrix.example.in")
        assert PortalSettings.from_env().base_url == "https://sentrix.example.in"


def _request(peer, headers):
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers)


class TestClientAddress:
    @pytest.fixture
    def trust(self, monkeypatch, portal_app):
        from dataclasses import replace

        from sentrix_portal import settings as portal_settings

        def set_trusted(*proxies):
            current = portal_settings.current()
            portal_settings.set_current(replace(current, trusted_proxies=proxies))
        return set_trusted

    def test_untrusted_peers_cannot_claim_an_address(self, trust):
        from sentrix_portal.security import client_ip
        trust("127.0.0.1")
        assert client_ip(_request("100.64.0.9", {"x-real-ip": "203.0.113.5"})) == "100.64.0.9"

    def test_railway_edge_x_real_ip(self, trust):
        from sentrix_portal.security import client_ip
        trust("*")
        headers = {"x-real-ip": "203.0.113.5", "x-forwarded-for": "198.51.100.1, 10.0.0.1"}
        assert client_ip(_request("100.64.0.9", headers)) == "203.0.113.5"
        assert client_ip(_request("100.64.0.9", {"x-forwarded-for": "198.51.100.1"})) \
            == "198.51.100.1"
        assert client_ip(_request("100.64.0.9", {})) == "100.64.0.9"

    def test_caddy_keeps_working(self, trust):
        from sentrix_portal.security import client_ip
        trust("127.0.0.1", "::1")
        assert client_ip(_request("127.0.0.1", {"x-forwarded-for": "198.51.100.1"})) \
            == "198.51.100.1"


class TestBootstrapAdmin:
    def test_first_admin_then_nothing_once_active(self, portal_app):
        from sentrix_portal.models import ActivationCode, User
        from sentrix_portal.services import accounts
        with portal_app.state.db.session() as db:
            link = accounts.bootstrap_admin(db, "Mehul_Admin")
            assert link and "/activate/" in link
            user = db.query(User).filter_by(username="mehul_admin").one()
            assert user.role == "company_admin" and user.status == "pending_activation"
            again = accounts.bootstrap_admin(db, "mehul_admin")
            assert again and again != link, "a fresh code while still pending"
            assert db.query(User).filter_by(role="company_admin").count() == 1
            assert db.query(ActivationCode).filter_by(user_id=user.id,
                                                      used_at=None).count() >= 1
            user.status = "active"
            db.flush()
            assert accounts.bootstrap_admin(db, "someone_else") is None
            assert db.query(User).filter_by(role="company_admin").count() == 1

    def test_bad_names_and_customers_are_refused(self, portal_app):
        from sentrix_portal.models import User
        from sentrix_portal.services import accounts
        with portal_app.state.db.session() as db:
            assert accounts.bootstrap_admin(db, "x") is None
            assert accounts.bootstrap_admin(db, "") is None
            db.add(User(username="riya", display_name="Riya", role="owner", status="active"))
            db.flush()
            assert accounts.bootstrap_admin(db, "riya") is None

    def test_the_link_is_printed_at_start_up(self, portal_app, monkeypatch, capsys):
        monkeypatch.setenv("PORTAL_BOOTSTRAP_ADMIN", "admin")
        with TestClient(portal_app):
            pass
        out = capsys.readouterr().out
        assert "company admin 'admin'" in out and "/activate/" in out

    def test_nothing_without_the_variable(self, portal_app, capsys):
        with TestClient(portal_app):
            pass
        assert "company admin" not in capsys.readouterr().out


class TestDeployFiles:
    def test_railway_json_builds_the_portal_image(self):
        config = json.loads((ROOT / "railway.json").read_text(encoding="utf-8"))
        assert config["build"]["builder"] == "DOCKERFILE"
        assert (ROOT / config["build"]["dockerfilePath"]).is_file()
        assert config["deploy"]["healthcheckPath"] == "/healthz"
        assert config["deploy"].get("numReplicas", 1) == 1, "the hub keeps sockets in memory"

    def test_both_dockerignores_agree(self):
        def rules(path):
            return [line.strip() for line in (ROOT / path).read_text(encoding="utf-8")
                    .splitlines() if line.strip() and not line.startswith("#")]
        assert rules(".dockerignore") == rules("deploy/portal/Dockerfile.dockerignore")

    def test_render_blueprint(self):
        import yaml
        (service,) = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))[
            "services"]
        assert service["runtime"] == "docker" and service["plan"] == "starter"
        assert (ROOT / service["dockerfilePath"]).is_file()
        assert service["disk"]["mountPath"] == "/data" and service["healthCheckPath"] == "/healthz"
        assert service.get("numInstances", 1) == 1
        env = {e["key"]: e for e in service["envVars"]}
        assert env["PORTAL_TEST_MODE"]["value"] == "1" and env["SENTRIX_TEST_MODE"]["value"] == "1"
        for key in ("PORTAL_SECRET_KEY", "PORTAL_DATA_KEY", "WHATSAPP_VERIFY_TOKEN"):
            assert env[key].get("generateValue") is True, key
        # D17: the repository never turns sending on; the team types these in.
        for key in ("SENTRIX_LIVE_ALERTS", "SENTRIX_WHATSAPP_LIVE", "SENTRIX_TEST_RECIPIENTS",
                    "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_APP_SECRET"):
            assert env[key] == {"key": key, "sync": False}, key

    def test_the_image_takes_the_platform_port(self):
        text = (ROOT / "deploy/portal/Dockerfile").read_text(encoding="utf-8")
        assert "PORTAL_PORT=8100" not in text, "a fixed PORTAL_PORT would hide PORT"
        assert "os.getenv('PORT')" in text

    def test_the_guide_names_every_variable(self):
        guide = (ROOT / "deploy/cloud/README.md").read_text(encoding="utf-8")
        for name in ("render.yaml", "railway.json", "PORTAL_BASE_URL", "PORTAL_WORKERS",
                     "PORTAL_SECURE_COOKIES", "PORTAL_TRUSTED_PROXIES", "PORTAL_SECRET_KEY",
                     "PORTAL_DATA_KEY", "PORTAL_BOOTSTRAP_ADMIN", "WHATSAPP_ACCESS_TOKEN",
                     "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_APP_SECRET",
                     "WHATSAPP_VERIFY_TOKEN", "SENTRIX_WHATSAPP_LIVE",
                     "SENTRIX_LIVE_ALERTS", "SENTRIX_TEST_RECIPIENTS"):
            assert name in guide, name


def test_template_definitions_match_the_channel():
    from sentrix.alerting.channels import whatsapp as W
    from tools.whatsapp_templates import definitions
    bodies = {d["name"]: d for d in definitions("en")}
    assert set(bodies) == {"sentrix_prompt", "sentrix_contact", "sentrix_notice"}
    prompt = bodies["sentrix_prompt"]
    assert prompt["category"] == "UTILITY" and prompt["language"] == "en"
    body, buttons = prompt["components"]
    assert body["text"] == W.TEMPLATES[W.PROMPT]["body"] and body["example"]["body_text"]
    assert [b["text"] for b in buttons["buttons"]] == ["Alert contacts", "I'm checking",
                                                       "False alarm"]
    assert len(bodies["sentrix_notice"]["components"]) == 1


# ── deploy/portal/start.py ────────────────────────────────────────────────────
def _start_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("portal_start",
                                                  ROOT / "deploy/portal/start.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeOs:
    """The calls start.py makes, recorded; ``uid`` is who we are."""

    def __init__(self, uid):
        self.uid, self.calls = uid, []

    def getuid(self):
        return self.uid

    def geteuid(self):
        return self.uid

    def setgroups(self, groups):
        self.calls.append(("setgroups", tuple(groups)))

    def setgid(self, gid):
        self.calls.append(("setgid", gid))

    def setuid(self, uid):
        self.calls.append(("setuid", uid))
        self.uid = uid

    def execvp(self, file, args):
        self.calls.append(("exec", tuple(args)))


@pytest.fixture
def start(monkeypatch, tmp_path):
    module = _start_module()
    fake = FakeOs(0)
    for name in ("getuid", "geteuid", "setgroups", "setgid", "setuid", "execvp"):
        monkeypatch.setattr(module.os, name, getattr(fake, name), raising=False)
    owned = []
    monkeypatch.setattr(module, "take_ownership",
                        lambda root, uid, gid: owned.append((root, uid, gid)) or 1)
    monkeypatch.setattr(module, "DATA", str(tmp_path / "data"))
    import sys
    import types
    entry = types.SimpleNamespace(pw_uid=10001, pw_gid=10001, pw_dir="/app")
    monkeypatch.setitem(sys.modules, "pwd", types.SimpleNamespace(getpwnam=lambda name: entry))
    module.fake, module.owned, module.tmp = fake, owned, tmp_path
    return module


class TestStartScript:
    def test_root_prepares_the_volume_then_drops_for_good(self, start):
        start.main([])
        data = start.tmp / "data"
        assert (data / "portal").is_dir() and (data / "var").is_dir()
        assert start.owned == [(str(data), 10001, 10001)]
        assert start.fake.calls == [("setgroups", ()), ("setgid", 10001), ("setuid", 10001),
                                    ("exec", ("python", "-m", "sentrix_portal"))]

    def test_another_user_runs_the_command_unchanged(self, start):
        start.fake.uid = 10001
        start.main(["python", "-m", "sentrix_portal", "--port", "9000"])
        assert start.owned == []
        assert start.fake.calls == [("exec", ("python", "-m", "sentrix_portal", "--port",
                                              "9000"))]

    def test_a_failed_drop_stops(self, start, monkeypatch):
        monkeypatch.setattr(start.os, "setuid", lambda uid: None, raising=False)
        with pytest.raises(SystemExit):
            start.main([])
        assert not any(call[0] == "exec" for call in start.fake.calls)

    def test_take_ownership_walks_without_following_links(self, tmp_path, monkeypatch):
        module = _start_module()
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "f").write_text("x")
        seen = []
        monkeypatch.setattr(module.os, "chown",
                            lambda path, uid, gid, follow_symlinks=True:
                            seen.append((Path(path).name, follow_symlinks)), raising=False)
        assert module.take_ownership(str(tmp_path), 1, 1) == 3
        assert all(follow is False for _, follow in seen)

