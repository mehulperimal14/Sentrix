"""Gate P2 for the T-ACCT routes: every POST needs a CSRF token, and no route
leaks or changes another household's data (docs/7 §11, §15 P2)."""

from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute

from sentrix_portal import security
from sentrix_portal.models import (AuditLog, Device, EmergencyContact, Household,
                                   HouseholdMember, TelegramLink, User, UserSession)
from sentrix_portal.routes import (admin, auth, consent, home, household, onboarding,
                                   privacy, settings)
from test_acct_support import (PASSWORD, Browser, add_device, add_household,  # noqa: F401
                               add_user, fake_hub, outbox, owner_browser, portal, query,
                               staff_browser)

ROUTERS = (auth, home, onboarding, household, settings, privacy, admin, consent)
#: Routes that answer without a session (docs/7 §11): the rest need one.
PUBLIC = re.compile(r"^/(login|activate|consent/|account/verify-email/\{)")


def _routes(method: str) -> list[str]:
    return sorted({route.path for module in ROUTERS for route in module.router.routes
                   if isinstance(route, APIRoute) and method in route.methods})


@pytest.fixture
def two_homes(portal):
    a = add_household(portal, complete=True)
    b = add_household(portal, code="SX-0002", owner="zeb", complete=True, name="Zebulon Bina",
                      email="zeb@example.com", phone="+919811122233")
    with portal.state.db.session() as db:
        contact = db.get(EmergencyContact, b["contact_id"])
        contact.name = "Quentin Contact"
        member = HouseholdMember(household_id=b["household_id"], name="Yara Member",
                                 age_band="adult")
        db.add(member)
        db.add(TelegramLink(subject_type="user", subject_id=b["owner_id"],
                            household_id=b["household_id"], chat_id="999"))
        db.flush()
        b["member_id"] = member.id
        b["link_id"] = db.query(TelegramLink).one().id
    b["assigned"] = add_device(portal, serial="SX5-000777", household_id=b["household_id"],
                               state="ASSIGNED", claim_code="KTRW-BQXL")
    return a, b


def test_every_post_needs_csrf(portal, two_homes):
    a, _ = two_homes
    staff_id = add_user(portal, username="desk9", role="operator")
    owner = owner_browser(portal)
    admin_browser = staff_browser(portal)
    anonymous = Browser(portal)
    anonymous.get("/login")                           # has the cookie, not the form token
    values = {"code": "BBBBBBBBBBBB", "token": "x" * 43, "step": "1",
              "household_id": a["household_id"], "device_id": a["device_id"],
              "user_id": staff_id}
    paths = _routes("POST")
    assert len(paths) >= 25, paths
    before = len(query(portal, AuditLog))
    for template in paths:
        path = template.format(**values)
        if PUBLIC.match(template):
            browser = anonymous
        elif template.startswith("/admin"):
            browser = admin_browser
        else:
            browser = owner
        response = browser.post(path, {"action": "x", "section": "sos"}, csrf=False)
        assert response.status_code == 403, (template, response.status_code)
    assert len(query(portal, AuditLog)) == before, "nothing happened"


def test_unauthenticated_routes_are_only_the_listed_ones(portal):
    anonymous = Browser(portal)
    for path in _routes("GET"):
        if PUBLIC.match(path) or path == "/":
            continue
        concrete = path.format(step=1, household_id="x", device_id="x", code="x", token="x")
        response = anonymous.get(concrete)
        assert response.status_code in (303, 401), path
        if response.status_code == 303:
            assert response.headers["location"] == "/login", path


def _ids_of_b(b) -> list[str]:
    return [b["household_id"], b["owner_id"], b["device_id"], b["contact_id"],
            b["member_id"], b["serial"], "Zebulon", "Quentin", "Yara", "SX-0002",
            "+919811122233", "98xxxxxx33"]


def test_no_page_shows_another_household(portal, two_homes):
    a, b = two_homes
    owner = owner_browser(portal)
    for path in ("/home", "/account", "/account/sessions", "/onboarding/1", "/onboarding/2",
                 "/onboarding/3", "/onboarding/4", "/onboarding/5", "/onboarding/6",
                 "/onboarding/7", "/claim", "/household", "/household/members",
                 "/household/contacts", "/household/audit", "/settings", "/privacy",
                 "/privacy/export", "/privacy/erase"):
        html = owner.get(path).text
        for marker in _ids_of_b(b):
            assert marker not in html, (path, marker)
    owner.post("/account/step-up", {"password": PASSWORD, "next": "/privacy/export"})
    export = owner.post("/privacy/export", {}).text
    for marker in _ids_of_b(b):
        assert marker not in export, marker


def test_no_form_reaches_another_household(portal, two_homes, fake_hub):
    a, b = two_homes
    owner = owner_browser(portal)
    owner.get("/settings")
    owner.post("/account/step-up", {"password": PASSWORD, "next": "/settings"})
    owner.get("/settings")
    attempts = [
        ("/onboarding/3", {"action": "remove_member", "member_id": b["member_id"]}),
        ("/onboarding/4", {"action": "remove_contact", "contact_id": b["contact_id"]}),
        ("/onboarding/4", {"action": "resend_contact", "contact_id": b["contact_id"]}),
        ("/onboarding/4", {"action": "update_contact", "contact_id": b["contact_id"],
                           "name": "Mine", "relation": "x", "phone": "9876500000",
                           "priority": "3"}),
        ("/onboarding/5", {"action": "device", "device_id": b["device_id"], "name": "Mine",
                           "room_label": "Mine", "view_checked": "1"}),
        ("/household/members", {"action": "remove_member", "member_id": b["member_id"]}),
        ("/household/members", {"action": "update_member", "member_id": b["member_id"],
                                "age_band": "adult"}),
        ("/household/contacts", {"action": "remove_contact", "contact_id": b["contact_id"]}),
        ("/household/contacts", {"action": "update_contact", "contact_id": b["contact_id"],
                                 "name": "Mine", "relation": "x", "phone": "9876500000",
                                 "priority": "3"}),
        ("/settings", {"section": "device", "device_id": b["device_id"], "name": "Mine",
                       "room_label": "Mine"}),
        ("/settings", {"section": "unpair", "device_id": b["device_id"]}),
        ("/settings", {"section": "telegram_unlink", "link_id": b["link_id"]}),
    ]
    for path, data in attempts:
        response = owner.post(path, data)
        assert response.status_code == 404, (path, data, response.status_code)
    claim = owner.post("/claim", {"serial": "SX5-000777", "claim_code": "KTRW-BQXL"})
    assert claim.status_code == 400
    victim_session = query(portal, UserSession, user_id=b["owner_id"])
    if victim_session:
        owner.post("/account/sessions", {"session": victim_session[0].id_hash})
    assert query(portal, Device, id=b["device_id"])[0].name == "Living room unit"
    assert query(portal, Device, id=b["assigned"])[0].state == "ASSIGNED"
    assert query(portal, EmergencyContact, id=b["contact_id"])[0].name == "Quentin Contact"
    assert query(portal, HouseholdMember, id=b["member_id"])
    assert query(portal, TelegramLink)[0].status == "active"
    assert not fake_hub.unpaired
    assert all(s.revoked_at is None for s in query(portal, UserSession, user_id=b["owner_id"]))
    assert {p["household_id"] for p in fake_hub.profiles} <= {a["household_id"]}


def test_sessions_follow_the_user_not_a_parameter(portal, two_homes):
    a, b = two_homes
    zeb = owner_browser(portal, "zeb")
    riya = owner_browser(portal)
    assert "Zebulon" in zeb.get("/household").text or "Zebulon" in zeb.get("/home").text
    assert "Zebulon" not in riya.get(f"/home?household_id={b['household_id']}").text
    with portal.state.db.session() as db:
        assert db.get(User, a["owner_id"]).household_id == a["household_id"]
        assert db.query(Household).count() == 2
        assert security.verify_audit_chain(db)["ok"]
