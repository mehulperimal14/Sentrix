"""Gate P2 for the account API (docs/7 §11, §15 P2): every state change needs
the CSRF header, and nothing reads or changes another household's data."""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from sentrix_portal import security
from sentrix_portal.models import (AuditLog, Device, EmergencyContact, Household,
                                   HouseholdMember, TelegramLink, User, UserSession)
from test_acct_support import (add_device, add_household, fake_hub, outbox,  # noqa: F401
                               portal, query)
from test_portal_api_v2_support import V2, Api, api_as, staff_api

#: API routes that answer without a session (token pages, sign-in, activation).
PUBLIC = ("/session", "/auth/login", "/auth/logout", "/auth/activate", "/action/",
          "/consent/", "/contact-page/", "/account/verify-email/")


def _routes(app, method: str) -> list[str]:
    def walk(routes, prefix=""):
        for route in routes:
            inner = getattr(route, "original_router", None)
            if inner is not None:
                context = getattr(route, "include_context", None)
                yield from walk(inner.routes, prefix + (getattr(context, "prefix", "") or ""))
            elif isinstance(route, APIRoute) and method in route.methods:
                yield prefix + route.path
    return sorted({p for p in walk(app.routes) if p.startswith(V2)})


@pytest.fixture
def two_homes(portal):
    a = add_household(portal, complete=True)
    b = add_household(portal, code="SX-0002", owner="zeb", complete=True, name="Zebulon Bina",
                      email="zeb@example.com", phone="+919811122233")
    with portal.state.db.session() as db:
        db.get(EmergencyContact, b["contact_id"]).name = "Quentin Contact"
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


def test_every_change_needs_the_csrf_header(portal, two_homes):
    a, _ = two_homes
    owner = api_as(portal, a["owner_id"])
    admin = staff_api(portal)
    desk = staff_api(portal, role="operator", username="desk1")
    values = {"code": "BBBBBBBBBBBB", "token": "x" * 43, "household_id": a["household_id"],
              "device_id": a["device_id"], "user_id": "x", "member_id": "x",
              "contact_id": "x", "link_id": "x", "session_id": "x", "request_id": "x"}
    before = len(query(portal, AuditLog))
    checked = 0
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        for template in _routes(portal, method):
            path = template.format(**values)
            inner = template[len(V2):]
            if inner == "/auth/logout":
                continue                  # no session: just clears (test_portal_api_v2_auth)
            if inner.startswith(PUBLIC):
                client = Api(portal).client
                client.get(V2 + "/session")              # has the cookie, not the header
            else:
                client = (admin if inner.startswith("/admin") else
                          desk if inner.startswith("/desk") else owner).client
            # A body that passes validation, so only the missing header refuses it.
            body = ({"action": "approve"} if inner.startswith("/action/") else
                    {"action": "acknowledge"} if inner.startswith("/contact-page/") else {})
            response = client.request(method, path, json=body)
            assert response.status_code == 403, (method, template, response.status_code)
            assert response.json()["error"]["code"] == "csrf", (method, template)
            checked += 1
    assert checked >= 50
    assert len(query(portal, AuditLog)) == before, "nothing happened"


def _ids_of_b(b) -> list[str]:
    return [b["household_id"], b["owner_id"], b["device_id"], b["contact_id"],
            b["member_id"], b["serial"], "Zebulon", "Quentin", "Yara", "SX-0002",
            "+919811122233"]


def test_nothing_shows_another_household(portal, two_homes):
    a, b = two_homes
    owner = api_as(portal, a["owner_id"])
    for path in ("/session", "/home", "/account", "/account/sessions", "/onboarding",
                 "/onboarding/1", "/onboarding/2", "/onboarding/3", "/onboarding/4",
                 "/onboarding/5", "/onboarding/6", "/onboarding/7", "/household",
                 "/household/members", "/household/contacts", "/household/audit",
                 "/settings", "/privacy", "/sos"):
        text = owner.get(path).text
        for marker in _ids_of_b(b):
            assert marker not in text, (path, marker)
    export = owner.post("/privacy/export").text
    for marker in _ids_of_b(b):
        assert marker not in export, marker


def test_no_change_reaches_another_household(portal, two_homes, fake_hub):
    a, b = two_homes
    owner = api_as(portal, a["owner_id"])
    attempts = [
        ("DELETE", f"/household/members/{b['member_id']}", None),
        ("PATCH", f"/household/members/{b['member_id']}", {"age_band": "adult"}),
        ("DELETE", f"/household/contacts/{b['contact_id']}", None),
        ("POST", f"/household/contacts/{b['contact_id']}/resend", None),
        ("PATCH", f"/household/contacts/{b['contact_id']}",
         {"name": "Mine", "relation": "x", "phone": "9876500000", "priority": 3}),
        ("PUT", f"/onboarding/5/devices/{b['device_id']}",
         {"name": "Mine", "room_label": "Mine", "view_checked": True}),
        ("PATCH", f"/devices/{b['device_id']}", {"name": "Mine", "room_label": "Mine"}),
        ("POST", f"/devices/{b['device_id']}/unpair", None),
        ("DELETE", f"/telegram/links/{b['link_id']}", None),
        ("GET", f"/devices/{b['device_id']}/mirror", None),
    ]
    for method, path, body in attempts:
        response = owner.client.request(method, V2 + path, json=body,
                                        headers={"X-CSRF-Token": owner.csrf})
        assert response.status_code == 404, (method, path, response.status_code)
    claim = owner.post("/claim", {"serial": "SX5-000777", "claim_code": "KTRW-BQXL"})
    assert claim.status_code == 422
    victim = query(portal, UserSession, user_id=b["owner_id"])
    if victim:
        assert owner.delete(f"/account/sessions/{victim[0].id_hash}").status_code == 404
    assert query(portal, Device, id=b["device_id"])[0].name == "Living room unit"
    assert query(portal, Device, id=b["assigned"])[0].state == "ASSIGNED"
    assert query(portal, EmergencyContact, id=b["contact_id"])[0].name == "Quentin Contact"
    assert query(portal, HouseholdMember, id=b["member_id"])
    assert query(portal, TelegramLink)[0].status == "active"
    assert not fake_hub.unpaired
    assert {p["household_id"] for p in fake_hub.profiles} <= {a["household_id"]}


def test_sessions_follow_the_user_not_a_parameter(portal, two_homes):
    a, b = two_homes
    zeb = api_as(portal, b["owner_id"])
    riya = api_as(portal, a["owner_id"])
    assert zeb.get("/household").json()["name"] == "Zebulon Bina"
    assert "Zebulon" not in riya.get(f"/home?household_id={b['household_id']}").text
    with portal.state.db.session() as db:
        assert db.get(User, a["owner_id"]).household_id == a["household_id"]
        assert db.query(Household).count() == 2
        assert security.verify_audit_chain(db)["ok"]
