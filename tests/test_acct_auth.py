"""Sign-in, back-off, activation, TOTP, sessions, step-up and e-mail
verification through the app's JSON API (docs/7 §10, D14, docs/9). Nothing is
sent: e-mails land in a recorder."""

from __future__ import annotations

import re
from datetime import timedelta

import pyotp
import pytest

from sentrix_portal import security
from sentrix_portal.models import AuditLog, User
from test_acct_support import (PASSWORD, add_household, add_user, fake_hub,  # noqa: F401
                               outbox, portal, query)
from test_portal_api_v2_support import Api, api_as, expire_step_up, staff_api


def _age(app, model, seconds: int, **filters) -> None:
    """Move rows of a model back in time (at / expires_at / created_at)."""
    with app.state.db.session() as db:
        for row in db.query(model).filter_by(**filters):
            for column in ("at", "expires_at", "created_at"):
                if getattr(row, column, None) is not None:
                    setattr(row, column, getattr(row, column) - timedelta(seconds=seconds))


def _pending_customer(app, admin: Api, *, username="riya", email="riya@example.com"):
    """Admin creates a customer and prints the letter; returns (household id, code)."""
    created = admin.post("/admin/customers", {"name": "Riya Sharma", "username": username,
                                              "email": email, "city": "Patiala"})
    assert created.status_code == 201, created.text
    household_id = created.json()["id"]
    letter = admin.post(f"/admin/customers/{household_id}/letter", {})
    assert letter.status_code == 200 and letter.headers["cache-control"] == "no-store"
    return household_id, letter.json()["activation_code"]


def _login(app, username, password=PASSWORD, ip="testclient"):
    api = Api(app, ip=ip)
    return api, api.login(username, password)


def _activate(app, body, ip="testclient", code_in_path=None):
    api = Api(app, ip=ip)
    api.session()
    path = f"/auth/activate/{code_in_path}" if code_in_path else "/auth/activate"
    return api, api.post(path, body)


# ── sign-in and sign-out ──────────────────────────────────────────────────────
class TestLogin:
    def test_login_lands_by_role_and_logout_needs_csrf(self, portal):
        add_household(portal, complete=True)
        api, response = _login(portal, "riya")
        assert response.json()["next"] == "/home"
        assert api.get("/home").status_code == 200
        assert api.post("/auth/logout", csrf=False).status_code == 403
        assert api.get("/home").status_code == 200, "still signed in"
        assert api.post("/auth/logout").status_code == 200
        assert api.get("/home").status_code == 401
        events = [r.event for r in query(portal, AuditLog)]
        assert "auth.login" in events and "auth.logout" in events

    def test_unfinished_onboarding_lands_in_the_wizard(self, portal):
        add_household(portal)
        assert _login(portal, "riya")[1].json()["next"] == "/onboarding"

    def test_login_needs_its_csrf_cookie(self, portal):
        add_household(portal)
        api = Api(portal)
        response = api.post("/auth/login", {"username": "riya", "password": PASSWORD},
                            headers={"X-CSRF-Token": "forged-forged-forged"})
        assert response.status_code == 403

    def test_wrong_password_and_unknown_user_look_the_same(self, portal):
        add_household(portal)
        wrong = _login(portal, "riya", "nope nope nope")[1]
        ghost = _login(portal, "ghost", "nope nope nope")[1]
        assert wrong.status_code == ghost.status_code == 401
        assert wrong.json() == ghost.json()

    def test_next_is_never_an_open_redirect(self, portal):
        add_household(portal, complete=True)
        for target, expected in (("//evil.example/x", "/home"), ("/settings", "/settings")):
            api = Api(portal)
            api.session()
            response = api.post("/auth/login", {"username": "riya", "password": PASSWORD,
                                                "next": target})
            assert response.json()["next"] == expected

    def test_disabled_account_cannot_sign_in(self, portal):
        add_user(portal, username="gone", role="owner", status="disabled")
        assert _login(portal, "gone")[1].status_code == 403


class TestBackoff:
    def test_back_off_per_username_and_address_never_a_lockout(self, portal):
        from sentrix_portal.models import LoginAttempt
        add_household(portal, complete=True)
        attacker = Api(portal, ip="203.0.113.9")
        for _ in range(4):                            # BACKOFF_FREE_FAILURES
            assert attacker.login("riya", "guess guess").status_code == 401
        blocked = attacker.login("riya")
        assert blocked.status_code == 429, "the right password waits too: no oracle"
        # The owner's phone on another address is not affected (no hard lockout).
        assert _login(portal, "riya", ip="198.51.100.4")[1].status_code == 200
        # The wait ends by itself.
        _age(portal, LoginAttempt, 120)
        assert attacker.login("riya").status_code == 200

    def test_wait_doubles_and_is_capped(self):
        free = security.BACKOFF_FREE_FAILURES
        waits = [security._wait_for(n, free) for n in range(free + 8)]
        assert waits[:free] == [0] * free
        assert waits[free:free + 3] == [30, 60, 120]
        assert max(waits) == security.BACKOFF_CAP_SECONDS

    def test_a_success_resets_the_count(self, portal):
        add_household(portal, complete=True)
        api = Api(portal, ip="203.0.113.10")
        for _ in range(3):
            api.login("riya", "guess guess")
        assert api.login("riya").status_code == 200
        fresh = Api(portal, ip="203.0.113.10")
        statuses = [fresh.login("riya", "guess guess").status_code for _ in range(5)]
        assert statuses == [401, 401, 401, 401, 429], "counted again from the success"


# ── activation (D14) ──────────────────────────────────────────────────────────
class TestActivation:
    def test_letter_code_activates_once(self, portal, outbox):
        admin = staff_api(portal)
        _, code = _pending_customer(portal, admin)
        assert _login(portal, "riya")[1].status_code == 401
        customer, response = _activate(portal, {"username": "riya", "code": code.lower(),
                                                "password": PASSWORD,
                                                "password_confirm": PASSWORD})
        assert response.status_code == 200 and response.json()["next"] == "/onboarding"
        assert customer.get("/onboarding/1").status_code == 200, "signed in after activation"
        assert outbox.links("riya@example.com", "/account/verify-email/"), "verify e-mail"
        _, reused = _activate(portal, {"username": "riya", "code": code,
                                       "password": "another good phrase",
                                       "password_confirm": "another good phrase"})
        assert reused.status_code == 422, "single use"
        assert _login(portal, "riya")[1].status_code == 200

    def test_activation_link_and_expiry(self, portal):
        from sentrix_portal.models import ActivationCode
        admin = staff_api(portal)
        _, code = _pending_customer(portal, admin)
        link = code.replace("-", "")
        api = Api(portal)
        page = api.get(f"/auth/activate/{link}")
        assert page.status_code == 200 and page.json()["username"] == "riya"
        _age(portal, ActivationCode, 73 * 3600)
        assert api.get(f"/auth/activate/{link}").status_code == 404
        _, response = _activate(portal, {"password": PASSWORD, "password_confirm": PASSWORD},
                                code_in_path=link)
        assert response.status_code == 422, "an expired code sets nothing"
        user = query(portal, User, username="riya")[0]
        assert user.password_hash is None and user.status == "pending_activation"

    def test_a_new_code_voids_the_old_one(self, portal):
        admin = staff_api(portal)
        household_id, first = _pending_customer(portal, admin)
        second = admin.post(f"/admin/customers/{household_id}/letter", {})
        assert first not in second.text
        assert Api(portal).get(f"/auth/activate/{first.replace('-', '')}").status_code == 404

    def test_wrong_codes_are_rate_limited_and_burn_the_code(self, portal):
        from sentrix_portal.models import ActivationCode
        admin = staff_api(portal)
        _, code = _pending_customer(portal, admin)
        api = Api(portal, ip="203.0.113.20")
        api.session()
        statuses = [api.post("/auth/activate", {
            "username": "riya", "code": "BBBB-BBBB-BBBB", "password": PASSWORD,
            "password_confirm": PASSWORD}).status_code for _ in range(5)]
        assert statuses == [422, 422, 422, 422, 429]
        assert query(portal, ActivationCode, used_at=None)[0].attempts == 4
        # Enough wrong guesses from many addresses burn the code itself.
        for n in range(6):
            _activate(portal, {"username": "riya", "code": "CCCC-CCCC-CCCC",
                               "password": PASSWORD, "password_confirm": PASSWORD},
                      ip=f"198.51.100.{30 + n}")
        _, late = _activate(portal, {"username": "riya", "code": code, "password": PASSWORD,
                                     "password_confirm": PASSWORD}, ip="198.51.100.99")
        assert late.status_code == 422

    def test_password_policy(self, portal):
        admin = staff_api(portal)
        _, code = _pending_customer(portal, admin)
        api = Api(portal)
        api.session()
        for password, confirm in (("short", "short"), ("password123", "password123"),
                                  ("riya-is-my-name", "riya-is-my-name"),
                                  (PASSWORD, PASSWORD + "x")):
            response = api.post("/auth/activate", {"username": "riya", "code": code,
                                                   "password": password,
                                                   "password_confirm": confirm})
            assert response.status_code == 422, password
        assert api.post("/auth/activate", {"username": "riya", "code": code,
                                           "password": PASSWORD,
                                           "password_confirm": PASSWORD}).status_code == 200

    def test_codes_are_stored_as_digests_only(self, portal):
        from sentrix_portal.models import ActivationCode
        admin = staff_api(portal)
        _, code = _pending_customer(portal, admin)
        rows = query(portal, ActivationCode)
        assert [r.code_hash for r in rows] == [security.code_digest(code)]
        with portal.state.db.engine.connect() as conn:
            dump = "\n".join(str(row) for table in ("activation_codes", "users", "audit_log")
                             for row in conn.exec_driver_sql(f"SELECT * FROM {table}"))
        assert code not in dump and code.replace("-", "") not in dump


class TestAdminNeverSeesACredential:
    def test_after_activation_only_the_customer_gets_a_code(self, portal, outbox):
        admin = staff_api(portal)
        household_id, code = _pending_customer(portal, admin)
        _activate(portal, {"username": "riya", "code": code, "password": PASSWORD,
                           "password_confirm": PASSWORD})
        refused = admin.post(f"/admin/customers/{household_id}/letter", {})
        assert refused.status_code == 409 and "activation_code" not in refused.text
        reset = admin.post(f"/admin/customers/{household_id}/resend")
        assert reset.status_code == 200 and reset.json()["message"] == "E-mail queued."
        codes = outbox.codes("riya@example.com")
        assert len(codes) == 1, "the reset code went to the customer's e-mail"
        for path in (f"/admin/customers/{household_id}", "/admin/customers", "/admin/audit"):
            text = admin.get(path).text
            assert codes[0] not in text and codes[0].replace("-", "") not in text
            assert "$argon2" not in text
        # The old password keeps working until the reset code is used.
        assert _login(portal, "riya")[1].status_code == 200
        _, done = _activate(portal, {"password": "a brand new phrase",
                                     "password_confirm": "a brand new phrase"},
                            code_in_path=codes[0].replace("-", ""))
        assert done.status_code == 200

    def test_admin_cannot_reach_household_pages(self, portal):
        add_household(portal, complete=True)
        admin = staff_api(portal)
        for path in ("/household", "/household/contacts", "/settings", "/privacy",
                     "/onboarding/1", "/home"):
            assert admin.get(path).status_code == 403, path
        assert admin.post("/claim", {}).status_code == 403
        assert admin.post("/privacy/export").status_code == 403
        assert admin.get("/session").json()["landing"] == "/admin"


# ── TOTP for staff ────────────────────────────────────────────────────────────
class TestTotp:
    def test_staff_must_pass_totp_every_session(self, portal):
        secret = pyotp.random_base32()
        add_user(portal, username="desk1", role="operator", totp_secret=secret)
        api, response = _login(portal, "desk1")
        assert response.json()["next"] == "/account/2fa"
        assert api.get("/account").json()["error"]["code"] == "totp_required"
        assert api.get("/admin/overview").status_code == 403
        assert api.post("/account/2fa/verify", {"code": "000000"}).status_code == 422
        security._totp_last_step.clear()
        code = pyotp.TOTP(secret).now()
        ok = api.post("/account/2fa/verify", {"code": code})
        assert ok.status_code == 200 and ok.json()["next"] == "/desk"
        assert api.get("/account").status_code == 200
        second, _ = _login(portal, "desk1")
        assert second.post("/account/2fa/verify", {"code": code}).status_code == 422, \
            "no replay"

    def test_first_sign_in_sets_up_totp_and_recovery_codes(self, portal):
        add_user(portal, username="boss", role="company_admin")
        api, _ = _login(portal, "boss")
        state = api.get("/account/2fa").json()
        assert state["setup"]["uri"].startswith("otpauth://")
        secret = security.unseal(query(portal, User, username="boss")[0].totp_secret_enc)
        assert state["setup"]["key"].replace(" ", "") == secret
        security._totp_last_step.clear()
        setup = api.post("/account/2fa/setup", {"code": pyotp.TOTP(secret).now()})
        codes = setup.json()["recovery_codes"]
        assert len(codes) == security.RECOVERY_CODE_COUNT
        assert api.get("/admin/overview").status_code == 200
        user = query(portal, User, username="boss")[0]
        assert user.totp_enabled and codes[0] not in str(user.recovery_codes)
        # A recovery code works once.
        for expected in (200, 422):
            other, _ = _login(portal, "boss")
            assert other.post("/account/2fa/verify",
                              {"code": codes[0]}).status_code == expected

    def test_household_roles_need_no_totp(self, portal):
        home = add_household(portal, complete=True)
        api = api_as(portal, home["owner_id"])
        assert api.get("/account/2fa").status_code == 200
        assert api.get("/settings").status_code == 200


# ── sessions, step-up, password, e-mail ──────────────────────────────────────
class TestAccount:
    def test_sessions_revoke_only_your_own(self, portal):
        home = add_household(portal, complete=True)
        laptop, phone = api_as(portal, home["owner_id"]), api_as(portal, home["owner_id"])
        other_id = add_user(portal, username="other", role="owner")
        stranger = api_as(portal, other_id)
        theirs = stranger.get("/account/sessions").json()["sessions"][0]["id"]
        assert laptop.delete(f"/account/sessions/{theirs}").status_code == 404
        assert stranger.get("/account").status_code == 200, "not yours to revoke"
        laptop.post("/account/sessions/revoke-others")
        assert phone.get("/home").status_code == 401
        assert laptop.get("/home").status_code == 200

    def test_step_up_and_password_change(self, portal, outbox):
        from sentrix_portal.models import UserSession
        home = add_household(portal, complete=True)
        api, other = api_as(portal, home["owner_id"]), api_as(portal, home["owner_id"])
        assert api.post("/auth/step-up", {"password": "nope"}).status_code == 422
        assert api.post("/account/password", {
            "current_password": "wrong one", "password": "a new long phrase",
            "password_confirm": "a new long phrase"}).status_code == 422
        changed = api.post("/account/password", {
            "current_password": PASSWORD, "password": "a new long phrase",
            "password_confirm": "a new long phrase"})
        assert changed.status_code == 200
        assert other.get("/home").status_code == 401
        assert outbox.to("riya@example.com"), "the owner is told"
        assert len([s for s in query(portal, UserSession) if s.revoked_at is None]) == 1

    def test_email_change_needs_step_up_and_reverification(self, portal, outbox):
        home = add_household(portal, complete=True)
        api = api_as(portal, home["owner_id"])
        expire_step_up(portal)
        refused = api.patch("/account", {"display_name": "Riya", "email": "new@example.com"})
        assert refused.status_code == 403
        api.step_up()
        assert api.patch("/account", {"display_name": "Riya",
                                      "email": "new@example.com"}).status_code == 200
        links = outbox.links("new@example.com", "/account/verify-email/")
        assert links
        verifier = Api(portal)
        verifier.session()
        assert verifier.get(f"/account/verify-email/{links[0]}").status_code == 200
        assert verifier.post(f"/account/verify-email/{links[0]}").status_code == 200
        assert verifier.get(f"/account/verify-email/{links[0]}").status_code == 404, "once"

    def test_mobile_confirmed_by_an_sms_from_the_device(self, portal, fake_hub):
        home = add_household(portal, complete=True)
        api = api_as(portal, home["owner_id"])
        assert api.post("/account/phone/send").status_code == 200
        device_id, name, args, actor = fake_hub.commands[-1]
        assert (device_id, name) == (home["device_id"], "sms.send")
        assert args["to"] == "+919812345610" and actor["type"] == "owner"
        code = re.search(r"code (\d{6})", args["text"]).group(1)
        wrong = "000000" if code != "000000" else "111111"
        assert api.post("/account/phone/confirm", {"code": wrong}).status_code == 422
        assert api.post("/account/phone/confirm", {"code": code}).status_code == 200
        assert query(portal, User, id=home["owner_id"])[0].phone_verified_at is not None
        assert fake_hub.profiles[-1]["owner"]["phone_verified"] is True
        assert api.post("/account/phone/confirm", {"code": code}).status_code == 422, \
            "single use"

    def test_mobile_codes_are_limited_and_need_a_device(self, portal, fake_hub, monkeypatch):
        from sentrix_portal.services import hub
        home = add_household(portal, complete=True)
        api = api_as(portal, home["owner_id"])
        for _ in range(3):
            api.post("/account/phone/send")
        assert api.post("/account/phone/send").json()["error"]["code"] == "rate_limited"
        other = add_household(portal, code="SX-0002", owner="nodev", email="n@example.com",
                              phone="+919800000002")
        assert api_as(portal, other["owner_id"]).post("/account/phone/send").status_code == 409

        async def offline(*args, **kwargs):
            return {"ok": False, "code": "device_offline", "status": 503}
        monkeypatch.setattr(hub, "send_command", offline)
        third = add_household(portal, code="SX-0003", owner="third", complete=True,
                              email="t@example.com", phone="+919800000003")
        failing = api_as(portal, third["owner_id"]).post("/account/phone/send")
        assert failing.json()["error"]["code"] == "device_offline"

    def test_admin_action_then_new_address_raises_an_alert(self, portal, outbox):
        home = add_household(portal, complete=True)
        _login(portal, "riya", ip="198.51.100.7")
        admin = staff_api(portal)
        admin.post(f"/admin/customers/{home['household_id']}/resend")
        _login(portal, "riya", ip="198.51.100.7")
        assert not query(portal, AuditLog, event="security.admin_then_new_ip")
        _login(portal, "riya", ip="203.0.113.77")
        alerts = query(portal, AuditLog, event="security.admin_then_new_ip")
        assert len(alerts) == 1 and alerts[0].detail["admin_event"].startswith("admin.")
        assert any("new sign-in" in (m.subject or "") for m in outbox.to("riya@example.com"))


@pytest.mark.parametrize("target,expected", [
    ("/settings", "/settings"), ("//evil.example", "/x"), ("https://evil", "/x"),
    ("/ok\\evil", "/x"), ("", "/x"), (None, "/x")])
def test_safe_next(target, expected):
    from sentrix_portal.security import safe_next
    assert safe_next(target, "/x") == expected
