"""The 7-step onboarding wizard, its validation and the device claim
(design §4.1 as amended; docs/7 §5.2; decisions D8, D9, D14)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from sentrix_portal import security
from sentrix_portal.models import (AuditLog, ClaimAttempt, Consent, Device,
                                   EmergencyContact, Household, User)
from sentrix_portal.services import accounts
from test_acct_support import (PASSWORD, add_device, add_household, add_user,  # noqa: F401
                               fake_hub, outbox, portal, query)
from test_portal_api_v2_support import Api, api_as, expire_step_up

STEP1 = {"display_name": "Riya Sharma", "phone": "+91 98123 45610",
         "email": "riya@example.com", "language": "en", "adult": True}
STEP2 = {"house": "12-B", "building": "Green Park", "street": "Mall Road",
         "locality": "Sector 4", "landmark": "opposite the SBI ATM", "city": "Patiala",
         "district": "Patiala", "state": "Punjab", "pincode": "147001", "floor": "2",
         "lat": 30.3398, "lon": 76.3869, "entry_note": "Gate 2, guard cabin on the left",
         "police_name": "Civil Lines PS", "police_phone": "0175 2212345"}
STEP3 = {"adults": 2, "children": 1, "seniors": 1, "pets": "one dog",
         "medical": "Senior uses a wheelchair", "medical_consent": True,
         "include_medical": True}
CONTACT = {"name": "Arjun Sharma", "relation": "brother", "phone": "98765 43210",
           "email": "arjun@example.com", "priority": 1, "can_reach_30min": True}
STEP6 = {"window": 2700, "safety_defaults": True, "channels": {"email": True, "sms": True},
         "sos_pin": "482916", "sos_pin_confirm": "482916", "duress_pin": "482971",
         "duress_pin_confirm": "482971"}


# ── validators ────────────────────────────────────────────────────────────────
class TestValidators:
    @pytest.mark.parametrize("raw,expected", [
        ("+91 98123 45610", "+919812345610"), ("9812345610", "+919812345610"),
        ("09812345610", "+919812345610"), ("919812345610", "+919812345610"),
        ("+91-98123-45610", "+919812345610"), ("(0175) 221-2345", "+911752212345"),
        ("+1 415 555 0100", None), ("112", None), ("98123", None), ("0012345678", None)])
    def test_phone_normalisation(self, raw, expected):
        assert accounts.normalize_phone(raw) == expected

    def test_mobile_needs_6_to_9_first(self):
        errors = accounts.FormErrors()
        assert accounts.clean_phone(errors, "p", "0175 2212345", label="Mobile") is None
        assert accounts.clean_phone(errors, "q", "0175 2212345", label="Phone",
                                    mobile=False) == "+911752212345"

    @pytest.mark.parametrize("pincode,ok", [("147001", True), ("110001", True),
                                            ("847226", True), ("047001", False),
                                            ("947001", False), ("14700", False),
                                            ("14700a", False)])
    def test_pincode(self, pincode, ok):
        errors = accounts.FormErrors()
        assert (accounts.clean_pincode(errors, "pincode", pincode) is not None) == ok

    @pytest.mark.parametrize("pin", ["111111", "123456", "654321", "121212", "123123",
                                     "123321", "135791", "098765", "112233", "000000",
                                     "100100", "12345", "1234567", "12a456"])
    def test_weak_pins_are_refused(self, pin):
        assert accounts.pin_problem(pin) is not None

    @pytest.mark.parametrize("pin", ["482916", "739204", "305718"])
    def test_good_pins(self, pin):
        assert accounts.pin_problem(pin) is None

    def test_pin_is_not_the_phone_or_pincode(self):
        assert accounts.pin_problem("345610", phone="+919812345610")
        assert accounts.pin_problem("147091", pincode="147091")

    def test_duress_rules(self):
        def check(sos, duress, contacts=True):
            errors = accounts.FormErrors()
            result = accounts.check_pins(errors, sos, sos, duress, duress, phone=None,
                                         pincode=None, contacts_exist=contacts)
            return result, errors
        assert check("482916", "")[0] == ("482916", None)
        assert check("482916", "482917")[1].get("duress_pin"), "1 digit is not enough"
        assert check("482916", "482971")[0] == ("482916", "482971")
        assert check("482916", "482971", contacts=False)[1].get("duress_pin")
        assert check("482916", "111111")[1].get("duress_pin"), "weak duress PIN"
        errors = accounts.FormErrors()
        accounts.check_pins(errors, "482916", "482917", "", "", phone=None, pincode=None,
                            contacts_exist=True)
        assert errors.get("sos_pin_confirm")

    def test_entry_note_never_holds_codes(self):
        errors = accounts.FormErrors()
        assert accounts.clean_entry_note(errors, "Gate code 4512, then left") is None
        assert accounts.clean_entry_note(accounts.FormErrors(), "Gate 2, guard cabin") == \
            "Gate 2, guard cabin"

    def test_coordinates_inside_india(self):
        errors = accounts.FormErrors()
        assert accounts.clean_coordinates(errors, "30.339801", "76.386912") == (30.3398, 76.38691)
        assert accounts.clean_coordinates(errors, "", "") == (None, None)
        assert accounts.clean_coordinates(accounts.FormErrors(), "51.5", "-0.1") == (None, None)
        assert accounts.clean_coordinates(accounts.FormErrors(), "30.3", "") == (None, None)


# ── the wizard end to end ─────────────────────────────────────────────────────
def _household(app) -> Household:
    return query(app, Household)[0]


class TestWizard:
    def test_steps_open_in_order(self, portal):
        home = add_household(portal)
        api = api_as(portal, home["owner_id"])
        assert api.get("/onboarding").json()["current_step"] == 1
        assert api.get("/onboarding/4").status_code == 409
        assert api.post("/onboarding/4").status_code == 409
        assert api.get("/onboarding/9").status_code == 404

    def test_members_cannot_run_the_wizard(self, portal):
        home = add_household(portal)
        member = api_as(portal, add_user(portal, username="meera", role="member",
                                         household_id=home["household_id"]))
        assert member.get("/session").json()["landing"] == "/home"
        assert member.get("/onboarding/1").status_code == 403
        assert member.put("/onboarding/1", STEP1).status_code == 403

    def test_full_wizard(self, portal, outbox, fake_hub):
        home = add_household(portal, email="old@example.com")
        add_device(portal, serial="SX5-000123", household_id=home["household_id"],
                   state="ASSIGNED", claim_code="KTRW-BQXL")
        api = api_as(portal, home["owner_id"])

        # 1. About you
        bad = api.put("/onboarding/1", {**STEP1, "phone": "12345", "adult": False})
        fields = bad.json()["error"]["fields"]
        assert bad.status_code == 422 and "Indian mobile" in fields["phone"] and "adult" in fields
        assert api.put("/onboarding/1", STEP1).json()["next"] == "/onboarding/2"
        owner = query(portal, User, username="riya")[0]
        assert security.unseal(owner.phone_enc) == "+919812345610"
        assert owner.email == "riya@example.com" and owner.email_verified_at is None
        assert outbox.links("riya@example.com", "/account/verify-email/")

        # 2. Home
        assert api.put("/onboarding/2", {**STEP2, "pincode": "047001"}).status_code == 422
        assert api.put("/onboarding/2", {**STEP2, "state": "Atlantis"}).status_code == 422
        assert api.put("/onboarding/2", STEP2).json()["next"] == "/onboarding/3"
        household = _household(portal)
        assert household.pincode == "147001" and household.lat == 30.3398
        assert household.nearest["police"] == {"name": "Civil Lines PS",
                                               "phone": "+911752212345"}

        # 3. Household
        assert api.put("/onboarding/3", {**STEP3, "adults": 0}).status_code == 422
        assert api.put("/onboarding/3", {**STEP3, "medical_consent": False}).status_code == 422
        minor = api.post("/household/members", {"member_name": "Kiaan", "age_band": "minor",
                                                "invite_email": "kiaan@example.com"})
        assert minor.status_code == 422 and "Minors do not get" in minor.text
        added = api.post("/household/members", {"member_name": "Meera",
                                                "member_relation": "mother",
                                                "age_band": "senior",
                                                "invite_email": "meera@example.com"})
        member_user = query(portal, User, role="member")[0]
        assert member_user.status == "pending_activation" and member_user.password_hash is None
        member_code = outbox.codes("meera@example.com")[0]
        assert member_code not in added.text, "the owner never sees the member's code"
        assert api.put("/onboarding/3", STEP3).json()["next"] == "/onboarding/4"
        household = _household(portal)
        assert household.occupants == {"adults": 2, "children": 1, "seniors": 1}
        assert accounts.medical_notes(household) == "Senior uses a wheelchair"
        assert household.medical_enc != "Senior uses a wheelchair"

        # 4. Contacts
        assert api.post("/onboarding/4").status_code == 422
        own = api.post("/household/contacts", {**CONTACT, "phone": "+919812345610"})
        assert own.status_code == 422 and "your own number" in own.text
        shared = api.post("/household/contacts", CONTACT)
        assert shared.status_code == 201 and "/consent/" in shared.json()["share_link"]
        assert outbox.links("arjun@example.com", "/consent/")
        assert api.post("/household/contacts", {**CONTACT, "phone": "98765 43211",
                                                "email": ""}).status_code == 422, \
            "priority taken"
        api.post("/household/contacts", {**CONTACT, "name": "Neighbour",
                                         "phone": "98765 43299", "email": "", "priority": 2})
        contacts = api.get("/onboarding/4").json()["contacts"]["contacts"]
        assert {c["name"]: c["unreachable_offline"] for c in contacts}["Neighbour"] is True
        assert api.post("/onboarding/4").json()["next"] == "/onboarding/5"

        # 5. Device
        assert api.get("/onboarding/5", params={"sn": "sx5-000123"}).json()["serial"] == \
            "SX5-000123"
        wrong = api.post("/onboarding/5/claim", {"serial": "SX5-000123",
                                                 "claim_code": "BBBB-BBBB"})
        assert wrong.status_code == 422
        claimed = api.post("/onboarding/5/claim", {"serial": "sx5 000123",
                                                   "claim_code": "ktrw bqxl"})
        assert claimed.status_code == 200 and "is now yours" in claimed.json()["message"]
        device = query(portal, Device, serial="SX5-000123")[0]
        assert device.state == "CLAIMED" and device.claim_code_hash is None
        assert fake_hub.tokens == [device.id]
        # The step-5 view check needs the device's remote_view consent.
        assert [c.method for c in query(portal, Consent, purpose="remote_view")
                if c.withdrawn_at is None] == ["wizard_view_check"]
        no_check = api.put(f"/onboarding/5/devices/{device.id}",
                           {"name": "Living room unit", "room_label": "Living room",
                            "mode": "armed"})
        assert no_check.status_code == 422
        done5 = api.put(f"/onboarding/5/devices/{device.id}",
                        {"name": "Living room unit", "room_label": "Living room",
                         "floor": "Ground", "mode": "armed", "view_checked": True})
        assert done5.json()["next"] == "/onboarding/6"
        assert fake_hub.commands[-1][1:3] == ("mode.set", {"armed": True})

        # 6. Alerts and SOS
        assert api.put("/onboarding/6", {**STEP6, "sos_pin": "123456",
                                         "sos_pin_confirm": "123456"}).status_code == 422
        assert api.put("/onboarding/6", {**STEP6, "duress_pin": "482917",
                                         "duress_pin_confirm": "482917"}).status_code == 422
        assert api.put("/onboarding/6", {**STEP6, "safety_defaults": False}).status_code == 422
        assert api.put("/onboarding/6", {**STEP6, "window": 600}).status_code == 422
        assert api.put("/onboarding/6", STEP6).json()["next"] == "/onboarding/7"
        household = _household(portal)
        assert household.sos_window_s == 2700 and household.safety_defaults
        assert security.verify_pin(household.sos_pin_hash, "482916")
        assert security.verify_pin(household.duress_pin_hash, "482971")
        assert household.channels == {"email": True, "telegram": False, "whatsapp": False,
                                      "sms": True}

        # 7. Consent and review
        review = api.get("/onboarding/7").json()
        full = review["preview"]["full"]
        assert "SENTRIX does not contact 112 itself." in full
        assert "Mall Road" in full and "opposite the SBI ATM" in full
        assert "Arjun" not in full, "an unconfirmed contact is not alerted"
        assert "Arjun Sharma" in review["pending_contacts"]
        assert api.put("/onboarding/7", {}).status_code == 422
        finish = api.put("/onboarding/7", {"consent_account_sos": True,
                                           "consent_medical_notes": True,
                                           "consent_sos_snapshot": True,
                                           "consent_remote_view": True})
        assert finish.json()["next"] == "/home?welcome=1"
        household = _household(portal)
        assert household.onboarding_completed_at is not None
        with portal.state.db.session() as db:
            assert accounts.consent_states(db, household.id) == {
                "account_sos": True, "face_recognition": False, "medical_notes": True,
                "sos_snapshot": True, "remote_view": True, "desk_live_view": False,
                "model_improvement": False}
        profile = fake_hub.profiles[-1]
        assert profile["version"] == household.profile_version
        assert profile["household"]["medical_notes"] == "Senior uses a wheelchair"
        assert profile["sos"]["window_seconds"] == 2700
        assert query(portal, AuditLog, event="onboarding.completed")
        assert api.get("/home").status_code == 200

    def test_withdrawing_medical_consent_in_review_erases_the_notes(self, portal):
        home = add_household(portal, complete=True)
        with portal.state.db.session() as db:
            household = db.get(Household, home["household_id"])
            household.medical_enc = security.seal("asthma")
            db.add(Consent(household_id=household.id, subject_type="user",
                           subject_id=home["owner_id"], purpose="medical_notes",
                           notice_version="2026-10-1", granted_at=accounts.utcnow()))
        api_as(portal, home["owner_id"]).put("/onboarding/7", {"consent_account_sos": True})
        assert _household(portal).medical_enc is None

    def test_step7_defaults_survive_the_claims_remote_view_consent(self, portal):
        """Claiming in step 5 grants remote_view; step 7 must still offer
        the defaults (account and SOS, SOS snapshot) for undecided purposes,
        and keep what the owner did decide."""
        home = add_household(portal, complete=True)
        now = accounts.utcnow()
        with portal.state.db.session() as db:
            hid, uid = home["household_id"], home["owner_id"]
            db.query(Consent).filter_by(household_id=hid).delete()
            db.add(Consent(household_id=hid, subject_type="user", subject_id=uid,
                           purpose="remote_view", notice_version="2026-10-1", granted_at=now))
            db.add(Consent(household_id=hid, subject_type="user", subject_id=uid,
                           purpose="sos_snapshot", notice_version="2026-10-1",
                           granted_at=now, withdrawn_at=now))
        with portal.state.db.session() as db:
            shown = accounts.consent_choices(db, hid)
        assert shown["account_sos"] is True, "undecided: its default"
        assert shown["remote_view"] is True, "granted at the claim"
        assert shown["sos_snapshot"] is False, "withdrawn by the owner: stays off"
        assert shown["face_recognition"] is False and shown["desk_live_view"] is False

    def test_changing_the_email_later_needs_step_up(self, portal):
        """Step 1 stays reachable after onboarding: the recovery e-mail may
        not change there without a fresh password, as on /account."""
        home = add_household(portal, complete=True)
        api = api_as(portal, home["owner_id"])
        expire_step_up(portal)
        refused = api.put("/onboarding/1", {**STEP1, "email": "thief@example.com"})
        assert refused.status_code == 422 and "Confirm your password" in refused.text
        with portal.state.db.session() as db:
            assert "thief@example.com" not in {u.email for u in db.query(User)}

    def test_changing_pins_later_needs_step_up(self, portal):
        home = add_household(portal, complete=True)
        api = api_as(portal, home["owner_id"])
        expire_step_up(portal)
        refused = api.put("/onboarding/6", {**STEP6, "duress_pin": "",
                                            "duress_pin_confirm": ""})
        assert refused.status_code == 403 and "Confirm your password" in refused.text
        keep = api.put("/onboarding/6", {"window": 1800, "safety_defaults": True})
        assert keep.status_code == 200, "no PIN fields: the PINs are kept"
        assert security.verify_pin(_household(portal).sos_pin_hash, "482916")


# ── claim ─────────────────────────────────────────────────────────────────────
class TestClaim:
    def test_claim_and_qr_link(self, portal, fake_hub):
        home = add_household(portal, complete=True)
        add_device(portal, serial="SX5-000222", household_id=home["household_id"],
                   state="ASSIGNED", claim_code="KTRW-BQXL")
        api = api_as(portal, home["owner_id"])
        done = api.post("/claim", {"serial": "SX5-000222", "claim_code": "KTRW-BQXL"})
        assert done.status_code == 200
        assert query(portal, Device, serial="SX5-000222")[0].state == "CLAIMED"
        again = api.post("/claim", {"serial": "SX5-000222", "claim_code": "KTRW-BQXL"})
        assert again.status_code == 422 and "already claimed" in again.text

    def test_a_qr_scanned_before_step_5_waits_for_it(self, portal):
        home = add_household(portal)
        add_device(portal, serial="SX5-000777", household_id=home["household_id"],
                   state="ASSIGNED", claim_code="KTRW-BQXL")
        api = api_as(portal, home["owner_id"])
        assert api.get("/onboarding/5", params={"sn": "SX5-000777"}).status_code == 409
        with portal.state.db.session() as db:
            db.query(Household).one().onboarding_step = 5
        step = api.get("/onboarding/5", params={"sn": "sx5 000777"}).json()
        assert step["serial"] == "SX5-000777"
        assert step["assigned"][0]["serial"] == "SX5-000777"

    def test_signed_out_links_come_back_after_sign_in(self, portal):
        from fastapi.testclient import TestClient
        add_household(portal, complete=True)
        client = TestClient(portal, follow_redirects=False)
        assert client.get("/api/v2/onboarding/5").status_code == 401, "no redirect"
        api = Api(portal)
        api.session()
        signed_in = api.post("/auth/login", {"username": "riya", "password": PASSWORD,
                                             "next": "/claim?sn=SX5-000222"})
        assert signed_in.json()["next"] == "/claim?sn=SX5-000222"

    def test_only_a_device_assigned_to_your_household(self, portal):
        mine = add_household(portal)
        theirs = add_household(portal, code="SX-0002", owner="other", email="o@example.com",
                               phone="+919811111111")
        add_device(portal, serial="SX5-000300", household_id=theirs["household_id"],
                   state="ASSIGNED", claim_code="KTRW-BQXL")
        add_device(portal, serial="SX5-000301", claim_code="KTRW-BQXL")   # unassigned
        user = query(portal, User, id=mine["owner_id"])[0]
        with portal.state.db.session() as db:
            for serial in ("SX5-000300", "SX5-000301", "SX5-999999"):
                outcome = accounts.claim_device(db, user=user, serial=serial,
                                                code="KTRW-BQXL", ip="1.2.3.4")
                assert not outcome.ok and outcome.error == accounts.CLAIM_REFUSED
        assert query(portal, Device, serial="SX5-000300")[0].state == "ASSIGNED"

    def test_five_failures_lock_the_account_and_the_serial_for_15_minutes(self, portal):
        mine = add_household(portal)
        other = add_household(portal, code="SX-0002", owner="other", email="o@example.com",
                              phone="+919811111111")
        add_device(portal, serial="SX5-000400", household_id=mine["household_id"],
                   state="ASSIGNED", claim_code="KTRW-BQXL")
        user = query(portal, User, id=mine["owner_id"])[0]
        stranger = query(portal, User, id=other["owner_id"])[0]
        with portal.state.db.session() as db:
            for _ in range(5):            # someone else hammers the serial
                assert not accounts.claim_device(db, user=stranger, serial="SX5-000400",
                                                 code="BBBB-BBBB", ip="9.9.9.9").ok
            locked = accounts.claim_device(db, user=user, serial="SX5-000400",
                                           code="KTRW-BQXL", ip="1.2.3.4")
            assert not locked.ok and locked.retry_after > 0, "per serial"
        with portal.state.db.session() as db:
            for row in db.query(ClaimAttempt):
                row.at = row.at - timedelta(minutes=16)
        with portal.state.db.session() as db:
            assert accounts.claim_device(db, user=user, serial="SX5-000400",
                                         code="KTRW-BQXL", ip="1.2.3.4").ok
        third = add_household(portal, code="SX-0003", owner="third", email="t@example.com",
                              phone="+919822222222")
        add_device(portal, serial="SX5-000401", household_id=third["household_id"],
                   state="ASSIGNED", claim_code="KTRW-BQXL")
        attacker = query(portal, User, id=third["owner_id"])[0]
        with portal.state.db.session() as db:
            for n in range(5):            # per account, across serials
                accounts.claim_device(db, user=attacker, serial=f"SX5-0009{n:02d}",
                                      code="BBBB-BBBB", ip="9.9.9.9")
            assert accounts.claim_device(db, user=attacker, serial="SX5-000401",
                                         code="KTRW-BQXL", ip="9.9.9.9").retry_after > 0

    def test_claim_failures_are_audited(self, portal):
        home = add_household(portal)
        api = api_as(portal, home["owner_id"])
        with portal.state.db.session() as db:
            db.query(Household).one().onboarding_step = 5
        api.post("/onboarding/5/claim", {"serial": "SX5-000777", "claim_code": "BBBB-BBBB"})
        assert query(portal, AuditLog, event="device.claim_failed")
        assert query(portal, ClaimAttempt)[0].success is False


def test_contacts_count_towards_three(portal):
    home = add_household(portal, complete=True)
    api = api_as(portal, home["owner_id"])
    for n, phone in enumerate(("98765 43201", "98765 43202"), start=2):
        assert api.post("/household/contacts", {**CONTACT, "name": f"C{n}", "phone": phone,
                                                "email": "", "priority": n}).status_code == 201
    assert api.get("/household/contacts").json()["can_add"] is False
    assert api.post("/household/contacts", {**CONTACT, "phone": "98765 43203",
                                            "priority": 1}).status_code == 422
    assert len(query(portal, EmergencyContact, household_id=home["household_id"])) == 3
