"""The Portal on Railway (D18): the port, the client address behind the edge,
the first admin without a shell, ``railway.json``, the image and the
WhatsApp template tool's definitions. No network, no Docker."""

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

    def test_the_image_takes_the_platform_port(self):
        text = (ROOT / "deploy/portal/Dockerfile").read_text(encoding="utf-8")
        assert "PORTAL_PORT=8100" not in text, "a fixed PORTAL_PORT would hide PORT"
        assert "os.getenv('PORT')" in text

    def test_the_railway_guide_names_every_variable(self):
        guide = (ROOT / "deploy/railway/README.md").read_text(encoding="utf-8")
        for name in ("RAILWAY_RUN_UID", "PORTAL_BASE_URL", "PORTAL_WORKERS",
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
