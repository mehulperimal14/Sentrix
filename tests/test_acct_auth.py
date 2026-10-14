"""Sign-in, back-off, activation, TOTP, sessions, step-up and e-mail
verification (docs/7 §10, D14). Nothing is sent: e-mails land in a recorder."""

from __future__ import annotations

import re
from datetime import timedelta

import pyotp
import pytest

from sentrix_portal.models import AuditLog, User
from test_acct_support import (PASSWORD, Browser, add_household, add_user,  # noqa: F401
                               fake_hub, outbox, owner_browser, portal, query,
                               staff_browser)


def _age(app, model, seconds: int, **filters) -> None:
    """Move rows of ``model`` back in time (``at`` / ``expires_at``)."""
    with app.state.db.session() as db:
        for row in db.query(model).filter_by(**filters):
            for column in ("at", "expires_at", "created_at"):
                if getattr(row, column, None) is not None:
                    setattr(row, column, getattr(row, column) - timedelta(seconds=seconds))


def _pending_customer(app, browser: Browser, *, username="riya", email="riya@example.com"):
    """Admin creates a customer and prints the letter; returns (household id, code)."""
    response = browser.form("/admin/customers", {"name": "Riya Sharma", "username": username,
                                                 "email": email, "city": "Patiala"})
    assert response.status_code == 303, response.text
    household_id = response.headers["location"].split("/")[3].split("?")[0]
    browser.get(f"/admin/customers/{household_id}/letter")
    letter = browser.post(f"/admin/customers/{household_id}/letter", {})
    assert letter.status_code == 200 and letter.headers["cache-control"] == "no-store"
    code = re.search(r'class="code-big">([A-Z]{4}-[A-Z]{4}-[A-Z]{4})<', letter.text).group(1)
    return household_id, code


# ── sign-in and sign-out ──────────────────────────────────────────────────────
class TestLogin:
    def test_login_lands_by_role_and_logout_needs_csrf(self, portal):
        add_household(portal, complete=True)
        browser = owner_browser(portal)
        assert browser.last.headers["location"] == "/home"
        assert browser.get("/home").status_code == 200
        assert browser.post("/logout", {}, csrf=False).status_code == 403
        assert browser.get("/home").status_code == 200, "still signed in"
        response = browser.post("/logout", {})
        assert response.status_code == 303 and response.headers["location"] == "/login"
        assert browser.get("/home").headers["location"] == "/login"
        events = [r.event for r in query(portal, AuditLog)]
        assert "auth.login" in events and "auth.logout" in events

    def test_unfinished_onboarding_lands_in_the_wizard(self, portal):
        add_household(portal)
        assert owner_browser(portal).last.headers["location"] == "/onboarding"

    def test_login_form_needs_its_csrf_cookie(self, portal):
        add_household(portal)
        browser = Browser(portal)
        response = browser.post("/login", {"username": "riya", "password": PASSWORD,
                                           "csrf_token": "forged"}, csrf=False)
        assert response.status_code == 403

    def test_wrong_password_and_unknown_user_look_the_same(self, portal):
        add_household(portal)
        browser = Browser(portal)
        browser.get("/login")
        wrong = browser.post("/login", {"username": "riya", "password": "nope nope nope"})
        browser.get("/login")
        ghost = browser.post("/login", {"username": "ghost", "password": "nope nope nope"})
        assert wrong.status_code == ghost.status_code == 401
        assert "Wrong username or password" in wrong.text and "Wrong username" in ghost.text

    def test_next_is_never_an_open_redirect(self, portal):
        add_household(portal, complete=True)
        browser = Browser(portal)
        browser.get("/login?next=//evil.example/x")
        response = browser.post("/login", {"username": "riya", "password": PASSWORD,
                                           "next": "//evil.example/x"})
        assert response.headers["location"] == "/home"
        browser = Browser(portal)
        browser.get("/login")
        response = browser.post("/login", {"username": "riya", "password": PASSWORD,
                                           "next": "/settings"})
        assert response.headers["location"] == "/settings"

    def test_disabled_account_cannot_sign_in(self, portal):
        add_user(portal, username="gone", role="owner", status="disabled")
        browser = Browser(portal)
        browser.get("/login")
        assert browser.post("/login", {"username": "gone",
                                       "password": PASSWORD}).status_code == 401


class TestBackoff:
    def test_back_off_per_username_and_address_never_a_lockout(self, portal):
        from sentrix_portal.models import LoginAttempt
        add_household(portal, complete=True)
        attacker = Browser(portal, ip="203.0.113.9")
        for _ in range(4):                            # BACKOFF_FREE_FAILURES
            attacker.get("/login")
            assert attacker.post("/login", {"username": "riya",
                                            "password": "guess guess"}).status_code == 401
        attacker.get("/login")
        blocked = attacker.post("/login", {"username": "riya", "password": PASSWORD})
        assert blocked.status_code == 429, "the right password waits too: no oracle"
        # The owner's phone on another address is not affected (no hard lockout).
        phone = Browser(portal, ip="198.51.100.4")
        phone.get("/login")
        assert phone.post("/login", {"username": "riya",
                                     "password": PASSWORD}).status_code == 303
        # The wait ends by itself.
        _age(portal, LoginAttempt, 120)
        attacker.get("/login")
        assert attacker.post("/login", {"username": "riya",
                                        "password": PASSWORD}).status_code == 303

    def test_wait_doubles_and_is_capped(self):
        from sentrix_portal import security
        free = security.BACKOFF_FREE_FAILURES
        waits = [security._wait_for(n, free) for n in range(free + 8)]
        assert waits[:free] == [0] * free
        assert waits[free:free + 3] == [30, 60, 120]
        assert max(waits) == security.BACKOFF_CAP_SECONDS

    def test_a_success_resets_the_count(self, portal):
        add_household(portal, complete=True)
        browser = Browser(portal, ip="203.0.113.10")
        for _ in range(3):
            browser.get("/login")
            browser.post("/login", {"username": "riya", "password": "guess guess"})
        browser.get("/login")
        assert browser.post("/login", {"username": "riya",
                                       "password": PASSWORD}).status_code == 303
        fresh = Browser(portal, ip="203.0.113.10")
        statuses = []
        for _ in range(5):
            fresh.get("/login")
            statuses.append(fresh.post("/login", {"username": "riya",
                                                  "password": "guess guess"}).status_code)
        assert statuses == [401, 401, 401, 401, 429], "counted again from the success"


# ── activation (D14) ──────────────────────────────────────────────────────────
class TestActivation:
    def test_letter_code_activates_once(self, portal, outbox):
        admin = staff_browser(portal)
        household_id, code = _pending_customer(portal, admin)
        customer = Browser(portal)
        customer.get("/login")
        assert customer.post("/login", {"username": "riya",
                                        "password": PASSWORD}).status_code == 401
        customer.get("/activate?u=riya")
        response = customer.post("/activate", {"username": "riya", "code": code.lower(),
                                               "password": PASSWORD,
                                               "password_confirm": PASSWORD})
        assert response.status_code == 303 and response.headers["location"] == "/onboarding"
        assert customer.get("/onboarding/1").status_code == 200, "signed in after activation"
        assert outbox.links("riya@example.com", "/account/verify-email/"), "verify e-mail"
        again = Browser(portal)
        again.get("/activate")
        reused = again.post("/activate", {"username": "riya", "code": code,
                                          "password": "another good phrase",
                                          "password_confirm": "another good phrase"})
        assert reused.status_code == 400, "single use"
        again.get("/login")
        assert again.post("/login", {"username": "riya",
                                     "password": PASSWORD}).status_code == 303

    def test_activation_link_and_expiry(self, portal):
        from sentrix_portal.models import ActivationCode
        admin = staff_browser(portal)
        _, code = _pending_customer(portal, admin)
        browser = Browser(portal)
        page = browser.get(f"/activate/{code.replace('-', '')}")
        assert page.status_code == 200 and "riya" in page.text
        _age(portal, ActivationCode, 73 * 3600)
        expired = browser.get(f"/activate/{code.replace('-', '')}")
        assert expired.status_code == 404
        response = browser.post(f"/activate/{code.replace('-', '')}",
                                {"password": PASSWORD, "password_confirm": PASSWORD})
        assert response.status_code == 400, "an expired code sets nothing"
        user = query(portal, User, username="riya")[0]
        assert user.password_hash is None and user.status == "pending_activation"

    def test_a_new_code_voids_the_old_one(self, portal):
        admin = staff_browser(portal)
        household_id, first = _pending_customer(portal, admin)
        admin.get(f"/admin/customers/{household_id}/letter")
        second = admin.post(f"/admin/customers/{household_id}/letter", {})
        assert first not in second.text
        browser = Browser(portal)
        assert browser.get(f"/activate/{first.replace('-', '')}").status_code == 404

    def test_wrong_codes_are_rate_limited_and_burn_the_code(self, portal):
        from sentrix_portal.models import ActivationCode
        admin = staff_browser(portal)
        _, code = _pending_customer(portal, admin)
        browser = Browser(portal, ip="203.0.113.20")
        statuses = []
        for _ in range(5):
            browser.get("/activate")
            statuses.append(browser.post("/activate", {
                "username": "riya", "code": "BBBB-BBBB-BBBB", "password": PASSWORD,
                "password_confirm": PASSWORD}).status_code)
        assert statuses == [400, 400, 400, 400, 429]
        assert query(portal, ActivationCode, used_at=None)[0].attempts == 4
        # Enough wrong guesses from many addresses burn the code itself.
        for n in range(6):
            other = Browser(portal, ip=f"198.51.100.{30 + n}")
            other.get("/activate")
            other.post("/activate", {"username": "riya", "code": "CCCC-CCCC-CCCC",
                                     "password": PASSWORD, "password_confirm": PASSWORD})
        late = Browser(portal, ip="198.51.100.99")
        late.get("/activate")
        assert late.post("/activate", {"username": "riya", "code": code,
                                       "password": PASSWORD,
                                       "password_confirm": PASSWORD}).status_code == 400

    def test_password_policy(self, portal):
        admin = staff_browser(portal)
        _, code = _pending_customer(portal, admin)
        browser = Browser(portal)
        for password, confirm in (("short", "short"), ("password123", "password123"),
                                  ("riya-is-my-name", "riya-is-my-name"),
                                  (PASSWORD, PASSWORD + "x")):
            browser.get("/activate")
            response = browser.post("/activate", {"username": "riya", "code": code,
                                                  "password": password,
                                                  "password_confirm": confirm})
            assert response.status_code == 400, password
        browser.get("/activate")
        assert browser.post("/activate", {"username": "riya", "code": code,
                                          "password": PASSWORD,
                                          "password_confirm": PASSWORD}).status_code == 303

    def test_codes_are_stored_as_digests_only(self, portal):
        from sentrix_portal import security
        from sentrix_portal.models import ActivationCode
        admin = staff_browser(portal)
        _, code = _pending_customer(portal, admin)
        rows = query(portal, ActivationCode)
        assert [r.code_hash for r in rows] == [security.code_digest(code)]
        with portal.state.db.engine.connect() as conn:
            dump = "\n".join(str(row) for table in ("activation_codes", "users", "audit_log")
                             for row in conn.exec_driver_sql(f"SELECT * FROM {table}"))
        assert code not in dump and code.replace("-", "") not in dump


class TestAdminNeverSeesACredential:
    def test_after_activation_only_the_customer_gets_a_code(self, portal, outbox):
        admin = staff_browser(portal)
        household_id, code = _pending_customer(portal, admin)
        customer = Browser(portal)
        customer.get("/activate")
        customer.post("/activate", {"username": "riya", "code": code, "password": PASSWORD,
                                    "password_confirm": PASSWORD})
        admin.get(f"/admin/customers/{household_id}/letter")
        refused = admin.post(f"/admin/customers/{household_id}/letter", {})
        assert refused.status_code == 409 and "code-big" not in refused.text
        page = admin.get(f"/admin/customers/{household_id}")
        reset = admin.post(f"/admin/customers/{household_id}/resend", {})
        assert reset.status_code == 303 and "done=resent" in reset.headers["location"]
        codes = outbox.codes("riya@example.com")
        assert len(codes) == 1, "the reset code went to the customer's e-mail"
        for path in (reset.headers["location"], f"/admin/customers/{household_id}",
                     "/admin/customers", "/admin/audit"):
            html = admin.get(path).text
            assert codes[0] not in html and codes[0].replace("-", "") not in html
            assert "$argon2" not in html
        assert "$argon2" not in page.text
        # The reset code works for the customer; the old password keeps working until then.
        customer = Browser(portal)
        customer.get("/login")
        assert customer.post("/login", {"username": "riya",
                                        "password": PASSWORD}).status_code == 303
        customer = Browser(portal)
        customer.get(f"/activate/{codes[0].replace('-', '')}")
        done = customer.post(f"/activate/{codes[0].replace('-', '')}",
                             {"password": "a brand new phrase", "password_confirm":
                              "a brand new phrase"})
        assert done.status_code == 303

    def test_admin_cannot_reach_household_pages(self, portal):
        add_household(portal, complete=True)
        admin = staff_browser(portal)
        for path in ("/household", "/household/contacts", "/settings", "/privacy",
                     "/onboarding/1", "/claim", "/privacy/export"):
            assert admin.get(path).status_code == 403, path
        assert admin.get("/home").headers["location"] == "/admin"


# ── TOTP for staff ────────────────────────────────────────────────────────────
class TestTotp:
    def test_staff_must_pass_totp_every_session(self, portal):
        secret = pyotp.random_base32()
        add_user(portal, username="desk1", role="operator", totp_secret=secret)
        browser = Browser(portal)
        response = browser.login("desk1")
        assert response.headers["location"] == "/account/2fa"
        assert browser.get("/account").headers["location"] == "/account/2fa"
        assert browser.get("/admin").status_code in (303, 403)
        browser.get("/account/2fa")
        assert browser.post("/account/2fa", {"action": "verify",
                                             "code": "000000"}).status_code == 400
        from sentrix_portal import security
        security._totp_last_step.clear()
        code = pyotp.TOTP(secret).now()
        ok = browser.post("/account/2fa", {"action": "verify", "code": code})
        assert ok.status_code == 303 and ok.headers["location"] == "/desk"
        assert browser.get("/account").status_code == 200
        second = Browser(portal)
        second.login("desk1")
        second.get("/account/2fa")
        assert second.post("/account/2fa", {"action": "verify",
                                            "code": code}).status_code == 400, "no replay"

    def test_first_sign_in_sets_up_totp_and_recovery_codes(self, portal):
        from sentrix_portal import security
        from sentrix_portal.models import User
        add_user(portal, username="boss", role="company_admin")
        browser = Browser(portal)
        browser.login("boss")
        page = browser.get("/account/2fa")
        assert '<svg class="qr"' in page.text
        secret = security.unseal(query(portal, User, username="boss")[0].totp_secret_enc)
        security._totp_last_step.clear()
        setup = browser.post("/account/2fa", {"action": "setup",
                                              "code": pyotp.TOTP(secret).now()})
        codes = re.findall(r"\b[A-Z]{5}-[A-Z]{5}\b", setup.text)
        assert setup.status_code == 200 and len(codes) == security.RECOVERY_CODE_COUNT
        assert browser.get("/admin").status_code == 200
        user = query(portal, User, username="boss")[0]
        assert user.totp_enabled and codes[0] not in str(user.recovery_codes)
        # A recovery code works once.
        for expected in (303, 400):
            other = Browser(portal)
            other.login("boss")
            other.get("/account/2fa")
            assert other.post("/account/2fa", {"action": "verify",
                                               "code": codes[0]}).status_code == expected

    def test_household_roles_need_no_totp(self, portal):
        add_household(portal, complete=True)
        browser = owner_browser(portal)
        assert browser.get("/account/2fa").status_code == 200
        assert browser.get("/settings").status_code == 200


# ── sessions, step-up, password, e-mail ──────────────────────────────────────
class TestAccount:
    def test_sessions_page_revokes_only_your_own(self, portal):
        from sentrix_portal.models import UserSession
        add_household(portal, complete=True)
        laptop, phone = owner_browser(portal), owner_browser(portal)
        other_id = add_user(portal, username="other", role="owner")
        stranger = owner_browser(portal, "other")
        theirs = query(portal, UserSession, user_id=other_id)[0].id_hash
        laptop.get("/account/sessions")
        laptop.post("/account/sessions", {"session": theirs})
        assert stranger.get("/account").status_code == 200, "not yours to revoke"
        laptop.post("/account/sessions", {"session": "others"})
        assert phone.get("/home").headers["location"] == "/login"
        assert laptop.get("/home").status_code == 200

    def test_step_up_and_password_change(self, portal, outbox):
        from sentrix_portal.models import UserSession
        add_household(portal, complete=True)
        browser, other = owner_browser(portal), owner_browser(portal)
        browser.get("/account")
        failed = browser.post("/account/step-up", {"password": "nope", "next": "/settings"})
        assert failed.headers["location"] == "/settings?stepup=failed"
        browser.get("/account/password")
        assert browser.post("/account/password", {
            "current_password": "wrong one", "password": "a new long phrase",
            "password_confirm": "a new long phrase"}).status_code == 400
        changed = browser.post("/account/password", {
            "current_password": PASSWORD, "password": "a new long phrase",
            "password_confirm": "a new long phrase"})
        assert changed.status_code == 303
        assert other.get("/home").headers["location"] == "/login"
        assert outbox.to("riya@example.com"), "the owner is told"
        assert len([s for s in query(portal, UserSession) if s.revoked_at is None]) == 1

    def test_email_change_needs_step_up_and_reverification(self, portal, outbox):
        from sentrix_portal.models import UserSession
        add_household(portal, complete=True)
        browser = owner_browser(portal)
        _age(portal, UserSession, 0)
        with portal.state.db.session() as db:
            for row in db.query(UserSession):
                row.step_up_at = row.step_up_at - timedelta(minutes=10)
        browser.get("/account")
        refused = browser.post("/account", {"display_name": "Riya", "email": "new@example.com"})
        assert refused.status_code == 400
        browser.post("/account/step-up", {"password": PASSWORD, "next": "/account"})
        browser.get("/account")
        assert browser.post("/account", {"display_name": "Riya",
                                         "email": "new@example.com"}).status_code == 303
        links = outbox.links("new@example.com", "/account/verify-email/")
        assert links
        verifier = Browser(portal)
        assert verifier.get(f"/account/verify-email/{links[0]}").status_code == 200
        assert verifier.post(f"/account/verify-email/{links[0]}", {}).status_code == 200
        assert verifier.get(f"/account/verify-email/{links[0]}").status_code == 404, "once"

    def test_admin_action_then_new_address_raises_an_alert(self, portal, outbox):
        home = add_household(portal, complete=True)
        owner_browser(portal, ip="198.51.100.7")
        admin = staff_browser(portal)
        admin.get(f"/admin/customers/{home['household_id']}")
        admin.post(f"/admin/customers/{home['household_id']}/resend", {})
        owner_browser(portal, ip="198.51.100.7")
        assert not query(portal, AuditLog, event="security.admin_then_new_ip")
        owner_browser(portal, ip="203.0.113.77")
        alerts = query(portal, AuditLog, event="security.admin_then_new_ip")
        assert len(alerts) == 1 and alerts[0].detail["admin_event"].startswith("admin.")
        assert any("new sign-in" in (m.subject or "") for m in outbox.to("riya@example.com"))


@pytest.mark.parametrize("target,expected", [
    ("/settings", "/settings"), ("//evil.example", "/x"), ("https://evil", "/x"),
    ("/ok\\evil", "/x"), ("", "/x"), (None, "/x")])
def test_safe_next(target, expected):
    from sentrix_portal.security import safe_next
    assert safe_next(target, "/x") == expected
