"""The Portal's pages are the single-page app (docs/9, D22): every app path
answers ``static/app/index.html`` with the security headers and a strict CSP
(no inline script or style), server prefixes stay JSON 404s, the app's assets
are cacheable, and HSTS is sent only behind TLS (docs/7 §11)."""

from __future__ import annotations

import re

import pytest

from test_acct_support import fake_hub, outbox, portal  # noqa: F401

APP_PATHS = ["/", "/login", "/activate", "/activate/KTRWBQXLMNPQ", "/home", "/account",
             "/onboarding/3", "/claim", "/household/contacts", "/settings", "/privacy",
             "/sos/123", "/a/token", "/consent/token", "/c/token", "/desk",
             "/desk/requests/x", "/admin/customers", "/live/abc"]


def _built():
    from sentrix_portal.routes.spa import INDEX
    if not INDEX.is_file():
        pytest.skip("the Portal app is not built (cd frontend && npm run build)")
    return INDEX.read_text(encoding="utf-8")


def test_every_app_path_serves_the_app(portal):
    from fastapi.testclient import TestClient
    from sentrix_portal.routes.spa import INDEX
    index = _built()
    # Compared as bytes: the file is served as stored, and a build on Windows
    # leaves CR line endings that reading it as text would turn into LF.
    raw = INDEX.read_bytes()
    client = TestClient(portal, follow_redirects=False)
    for path in APP_PATHS:
        response = client.get(path, headers={"accept": "text/html"})
        assert response.status_code == 200, path
        assert response.content == raw, path
        headers = response.headers
        csp = headers["content-security-policy"]
        assert "script-src 'self'" in csp and "unsafe-inline" not in csp, path
        assert headers["x-frame-options"] == "DENY" and "camera=()" in headers[
            "permissions-policy"]
    assert "<script>" not in index and " style=" not in index and " onclick=" not in index
    for script in re.findall(r"<script[^>]*>", index):
        assert 'src="/static/app/' in script


def test_server_prefixes_never_get_the_app(portal):
    from fastapi.testclient import TestClient
    client = TestClient(portal, follow_redirects=False)
    for path in ("/api/v2/nope", "/api/nope", "/hooks/nope", "/static/nope.js", "/link/x",
                 "/openapi.json", "/docs"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.headers["content-type"].startswith("application/json"), path
    assert client.get("/healthz").json()["status"] == "ok"


def test_static_files_are_cacheable_and_served(portal):
    from fastapi.testclient import TestClient
    index = _built()
    client = TestClient(portal)
    for path in re.findall(r'(?:src|href)="(/static/app/[^"]+)"', index):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "no-store" not in response.headers.get("cache-control", ""), path


def test_hsts_only_behind_tls(tmp_path, outbox, fake_hub):
    from fastapi.testclient import TestClient

    from sentrix_portal.app import create_app
    from sentrix_portal.settings import PortalSettings
    plain = create_app(PortalSettings.for_tests(tmp_path / "plain"))
    assert "strict-transport-security" not in TestClient(plain).get("/healthz").headers
    secure = create_app(PortalSettings.for_tests(tmp_path / "tls", secure_cookies=True))
    assert "max-age" in TestClient(secure).get("/healthz").headers["strict-transport-security"]
