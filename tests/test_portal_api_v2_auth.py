"""Portal API v2: session, sign-in, activation, account, two-factor and
sessions (docs/9 §4). JSON only, never a redirect; the error model; the
same back-off, audit events and step-up rules as the HTML pages."""

from __future__ import annotations

import re

import pyotp

from sentrix_portal import security
from sentrix_portal.models import (AuditLog, LoginAttempt, User, UserSession)
from test_acct_support import (PASSWORD, add_household, add_user, fake_hub,  # noqa: F401
                               outbox, portal, query)
from test_portal_api_v2_support import (Api, api_as, assert_no_secrets, error_of,
                                        expire_step_up, staff_api)


# ── the session ───────────────────────────────────────────────────────────────
def test_anonymous_session_sets_the_login_token(portal):
    api = Api(portal)
    response = api.get("/session")
    assert response.status_code == 200
    data = response.json()
    assert data["signed_in"] is False and data["user"] is None and data["csrf_token"] is None
    token = data["login_csrf_token"]
    assert token and response.cookies.get("portal_csrf") == token
    assert data["test_mode"]["on"] is True and data["password_min_length"] == 10
    assert "TEST MODE" in data["test_mode"]["banner"]
    again = api.get("/session").json()
    assert again["login_csrf_token"] == token, "two tabs keep the same token"


def test_login_from_json_and_the_signed_in_session(portal):
    home = add_household(portal)
    api = Api(portal)
    api.session()
    response = api.post("/auth/login", {"username": "riya", "password": PASSWORD})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["next"] == "/onboarding"
    session = body["session"]
    assert session["signed_in"] and session["user"]["role"] == "owner"
    assert session["user"]["totp"] == {"required": False, "enabled": False, "verified": False}
    assert session["household"]["id"] == home["household_id"]
    assert session["household"]["time_zone"] == "Asia/Kolkata"
    assert session["household"]["setup_done"] is False
    assert session["household"]["setup_step"] == 1
    assert session["stepped_up"] is True and session["step_up_until"].endswith("Z")
    api.csrf = session["csrf_token"]
    me = api.get("/session").json()
    assert me["csrf_token"] == session["csrf_token"] and me["login_csrf_token"] is None
    assert any(r.event == "auth.login" for r in query(portal, AuditLog))
    assert_no_secrets(portal, response, api.get("/session"))


def test_login_needs_the_login_csrf_token(portal):
    add_household(portal)
    api = Api(portal)
    api.session()
    refused = api.post("/auth/login", {"username": "riya", "password": PASSWORD}, csrf=False)
    assert refused.status_code == 403
    assert error_of(refused) == {"code": "csrf",
                                 "message": "The form expired. Please sign in again."}
    wrong = api.post("/auth/login", {"username": "riya", "password": PASSWORD},
                     headers={"X-CSRF-Token": "not-the-cookie-value-x"})
    assert wrong.status_code == 403 and error_of(wrong)["code"] == "csrf"
    assert not query(portal, LoginAttempt), "a refused form records nothing"


def test_wrong_password_and_back_off(portal):
    add_household(portal)
    api = Api(portal)
    api.session()
    for _ in range(4):
        wrong = api.post("/auth/login", {"username": "riya", "password": "nope nope nope"})
        assert wrong.status_code == 401
        assert error_of(wrong) == {"code": "not_signed_in",
                                   "message": "Wrong username or password."}
    assert len(query(portal, LoginAttempt)) == 4, "failures are recorded (committed)"
    assert sum(r.event == "auth.login_failed" for r in query(portal, AuditLog)) == 4
    blocked = api.post("/auth/login", {"username": "riya", "password": PASSWORD})
    assert blocked.status_code == 429
    error = error_of(blocked)
    assert error["code"] == "rate_limited" and error["retry_after"] > 0
    assert blocked.headers["retry-after"] == str(error["retry_after"])
    assert error["message"].startswith("Too many attempts. Wait ")


def test_a_disabled_account_is_refused(portal):
    add_user(portal, username="gone", role="owner", status="disabled")
    api = Api(portal)
    api.session()
    refused = api.post("/auth/login", {"username": "gone", "password": PASSWORD})
    assert refused.status_code == 403
    assert error_of(refused)["message"] == "This account is disabled. Contact SENTRIX."


def test_api_never_redirects_and_says_not_signed_in(portal):
    api = Api(portal)
    for path in ("/home", "/account", "/settings", "/sos", "/desk/queue", "/admin/overview"):
        response = api.get(path, headers={"accept": "text/html"})
        assert response.status_code == 401, path
        assert error_of(response) == {"code": "not_signed_in", "message": "Please sign in."}
    assert api.put("/household", {}).status_code == 401


def test_state_changes_need_the_csrf_header(portal):
    add_household(portal)
    api = Api(portal)
    assert api.login("riya").status_code == 200
    refused = api.patch("/account", {"display_name": "Riya S"}, csrf=False)
    assert refused.status_code == 403 and error_of(refused)["code"] == "csrf"
    stale = api.patch("/account", {"display_name": "Riya S"},
                      headers={"X-CSRF-Token": "x" * 32})
    assert stale.status_code == 403 and error_of(stale)["code"] == "csrf"
    ok = api.patch("/account", {"display_name": "Riya S", "email": "riya@example.com"})
    assert ok.status_code == 200 and ok.json()["display_name"] == "Riya S"


def test_logout(portal):
    add_household(portal)
    api = Api(portal)
    api.login("riya")
    assert api.post("/auth/logout", csrf=False).status_code == 403
    out = api.post("/auth/logout")
    assert out.status_code == 200 and out.json()["ok"] is True
    assert all(r.revoked_at for r in query(portal, UserSession))
    api.client.cookies.clear()
    assert api.post("/auth/logout", csrf=False).status_code == 200, "stale: just clears"


# ── activation ────────────────────────────────────────────────────────────────
def _pending_owner(app) -> tuple[str, str]:
    from sentrix_portal.services import accounts
    with app.state.db.session() as db:
        household, owner = accounts.create_customer(db, name="Asha Rao", username="asha",
                                                    email="asha@example.com", phone=None,
                                                    city="Patiala", created_by=None)
        code, _ = accounts.issue_activation_code(db, owner)
        return owner.id, code


def test_activation_with_a_typed_code(portal, outbox):
    _, code = _pending_owner(portal)
    api = Api(portal)
    api.session()
    mismatch = api.post("/auth/activate", {"username": "asha", "code": code,
                                           "password": "a long new phrase",
                                           "password_confirm": "something else"})
    assert mismatch.status_code == 422
    assert error_of(mismatch)["fields"] == {
        "password_confirm": "The two passwords do not match."}
    short = api.post("/auth/activate", {"username": "asha", "code": code,
                                        "password": "short", "password_confirm": "short"})
    assert error_of(short)["fields"] == {"password": "Use at least 10 characters."}
    wrong = api.post("/auth/activate", {"username": "asha", "code": "BBBB-BBBB-BBBB",
                                        "password": "a long new phrase",
                                        "password_confirm": "a long new phrase"})
    assert wrong.status_code == 422
    assert "do not match a valid activation code" in error_of(wrong)["message"]
    done = api.post("/auth/activate", {"username": "asha", "code": code,
                                       "password": "a long new phrase",
                                       "password_confirm": "a long new phrase"})
    assert done.status_code == 200, done.text
    assert done.json()["next"] == "/onboarding" and done.json()["session"]["signed_in"]
    assert query(portal, User, username="asha")[0].status == "active"
    assert outbox.to("asha@example.com"), "the e-mail verification went out"


def test_activation_by_link(portal):
    _, code = _pending_owner(portal)
    api = Api(portal)
    api.session()
    link = api.get(f"/auth/activate/{security.normalize_code(code)}")
    assert link.status_code == 200 and link.json()["username"] == "asha"
    assert error_of(api.get("/auth/activate/ZZZZZZZZZZZZ"))["code"] == "not_found"
    no_csrf = api.post(f"/auth/activate/{code}", {"password": "x"}, csrf=False)
    assert error_of(no_csrf)["code"] == "csrf"
    done = api.post(f"/auth/activate/{security.normalize_code(code)}",
                    {"password": "a long new phrase", "password_confirm": "a long new phrase"})
    assert done.status_code == 200, done.text


# ── step-up and account ───────────────────────────────────────────────────────
def test_step_up_and_the_e_mail_change(portal):
    home = add_household(portal)
    api = api_as(portal, home["owner_id"])
    expire_step_up(portal)
    assert api.get("/session").json()["stepped_up"] is False
    change = api.patch("/account", {"display_name": "Riya", "email": "new@example.com"})
    assert change.status_code == 403
    error = error_of(change)
    assert error["code"] == "step_up_required"
    assert error["fields"] == {"email": "Confirm your password below before changing the "
                                        "e-mail."}
    wrong = api.post("/auth/step-up", {"password": "not my password"})
    assert wrong.status_code == 422
    assert error_of(wrong)["fields"] == {"password": "That password was not right."}
    assert any(r.event == "auth.step_up_failed" for r in query(portal, AuditLog))
    assert api.step_up().json()["step_up_until"].endswith("Z")
    changed = api.patch("/account", {"display_name": "Riya", "email": "new@example.com"})
    assert changed.status_code == 200
    assert changed.json()["email"] == "new@example.com"
    assert changed.json()["email_verified"] is False
    bad = api.patch("/account", {"display_name": "R", "email": "new@example.com"})
    assert error_of(bad)["fields"] == {"display_name": "Name must be 2-80 characters."}


def test_account_view_has_no_secrets(portal):
    home = add_household(portal)
    api = api_as(portal, home["owner_id"])
    account = api.get("/account")
    data = account.json()
    assert data["username"] == "riya" and data["two_factor"] == "not_used"
    assert data["phone_masked"] and "98123" not in data["phone_masked"]
    assert_no_secrets(portal, account)


def test_password_change(portal):
    home = add_household(portal)
    other = api_as(portal, home["owner_id"])
    api = api_as(portal, home["owner_id"])
    wrong = api.post("/account/password", {"current_password": "nope", "password": "x",
                                           "password_confirm": "x"})
    assert error_of(wrong)["fields"] == {
        "current_password": "That is not your current password."}
    done = api.post("/account/password", {"current_password": PASSWORD,
                                          "password": "an entirely new phrase",
                                          "password_confirm": "an entirely new phrase"})
    assert done.status_code == 200
    assert done.json()["message"] == "Password changed. Your other sessions were signed out."
    assert other.get("/account").status_code == 401, "the other session ended"
    assert api.get("/account").status_code == 200


def test_mobile_confirmation_needs_a_device(portal):
    home = add_household(portal)
    api = api_as(portal, home["owner_id"])
    no_device = api.post("/account/phone/send")
    assert no_device.status_code == 409
    assert error_of(no_device)["message"] == ("Claim your device first: it sends the code "
                                              "from its own SIM.")
    bad = api.post("/account/phone/confirm", {"code": "123456"})
    assert bad.status_code == 422
    assert error_of(bad)["fields"] == {"code": "That code is not right or has expired."}
    assert query(portal, LoginAttempt), "the wrong code counts for the back-off"


def test_mobile_confirmation_by_the_device(portal, fake_hub):
    home = add_household(portal, complete=True)
    api = api_as(portal, home["owner_id"])
    sent = api.post("/account/phone/send")
    assert sent.status_code == 200, sent.text
    assert "sending the code" in sent.json()["message"]
    (device_id, name, args, _actor), = fake_hub.commands
    assert name == "sms.send" and device_id == home["device_id"]
    code = re.search(r"code (\d{6})", args["text"]).group(1)
    ok = api.post("/account/phone/confirm", {"code": code})
    assert ok.status_code == 200 and ok.json()["message"] == "Mobile number confirmed."
    assert api.get("/account").json()["phone_verified"] is True


def test_member_cannot_send_phone_codes(portal):
    home = add_household(portal)
    member = add_user(portal, username="dev", role="member",
                      household_id=home["household_id"])
    refused = api_as(portal, member).post("/account/phone/send")
    assert refused.status_code == 403 and error_of(refused)["code"] == "forbidden"


def test_e_mail_verification_link(portal):
    from sentrix_portal.services import accounts
    home = add_household(portal)
    with portal.state.db.session() as db:
        raw, _ = accounts.mint_email_verification(db, db.get(User, home["owner_id"]))
    api = Api(portal)
    api.session()
    assert api.get(f"/account/verify-email/{raw}").json() == {"valid": True}
    assert api.post(f"/account/verify-email/{raw}", csrf=False).status_code == 403
    done = api.post(f"/account/verify-email/{raw}")
    assert done.status_code == 200 and "Thank you" in done.json()["message"]
    again = api.post(f"/account/verify-email/{raw}")
    assert again.status_code == 404 and error_of(again)["code"] == "not_found"
    assert query(portal, User, id=home["owner_id"])[0].email_verified_at is not None


# ── sessions ──────────────────────────────────────────────────────────────────
def test_sessions_list_and_revoke(portal):
    home = add_household(portal)
    first = api_as(portal, home["owner_id"])
    second = api_as(portal, home["owner_id"])
    listing = first.get("/account/sessions")
    rows = listing.json()["sessions"]
    assert len(rows) == 2 and sum(r["current"] for r in rows) == 1
    assert_no_secrets(portal, listing)
    other = next(r for r in rows if not r["current"])
    gone = first.delete(f"/account/sessions/{other['id']}")
    assert gone.json() == {"revoked": 1, "signed_out": False}
    assert second.get("/account").status_code == 401
    assert first.delete(f"/account/sessions/{other['id']}").status_code == 404
    third = api_as(portal, home["owner_id"])
    assert first.post("/account/sessions/revoke-others").json()["revoked"] == 1
    assert third.get("/account").status_code == 401
    mine = next(r for r in first.get("/account/sessions").json()["sessions"] if r["current"])
    out = first.delete(f"/account/sessions/{mine['id']}")
    assert out.json() == {"revoked": 1, "signed_out": True}
    assert first.get("/account").status_code == 401


# ── two-factor (staff) ────────────────────────────────────────────────────────
def test_staff_must_pass_totp_and_the_setup_returns_codes_once(portal):
    add_user(portal, username="desk1", role="operator", name="Kartik")
    api = Api(portal)
    assert api.login("desk1").json()["next"] == "/account/2fa"
    blocked = api.get("/desk/queue")
    assert blocked.status_code == 403 and error_of(blocked)["code"] == "totp_required"
    state = api.get("/account/2fa").json()
    assert state["required"] and not state["enabled"] and state["setup"]["uri"].startswith(
        "otpauth://")
    secret = state["setup"]["key"].replace(" ", "")
    security._totp_last_step.clear()
    wrong = api.post("/account/2fa/setup", {"code": "000000"})
    assert wrong.status_code == 422 and "time on your phone" in error_of(wrong)["message"]
    done = api.post("/account/2fa/setup", {"code": pyotp.TOTP(secret).now()})
    assert done.status_code == 200
    codes = done.json()["recovery_codes"]
    assert len(codes) == 10
    after = api.get("/account/2fa")
    assert after.json()["enabled"] and after.json()["setup"] is None
    assert after.json()["recovery_codes_left"] == 10
    assert not any(code in after.text for code in codes), "shown once"
    assert api.get("/desk/queue").status_code == 200
    regen = api.post("/account/2fa/recovery-codes")
    assert regen.status_code == 200, "signed in just now: still stepped up"
    assert_no_secrets(portal, after)


def test_staff_verify_and_recovery_code(portal):
    secret = pyotp.random_base32()
    add_user(portal, username="admin9", role="company_admin", totp_secret=secret)
    api = Api(portal)
    api.login("admin9")
    security._totp_last_step.clear()
    wrong = api.post("/account/2fa/verify", {"code": "000000"})
    assert wrong.status_code == 422 and error_of(wrong)["message"] == "That code is not right."
    ok = api.post("/account/2fa/verify", {"code": pyotp.TOTP(secret).now()})
    assert ok.status_code == 200 and ok.json()["next"] == "/admin"
    assert api.get("/admin/overview").status_code == 200


def test_household_accounts_do_not_use_two_factor(portal):
    home = add_household(portal)
    api = api_as(portal, home["owner_id"])
    state = api.get("/account/2fa").json()
    assert state["required"] is False and state["setup"] is None
    refused = api.post("/account/2fa/setup", {"code": "123456"})
    assert refused.status_code == 409 and error_of(refused)["code"] == "conflict"


def test_regenerating_recovery_codes_needs_a_fresh_password(portal):
    api = staff_api(portal, role="operator", username="desk2")
    expire_step_up(portal)
    refused = api.post("/account/2fa/recovery-codes")
    assert refused.status_code == 403 and error_of(refused)["code"] == "step_up_required"
    api.step_up()
    assert len(api.post("/account/2fa/recovery-codes").json()["recovery_codes"]) == 10
