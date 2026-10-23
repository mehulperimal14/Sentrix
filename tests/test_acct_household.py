"""Household, contacts with their consent flow, members, settings, privacy
(consents, export, erasure), the profile JSON contract and the retention
worker (docs/7 §3.8, §5.11, §10; design §4.3-4.4)."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest

from sentrix.link import profile as device_profile
from sentrix.link.protocol import canonical_json, sha256_hex
from sentrix_portal import security
from sentrix_portal.models import (ActionToken, AssistanceRequest, AuditLog, Camera, Consent,
                                   Device, EmergencyContact, Household, HouseholdMember,
                                   IncidentMirror, RequestEvent, SosSnapshot, TelegramLink,
                                   User, UserSession, utcnow)
from sentrix_portal.services import accounts, retention
from test_acct_support import (PASSWORD, Browser, add_household, add_user,  # noqa: F401
                               fake_hub, outbox, owner_browser, portal, query)

CONTACT = {"action": "add_contact", "name": "Kavya", "relation": "neighbour",
           "phone": "98765 11111", "email": "kavya@example.com", "priority": "2"}


def _expire_step_up(app) -> None:
    with app.state.db.session() as db:
        for row in db.query(UserSession):
            row.step_up_at = row.step_up_at - timedelta(minutes=10)


def _step_up(browser: Browser, next_url: str = "/settings") -> None:
    browser.get(next_url)
    assert browser.post("/account/step-up", {"password": PASSWORD,
                                             "next": next_url}).status_code == 303


# ── the profile JSON (docs/7 §5.11) ───────────────────────────────────────────
EXPECTED_KEYS = {
    "": {"schema", "version", "issued_at", "device_id", "household_id", "customer_code",
         "language", "test_mode", "owner", "members", "contacts", "desk", "address",
         "household", "nearest", "cameras", "sos", "consents", "mode"},
    "owner": {"user_id", "name", "phone", "phone_verified"},
    "desk": {"name", "phone", "simulated"},
    "address": {"house", "building", "street", "locality", "landmark", "city", "district",
                "state", "pincode", "floor", "entry_note", "lat", "lon", "map_url"},
    "household": {"occupants", "pets", "medical_notes"},
    "sos": {"window_seconds", "safety_defaults", "contacts_for_single_source_l3",
            "pin_hash", "duress_pin_hash", "pin_version", "channels", "templates"},
    "consents": {"account_sos", "face_recognition", "medical_notes", "sos_snapshot",
                 "remote_view", "desk_live_view", "model_improvement", "notice_version"},
    "mode": {"armed"},
}


class TestProfile:
    def _profile(self, app, home):
        with app.state.db.session() as db:
            return accounts.build_profile(db, home["household_id"], home["device_id"])

    def test_matches_the_contract_schema(self, portal):
        home = add_household(portal, complete=True)
        with portal.state.db.session() as db:
            db.add(HouseholdMember(household_id=home["household_id"], name="Meera",
                                   relation="mother", age_band="senior"))
        profile = self._profile(portal, home)
        for path, keys in EXPECTED_KEYS.items():
            node = profile if not path else profile[path]
            assert set(node) == keys, path
        assert profile["schema"] == 1 and profile["test_mode"] is True
        assert profile["device_id"] == home["device_id"]
        assert profile["owner"]["phone"] == "+919812345610"
        assert profile["contacts"] == [{
            "id": home["contact_id"], "name": "Arjun", "relation": "brother",
            "phone": "+919876543210", "telegram": False, "priority": 1,
            "consent_status": "CONFIRMED", "can_reach_30min": False}]
        assert set(profile["members"][0]) == {"id", "name", "relation", "age_band", "user_id"}
        assert profile["cameras"] == [{"camera_id": "CAM_0", "room_label": "Living room",
                                       "floor": "Ground"}]
        assert profile["address"]["map_url"] == "https://maps.google.com/?q=30.3398,76.3869"
        assert profile["household"]["occupants"] == {"adults": 2, "children": 1, "seniors": 1}
        assert profile["sos"]["templates"] is None and profile["sos"]["window_seconds"] == 1800
        assert profile["sos"]["pin_hash"].startswith("$argon2id$")
        assert profile["desk"] == {"name": "SENTRIX Desk (simulated)", "phone": None,
                                   "simulated": True}
        assert device_profile.validate(profile) is None
        canonical_json(profile)                       # JSON-only values, no default=str

    def test_never_carries_credentials_or_contact_emails(self, portal):
        home = add_household(portal, complete=True)
        owner = query(portal, User, id=home["owner_id"])[0]
        text = canonical_json(self._profile(portal, home)).decode()
        assert owner.password_hash not in text
        assert "arjun@example.com" not in text and "riya@example.com" not in text
        for key in device_profile.FORBIDDEN_KEYS:
            assert f'"{key}"' not in text, key
        assert all("email" not in c for c in self._profile(portal, home)["contacts"])

    def test_the_device_accepts_and_caches_it(self, portal, temp_db):
        home = add_household(portal, complete=True)
        profile = self._profile(portal, home)
        ack = device_profile.store(profile, version=max(1, profile["version"]),
                                   sha256=sha256_hex(profile))
        assert ack == {"version": max(1, profile["version"]), "ok": True, "error": None}
        assert device_profile.sms_allowlist(profile) == frozenset({"+919812345610",
                                                                   "+919876543210"})

    def test_only_confirmed_contacts_and_consented_notes(self, portal):
        home = add_household(portal, complete=True)
        with portal.state.db.session() as db:
            db.add(EmergencyContact(household_id=home["household_id"], name="Pending",
                                    phone_enc=security.seal("+919800000001"), priority=2,
                                    consent_status="PENDING"))
            household = db.get(Household, home["household_id"])
            household.medical_enc = security.seal("uses a wheelchair")
            household.include_medical = True
        assert [c["name"] for c in self._profile(portal, home)["contacts"]] == ["Arjun"]
        assert self._profile(portal, home)["household"]["medical_notes"] is None, \
            "no medical consent"
        with portal.state.db.session() as db:
            household = db.get(Household, home["household_id"])
            owner = db.get(User, home["owner_id"])
            accounts.set_consent(db, household, "medical_notes", True, user=owner,
                                 method="test")
        assert self._profile(portal, home)["household"]["medical_notes"] == "uses a wheelchair"

    def test_other_households_device_is_refused(self, portal):
        home = add_household(portal, complete=True)
        other = add_household(portal, code="SX-0002", owner="other", complete=True,
                              email="o@example.com", phone="+919811111111")
        with portal.state.db.session() as db:
            with pytest.raises(LookupError):
                accounts.build_profile(db, home["household_id"], other["device_id"])

    def test_every_change_bumps_the_version_and_pushes(self, portal, fake_hub):
        home = add_household(portal, complete=True)
        browser = owner_browser(portal)
        before = query(portal, Household)[0].profile_version
        browser.get("/household/contacts")
        browser.post("/household/contacts", CONTACT)
        assert query(portal, Household)[0].profile_version == before, \
            "a pending contact is not in the profile"
        browser.get("/household/members")
        browser.post("/household/members", {"action": "add_member", "member_name": "Meera",
                                            "age_band": "senior"})
        browser.get("/settings")
        browser.post("/settings", {"section": "sos", "window": "1800", "safety_defaults": "1",
                                   "channel_email": "1", "channel_telegram": "1"})
        after = query(portal, Household)[0].profile_version
        assert after >= before + 2
        assert fake_hub.pushes and set(fake_hub.pushes) == {home["device_id"]}
        assert fake_hub.profiles[-1]["version"] == after
        assert fake_hub.profiles[-1]["sos"]["channels"]["telegram"] is True


# ── contacts and their consent ────────────────────────────────────────────────
class TestContactConsent:
    def test_confirm_then_opt_out(self, portal, outbox, fake_hub):
        add_household(portal, complete=True)
        owner = owner_browser(portal)
        owner.get("/household/contacts")
        added = owner.post("/household/contacts", CONTACT)
        assert added.status_code == 200 and "Ask Kavya to confirm" in added.text
        kavya = query(portal, EmergencyContact, name="Kavya")[0]
        assert kavya.consent_status == "PENDING"
        assert "Kavya" not in [c["name"] for c in fake_hub.profiles[-1]["contacts"]]
        token = outbox.links("kavya@example.com", "/consent/")[0]
        assert token in added.text, "the owner may also share the same link"

        contact = Browser(portal)
        page = contact.get(f"/consent/{token}")
        assert page.status_code == 200 and "Riya Sharma (demo)" in page.text
        assert "Mall Road" not in page.text, "nothing about the home on this page"
        assert query(portal, ActionToken, purpose="contact_consent")[0].opened_count == 1
        assert Browser(portal).post(f"/consent/{token}", {"decision": "confirm",
                                                          "csrf_token": "x"},
                                    csrf=False).status_code == 403
        done = contact.post(f"/consent/{token}", {"decision": "confirm"})
        assert done.status_code == 200 and "opt out later" in done.text
        kavya = query(portal, EmergencyContact, name="Kavya")[0]
        assert kavya.consent_status == "CONFIRMED" and kavya.confirmed_at is not None
        assert query(portal, Consent, subject_type="contact", subject_id=kavya.id)
        assert "Kavya" in [c["name"] for c in fake_hub.profiles[-1]["contacts"]]
        assert contact.get(f"/consent/{token}").status_code == 404, "single use"

        opt_out = done.text.split('id="opt-out-link" type="text" readonly value="')[1]
        opt_out = opt_out.split('"')[0].rsplit("/", 1)[1]
        page = contact.get(f"/consent/{opt_out}")
        assert "Opt out now" in page.text and "Yes, I confirm" not in page.text
        contact.post(f"/consent/{opt_out}", {"decision": "decline"})
        kavya = query(portal, EmergencyContact, name="Kavya")[0]
        assert kavya.consent_status == "REVOKED"
        assert kavya.phone_enc is None and kavya.email is None, "erased at once"
        assert "Kavya" not in [c["name"] for c in fake_hub.profiles[-1]["contacts"]]
        assert query(portal, AuditLog, event="contact.declined")

    def test_decline_from_pending_and_the_owner_sees_it(self, portal, outbox):
        add_household(portal, complete=True)
        owner = owner_browser(portal)
        owner.get("/household/contacts")
        owner.post("/household/contacts", CONTACT)
        token = outbox.links("kavya@example.com", "/consent/")[0]
        contact = Browser(portal)
        contact.get(f"/consent/{token}")
        assert "opted out" in contact.post(f"/consent/{token}", {"decision": "decline"}).text
        kavya = query(portal, EmergencyContact, name="Kavya")[0]
        assert kavya.consent_status == "DECLINED" and kavya.phone_enc is None
        assert "removed within 7 days" in owner.get("/household/contacts").text

    def test_a_new_link_voids_the_old_one(self, portal, outbox):
        add_household(portal, complete=True)
        owner = owner_browser(portal)
        owner.get("/household/contacts")
        owner.post("/household/contacts", CONTACT)
        first = outbox.links("kavya@example.com", "/consent/")[0]
        kavya = query(portal, EmergencyContact, name="Kavya")[0]
        again = owner.post("/household/contacts", {"action": "resend_contact",
                                                   "contact_id": kavya.id})
        second = outbox.links("kavya@example.com", "/consent/")[1]
        assert again.status_code == 200 and second in again.text
        assert Browser(portal).get(f"/consent/{first}").status_code == 404
        assert Browser(portal).get(f"/consent/{second}").status_code == 200

    def test_contact_without_email_gets_a_link_to_share(self, portal, outbox):
        add_household(portal, complete=True)
        owner = owner_browser(portal)
        owner.get("/household/contacts")
        page = owner.post("/household/contacts", {**CONTACT, "email": ""})
        assert "They have no e-mail on file" in page.text and "/consent/" in page.text
        assert not outbox.to("kavya@example.com")
        assert "no Telegram or e-mail" in page.text

    def test_new_number_needs_a_new_confirmation(self, portal, outbox, fake_hub):
        home = add_household(portal, complete=True)
        owner = owner_browser(portal)
        owner.get("/household/contacts")
        response = owner.post("/household/contacts", {
            "action": "update_contact", "contact_id": home["contact_id"], "name": "Arjun",
            "relation": "brother", "phone": "98765 00000", "email": "arjun@example.com",
            "priority": "1"})
        assert response.status_code == 200
        arjun = query(portal, EmergencyContact, id=home["contact_id"])[0]
        assert arjun.consent_status == "PENDING"
        assert fake_hub.profiles[-1]["contacts"] == []
        assert outbox.links("arjun@example.com", "/consent/")

    def test_consent_link_of_a_closed_household_is_dead(self, portal, outbox):
        home = add_household(portal, complete=True)
        owner = owner_browser(portal)
        owner.get("/household/contacts")
        owner.post("/household/contacts", CONTACT)
        token = outbox.links("kavya@example.com", "/consent/")[0]
        with portal.state.db.session() as db:
            db.get(Household, home["household_id"]).status = "disabled"
        assert Browser(portal).get(f"/consent/{token}").status_code == 404

    def test_confirmed_contact_may_link_telegram(self, tmp_path, outbox, fake_hub):
        from sentrix_portal.app import create_app
        from sentrix_portal.settings import PortalSettings
        app = create_app(PortalSettings.for_tests(tmp_path, telegram_bot_name="SentrixDemoBot"))
        add_household(app, complete=True)
        owner = owner_browser(app)
        owner.get("/household/contacts")
        owner.post("/household/contacts", CONTACT)
        token = outbox.links("kavya@example.com", "/consent/")[0]
        contact = Browser(app)
        contact.get(f"/consent/{token}")
        done = contact.post(f"/consent/{token}", {"decision": "confirm"})
        assert "https://t.me/SentrixDemoBot?start=" in done.text
        code = done.text.split("?start=")[1].split('"')[0]
        row = query(app, ActionToken, purpose="telegram_link")[0]
        assert row.token_hash == security.token_digest(code) and row.subject_type == "contact"
        app.state.db.dispose()


# ── members ───────────────────────────────────────────────────────────────────
class TestMembers:
    def test_add_update_remove(self, portal, outbox):
        home = add_household(portal, complete=True)
        owner = owner_browser(portal)
        owner.get("/household/members")
        owner.post("/household/members", {"action": "add_member", "member_name": "Dev",
                                           "member_relation": "son", "age_band": "adult",
                                           "invite_email": "dev@example.com"})
        member = query(portal, HouseholdMember, name="Dev")[0]
        dev = query(portal, User, id=member.user_id)[0]
        assert dev.role == "member" and dev.household_id == home["household_id"]
        assert dev.username.startswith("sx-0001-dev")
        code = outbox.codes("dev@example.com")[0]
        member_browser = Browser(portal)
        member_browser.get("/activate")
        assert member_browser.post("/activate", {"username": dev.username, "code": code,
                                                 "password": PASSWORD,
                                                 "password_confirm": PASSWORD}).status_code == 303
        assert member_browser.get("/household").status_code == 200
        bad = owner.post("/household/members", {"action": "update_member",
                                                "member_id": member.id, "age_band": "minor"})
        assert bad.status_code == 400, "a member with an account is no minor"
        owner.post("/household/members", {"action": "update_member", "member_id": member.id,
                                          "member_relation": "elder son", "age_band": "adult"})
        assert query(portal, HouseholdMember, id=member.id)[0].relation == "elder son"
        owner.post("/household/members", {"action": "remove_member", "member_id": member.id})
        assert not query(portal, HouseholdMember, id=member.id)
        assert query(portal, User, id=dev.id)[0].status == "closed"
        assert member_browser.get("/household").headers["location"].split("?")[0] == "/login"

    def test_members_view_but_never_edit(self, portal):
        home = add_household(portal, complete=True)
        add_user(portal, username="meera", role="member", household_id=home["household_id"])
        member = owner_browser(portal, "meera")
        member.get("/household")
        for path, data in (("/household", {"house": "x"}),
                           ("/household/contacts", CONTACT),
                           ("/household/members", {"action": "add_member"}),
                           ("/settings", {"section": "sos"}),
                           ("/privacy/consents", {"consent_account_sos": "1"}),
                           ("/privacy/export", {}), ("/privacy/erase", {}),
                           ("/claim", {"serial": "SX5-000001"})):
            assert member.post(path, data).status_code == 403, path


# ── settings ──────────────────────────────────────────────────────────────────
class TestSettings:
    def test_safety_settings_and_pins_need_a_fresh_password(self, portal):
        add_household(portal, complete=True)
        browser = owner_browser(portal)
        _expire_step_up(portal)
        browser.get("/settings")
        window = browser.post("/settings", {"section": "sos", "window": "3600",
                                            "safety_defaults": "1", "channel_email": "1"})
        assert window.status_code == 403
        assert query(portal, Household)[0].sos_window_s == 1800
        pins = browser.post("/settings", {"section": "pins", "sos_pin": "739204",
                                          "sos_pin_confirm": "739204"})
        assert pins.status_code == 403
        channels = browser.post("/settings", {"section": "sos", "window": "1800",
                                              "safety_defaults": "1"})
        assert channels.status_code == 303, "channels alone need no step-up"
        assert query(portal, Household)[0].channels == {"email": False, "telegram": False,
                                                        "whatsapp": False, "sms": False}
        _step_up(browser)
        assert browser.post("/settings", {"section": "sos", "window": "3600",
                                          "safety_defaults": "1"}).status_code == 303
        off = browser.post("/settings", {"section": "sos", "window": "3600"})
        assert off.status_code == 400, "turning safety defaults off needs the acknowledgement"
        assert browser.post("/settings", {"section": "sos", "window": "3600",
                                          "safety_off_ack": "1"}).status_code == 303
        household = query(portal, Household)[0]
        assert household.sos_window_s == 3600 and household.safety_defaults is False
        assert browser.post("/settings", {"section": "pins", "sos_pin": "739204",
                                          "sos_pin_confirm": "739204", "duress_pin": "739299",
                                          "duress_pin_confirm": "739299"}).status_code == 303
        household = query(portal, Household)[0]
        assert security.verify_pin(household.sos_pin_hash, "739204")
        assert security.verify_pin(household.duress_pin_hash, "739299")
        assert household.pin_version == 1
        assert "739204" not in json.dumps([r.detail for r in query(portal, AuditLog)])

    def test_telegram_link_code(self, tmp_path, outbox, fake_hub):
        from sentrix_portal.app import create_app
        from sentrix_portal.settings import PortalSettings
        app = create_app(PortalSettings.for_tests(tmp_path, telegram_bot_name="@SentrixDemoBot"))
        home = add_household(app, complete=True)
        browser = owner_browser(app)
        browser.get("/settings")
        page = browser.post("/settings", {"section": "telegram"})
        code = page.text.split("/start ")[1].split("<")[0]
        assert f"https://t.me/SentrixDemoBot?start={code}" in page.text
        assert len(code) == 22 and code.replace("-", "").replace("_", "").isalnum()
        row = query(app, ActionToken, purpose="telegram_link")[0]
        assert row.token_hash == security.token_digest(code)
        assert row.subject_id == home["owner_id"] and row.household_id == home["household_id"]
        browser.post("/settings", {"section": "telegram"})
        assert len([r for r in query(app, ActionToken, purpose="telegram_link")
                    if r.revoked_at is None]) == 1, "a new code voids the old one"
        with app.state.db.session() as db:
            db.add(TelegramLink(subject_type="user", subject_id=home["owner_id"],
                                household_id=home["household_id"], chat_id="12345"))
        link = query(app, TelegramLink)[0]
        assert "Unlink" in browser.get("/settings").text
        browser.post("/settings", {"section": "telegram_unlink", "link_id": link.id})
        assert query(app, TelegramLink)[0].status == "revoked"
        app.state.db.dispose()

    def test_device_names_and_unpair(self, portal, fake_hub):
        home = add_household(portal, complete=True)
        browser = owner_browser(portal)
        browser.get("/settings")
        browser.post("/settings", {"section": "device", "device_id": home["device_id"],
                                   "name": "Hall unit", "room_label": "Hall", "floor": "1"})
        assert query(portal, Camera, device_id=home["device_id"])[0].room_label == "Hall"
        assert fake_hub.profiles[-1]["cameras"][0]["room_label"] == "Hall"
        _expire_step_up(portal)
        browser.get("/settings")
        assert browser.post("/settings", {"section": "unpair",
                                          "device_id": home["device_id"]}).status_code == 403
        _step_up(browser)
        assert browser.post("/settings", {"section": "unpair",
                                          "device_id": home["device_id"]}).status_code == 303
        assert fake_hub.unpaired[0][0] == home["device_id"]
        assert fake_hub.unpaired[0][1]["type"] == "owner"
        device = query(portal, Device, id=home["device_id"])[0]
        assert device.state == "ASSIGNED" and device.claim_code_hash is None


# ── privacy ───────────────────────────────────────────────────────────────────
class TestPrivacy:
    def test_consent_toggles(self, portal, fake_hub):
        home = add_household(portal, complete=True)
        with portal.state.db.session() as db:
            household = db.get(Household, home["household_id"])
            household.medical_enc, household.include_medical = security.seal("asthma"), True
            accounts.set_consent(db, household, "medical_notes", True,
                                 user=db.get(User, home["owner_id"]), method="test")
        browser = owner_browser(portal)
        browser.get("/privacy")
        refused = browser.post("/privacy/consents", {"consent_remote_view": "1"})
        assert refused.status_code == 400, "account_sos is the service itself"
        browser.post("/privacy/consents", {"consent_account_sos": "1",
                                           "consent_remote_view": "1",
                                           "consent_face_recognition": "1"})
        with portal.state.db.session() as db:
            states = accounts.consent_states(db, home["household_id"])
        assert states["face_recognition"] and states["remote_view"]
        assert not states["medical_notes"] and not states["sos_snapshot"]
        household = query(portal, Household)[0]
        assert household.medical_enc is None and not household.include_medical
        assert fake_hub.profiles[-1]["consents"]["face_recognition"] is True
        assert fake_hub.profiles[-1]["consents"]["sos_snapshot"] is False
        withdrawn = [c for c in query(portal, Consent, purpose="sos_snapshot")
                     if c.withdrawn_at is not None]
        assert withdrawn and withdrawn[0].notice_version == "2026-10-1"
        assert "Consent history" in browser.get("/privacy").text

    def test_export_needs_step_up_and_holds_no_secrets(self, portal):
        home = add_household(portal, complete=True)
        with portal.state.db.session() as db:
            request = AssistanceRequest(id="11111111-1111-4111-8111-111111111111",
                                        device_id=home["device_id"],
                                        household_id=home["household_id"], state="APPROVED",
                                        flags={"duress": True}, authority="POLICE",
                                        incident_type="weapon")
            db.add(request)
            db.flush()
            db.add_all([
                RequestEvent(request_id=request.id, seq=1, origin="device", event="created",
                             from_state=None, to_state="DRAFTED"),
                RequestEvent(request_id=request.id, seq=2, origin="device", event="prompted",
                             from_state="DRAFTED", to_state="PENDING"),
                RequestEvent(request_id=request.id, seq=3, origin="device", event="approve",
                             from_state="PENDING", to_state="APPROVED", restricted=True,
                             detail={"duress": True}),
                RequestEvent(request_id=request.id, seq=1, origin="cloud", event="delivered",
                             from_state="APPROVED", to_state="DISPATCHED")])
        browser = owner_browser(portal)
        _expire_step_up(portal)
        browser.get("/privacy/export")
        assert browser.post("/privacy/export", {}).headers["location"].endswith("stepup=required")
        _step_up(browser, "/privacy/export")
        response = browser.post("/privacy/export", {})
        assert response.headers["content-type"].startswith("application/json")
        assert "attachment" in response.headers["content-disposition"]
        data = response.json()
        assert data["household"]["address"]["street"] == "Mall Road"
        assert data["contacts"][0]["phone"] == "+919876543210"
        assert data["household"]["sos"]["sos_pin_set"] is True
        text = response.text
        for secret in ("$argon2", "password_hash", "totp_secret", "recovery_codes",
                       "code_hash", "token_hash", "bootstrap_hash", "csrf_token", "id_hash"):
            assert secret not in text, secret
        sos = data["sos_requests"][0]
        assert sos["state"] == "DECLINED" and "duress" not in sos["flags"]
        assert sos["authority"] is None
        assert [e["event"] for e in data["sos_events"]] == ["created", "prompted"]
        assert query(portal, AuditLog, event="privacy.export")

    def test_erasure_is_scheduled_cancellable_then_done(self, portal, fake_hub):
        home = add_household(portal, complete=True)
        browser = owner_browser(portal)
        _expire_step_up(portal)
        browser.get("/privacy/erase")
        assert browser.post("/privacy/erase", {"action": "schedule",
                                               "confirm": "ERASE"}).status_code == 403
        _step_up(browser, "/privacy/erase")
        assert browser.post("/privacy/erase", {"action": "schedule",
                                               "confirm": "erase"}).status_code == 400
        assert browser.post("/privacy/erase", {"action": "schedule",
                                               "confirm": "ERASE"}).status_code == 303
        household = query(portal, Household)[0]
        assert household.status == "erasing"
        assert "scheduled for erasure" in browser.get("/home").text
        browser.get("/privacy")
        browser.post("/privacy/erase", {"action": "cancel"})
        assert query(portal, Household)[0].status == "active"
        _step_up(browser, "/privacy/erase")
        browser.post("/privacy/erase", {"action": "schedule", "confirm": "ERASE"})
        with portal.state.db.session() as db:
            assert retention.prune(db) == {}, "not before the grace period ends"
        with portal.state.db.session() as db:
            db.get(Household, home["household_id"]).closed_at -= timedelta(
                days=accounts.ERASURE_GRACE_DAYS + 1)
        counts = asyncio.run(retention.run_once(portal))
        assert fake_hub.unpaired and fake_hub.unpaired[0][0] == home["device_id"]
        assert counts["erased.households"] == 1 and counts["erased.users"] == 1
        assert not query(portal, Household) and not query(portal, User, role="owner")
        assert not query(portal, EmergencyContact) and not query(portal, Consent)
        device = query(portal, Device, id=home["device_id"])[0]
        assert device.state == "UNASSIGNED" and device.household_id is None
        assert query(portal, AuditLog, event="privacy.erased")
        with portal.state.db.session() as db:
            assert security.verify_audit_chain(db)["ok"]
        assert browser.get("/home").headers["location"].split("?")[0] == "/login"


# ── the household's audit trail ───────────────────────────────────────────────
def test_audit_page_is_the_households_own_and_hides_duress(portal):
    home = add_household(portal, complete=True)
    other = add_household(portal, code="SX-0002", owner="other", email="o@example.com",
                          phone="+919811111111")
    with portal.state.db.session() as db:
        security.audit(db, "profile.updated", actor_type="user", actor_id=home["owner_id"],
                       household_id=home["household_id"], detail={"what": "address"})
        security.audit(db, "sos.approve", actor_type="user", actor_id=home["owner_id"],
                       household_id=home["household_id"], detail={"restricted": True})
        security.audit(db, "sos.duress_marker", household_id=home["household_id"],
                       detail={"duress": True})
        security.audit(db, "profile.updated", actor_type="user", actor_id=other["owner_id"],
                       household_id=other["household_id"], detail={"what": "secret-of-other"})
        security.audit(db, "admin.activation_resent", actor_type="admin",
                       household_id=home["household_id"])
    page = owner_browser(portal).get("/household/audit").text
    assert "profile.updated" in page and "SENTRIX staff" in page
    assert "sos.approve" not in page and "duress" not in page
    assert "secret-of-other" not in page


# ── the retention worker ──────────────────────────────────────────────────────
class TestRetention:
    def test_caps(self, portal):
        home = add_household(portal, complete=True)
        now = utcnow()
        old = lambda days: now - timedelta(days=days)            # noqa: E731
        with portal.state.db.session() as db:
            request = AssistanceRequest(id="22222222-2222-4222-8222-222222222222",
                                        household_id=home["household_id"],
                                        device_id=home["device_id"], state="EXPIRED",
                                        created_at=old(10))
            db.add(request)
            db.flush()
            db.add(SosSnapshot(request_id=request.id, data=b"jpeg", created_at=old(31),
                               expires_at=old(1)))
            for name, verdict, level, days in (("unreviewed-old", None, 3, 181),
                                               ("unreviewed-new", None, 3, 100),
                                               ("true-old", "TRUE", 3, 366),
                                               ("true-mid", "TRUE", 3, 200),
                                               ("false-low", "FALSE", 2, 40),
                                               ("false-high", "FALSE", 4, 40)):
                db.add(IncidentMirror(id=name, household_id=home["household_id"],
                                      device_id=home["device_id"], verdict=verdict,
                                      peak_level=level, started_at=old(days)))
            declined = EmergencyContact(household_id=home["household_id"], name="Gone",
                                        priority=3, consent_status="DECLINED")
            fresh = EmergencyContact(household_id=home["household_id"], name="Recent",
                                     priority=3, consent_status="DECLINED")
            db.add_all([declined, fresh])
            db.flush()
            db.add(Consent(household_id=home["household_id"], subject_type="contact",
                           subject_id=declined.id, purpose="account_sos", granted_at=None,
                           withdrawn_at=old(8)))
            db.add(Consent(household_id=home["household_id"], subject_type="contact",
                           subject_id=fresh.id, purpose="account_sos", granted_at=None,
                           withdrawn_at=old(2)))
        with portal.state.db.session() as db:
            counts = retention.prune(db)
        assert counts["sos_snapshots"] == 1 and counts["emergency_contacts"] == 1
        kept = {row.id for row in query(portal, IncidentMirror)}
        assert kept == {"unreviewed-new", "true-mid", "false-high"}
        assert {c.name for c in query(portal, EmergencyContact)} == {"Arjun", "Recent"}
        assert query(portal, AssistanceRequest), "requests stay for a year"

    def test_audit_prefix_is_pruned_behind_a_signed_boundary(self, portal):
        with portal.state.db.session() as db:
            for n in range(5):
                security.audit(db, "auth.login", detail={"n": n})
            for row in db.query(AuditLog).filter(AuditLog.id <= 3):
                row.at = row.at - timedelta(days=400)
        with portal.state.db.session() as db:
            # Moving ``at`` broke those rows' hashes on purpose: they are the ones dropped.
            assert retention.prune(db)["audit_log"] == 3
        with portal.state.db.session() as db:
            result = security.verify_audit_chain(db)
        assert result["ok"] and result["entries"] == 3
        assert result["retention_gaps"][0]["deleted"] == 3
        with portal.state.db.session() as db:
            first = db.query(AuditLog).order_by(AuditLog.id).first()
            first.prev_hash = "f" * 64
        with portal.state.db.session() as db:
            assert not security.verify_audit_chain(db)["ok"], "a forged cut is tampering"

    def test_housekeeping(self, portal):
        add_household(portal, complete=True)
        owner_browser(portal)
        with portal.state.db.session() as db:
            for row in db.query(UserSession):
                row.expires_at = utcnow() - timedelta(days=2)
            db.add(ActionToken(token_hash="x" * 64, purpose="email_verify",
                               expires_at=utcnow() - timedelta(days=31)))
        with portal.state.db.session() as db:
            counts = retention.prune(db)
        assert counts["sessions"] == 1 and counts["action_tokens"] == 1

    def test_background_module_starts_and_stops(self, portal, monkeypatch):
        calls = []
        monkeypatch.setattr(retention, "FIRST_RUN_DELAY_SECONDS", 0)

        async def fake_run(app):
            calls.append(app)
            return {}
        monkeypatch.setattr(retention, "run_once", fake_run)

        async def scenario():
            stop = retention.start(portal)
            await asyncio.sleep(0.05)
            await stop()
        asyncio.run(scenario())
        assert calls == [portal]
