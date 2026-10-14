"""deploy/portal (docs/7 §15.4): static checks of the Docker/Caddy deployment.
Docker is never run here; the files are parsed and their rules asserted
(decisions: Postgres on an internal network only, only Caddy publishes
80/443, test mode on, names only in the env template)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

from sentrix.paths import ROOT_DIR

DEPLOY = ROOT_DIR / "deploy" / "portal"


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))


def _env_example() -> dict[str, str]:
    values = {}
    for line in (DEPLOY / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


class TestCompose:
    def test_only_caddy_publishes_and_only_80_443(self, compose):
        services = compose["services"]
        assert set(services) == {"db", "portal", "caddy"}
        published = {name for name, svc in services.items() if svc.get("ports")}
        assert published == {"caddy"}
        ports = {str(p).split(":")[-1].split("/")[0] for p in services["caddy"]["ports"]}
        assert ports == {"80", "443"}
        assert "8100" in [str(p) for p in services["portal"].get("expose", [])]

    def test_postgres_is_internal_only(self, compose):
        db = compose["services"]["db"]
        assert "ports" not in db and "expose" not in db
        assert db["networks"] == ["backend"]
        assert compose["networks"]["backend"]["internal"] is True
        assert db["image"].startswith("postgres:16")
        assert "caddy" not in [n for n in compose["services"]["caddy"]["networks"]
                               if n == "backend"]
        assert "backend" not in compose["services"]["caddy"]["networks"]

    def test_portal_settings_behind_the_proxy(self, compose):
        portal = compose["services"]["portal"]
        env = portal["environment"]
        assert env["PORTAL_DATABASE_URL"].startswith("postgresql+psycopg://")
        assert "@db:5432/" in env["PORTAL_DATABASE_URL"]
        assert env["PORTAL_SECURE_COOKIES"] == "1" and env["PORTAL_WORKERS"] == "1"
        assert env["PORTAL_BASE_URL"].startswith("https://")
        caddy_ip = compose["services"]["caddy"]["networks"]["frontend"]["ipv4_address"]
        assert env["PORTAL_TRUSTED_PROXIES"] == env["FORWARDED_ALLOW_IPS"] == caddy_ip
        subnet = compose["networks"]["frontend"]["ipam"]["config"][0]["subnet"]
        assert caddy_ip.rsplit(".", 1)[0] == subnet.split("/")[0].rsplit(".", 1)[0]
        assert set(portal["networks"]) == {"backend", "frontend"}
        assert portal["build"]["dockerfile"] == "deploy/portal/Dockerfile"
        assert portal["depends_on"]["db"]["condition"] == "service_healthy"

    def test_every_compose_variable_is_in_the_env_template(self, compose):
        text = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
        used = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)", text))
        assert used <= set(_env_example()), used - set(_env_example())


class TestImage:
    def test_slim_non_root_portal_only(self):
        text = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
        assert re.search(r"(?m)^FROM python:3\.\d+-slim$", text)
        users = re.findall(r"(?m)^USER\s+(\S+)", text)
        assert users and users[-1].split(":")[0] not in ("root", "0")
        assert re.search(r"useradd --system --uid (\d+)", text).group(1) != "0"
        copies = re.findall(r"(?m)^COPY\s+(.+)$", text)
        assert not any(re.match(r"\.\s", c) or ".env" in c or "models" in c for c in copies)
        assert "HEALTHCHECK" in text and "/healthz" in text
        assert 'CMD ["python", "-m", "sentrix_portal"]' in text
        assert "PORTAL_TEST_MODE=1" in text

    def test_build_context_excludes_secrets_and_runtime(self):
        lines = [line.strip() for line in (DEPLOY / "Dockerfile.dockerignore")
                 .read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.startswith("#")]
        assert lines[0] == "*", "deny everything first"
        allowed = {line[1:].rstrip("/") for line in lines if line.startswith("!")}
        assert allowed <= {"sentrix", "sentrix_portal", "deploy/portal/requirements.txt",
                           "tools/provision_device.py", "tools/portal_pg_check.py"}
        copied = re.findall(r"(?m)^COPY\s+(.+)\s+/app/", (DEPLOY / "Dockerfile")
                            .read_text(encoding="utf-8"))
        for sources in copied:
            for source in sources.split():
                assert source.rstrip("/") in allowed, source

    def test_requirements_cover_the_portal_imports(self):
        names = {re.split(r"[<>=\[ ]", line.strip())[0].lower()
                 for line in (DEPLOY / "requirements.txt").read_text(encoding="utf-8")
                 .splitlines() if line.strip() and not line.startswith("#")}
        assert {"psycopg", "fastapi", "uvicorn", "sqlalchemy", "argon2-cffi", "pyotp",
                "httpx", "jinja2", "cryptography", "websockets", "python-dotenv",
                "python-multipart", "qrcode"} <= names
        assert not names & {"torch", "opencv-python", "ultralytics", "insightface"}
        # every third-party import in the Portal package is covered
        dist = {"argon2": "argon2-cffi", "dotenv": "python-dotenv",
                "multipart": "python-multipart", "python_multipart": "python-multipart",
                "starlette": "fastapi", "pydantic": "fastapi"}
        stdlib = set(__import__("sys").stdlib_module_names)
        imported = set()
        for path in (ROOT_DIR / "sentrix_portal").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    imported |= {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    imported.add(node.module.split(".")[0])
        third = imported - stdlib - {"sentrix", "sentrix_portal", "__future__"}
        assert {dist.get(m, m).lower() for m in third} <= names, third


class TestCaddy:
    def test_https_hsts_and_proxy(self):
        text = (DEPLOY / "Caddyfile").read_text(encoding="utf-8")
        assert "{$PORTAL_DOMAIN}" in text and "email {$ACME_EMAIL}" in text
        assert re.search(r'Strict-Transport-Security "max-age=\d{7,}', text)
        assert "reverse_proxy portal:8100" in text and "flush_interval -1" in text
        assert "tls internal" not in text and "http://" not in text
        assert text.count("{") == text.count("}")


class TestEnvTemplate:
    SAFE_DEFAULTS = {"POSTGRES_DB", "POSTGRES_USER", "PORTAL_TEST_MODE", "SENTRIX_TEST_MODE",
                     "SENTRIX_LIVE_ALERTS", "SENTRIX_TELEGRAM_LIVE", "SENTRIX_EMAIL_LIVE",
                     "SENTRIX_WEBPUSH_LIVE", "SENTRIX_TELEGRAM_MODE", "PORTAL_API_DOCS",
                     "PORTAL_LOG_LEVEL"}

    def test_names_only_and_test_mode_on(self):
        values = _env_example()
        for key, value in values.items():
            if key not in self.SAFE_DEFAULTS:
                assert value == "", f"{key} must be empty in the template"
        assert values["PORTAL_TEST_MODE"] == values["SENTRIX_TEST_MODE"] == "1"
        for switch in ("SENTRIX_LIVE_ALERTS", "SENTRIX_TELEGRAM_LIVE", "SENTRIX_EMAIL_LIVE"):
            assert values[switch] == "0"
        for secret in ("POSTGRES_PASSWORD", "PORTAL_SECRET_KEY", "PORTAL_DATA_KEY",
                       "TELEGRAM_BOT_TOKEN", "BREVO_API_KEY", "RESEND_API_KEY"):
            assert secret in values and values[secret] == ""

    def test_the_template_is_tracked_and_a_filled_env_is_ignored(self):
        import subprocess
        ignored = subprocess.run(["git", "check-ignore", "-q", "deploy/portal/.env"],
                                 cwd=ROOT_DIR).returncode
        tracked_ok = subprocess.run(["git", "check-ignore", "-q", "deploy/portal/.env.example"],
                                    cwd=ROOT_DIR).returncode
        assert ignored == 0 and tracked_ok == 1


class TestScripts:
    def test_backup_and_restore_scripts(self):
        backup = (DEPLOY / "backup.sh").read_text(encoding="utf-8")
        restore = (DEPLOY / "restore_test.sh").read_text(encoding="utf-8")
        assert "pg_dump" in backup and "--format=custom" in backup and "set -euo pipefail" in backup
        assert "tar -C /data" in backup, "the key files are backed up with the dump"
        assert "pg_restore" in restore and "DROP DATABASE $SCRATCH" in restore
        for text in (backup, restore, (DEPLOY / "check_ports.sh").read_text(encoding="utf-8")):
            assert text.startswith("#!/usr/bin/env bash")
            assert "\r\n" not in text

    def test_readme_covers_the_runbook(self):
        text = (DEPLOY / "README.md").read_text(encoding="utf-8")
        for needle in ("Mumbai", "Budget", "80 and 443", "A record", "pg_dump",
                       "restore_test.sh", "Supabase", "Render", "SMTP", "HTTPS API",
                       "portal_pg_check.py"):
            assert needle in text, needle


class TestPiTemplate:
    def test_pi_env_has_the_link_and_sos_keys(self):
        text = (ROOT_DIR / "deploy" / "pi5" / "pi.env").read_text(encoding="utf-8")
        values = dict(line.split("=", 1) for line in text.splitlines()
                      if line and not line.startswith("#"))
        for key in ("SENTRIX_LINK_ENABLED", "SENTRIX_PORTAL_URL", "SENTRIX_DEVICE_IDENTITY_DIR",
                    "SENTRIX_SOS_ENABLED", "SENTRIX_CHANNELS_ENABLED", "SENTRIX_TEST_MODE"):
            assert key in values, key
        assert values["SENTRIX_TEST_MODE"] == "1" and values["SENTRIX_LIVE_ALERTS"] == "0"
        assert values["SENTRIX_LINK_ENABLED"] == "0" and values["SENTRIX_PORTAL_URL"] == ""
