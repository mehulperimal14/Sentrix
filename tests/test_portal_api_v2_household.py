"""Portal API v2: onboarding (steps 1-7, claim), home, household, members,
contacts, the household audit, settings (SOS, PINs, Telegram, devices) and
privacy (consents, notice, export, erasure). The rules are the HTML pages'
(``services.household_flows``): same messages, audit events, step-up."""

from __future__ import annotations

import json

from sentrix_portal.models import (AuditLog, ClaimAttempt, Device, EmergencyContact,
                                   Household, HouseholdMember, TelegramLink, User)
from test_acct_support import (PASSWORD, add_device, add_household, add_user,  # noqa: F401
                               fake_hub, outbox, portal, query)
from test_portal_api_v2_support import (api_as, assert_no_secrets, error_of,
                                        expire_step_up, staff_api)

ADDRESS = {"house": "12", "street": "Mall Road", "locality": "Sector 4",
           "landmark": "near SBI ATM", "city": "Patiala", "state": "Punjab",
           "pincode": "147001", "lat": 30.3398, "lon": 76.3869}


def _owner(portal, **kwargs):
    home = add_household(portal, **kwargs)
    return home, api_as(portal, home["owner_id"])


# ── onboarding ────────────────────────────────────────────────────────────────
def test_the_wizard_from_step_1_to_7(portal, fake_hub, outbox):
    home, api = _owner(portal)
    serial_id = add_device(portal, serial="SX5-000123", household_id=home["household_id"],
                           state="ASSIGNED", claim_code="KTRW-BQXL")
    wizard = api.get("/onboarding").json()
    assert wizard["current_step"] == 1 and not wizard["completed"]
    assert [s["reachable"] for s in wizard["steps"]] == [True] + [False] * 6
    locked = api.get("/onboarding/3")
    assert locked.status_code == 409
    assert error_of(locked) == {"code": "conflict",
                                "message": "Finish step 1 (About you) first."}

    step1 = api.get("/onboarding/1").json()
    assert step1["values"]["display_name"] == "Riya Sharma (demo)"
    bad = api.put("/onboarding/1", {"display_name": "Riya", "phone": "12345",
                                    "email": "riya@example.com"})
    assert bad.status_code == 422
    assert error_of(bad)["fields"] == {
        "phone": "Mobile: enter an Indian mobile number as +91 and 10 digits.",
        "adult": "Only an adult (18 or older) can set up the household."}
    saved = api.put("/onboarding/1", {"display_name": "Riya Sharma", "phone": "98123 45610",
                                      "email": "riya@example.com", "adult": True})
    assert saved.status_code == 200, saved.text
    assert saved.json()["next"] == "/onboarding/2"
    assert saved.json()["wizard"]["current_step"] == 2

    bad = api.put("/onboarding/2", {**ADDRESS, "pincode": "947001",
                                    "entry_note": "gate code 4455"})
    assert error_of(bad)["fields"] == {
        "pincode": "PIN code: 6 digits, the first from 1 to 8.",
        "entry_note": "Never put door or gate codes in the entry note: it is sent to your "
                      "contacts and the Desk in an SOS."}
    assert api.put("/onboarding/2", ADDRESS).json()["next"] == "/onboarding/3"
    assert api.get("/onboarding/2").json()["values"]["lat"] == 30.3398

    notes = api.put("/onboarding/3", {"adults": 2, "medical": "senior uses a wheelchair"})
    assert error_of(notes)["fields"] == {
        "medical_consent": "Tick the separate consent to store medical notes, or leave the "
                           "notes empty."}
    assert api.put("/onboarding/3", {"adults": 2, "children": 1}).status_code == 200

    empty = api.post("/onboarding/4")
    assert error_of(empty)["fields"] == {"contacts": "Add at least one emergency contact."}
    added = api.post("/household/contacts", {"name": "Arjun", "relation": "brother",
                                             "phone": "+919876543210",
                                             "email": "arjun@example.com", "priority": 1})
    assert added.status_code == 201, added.text
    assert added.json()["share_link"].startswith("http") and added.json()["emailed"]
    assert api.get("/onboarding/4").json()["contacts"]["contacts"][0]["status"] == "PENDING"
    assert api.post("/onboarding/4").json()["next"] == "/onboarding/5"

    step5 = api.get("/onboarding/5", params={"sn": "sx5 000123"}).json()
    assert step5["serial"] == "SX5-000123"
    assert step5["assigned"] == [{"id": serial_id, "serial": "SX5-000123"}]
    wrong = api.post("/onboarding/5/claim", {"serial": "SX5-000123", "claim_code": "BBBB-BBBB"})
    assert wrong.status_code == 422
    assert "claim code is not right" in error_of(wrong)["fields"]["claim_code"]
    assert len(query(portal, ClaimAttempt)) == 1, "the failed claim counts"
    claimed = api.post("/onboarding/5/claim", {"serial": "SX5-000123",
                                               "claim_code": "ktrw bqxl"})
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["message"] == "Device SX5-000123 is now yours. Name it below."
    assert fake_hub.tokens == [serial_id]
    unchecked = api.put(f"/onboarding/5/devices/{serial_id}",
                        {"name": "Hall unit", "room_label": "Living room"})
    assert "view_checked" in error_of(unchecked)["fields"]
    saved = api.put(f"/onboarding/5/devices/{serial_id}",
                    {"name": "Hall unit", "room_label": "Living room", "mode": "armed",
                     "view_checked": True})
    assert saved.status_code == 200 and saved.json()["next"] == "/onboarding/6"
    assert ("mode.set" in [c[1] for c in fake_hub.commands])

    step6 = api.get("/onboarding/6").json()
    assert step6["pins"]["pin_set"] is False and step6["window_choices"] == [1800, 2700, 3600]
    weak = api.put("/onboarding/6", {"window": 1800, "safety_defaults": True,
                                     "sos_pin": "123456", "sos_pin_confirm": "123456"})
    assert "too easy to guess" in error_of(weak)["fields"]["sos_pin"]
    duress = api.put("/onboarding/6", {"window": 1800, "safety_defaults": True,
                                       "channels": {"email": True},
                                       "sos_pin": "482916", "sos_pin_confirm": "482916",
                                       "duress_pin": "482917", "duress_pin_confirm": "482917"})
    assert error_of(duress)["fields"] == {
        "duress_pin": "The duress PIN must differ from the SOS PIN in at least 2 digits."}
    ok = api.put("/onboarding/6", {"window": 2700, "safety_defaults": True,
                                   "channels": {"email": True, "telegram": True},
                                   "sos_pin": "482916", "sos_pin_confirm": "482916",
                                   "duress_pin": "482961", "duress_pin_confirm": "482961"})
    assert ok.status_code == 200, ok.text
    with portal.state.db.session() as db:
        stored = db.get(Household, home["household_id"])
        assert stored.sos_window_s == 2700 and stored.sos_pin_hash and stored.duress_pin_hash
        assert stored.channels == {"email": True, "telegram": True, "whatsapp": False,
                                   "sms": False}

    review = api.get("/onboarding/7")
    data = review.json()
    assert data["consents"]["account_sos"] and data["consents"]["sos_snapshot"]
    assert "SENTRIX SOS" in data["preview"]["full"] and data["pending_contacts"] == ["Arjun"]
    assert_no_secrets(portal, review, api.get("/onboarding/6"))
    refused = api.put("/onboarding/7", {"consent_sos_snapshot": True})
    assert error_of(refused)["fields"] == {
        "consent_account_sos": "SENTRIX cannot protect your home without the account and "
                               "SOS purpose."}
    finished = api.put("/onboarding/7", {"consent_account_sos": True,
                                         "consent_sos_snapshot": True,
                                         "consent_remote_view": True})
    assert finished.status_code == 200, finished.text
    assert finished.json()["first"] is True and finished.json()["next"] == "/home?welcome=1"
    assert any(r.event == "onboarding.completed" for r in query(portal, AuditLog))
    assert api.get("/session").json()["household"]["setup_done"] is True
    # After the setup, changing the safety settings in step 6 needs a fresh password.
    expire_step_up(portal)
    later = api.put("/onboarding/6", {"window": 3600, "safety_defaults": True})
    assert later.status_code == 403 and error_of(later)["code"] == "step_up_required"


def test_onboarding_is_for_owners(portal):
    home = add_household(portal)
    member = add_user(portal, username="dev", role="member",
                      household_id=home["household_id"])
    api = api_as(portal, member)
    for call in (lambda: api.get("/onboarding"), lambda: api.put("/onboarding/1", {}),
                 lambda: api.post("/claim", {})):
        response = call()
        assert response.status_code == 403 and error_of(response)["code"] == "forbidden"
    staff = staff_api(portal, role="operator", username="desk7")
    assert staff.get("/onboarding/1").status_code == 403


def test_preview_and_standalone_claim(portal, fake_hub):
    home, api = _owner(portal, complete=True)
    preview = api.get("/onboarding/preview").json()
    assert "Living room" in preview["full"] and preview["sms"]
    add_device(portal, serial="SX5-000555", household_id=home["household_id"],
               state="ASSIGNED", claim_code="MNPQ-RSTV")
    claimed = api.post("/claim", {"serial": "SX5-000555", "claim_code": "MNPQ-RSTV"})
    assert claimed.status_code == 200 and claimed.json()["device"]["serial"] == "SX5-000555"
    again = api.post("/claim", {"serial": "SX5-000555", "claim_code": "MNPQ-RSTV"})
    assert error_of(again)["fields"]["claim_code"] == ("This device is already claimed by "
                                                      "your household.")


# ── home ──────────────────────────────────────────────────────────────────────
def test_home_cards(portal, fake_hub):
    home, api = _owner(portal, complete=True)
    fake_hub.online.add(home["device_id"])
    response = api.get("/home")
    data = response.json()
    assert data["setup_done"] is True and data["requests"] == []
    (card,) = data["devices"]
    assert card["serial"] == home["serial"] and card["online"] is True
    assert card["room"] == "Living room" and card["claimed"] is True
    assert_no_secrets(portal, response)
    staff = staff_api(portal)
    assert staff.get("/home").status_code == 403


# ── household, members, contacts, audit ───────────────────────────────────────
def test_household_profile_owner_edits_member_reads(portal):
    home, api = _owner(portal, complete=True)
    view = api.get("/household").json()
    assert view["editable"] and view["values"]["landmark"] == "near SBI ATM"
    assert view["values"]["adults"] == 2 and "Punjab" in view["states"]
    bad = api.put("/household", {**view["values"], "adults": 0})
    assert error_of(bad)["fields"] == {"adults": "Adults must be a whole number from 1 to 20."}
    saved = api.put("/household", {**view["values"], "pets": "one dog"})
    assert saved.status_code == 200 and saved.json()["values"]["pets"] == "one dog"
    member = api_as(portal, add_user(portal, username="dev", role="member",
                                     household_id=home["household_id"]))
    assert member.get("/household").json()["editable"] is False
    refused = member.put("/household", view["values"])
    assert refused.status_code == 403 and error_of(refused)["code"] == "forbidden"


def test_members(portal, outbox):
    home, api = _owner(portal, complete=True)
    minor = api.post("/household/members", {"member_name": "Kid", "age_band": "minor",
                                             "invite_email": "kid@example.com"})
    assert error_of(minor)["fields"] == {"invite_email": "Minors do not get a Portal account."}
    added = api.post("/household/members", {"member_name": "Dev Sharma",
                                            "member_relation": "brother", "age_band": "adult",
                                            "invite_email": "dev@example.com"})
    assert added.status_code == 201, added.text
    body = added.json()
    assert body["invited_username"] == "sx-0001-dev-sharma"
    assert body["message"] == ("Added. An invitation was e-mailed; their username is "
                               "sx-0001-dev-sharma.")
    member_id = body["member"]["id"]
    assert outbox.codes("dev@example.com"), "the code goes by e-mail, never to the owner"
    assert not any(code in added.text for code in outbox.codes("dev@example.com"))
    to_minor = api.patch(f"/household/members/{member_id}", {"age_band": "minor"})
    assert error_of(to_minor)["fields"] == {
        "age_band": "A member with a Portal account cannot be a minor."}
    saved = api.patch(f"/household/members/{member_id}", {"member_relation": "cousin",
                                                          "age_band": "senior"})
    assert saved.json()["member"]["relation"] == "cousin"
    listing = api.get("/household/members").json()
    assert [m["name"] for m in listing["members"]] == ["Dev Sharma"]
    assert api.delete(f"/household/members/{member_id}").json()["message"] == "Removed."
    assert query(portal, User, username="sx-0001-dev-sharma")[0].status == "closed"
    other = add_household(portal, code="SX-0002", owner="zeb", email="zeb@example.com",
                          phone="+919811122233")
    with portal.state.db.session() as db:
        foreign = HouseholdMember(household_id=other["household_id"], name="Yara",
                                  age_band="adult")
        db.add(foreign)
        db.flush()
        foreign_id = foreign.id
    assert api.delete(f"/household/members/{foreign_id}").status_code == 404


def test_contacts_add_edit_resend_remove(portal, outbox):
    home, api = _owner(portal, complete=True)
    own = api.post("/household/contacts", {"name": "Me", "relation": "self",
                                           "phone": "+919812345610", "priority": 2})
    assert error_of(own)["fields"] == {"phone": "A contact must not be your own number."}
    taken = api.post("/household/contacts", {"name": "Bee", "relation": "friend",
                                             "phone": "+919800000001", "priority": 1})
    assert error_of(taken)["fields"] == {"priority": "Another contact already has priority 1."}
    added = api.post("/household/contacts", {"name": "Bee", "relation": "friend",
                                             "phone": "+919800000001", "priority": 2,
                                             "can_reach_30min": True})
    assert added.status_code == 201
    link = added.json()["share_link"]
    contact = added.json()["contact"]
    assert contact["status"] == "PENDING" and contact["unreachable_offline"] is True
    assert contact["phone_masked"] and "00000001" not in contact["phone_masked"]
    listing = api.get("/household/contacts")
    assert link not in listing.text, "the share link is shown once"
    assert_no_secrets(portal, listing)
    resent = api.post(f"/household/contacts/{contact['id']}/resend")
    assert resent.status_code == 200 and resent.json()["share_link"] != link
    confirmed_id = home["contact_id"]
    not_pending = api.post(f"/household/contacts/{confirmed_id}/resend")
    assert not_pending.status_code == 409
    assert error_of(not_pending)["message"] == ("Only a contact awaiting confirmation gets a "
                                                "new link.")
    edited = api.patch(f"/household/contacts/{confirmed_id}",
                       {"name": "Arjun", "relation": "brother", "phone": "+919876543210",
                        "email": "arjun2@example.com", "priority": 1})
    assert edited.json()["contact"]["status"] == "PENDING", "a new e-mail: confirm again"
    assert edited.json()["share_link"]
    removed = api.delete(f"/household/contacts/{contact['id']}")
    assert removed.json()["message"] == "Contact removed."
    assert not query(portal, EmergencyContact, id=contact["id"])
    member = api_as(portal, add_user(portal, username="dev", role="member",
                                     household_id=home["household_id"]))
    assert member.get("/household/contacts").json()["editable"] is False
    assert member.post("/household/contacts", {"name": "x"}).status_code == 403


def test_household_audit_with_a_cursor(portal):
    home, api = _owner(portal, complete=True)
    for n in range(5):
        api.put("/household", {**api.get("/household").json()["values"], "pets": f"cat {n}"})
    first = api.get("/household/audit", params={"limit": 3}).json()
    assert len(first["rows"]) == 3 and first["next_cursor"]
    assert all(r["at"].endswith("Z") for r in first["rows"])
    second = api.get("/household/audit", params={"limit": 3,
                                                  "cursor": first["next_cursor"]}).json()
    assert second["rows"][0]["id"] < first["rows"][-1]["id"]
    assert not ({r["id"] for r in first["rows"]} & {r["id"] for r in second["rows"]})


# ── settings ──────────────────────────────────────────────────────────────────
def test_settings_sos_needs_a_fresh_password_for_safety(portal):
    home, api = _owner(portal, complete=True)
    view = api.get("/settings").json()
    assert view["editable"] and view["pins"]["pin_set"] and view["telegram"] is not None
    expire_step_up(portal)
    off = api.put("/settings/sos", {"window": 1800, "safety_defaults": False})
    assert error_of(off)["fields"]["safety_off_ack"].startswith("Safety defaults alert")
    change = api.put("/settings/sos", {"window": 3600, "safety_defaults": True,
                                       "channels": {"email": True}})
    assert change.status_code == 403
    assert error_of(change)["code"] == "step_up_required"
    assert "Confirm your password above first" in error_of(change)["fields"]["window"]
    channels = api.put("/settings/sos", {"window": 1800, "safety_defaults": True,
                                         "channels": {"email": True, "sms": True}})
    assert channels.status_code == 200, "channels alone need no step-up"
    api.step_up()
    done = api.put("/settings/sos", {"window": 3600, "safety_defaults": False,
                                     "safety_off_ack": True, "channels": {"email": True}})
    assert done.status_code == 200 and done.json()["sos"]["window"] == 3600
    assert done.json()["sos"]["safety_defaults"] is False


def test_settings_pins(portal):
    home, api = _owner(portal, complete=True)
    expire_step_up(portal)
    refused = api.put("/settings/pins", {"sos_pin": "604917", "sos_pin_confirm": "604917"})
    assert refused.status_code == 403 and error_of(refused)["code"] == "step_up_required"
    api.step_up()
    mismatch = api.put("/settings/pins", {"sos_pin": "604917", "sos_pin_confirm": "604918"})
    assert error_of(mismatch)["fields"] == {"sos_pin_confirm": "The two SOS PINs do not match."}
    ok = api.put("/settings/pins", {"sos_pin": "604917", "sos_pin_confirm": "604917"})
    assert ok.status_code == 200 and ok.json()["pins"]["pin_set"]
    assert any(r.event == "profile.pins_changed" for r in query(portal, AuditLog))
    assert_no_secrets(portal, ok)


def test_member_sees_settings_without_pins_or_telegram(portal):
    home = add_household(portal, complete=True)
    member = api_as(portal, add_user(portal, username="dev", role="member",
                                     household_id=home["household_id"]))
    view = member.get("/settings").json()
    assert view["editable"] is False and view["pins"] is None and view["telegram"] is None
    assert view["devices"][0]["room_label"] == "Living room"
    assert member.put("/settings/sos", {"window": 1800}).status_code == 403
    assert member.post("/telegram/link-code").status_code == 403


def test_telegram_link_and_unlink(portal):
    from sentrix_portal.settings import current
    home, api = _owner(portal, complete=True)
    code = api.post("/telegram/link-code")
    assert code.status_code == 201 and len(code.json()["code"]) >= 16
    assert code.json()["link"] is None if not current().telegram_bot_name else True
    with portal.state.db.session() as db:
        link = TelegramLink(subject_type="user", subject_id=home["owner_id"],
                            household_id=home["household_id"], chat_id="555")
        db.add(link)
        db.flush()
        link_id = link.id
    assert api.get("/settings").json()["telegram"]["links"][0]["id"] == link_id
    gone = api.delete(f"/telegram/links/{link_id}")
    assert gone.json()["message"] == "Telegram unlinked."
    assert api.get("/settings").json()["telegram"]["links"] == []
    member = add_user(portal, username="dev", role="member",
                      household_id=home["household_id"])
    with portal.state.db.session() as db:
        theirs = TelegramLink(subject_type="user", subject_id=member,
                              household_id=home["household_id"], chat_id="556")
        db.add(theirs)
        db.flush()
        theirs_id = theirs.id
    assert api.delete(f"/telegram/links/{theirs_id}").status_code == 404, "only your own"


def test_devices_rename_and_unpair(portal, fake_hub):
    home, api = _owner(portal, complete=True)
    device_id = home["device_id"]
    blank = api.patch(f"/devices/{device_id}", {"name": "", "room_label": "Hall"})
    assert error_of(blank)["fields"] == {"name": "Device name is required."}
    renamed = api.patch(f"/devices/{device_id}", {"name": "Hall unit", "room_label": "Hall",
                                                  "floor": "1"})
    assert renamed.json()["name"] == "Hall unit" and renamed.json()["room_label"] == "Hall"
    expire_step_up(portal)
    refused = api.post(f"/devices/{device_id}/unpair")
    assert refused.status_code == 403 and error_of(refused)["code"] == "step_up_required"
    api.step_up()
    done = api.post(f"/devices/{device_id}/unpair")
    assert done.status_code == 200 and done.json()["message"] == "Device unpaired."
    assert query(portal, Device, id=device_id)[0].state == "ASSIGNED"
    assert fake_hub.unpaired and fake_hub.unpaired[0][0] == device_id
    assert api.patch(f"/devices/{device_id}", {"name": "x"}).status_code == 404


# ── privacy ───────────────────────────────────────────────────────────────────
def test_privacy_consents_and_notice(portal):
    home, api = _owner(portal, complete=True)
    view = api.get("/privacy").json()
    assert view["consents"]["account_sos"] and view["consents"]["sos_snapshot"]
    assert view["notice"]["notice_version"] and view["notice"]["retention"]["audit_days"] == 365
    refused = api.put("/privacy/consents", {"consent_sos_snapshot": True})
    assert error_of(refused)["fields"]["consent_account_sos"].startswith(
        "The account and SOS purpose is the service itself")
    saved = api.put("/privacy/consents", {"consent_account_sos": True,
                                          "consent_desk_live_view": True})
    assert saved.json()["consents"]["desk_live_view"] is True
    assert saved.json()["consents"]["sos_snapshot"] is False, "unticked = withdrawn"
    assert saved.json()["history"][0]["granted_at"].endswith("Z")
    staff = staff_api(portal, role="operator", username="desk3")
    assert staff.get("/privacy/notice").status_code == 200
    assert staff.get("/privacy").status_code == 403


def test_export_download(portal):
    home, api = _owner(portal, complete=True)
    expire_step_up(portal)
    refused = api.post("/privacy/export")
    assert refused.status_code == 403
    assert error_of(refused) == {"code": "step_up_required",
                                 "message": "Confirm your password first."}
    api.step_up()
    response = api.post("/privacy/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["content-disposition"] == ('attachment; filename='
                                                       '"sentrix-export-SX-0001.json"')
    assert response.headers["cache-control"] == "no-store"
    data = json.loads(response.content)
    assert data["household"]["customer_code"] == "SX-0001"
    assert_no_secrets(portal, response)
    assert any(r.event == "privacy.export" for r in query(portal, AuditLog))
    member = api_as(portal, add_user(portal, username="dev", role="member",
                                     household_id=home["household_id"]))
    assert member.post("/privacy/export").status_code == 403


def test_erasure_schedule_and_cancel(portal):
    home, api = _owner(portal, complete=True)
    expire_step_up(portal)
    refused = api.post("/privacy/erase", {"confirm": "ERASE"})
    assert refused.status_code == 403 and error_of(refused)["code"] == "step_up_required"
    api.step_up()
    typo = api.post("/privacy/erase", {"confirm": "erase"})
    assert error_of(typo)["fields"] == {"confirm": "Type ERASE in capitals to confirm."}
    done = api.post("/privacy/erase", {"confirm": "ERASE"})
    assert done.status_code == 200 and done.json()["erasure_due"].endswith("Z")
    again = api.post("/privacy/erase", {"confirm": "ERASE"})
    assert again.status_code == 409
    assert error_of(again)["message"] == "Erasure is already scheduled."
    assert api.get("/home").json()["erasure_due"]
    cancelled = api.post("/privacy/erase/cancel")
    assert cancelled.json()["message"] == "Erasure cancelled. Your account stays open."
    assert query(portal, Household, id=home["household_id"])[0].status == "active"


def test_no_household_endpoint_leaks_secrets(portal):
    home, api = _owner(portal, complete=True)
    responses = [api.get(path) for path in (
        "/session", "/home", "/household", "/household/members", "/household/contacts",
        "/household/audit", "/settings", "/privacy", "/privacy/notice", "/account",
        "/account/sessions", "/onboarding", "/onboarding/1", "/onboarding/2",
        "/onboarding/3", "/onboarding/4", "/onboarding/5", "/onboarding/6",
        "/onboarding/7")]
    assert all(r.status_code == 200 for r in responses), [
        (r.request.url.path, r.status_code) for r in responses if r.status_code != 200]
    assert_no_secrets(portal, *responses)
    assert PASSWORD not in "".join(r.text for r in responses)
