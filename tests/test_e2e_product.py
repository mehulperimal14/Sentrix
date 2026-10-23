"""SENTRIX end to end, in one process (docs/7 §15.2).

The whole product runs here: the Portal app on a temporary SQLite database,
a device (the Local Console app with a fake pipeline, the real SOS engine on
an injected clock, the real link client and a fake modem), the device link
over an in-memory pipe into the Portal's real hub, and fake Telegram and
e-mail transports on the Portal. Browsers are ASGI clients. See
``tests/e2e/world.py``.

Covered: provisioning -> welcome letter -> single-use activation -> the
seven-step wizard with the contact's confirmation -> device claim -> link
online -> tunnelled console parity on every LC page -> an L3 weapon request
from DRAFTED to CLOSED with the Desk, and the decline (PIN), expiry,
"Request help now", safety-default, duress, device-offline, internet-down and
cross-household paths, answers from Telegram buttons and e-mail action links,
hostile text on every page, the admin's view of credentials and test mode.

Nothing is sent: every transport is a fake and the conftest socket guard
records any attempt to leave the machine (asserted empty per test).
"""

from __future__ import annotations

import re

import pytest

from e2e.world import (CONTACT_EMAIL, CONTACT_PHONE, DESK_PHONE, DURESS_PIN, OWNER_CHAT,
                       OWNER_EMAIL, OWNER_PHONE, PASSWORD, SOS_PIN, activate, build_world,
                       dom_ids, onboard, profile_ready, provision,
                       ready_world, staff, teardown_world, wait_for)


@pytest.fixture
def world(tmp_path, monkeypatch, temp_db):
    import conftest
    blocked_before = len(conftest.BLOCKED_CONNECTIONS)
    w = build_world(tmp_path, monkeypatch)
    try:
        yield w
    finally:
        teardown_world(w)
    assert conftest.BLOCKED_CONNECTIONS[blocked_before:] == [], \
        "the product tried to reach the network"


def rid_of(world) -> str:
    request = wait_for(world.open_request, what="an open request on the device")
    return request["id"]


def mirrored(world, request_id, state, timeout=10):
    wait_for(lambda: world.mirror_state(request_id) == state, timeout=timeout,
             what=f"Portal mirror {state}")


def mirror_flag(world, request_id, flag):
    row = world.mirror(request_id)
    return bool(row is not None and (row.flags or {}).get(flag))


def device_state(world, request_id):
    return world.device_request(request_id)["state"]


# ── the happy path, from the factory to a closed request ─────────────────────
def test_product_flow_end_to_end(world):
    from sentrix.link import client as link_client
    from sentrix.storage import database, requests
    from sentrix_portal import security
    from sentrix_portal.models import (ActivationCode, Device, EmergencyContact, Household,
                                       User)
    from sentrix_portal.services import hub
    from sentrix_portal.services import sos as portal_sos

    # 1. the company provisions a device and a customer; the welcome letter
    admin = staff(world, "admin1", "company_admin")
    facts = provision(world, admin)
    world.facts.update(facts)
    letter = facts["letter"]
    for item in ("riya", facts["serial"], facts["activation"], facts["claim"]):
        assert item in letter
    with world.db() as db:
        device = db.get(Device, facts["device_id"])
        assert device.state == "ASSIGNED" and device.household_id == facts["household_id"]
        assert device.claim_code_hash == security.code_digest(facts["claim"])
        owner_row = db.query(User).filter_by(username="riya").one()
        assert owner_row.status == "pending_activation" and owner_row.password_hash is None
        stored = db.query(ActivationCode).filter_by(user_id=owner_row.id).all()
        assert stored and all(facts["activation"] not in str(vars(r)) for r in stored), \
            "only a digest of the activation code is stored"

    # 2. the device is installed: it dials in on its bootstrap secret, unclaimed
    world.install_device(facts["identity"])
    session = wait_for(lambda: hub.current_hub() and
                       hub.current_hub().sessions.get(facts["device_id"]),
                       what="bootstrap session")
    assert session.auth == "bootstrap" and not hub.is_online(facts["device_id"])

    # 3. activation: single use, then the password is set
    owner = activate(world, facts)
    again = world.browser(ip="203.0.113.11")
    again.get("/activate")
    reused = again.post("/activate", {"username": "riya", "code": facts["activation"],
                                      "password": "another good phrase",
                                      "password_confirm": "another good phrase"})
    assert reused.status_code == 400, "an activation code works once"
    with world.db() as db:
        user = db.query(User).filter_by(username="riya").one()
        assert user.status == "active" and security.verify_password(user.password_hash,
                                                                    PASSWORD)

    # 4. the onboarding wizard, step by step
    seen = []

    def check(step, w):
        seen.append(step)
        with w.db() as db:
            home = db.get(Household, facts["household_id"])
            if step == 2:
                assert home.pincode == "147001" and home.landmark == "opposite the SBI ATM"
            if step == 4:
                contact = db.query(EmergencyContact).filter_by(household_id=home.id).one()
                assert contact.consent_status == "CONFIRMED", "the contact confirmed"
            if step == 5:
                assert db.get(Device, facts["device_id"]).state == "CLAIMED"
                assert link_client.load_token(w.rig.identity_dir), "the token reached the device"
            if step == 6:
                assert security.verify_pin(home.sos_pin_hash, SOS_PIN)
                assert security.verify_pin(home.duress_pin_hash, DURESS_PIN)
            if step == 7:
                assert home.onboarding_completed_at is not None
    onboard(world, owner, facts, check=check)
    assert seen == [1, 2, 3, 4, 5, 6, 7]
    assert hub.is_online(facts["device_id"])

    # The profile on the device: confirmed contact, PIN hashes, never a password.
    pushed = profile_ready(world)
    assert pushed["owner"]["phone"] == OWNER_PHONE
    assert [c["phone"] for c in pushed["contacts"]] == [CONTACT_PHONE]
    assert pushed["contacts"][0]["consent_status"] == "CONFIRMED"
    assert pushed["sos"]["pin_hash"] and pushed["sos"]["duress_pin_hash"]
    assert pushed["desk"]["phone"] == DESK_PHONE
    flat = repr(pushed)
    assert "password" not in flat and PASSWORD not in flat and SOS_PIN not in flat
    world.pump_portal()
    world.tg.sent.clear()
    world.mail.sent.clear()

    # 5. the console through the tunnel (parity is its own test below)
    dashboard = owner.get(f"/d/{facts['device_id']}/dashboard")
    assert dashboard.status_code == 200 and f'data-base="/d/{facts["device_id"]}"' in \
        dashboard.text

    # 6. L3 weapon: DRAFTED -> PENDING, text pre-filled from the profile
    incident_id = world.trigger("weapon", 3)
    rid = rid_of(world)
    trail = requests.events(rid, include_restricted=True)
    assert [(e["event"], e["to_state"]) for e in trail[:2]] == [("created", "DRAFTED"),
                                                              ("prompted", "PENDING")]
    request = world.device_request(rid)
    assert request["state"] == "PENDING" and request["incident_type"] == "weapon"
    text = request["sos_text_full"]
    for fact in ("Riya Sharma", "Mall Road", "opposite the SBI ATM", "147001",
                 "SENTRIX does not contact 112 itself."):
        assert fact in text, fact
    mirrored(world, rid, "PENDING")

    # 7. the prompt on Telegram (short text + link) and e-mail
    world.pump_portal()
    telegram = [(a, s, t) for a, _tpl, s, t in world.portal_messages(world.tg)]
    mail = [(a, s, t) for a, _tpl, s, t in world.portal_messages(world.mail)]
    assert [(a, s) for a, s, _ in telegram] == [(f"tg:{OWNER_CHAT}", "t0")]
    tg_text = telegram[0][2]
    assert tg_text.startswith("[SENTRIX DEMO")
    assert f"/sos/{rid}" in tg_text and "Do you need assistance?" in tg_text
    for private in ("Mall Road", "147001", "opposite the SBI ATM", "Gate 2"):
        assert private not in tg_text, f"Telegram carries {private!r}"
    tg_buttons = world.tg.sent[0][1].buttons
    assert [b.label for b in tg_buttons] == ["Alert my contacts and the SENTRIX desk",
                                            "I'm checking", "False alarm (PIN)"]
    assert (OWNER_EMAIL, "t0") in [(a, s) for a, s, _ in mail]
    assert not [m for m in telegram + mail if m[0] != f"tg:{OWNER_CHAT}" and
                m[0] != OWNER_EMAIL], "single-source L3: owner only"
    assert world.channels.sms() == [], "online: no device SMS"
    wait_for(lambda: any(e["event"] == "notification"
                         for e in requests.events(rid, include_restricted=True)),
             what="delivery feedback reached the device")

    # 8. the owner approves on the Portal: APPROVED -> DISPATCHED
    page = owner.get(f"/sos/{rid}")
    assert page.status_code == 200 and "Do you need assistance?" in page.text
    assert f'href="tel:{DESK_PHONE}"' in page.text and "tel:112" not in page.text
    approved = owner.post(f"/sos/{rid}/approve", {"authority": "POLICE"})
    assert approved.status_code == 303 and approved.headers["location"].endswith("m=ok"), \
        approved.headers.get("location")
    wait_for(lambda: device_state(world, rid) == "DISPATCHED", what="DISPATCHED on the device")
    mirrored(world, rid, "DISPATCHED")
    world.pump_portal()
    contact_mail = [t for a, s, t in [(a, s, t) for a, _x, s, t in
                                      world.portal_messages(world.mail)]
                    if a == CONTACT_EMAIL and s == "contacts_t0"]
    assert contact_mail and "Mall Road" in contact_mail[0], "contacts get the full SOS text"

    # 9. the Desk acknowledges and resolves; the verdict becomes TRUE
    desk = staff(world, "desk1", "operator")
    queue = desk.get("/desk/api/queue", headers={"accept": "application/json"}).json()
    assert [c["id"] for c in queue["requests"]] == [rid]
    card = desk.get(f"/desk/requests/{rid}")
    assert card.status_code == 200 and "Mall Road" in card.text
    ack = desk.post(f"/desk/requests/{rid}/acknowledge")
    assert ack.status_code == 303 and ack.headers["location"].endswith("m=ok")
    wait_for(lambda: device_state(world, rid) == "ACKNOWLEDGED", what="ACKNOWLEDGED")
    mirrored(world, rid, "ACKNOWLEDGED")
    resolved = desk.post(f"/desk/requests/{rid}/resolve",
                         {"outcome": "real_threat", "note": "simulated"})
    assert resolved.headers["location"].endswith("m=ok"), resolved.headers["location"]
    wait_for(lambda: device_state(world, rid) == "CLOSED", what="CLOSED on the device")
    mirrored(world, rid, "CLOSED")
    final = world.device_request(rid)
    assert final["outcome"] == "real_threat"
    verdicts = {r["id"]: r.get("verdict") for r in database.recent_incidents()}
    assert verdicts[incident_id] == "TRUE"

    # 10. both audit chains verify, end to end
    assert requests.verify_chain(rid)["ok"]
    with world.db() as db:
        chains = portal_sos.verify_chains(db, rid)
        assert chains["ok"] and chains["origins"]["device"] >= 6
    world.pump_portal()
    closed = [s for a, _t, s, _x in world.portal_messages() if a == OWNER_EMAIL]
    assert "resolved" in closed, "the owner hears it was closed"


# ── parity: every console page, directly and through the tunnel ──────────────
def _routes(routes, prefix=""):
    """(path, methods) of every HTTP route, through included routers."""
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:                      # FastAPI >= 0.140 include wrapper
            context = getattr(route, "include_context", None)
            yield from _routes(inner.routes, prefix + (getattr(context, "prefix", "") or ""))
        elif getattr(route, "methods", None):
            yield prefix + route.path, route.methods


def lc_page_routes(app) -> set[str]:
    """Every HTML page the Local Console serves to a signed-in user."""
    skip = ("/api/", "/media/", "/static", "/setup", "/video", "/ws/")
    return {path for path, methods in _routes(app.routes)
            if "GET" in methods and not path.startswith(skip) and "{" not in path
            and path not in ("/login", "/logout", "/")}


#: Controls only the device admin on the LAN has: signing out of the LC
#: session, the disclosed scripted trigger (docs/7 §9.2) and the buzzer test.
LAN_ONLY_IDS = frozenset({"logout-link", "sos-demo-trigger", "sos-demo-trigger-box",
                          "sos-demo-trigger-type", "buzzer-test-block", "buzzer-test-btn",
                          "buzzer-test-message"})


def test_tunnel_parity_on_every_console_page(world):
    from fastapi.testclient import TestClient
    from sentrix.link import policy
    from sentrix.web import auth
    from sentrix.web.security import generate_token
    from e2e.world import LC_PASSWORD
    owner = ready_world(world)
    device_id = world.facts["device_id"]
    base = f"/d/{device_id}"
    pages = lc_page_routes(world.lc)
    assert pages == set(policy.PAGES) - {"/"}, \
        "a console page is missing from the tunnel table (or the reverse)"
    world.trigger("weapon", 3)                     # an SOS card on the pages too
    cookie = generate_token(LC_PASSWORD)
    direct = TestClient(world.lc, follow_redirects=False)
    direct.cookies.set("lc_session", cookie)
    for path in sorted(pages):
        lan = direct.get(path, headers={"accept": "text/html"})
        remote = owner.get(base + path)
        assert lan.status_code == remote.status_code == 200, (path, lan.status_code,
                                                              remote.status_code)
        lan_ids, remote_ids = dom_ids(lan.text), dom_ids(remote.text)
        assert lan_ids - LAN_ONLY_IDS == remote_ids, (path, sorted(lan_ids ^ remote_ids))
        assert len(remote_ids) > 5, path
        # production CSP and CSRF on both sides, the URLs under the base
        for response in (lan, remote):
            csp = response.headers["content-security-policy"]
            assert "script-src 'self' 'nonce-" in csp and "unsafe-inline" not in \
                csp.split("script-src")[1].split(";")[0]
            assert re.search(r'<meta name="csrf-token" content="[^"]+">', response.text)
        assert f'data-base="{base}"' in remote.text and 'data-base=""' in lan.text
        # The tunnelled nav links back to the Portal's own pages on purpose.
        hrefs = [h for h in re.findall(r'(?:href|src|action)="(/[^"]*)"', remote.text)
                 if h not in ("/home", "/desk", "/account")]
        assert hrefs and all(h.startswith(base + "/") for h in hrefs), \
            (path, [h for h in hrefs if not h.startswith(base + "/")][:5])
    # The tunnel refuses what is never tunnelled.
    for path in ("/login", "/logout", "/setup/wifi", "/docs", "/openapi.json"):
        assert owner.get(base + path).status_code in (403, 404), path


# ── helpers for the scenarios ─────────────────────────────────────────────────
def lc_admin(world):
    """The device admin's browser on the LAN: (client, csrf headers)."""
    from fastapi.testclient import TestClient
    from sentrix.web import auth
    from sentrix.web.security import generate_token
    from e2e.world import LC_PASSWORD
    cookie = generate_token(LC_PASSWORD)
    client = TestClient(world.lc, follow_redirects=False)
    client.cookies.set("lc_session", cookie)
    return client, {"X-CSRF-Token": auth.csrf_for_session(cookie),
                    "accept": "application/json"}


def sent_to(world, address, step=None):
    """Texts the Portal's fakes delivered to ``address`` (at ``step``)."""
    return [t for a, _tpl, s, t in world.portal_messages()
            if a == address and (step is None or s == step)]


def audit_events(world, event):
    from sentrix_portal.models import AuditLog
    with world.db() as db:
        rows = db.query(AuditLog).filter_by(event=event).all()
        db.expunge_all()
        return rows


def package_of(request):
    from sentrix.storage import database
    assert request.get("package_id"), "the request carries a dispatch package"
    return database.get_dispatch(request["package_id"])


def verdict_of(incident_id):
    from sentrix.storage import database
    return {r["id"]: r.get("verdict") for r in database.recent_incidents()}[incident_id]


def events_of(request_id):
    from sentrix.storage import requests
    return [e["event"] for e in requests.events(request_id, include_restricted=True)]


# ── false alarm: the PIN declines, the package is cancelled ──────────────────
def test_decline_with_the_pin_cancels_the_package(world):
    from sentrix.storage import requests
    from sentrix_portal.models import DeviceCommand
    owner = ready_world(world)
    incident_id = world.trigger("weapon", 3)
    rid = rid_of(world)
    mirrored(world, rid, "PENDING")
    assert package_of(world.device_request(rid))["status"] == "PENDING"

    wrong = owner.post(f"/sos/{rid}/decline", {"pin": "000000"})
    assert wrong.headers["location"].endswith("m=pin_invalid"), wrong.headers["location"]
    assert device_state(world, rid) == "PENDING", "a wrong PIN changes nothing"

    done = owner.post(f"/sos/{rid}/decline", {"pin": SOS_PIN})
    assert done.headers["location"].endswith("m=ok"), done.headers["location"]
    request = world.device_request(rid)
    assert request["state"] == "DECLINED"
    assert package_of(request)["status"] == "CANCELLED"
    assert verdict_of(incident_id) == "FALSE"
    mirrored(world, rid, "DECLINED")
    page = owner.get(f"/sos/{rid}")
    assert "Marked as false alarm" in page.text
    assert requests.verify_chain(rid)["ok"]
    with world.db() as db:
        stored = [repr(c.args) for c in db.query(DeviceCommand).all()]
    assert stored and not any(SOS_PIN in s or "000000" in s for s in stored), \
        "no command row keeps a PIN"


# ── no answer: EXPIRED (unanswered), then "Request help now" ──────────────────
def test_unanswered_request_expires_and_help_now_opens_a_linked_request(world):
    from sentrix.sos import states as S
    owner = ready_world(world)
    world.trigger("weapon", 3)
    rid = rid_of(world)
    mirrored(world, rid, "PENDING")
    window = world.device_request(rid)["policy"]["window_seconds"]
    assert window == 1800, "the household's 30-minute window, frozen on the request"

    opened = world.clock.now
    for minute in (2, 5, 10, 20, 25):               # reminders and the lapse warning
        world.tick(opened + minute * 60 + 1 - world.clock.now)
        assert device_state(world, rid) == "PENDING"
    world.tick(opened + window - 5 - world.clock.now)   # 5 s before the end
    assert device_state(world, rid) == "PENDING"
    world.tick(10)
    request = world.device_request(rid)
    assert request["state"] == "EXPIRED"
    assert S.owner_view(request["state"], request["flags"])["label"] == "Unanswered"
    mirrored(world, rid, "EXPIRED")
    world.pump_portal()
    reminders = {s for a, _t, s, _x in world.portal_messages() if a == OWNER_EMAIL}
    assert {"t0", "lapse", "lapsed"} <= reminders, reminders
    lapsed = sent_to(world, OWNER_EMAIL, "lapsed")[0]
    assert "unanswered" in lapsed and "Nobody was told the home is safe" in lapsed
    page = owner.get(f"/sos/{rid}")
    assert "Unanswered" in page.text

    help_now = owner.post(f"/sos/{rid}/help_now", {"authority": "POLICE"})
    assert help_now.status_code == 303 and "m=ok" in help_now.headers["location"], \
        help_now.headers["location"]
    new_id = help_now.headers["location"].split("/sos/")[1].split("?")[0]
    assert new_id != rid
    linked = world.device_request(new_id)
    assert linked["parent_request_id"] == rid
    assert linked["state"] in ("APPROVED", "DISPATCHED")
    mirrored(world, new_id, "DISPATCHED")
    assert world.device_request(rid)["state"] == "EXPIRED", "the parent stays unanswered"


# ── safety defaults: corroborated L4 at once, confirmed fire at T+2 min ──────
def test_safety_defaults_alert_confirmed_contacts(world):
    owner = ready_world(world)
    world.trigger("weapon", 4, evidence="corroborated")
    rid = rid_of(world)
    mirrored(world, rid, "PENDING")
    assert "contacts_notified" in events_of(rid), "corroborated L4: contacts at once"
    wait_for(lambda: mirror_flag(world, rid, "contacts_notified"),
             what="the mirror has the contacts flag")
    world.pump_portal()
    contact = sent_to(world, CONTACT_EMAIL, "contacts_t0")
    assert contact and "confirmed by two different models" in contact[0]
    assert sent_to(world, OWNER_EMAIL, "t0"), "the owner is still asked"
    request = world.device_request(rid)
    assert request["state"] == "PENDING" and request["urgency"] == "HIGH"

    # Close it, then a confirmed fire: contacts at T+2 min, not before.
    assert owner.post(f"/sos/{rid}/decline", {"pin": SOS_PIN}).headers["location"] \
        .endswith("m=ok")
    world.tg.sent.clear()
    world.mail.sent.clear()
    world.trigger("fire", 3)
    fire = rid_of(world)
    assert fire != rid
    world.tick(110)
    assert "contacts_notified" not in events_of(fire)
    world.tick(11)
    assert "contacts_notified" in events_of(fire), "fire: contacts at T+2 min"
    wait_for(lambda: mirror_flag(world, fire, "contacts_notified"),
             what="the mirror has the contacts flag")
    world.pump_portal()
    assert sent_to(world, CONTACT_EMAIL, "contacts_t0")
    assert device_state(world, fire) == "PENDING", "the owner is still asked"


# ── duress PIN ────────────────────────────────────────────────────────────────
def test_duress_pin_looks_like_a_false_alarm_but_sends_help(world):
    from sentrix.storage import requests
    owner = ready_world(world)
    world.trigger("weapon", 3)
    rid = rid_of(world)
    mirrored(world, rid, "PENDING")
    done = owner.post(f"/sos/{rid}/decline", {"pin": DURESS_PIN})
    assert done.headers["location"].endswith("m=ok"), done.headers["location"]
    request = world.device_request(rid)
    assert request["state"] in ("APPROVED", "DISPATCHED") and request["flags"].get("duress")
    mirrored(world, rid, "DISPATCHED")
    page = owner.get(f"/sos/{rid}")
    assert "Marked as false alarm" in page.text and "duress" not in page.text.lower()
    restricted = [e for e in requests.events(rid, include_restricted=True) if e["restricted"]]
    assert restricted and restricted[0]["detail"].get("duress")
    assert all(not e.get("restricted") for e in requests.events(rid))
    mirrored(world, rid, "DISPATCHED")
    world.pump_portal()
    contact = sent_to(world, CONTACT_EMAIL, "duress")
    assert contact and "Do not go to the home. Call 112." in contact[0]
    assert not [t for t in sent_to(world, OWNER_EMAIL) + sent_to(world, f"tg:{OWNER_CHAT}")
                if "DURESS" in t.upper()], "the owner's channels never say duress"
    desk = staff(world, "desk1", "operator")
    queue = desk.get("/desk/api/queue", headers={"accept": "application/json"}).json()
    assert [c["id"] for c in queue["requests"]] == [rid]
    card = desk.get(f"/desk/requests/{rid}")
    assert "Do not go to the home. Call 112." in card.text
    view = owner.get(f"/sos/api/requests/{rid}", headers={"accept": "application/json"})
    assert view.status_code == 200 and "duress" not in view.text.lower()
    shown = [e["event"] for e in view.json()["events"]]
    assert "decline" in shown and "approve" not in shown,         "the owner's timeline reads as the decline the screen shows"
    assert [e["seq"] for e in view.json()["events"]] == list(range(1, len(shown) + 1))


# ── the device drops mid-request: cloud authority, tamper, reconnect rule ────
def test_device_offline_mid_request_hands_authority_to_the_cloud(world):
    from sentrix.storage import requests
    owner = ready_world(world)
    world.trigger("weapon", 3)
    rid = rid_of(world)
    mirrored(world, rid, "PENDING")
    world.pump_portal()
    world.mail.sent.clear()
    world.tg.sent.clear()

    world.go_offline()
    assert world.check_heartbeats(later=60) == [world.facts["device_id"]]
    row = world.mirror(rid)
    assert row.authority_side == "cloud" and (row.flags or {}).get("device_offline")
    assert (row.flags or {}).get("contacts_notified"), "safety default: contacts at once"
    tamper = audit_events(world, "device.tamper")
    assert tamper and tamper[0].detail["request_id"] == rid
    world.pump_portal()
    assert sent_to(world, OWNER_EMAIL, "offline"), "the owner hears the device went offline"
    assert sent_to(world, CONTACT_EMAIL, "contacts_t0")

    # The owner answers while the device is away: the cloud decides.
    page = owner.get(f"/d/{world.facts['device_id']}/dashboard")
    assert page.status_code in (502, 503, 504) or "offline" in page.text.lower()
    approved = owner.post(f"/sos/{rid}/approve", {"authority": "POLICE"})
    assert approved.headers["location"].endswith("m=ok"), approved.headers["location"]
    assert world.mirror_state(rid) in ("APPROVED", "DISPATCHED")
    world.run_cloud_timers(later=1)
    assert world.mirror_state(rid) == "DISPATCHED"
    assert device_state(world, rid) == "PENDING", "the device does not know yet"

    # Reconnect: sos.sync; the help path wins on both sides; device authority again.
    world.go_online()
    wait_for(lambda: device_state(world, rid) == "DISPATCHED", timeout=15,
             what="the device took the cloud's DISPATCHED")
    wait_for(lambda: world.mirror(rid).authority_side == "device", what="device authority")
    cloud_rows = [e for e in requests.events(rid, include_restricted=True)
                  if e["origin"] == "cloud"]
    assert {"device_offline", "approve", "delivered"} <= {e["event"] for e in cloud_rows}
    assert requests.verify_chain(rid)["ok"]
    from sentrix_portal.services import sos as portal_sos
    with world.db() as db:
        assert portal_sos.verify_chains(db, rid)["ok"]
    # the device carries on from here: the Desk closes it on the device
    desk = staff(world, "desk1", "operator")
    assert desk.post(f"/desk/requests/{rid}/acknowledge").headers["location"].endswith("m=ok")
    wait_for(lambda: device_state(world, rid) == "ACKNOWLEDGED", what="ACK on the device")


# ── internet down: the device texts the owner and the contacts itself ────────
def test_internet_down_device_sends_sms_through_the_modem(world):
    from sentrix.alerting import guard
    owner = ready_world(world)
    world.go_offline()
    world.trigger("weapon", 3)
    rid = rid_of(world)
    sms = world.channels.sms()
    assert [(a, s) for a, s, _ in sms] == [(OWNER_PHONE, "t0")], sms
    text = sms[0][2]
    assert text.startswith(guard.TEST_PREFIX_GSM) and "112" in text
    assert "SENTRIX does not contact 112 itself." in text

    # The owner approves on the console in the house (offline approval).
    client, headers = lc_admin(world)
    approved = client.post(f"/api/sos/requests/{rid}/approve", json={"authority": "POLICE"},
                           headers=headers)
    assert approved.status_code == 200, approved.text
    world.tick(1)
    targets = {(a, s) for a, s, _ in world.channels.sms()}
    assert (CONTACT_PHONE, "contacts_t0") in targets, targets
    assert (DESK_PHONE, "desk_t0") in targets, targets
    assert all(a in (OWNER_PHONE, CONTACT_PHONE, DESK_PHONE) for a, _s in targets), \
        "the device texts only numbers in its profile"
    contact_text = [t for a, s, t in world.channels.sms() if a == CONTACT_PHONE][0]
    assert "Riya Sharma" in contact_text

    # Back online: every event syncs, the mirror catches up with the device.
    world.go_online()
    wait_for(lambda: world.mirror_state(rid) == device_state(world, rid), timeout=15,
             what="mirror caught up")
    assert world.mirror_state(rid) in ("APPROVED", "DISPATCHED")
    from sentrix.storage import link_store, requests
    wait_for(lambda: link_store.depth() == 0, what="every outbox event acknowledged")
    device_events = [e for e in requests.events(rid, include_restricted=True)
                     if e["origin"] == "device"]
    from sentrix_portal.models import RequestEvent
    def portal_has_all():
        with world.db() as db:
            return db.query(RequestEvent).filter_by(request_id=rid,
                                                    origin="device").count() ==                 len(device_events)
    wait_for(portal_has_all, what="the Portal holds every device event")


# ── cross-household isolation ─────────────────────────────────────────────────
def test_a_second_household_cannot_reach_the_first(world):
    from test_acct_support import add_household
    from sentrix_portal.models import EmergencyContact
    owner = ready_world(world)
    device_id = world.facts["device_id"]
    world.trigger("weapon", 3)
    rid = rid_of(world)
    mirrored(world, rid, "PENDING")
    world.pump_portal()
    other = add_household(world.app, code="SX-0099", owner="other", email="o@example.com",
                          phone="+919811111111", complete=True)
    stranger = world.browser(ip="192.0.2.99")
    assert stranger.login("other").status_code == 303
    with world.db() as db:
        contact_id = db.query(EmergencyContact).filter_by(
            household_id=world.facts["household_id"]).one().id

    for path in (f"/d/{device_id}/dashboard", f"/d/{device_id}/live",
                 f"/d/{device_id}/api/health", f"/d/{device_id}/sos",
                 f"/sos/{rid}", f"/sos/api/requests/{rid}", f"/sos/{rid}/snapshot.jpg"):
        response = stranger.get(path)
        assert response.status_code in (403, 404), (path, response.status_code)
        assert "Mall Road" not in response.text and "Riya" not in response.text, path
    assert stranger.get("/sos/api/open", headers={"accept": "application/json"}).json() == \
        {"requests": []}
    stranger.get("/home")
    for action, data in (("approve", {"authority": "POLICE"}), ("decline", {"pin": SOS_PIN}),
                         ("help_now", {})):
        response = stranger.post(f"/sos/{rid}/{action}", data)
        assert response.status_code in (403, 404), (action, response.status_code)
    tunnelled = stranger.api("POST", f"/d/{device_id}/api/mode", json={"armed": True})
    assert tunnelled.status_code in (403, 404)
    assert device_state(world, rid) == "PENDING", "nothing the stranger did reached it"
    # the Desk card and queue are staff-only
    assert stranger.get(f"/desk/requests/{rid}").status_code == 403
    assert stranger.get("/desk/api/queue", headers={"accept": "application/json"}) \
        .status_code == 403
    # the first household's contacts are not the stranger's
    page = stranger.get("/household/contacts")
    assert page.status_code == 200 and contact_id not in page.text
    removed = stranger.post("/household/contacts", {"action": "remove_contact",
                                                    "contact_id": contact_id})
    assert removed.status_code == 404
    with world.db() as db:
        assert db.get(EmergencyContact, contact_id).consent_status == "CONFIRMED"
    # the stranger's own device is not the first one, and vice versa
    assert owner.get(f"/d/{other['device_id']}/dashboard").status_code in (403, 404)


# ── the company never sees a password; staff never reach the console ─────────
def test_admin_never_sees_a_password_or_a_pin(world):
    from sentrix_portal.models import User
    owner = ready_world(world)
    admin = world.facts["admin"]
    household_id = world.facts["household_id"]
    with world.db() as db:
        hashed = db.query(User).filter_by(username="riya").one().password_hash
    pages = [admin.get(p) for p in ("/admin", "/admin/customers",
                                    f"/admin/customers/{household_id}", "/admin/devices",
                                    "/admin/audit")]
    for page in pages:
        assert page.status_code == 200, page.url
        for secret in (PASSWORD, hashed, SOS_PIN, DURESS_PIN, "$argon2"):
            assert secret not in page.text, (str(page.url), secret[:8])
    # "reset" re-sends an activation e-mail; the admin's page never shows the code
    admin.get(f"/admin/customers/{household_id}")
    reset = admin.post(f"/admin/customers/{household_id}/resend", {})
    assert reset.status_code in (200, 303), reset.text[:300]
    world.pump_portal()
    codes = [c for t in sent_to(world, OWNER_EMAIL) for c in
             __import__("re").findall(r"[A-Z]{4}-[A-Z]{4}-[A-Z]{4}", t)]
    assert codes, "the owner got a fresh activation code"
    follow = admin.get(f"/admin/customers/{household_id}")
    assert not any(code in follow.text or code in reset.text for code in codes)
    # staff are not tunnel roles
    device_id = world.facts["device_id"]
    assert admin.get(f"/d/{device_id}/dashboard").status_code == 403
    desk = staff(world, "desk1", "operator")
    assert desk.get(f"/d/{device_id}/events").status_code in (403, 404)
    # the profile on the device never carries Portal credentials
    pushed = repr(profile_ready(world))
    assert hashed not in pushed and "password" not in pushed


# ── test mode: nothing real leaves, even with every switch on ────────────────
def test_test_mode_refuses_live_sends(world):
    from sentrix.alerting import guard
    from sentrix_portal.services import notify
    owner = ready_world(world)
    assert guard.test_mode() and not guard.SendPolicy.from_env().live_alerts
    # The Portal on its default policy (PORTAL_TEST_MODE=1, SENTRIX_LIVE_ALERTS=0).
    notify.configure(transports={"telegram": world.tg, "email": world.mail})
    world.trigger("weapon", 3)
    rid = rid_of(world)
    mirrored(world, rid, "PENDING")
    world.pump_portal()
    rows = world.notifications(rid)
    assert rows and {r.status for r in rows} == {"SUPPRESSED"}, {r.status for r in rows}
    assert world.tg.sent == [] and world.mail.sent == []
    # The device's own guard: an emergency number or a number outside the
    # allowlist is refused even with both switches on.
    on = guard.SendPolicy(test_mode=True, live_alerts=True,
                          switches={c: True for c in guard.CHANNEL_SWITCHES},
                          allowlist=frozenset({OWNER_PHONE}))
    assert not guard.may_send("modem_sms", "112", policy=on)
    assert not guard.may_send("modem_sms", "+919800000000", policy=on)
    assert guard.may_send("modem_sms", OWNER_PHONE, policy=on)
    assert not guard.may_send("modem_sms", OWNER_PHONE, policy=guard.SendPolicy.from_env())
    # Nothing on any page offers 112 as a link in test mode.
    for path in (f"/sos/{rid}", "/home", f"/d/{world.facts['device_id']}/sos"):
        page = owner.get(path)
        assert "tel:112" not in page.text and "sms:112" not in page.text, path


# ── hostile text stays inert on the Desk, the SOS page and the console ───────
XSS = "\"'><script>alert(1)</script><img src=x onerror=alert(2)>"


def test_xss_in_household_data_is_inert_everywhere(world):
    from sentrix_portal.models import Camera, Household, User
    from sentrix_portal.services import accounts
    owner = ready_world(world)
    household_id = world.facts["household_id"]
    with world.db() as db:
        db.get(Household, household_id).landmark = XSS
        db.query(User).filter_by(username="riya").one().display_name = XSS
        db.query(Camera).filter_by(device_id=world.facts["device_id"]).one().room_label = XSS
        accounts.bump_profile_version(db, household_id)

    async def push():
        with world.db() as db:
            return await accounts.push_profile(db, household_id)
    world.loop.run(push())
    profile_ready(world)
    world.trigger("weapon", 3)
    rid = rid_of(world)
    assert "<script>" in world.device_request(rid)["sos_text_full"], "the text carries it"
    mirrored(world, rid, "PENDING")
    assert owner.post(f"/sos/{rid}/approve", {"authority": "POLICE"}).headers["location"] \
        .endswith("m=ok")
    mirrored(world, rid, "DISPATCHED")
    desk = staff(world, "desk1", "operator")
    pages = [desk.get(f"/desk/requests/{rid}"), desk.get("/desk"), owner.get(f"/sos/{rid}"),
             owner.get("/home"), owner.get("/household"),
             owner.get(f"/d/{world.facts['device_id']}/sos"),
             owner.get(f"/d/{world.facts['device_id']}/dashboard")]
    for page in pages:
        assert page.status_code == 200, page.url
        assert "<script>alert(1)" not in page.text, page.url
        assert "onerror=alert(2)>" not in page.text, page.url


# ── answers from Telegram buttons and e-mail action links ────────────────────
def test_owner_answers_from_telegram_and_the_email_link(world):
    from e2e.world import telegram_press
    ready_world(world)
    world.trigger("weapon", 3)
    rid = rid_of(world)
    mirrored(world, rid, "PENDING")
    world.pump_portal()
    buttons = {b.action: b for b in world.tg.sent[0][1].buttons}
    assert buttons["decline_page"].url.endswith(f"/sos/{rid}"), "a PIN never goes in a chat"
    # someone else's chat cannot press the owner's button
    refused = telegram_press(world, buttons["checking"], chat_id=99887766)
    assert refused["handled"] == "refused"
    checking = telegram_press(world, buttons["checking"])
    assert checking["handled"] == "answered", checking
    wait_for(lambda: "checking" in events_of(rid), what="checking on the device")
    assert device_state(world, rid) == "PENDING", "I'm checking pauses reminders only"

    # the e-mail's action link opens a confirmation page; only the POST acts
    token = [m for t in sent_to(world, OWNER_EMAIL, "t0")
             for m in re.findall(r"/a/([A-Za-z0-9_\-]+)", t)][0]
    stranger_mail = world.browser(ip="203.0.113.77")
    page = stranger_mail.get(f"/a/{token}")
    assert page.status_code == 200 and device_state(world, rid) == "PENDING"
    forged = world.browser(ip="203.0.113.78").post(f"/a/{token}", {"action": "approve"})
    assert forged.status_code == 403, "no CSRF, no action"
    done = stranger_mail.post(f"/a/{token}", {"action": "approve", "authority": "POLICE"})
    assert done.status_code == 200, done.text[:300]
    wait_for(lambda: device_state(world, rid) == "DISPATCHED", what="approved by link")
    again = stranger_mail.post(f"/a/{token}", {"action": "approve"})
    assert again.status_code == 404, "the link is single use"
    telegram_press(world, buttons["approve"])          # a late tap is idempotent
    assert events_of(rid).count("approve") == 1 and device_state(world, rid) == "DISPATCHED"
