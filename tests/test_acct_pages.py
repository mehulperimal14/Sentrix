"""Every T-ACCT page renders for the roles that may see it, carries the
test-mode banner, the security headers, no inline script or style, and every
form posts with a CSRF token (docs/7 §4.1, §11; decisions D13/D14)."""

from __future__ import annotations

import re

import pytest

from test_acct_support import (Browser, add_device, add_household, fake_hub, outbox,  # noqa: F401
                               owner_browser, portal, staff_browser)

BANNER = "TEST MODE, NOT A REAL EMERGENCY SERVICE"

OWNER_PAGES = ["/home", "/account", "/account/password", "/account/2fa", "/account/sessions",
               "/onboarding/1", "/onboarding/2", "/onboarding/3", "/onboarding/4",
               "/onboarding/5", "/onboarding/6", "/onboarding/7", "/claim", "/household",
               "/household/members", "/household/contacts", "/household/audit", "/settings",
               "/privacy", "/privacy/notice", "/privacy/export", "/privacy/erase"]
MEMBER_PAGES = ["/home", "/account", "/household", "/household/members",
                "/household/contacts", "/household/audit", "/settings", "/privacy",
                "/privacy/notice"]
ADMIN_PAGES = ["/admin", "/admin/customers", "/admin/customers/{household_id}",
               "/admin/customers/{household_id}/letter", "/admin/devices",
               "/admin/devices/{spare}/label", "/admin/operators", "/admin/audit",
               "/account", "/account/2fa"]


def _check_page(response, path):
    assert response.status_code == 200, (path, response.status_code, response.text[:400])
    html = response.text
    assert BANNER in html, f"{path}: no test-mode banner"
    assert "<script>" not in html and " onclick=" not in html and " style=" not in html, path
    for script in re.findall(r"<script[^>]*>", html):
        assert "nonce=" in script and "src=" in script, (path, script)
    for form in re.findall(r"<form[^>]*method=\"post\"[^>]*>(.*?)</form>", html, re.S):
        assert 'name="csrf_token"' in form, f"{path}: a POST form without a CSRF token"
    headers = response.headers
    assert "nonce-" in headers["content-security-policy"]
    assert headers["x-frame-options"] == "DENY"
    assert headers["cache-control"] == "no-store"
    assert "camera=()" in headers["permissions-policy"]


@pytest.fixture
def world(portal):
    home = add_household(portal, complete=True)
    spare = add_device(portal, serial="SX5-000900")
    add_device(portal, serial="SX5-000901", household_id=home["household_id"],
               state="ASSIGNED")
    from test_acct_support import add_user
    add_user(portal, username="meera", role="member", household_id=home["household_id"])
    return {**home, "spare": spare}


def test_owner_pages(portal, world):
    browser = owner_browser(portal)
    for path in OWNER_PAGES:
        _check_page(browser.get(path), path)


def test_member_pages(portal, world):
    browser = owner_browser(portal, "meera")
    for path in MEMBER_PAGES:
        _check_page(browser.get(path), path)
    assert "Only the account owner" in browser.get("/household").text


def test_admin_pages(portal, world):
    browser = staff_browser(portal)
    for path in ADMIN_PAGES:
        path = path.format(**world)
        _check_page(browser.get(path), path)


def test_public_pages(portal, world):
    browser = Browser(portal)
    for path in ("/login", "/activate"):
        _check_page(browser.get(path), path)
    assert browser.get("/").headers["location"].split("?")[0] == "/login"


def test_static_files_are_cacheable_and_served(portal):
    browser = Browser(portal)
    for path in ("/static/acct/acct.css", "/static/acct/acct.js"):
        response = browser.get(path)
        assert response.status_code == 200
        assert "no-store" not in response.headers.get("cache-control", "")


def test_hsts_only_behind_tls(tmp_path, outbox, fake_hub):
    from fastapi.testclient import TestClient

    from sentrix_portal.app import create_app
    from sentrix_portal.settings import PortalSettings
    plain = create_app(PortalSettings.for_tests(tmp_path / "plain"))
    assert "strict-transport-security" not in TestClient(plain).get("/login").headers
    secure = create_app(PortalSettings.for_tests(tmp_path / "tls", secure_cookies=True))
    assert "max-age" in TestClient(secure).get("/login").headers["strict-transport-security"]
