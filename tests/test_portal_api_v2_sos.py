"""Portal API v2: owner SOS (list, one, answers, snapshot), action links,
the contact's consent and SOS pages, the Desk, a device's offline mirror.

No device is connected, so every answer takes the cloud path (the device
path is the same ``services.sos.respond``). Test mode: the call target is
the simulated Desk, never ``tel:112``; after a duress PIN the owner's JSON
reads exactly like a normal false alarm (docs/7 §7.8, docs/9 §6)."""

from __future__ import annotations

from sentrix_portal.models import AssistanceRequest, AuditLog, EmergencyContact
from test_acct_support import query
from test_link_support import DESK_PHONE, DURESS_PIN, PIN
from test_portal_api_v2_support import (Api, api_as, assert_no_secrets, error_of, mint,
                                        open_request, sos_world)  # noqa: F401

#: Owner-view fields that may differ between two requests of the same kind.
PER_REQUEST = {"id", "short_id", "created_at", "updated_at", "expires_at", "seen_at",
               "incident_ids", "sos_text"}


def _state(app, rid: str) -> str:
    with app.state.db.session() as db:
        return db.get(AssistanceRequest, rid).state


# ── owner pages ───────────────────────────────────────────────────────────────
def test_list_detail_and_seen(sos_world):
    app, seeded, _ = sos_world
    rid = open_request(app, seeded, snapshot=True)
    owner = api_as(app, seeded.owner_id)
    listing = owner.get("/sos").json()
    assert [r["id"] for r in listing["open"]] == [rid] and listing["recent"] == []
    assert owner.get("/sos/open").json()["requests"][0]["state"] == "PENDING"
    detail = owner.get(f"/sos/{rid}")
    data = detail.json()
    assert data["request"]["state"] == "PENDING"
    assert data["request"]["label"] == "Waiting for your answer"
    assert data["request"]["actions"] == ["approve", "call_112", "checking", "decline"]
    assert data["request"]["remaining_s"] > 0 and data["request"]["created_at"].endswith("Z")
    assert data["request"]["seen_at"], "opening it marks the prompt seen"
    assert data["default_authority"] == "POLICE" and len(data["authorities"]) == 3
    assert data["footer"] == "SENTRIX does not contact 112 itself."
    assert data["snapshot_url"] == f"/api/v2/sos/{rid}/snapshot.jpg"
    snapshot = owner.get(f"/sos/{rid}/snapshot.jpg")
    assert snapshot.content == b"\xff\xd8jpeg"
    assert snapshot.headers["content-type"] == "image/jpeg"
    assert snapshot.headers["cache-control"] == "no-store"
    assert all("hash" not in event and "prev_hash" not in event for event in data["events"])
    assert_no_secrets(app, detail)


def test_test_mode_never_offers_tel_112(sos_world, tmp_path):
    app, seeded, _ = sos_world
    rid = open_request(app, seeded)
    owner = api_as(app, seeded.owner_id)
    call = owner.get(f"/sos/{rid}").json()["call"]
    assert call == {"href": f"tel:{DESK_PHONE}", "test_mode": True,
                    "label": f"Call the SENTRIX Desk ({DESK_PHONE})"}
    session = owner.get("/session").json()["test_mode"]
    assert session["on"] and session["desk_number"] == DESK_PHONE
    assert DESK_PHONE in session["banner"]
    from sentrix_portal.services.sos_views import call_112
    from sentrix_portal.settings import PortalSettings
    for number in ("112", "", "+91 112"):
        target = call_112(PortalSettings.for_tests(tmp_path, desk_number=number))
        assert target["href"] is None and "tel:112" not in str(target)
    assert "tel:112" not in owner.get(f"/sos/{rid}").text


def test_answers_need_csrf_and_take_the_cloud_path(sos_world):
    app, seeded, _ = sos_world
    rid = open_request(app, seeded)
    owner = api_as(app, seeded.owner_id)
    refused = owner.post(f"/sos/{rid}/answer", {"action": "approve"}, csrf=False)
    assert refused.status_code == 403 and error_of(refused)["code"] == "csrf"
    checking = owner.post(f"/sos/{rid}/answer", {"action": "checking"})
    assert checking.status_code == 200 and checking.json()["message"] == "Done."
    bad = owner.post(f"/sos/{rid}/answer", {"action": "dance"})
    assert bad.status_code == 422 and error_of(bad)["code"] == "validation"
    approved = owner.post(f"/sos/{rid}/answer", {"action": "approve", "authority": "FIRE"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["request"]["state"] == "DISPATCHED"
    assert approved.json()["request"]["authority"] == "FIRE"
    again = owner.post(f"/sos/{rid}/answer", {"action": "checking"})
    assert again.status_code == 409 and error_of(again)["code"] == "conflict"
    assert error_of(again)["message"] == ("This request has moved on; the page shows where "
                                          "it is now.")


def test_wrong_pin_is_a_field_error_and_is_recorded(sos_world):
    app, seeded, _ = sos_world
    rid = open_request(app, seeded)
    member = api_as(app, seeded.member_id)
    wrong = member.post(f"/sos/{rid}/answer", {"action": "decline", "pin": "000000"})
    assert wrong.status_code == 422
    assert error_of(wrong)["fields"] == {"pin": "That PIN is not right."}
    owner = api_as(app, seeded.owner_id)
    events = [e["event"] for e in owner.get(f"/sos/{rid}").json()["events"]]
    assert "pin_failed" in events, "the wrong PIN was committed before the error"
    declined = member.post(f"/sos/{rid}/answer", {"action": "decline", "pin": PIN})
    assert declined.json()["request"]["state"] == "DECLINED"
    assert declined.json()["request"]["actions"] == ["help_now"]


def test_help_now_opens_a_linked_request(sos_world):
    app, seeded, _ = sos_world
    rid = open_request(app, seeded)
    owner = api_as(app, seeded.owner_id)
    owner.post(f"/sos/{rid}/answer", {"action": "decline", "pin": PIN})
    later = owner.post(f"/sos/{rid}/answer", {"action": "help_now", "authority": "MEDICAL"})
    assert later.status_code == 200
    new_id = later.json()["request_id"]
    assert new_id != rid and later.json()["request"]["parent_request_id"] == rid
    assert later.json()["request"]["authority"] == "MEDICAL"


def test_duress_reads_exactly_like_a_false_alarm(sos_world):
    app, seeded, _ = sos_world
    owner = api_as(app, seeded.owner_id)
    real = open_request(app, seeded)
    real_answer = owner.post(f"/sos/{real}/answer", {"action": "decline", "pin": PIN}).json()
    coerced = open_request(app, seeded)
    duress_answer = owner.post(f"/sos/{coerced}/answer",
                               {"action": "decline", "pin": DURESS_PIN}).json()
    assert _state(app, coerced) == "DISPATCHED", "help is on its way, silently"

    def same(a: dict, b: dict) -> None:
        assert {k: v for k, v in a.items() if k not in PER_REQUEST} == \
            {k: v for k, v in b.items() if k not in PER_REQUEST}

    def timeline(events: list[dict]) -> list[tuple]:
        def detail(d: dict) -> dict:
            return {k: v for k, v in d.items()
                    if k not in ("expires_at", "incident_id", "incident_ids")}
        return [(e["seq"], e["event"], e["from_state"], e["to_state"], e["actor_type"],
                 e["channel"], e["origin"], detail(e["detail"]), e["restricted"])
                for e in events]

    assert duress_answer["message"] == real_answer["message"] == "Done."
    same(duress_answer["request"], real_answer["request"])
    assert duress_answer["request"]["label"] == "Marked as false alarm"
    assert timeline(duress_answer["events"]) == timeline(real_answer["events"])

    # The Desk takes it on: the owner still sees a false alarm.
    desk = api_as(app, seeded.operator_id, totp=True)
    card = desk.get(f"/desk/requests/{coerced}").json()
    assert card["request"]["duress"] is True
    assert card["request"]["banner"] == f"Do not go to the home. Call {DESK_PHONE}."
    assert desk.post(f"/desk/requests/{coerced}/acknowledge").status_code == 200
    assert desk.post(f"/desk/requests/{coerced}/resolve",
                     {"outcome": "real_threat"}).status_code == 200
    views = {}
    for rid in (real, coerced):
        detail = owner.get(f"/sos/{rid}")
        views[rid] = detail.json()
        assert "duress" not in detail.text.lower() and "Do not go" not in detail.text
    same(views[coerced]["request"], views[real]["request"])
    assert timeline(views[coerced]["events"]) == timeline(views[real]["events"])
    listing = owner.get("/sos").json()
    assert {r["state"] for r in listing["recent"]} == {"DECLINED"} and listing["open"] == []
    assert owner.get("/sos/open").json()["requests"] == []
    assert owner.get("/home").json()["requests"] == []
    audit = owner.get("/household/audit", params={"limit": 200}).text
    assert "duress" not in audit.lower()


def test_role_refusals_on_owner_sos(sos_world):
    app, seeded, _ = sos_world
    rid = open_request(app, seeded)
    anonymous = Api(app)
    assert error_of(anonymous.get(f"/sos/{rid}"))["code"] == "not_signed_in"
    desk = api_as(app, seeded.operator_id, totp=True)
    assert error_of(desk.get("/sos"))["code"] == "forbidden"
    stranger = api_as(app, seeded.extra["stranger_id"])
    assert stranger.get(f"/sos/{rid}").status_code == 404
    assert stranger.post(f"/sos/{rid}/answer", {"action": "checking"}).status_code == 404
    assert stranger.get("/sos").json()["open"] == []
    assert stranger.get(f"/sos/{rid}/snapshot.jpg").status_code == 404


# ── action links ──────────────────────────────────────────────────────────────
def test_action_link_answers_once(sos_world):
    app, seeded, _ = sos_world
    rid = open_request(app, seeded)
    token = mint(app, purpose="action_link", request_id=rid, subject_type="user",
                 subject_id=seeded.owner_id)
    link = Api(app)
    page = link.get(f"/action/{token}")
    assert page.status_code == 200
    data = page.json()
    assert data["can_act"] and data["request"]["state"] == "PENDING"
    assert data["call"]["href"] == f"tel:{DESK_PHONE}"
    assert page.cookies.get("portal_csrf") == data["csrf_token"]
    assert _state(app, rid) == "PENDING", "opening the link changes nothing"
    assert any(r.event == "action_link.opened" for r in query(app, AuditLog))
    link.csrf = data["csrf_token"]
    assert link.post(f"/action/{token}", {"action": "approve"}, csrf=False).status_code == 403
    decline = link.post(f"/action/{token}", {"action": "decline"})
    assert decline.status_code == 422, "a link never declines (that needs the PIN)"
    approved = link.post(f"/action/{token}", {"action": "approve", "authority": "POLICE"})
    assert approved.status_code == 200 and approved.json()["request"]["state"] == "DISPATCHED"
    used = link.post(f"/action/{token}", {"action": "approve"})
    assert used.status_code == 404 and "work once" in error_of(used)["message"]
    assert link.get(f"/action/{token}").status_code == 404


# ── the contact's pages ───────────────────────────────────────────────────────
def test_consent_page_confirm(sos_world):
    from sentrix_portal.services import accounts
    app, seeded, _ = sos_world
    with app.state.db.session() as db:
        contact = EmergencyContact(household_id=seeded.household_id, name="Bee",
                                   relation="friend", priority=2, consent_status="PENDING")
        db.add(contact)
        db.flush()
        raw, _ = accounts.mint_consent_token(db, contact)
        contact_id = contact.id
    api = Api(app)
    page = api.get(f"/consent/{raw}")
    assert page.status_code == 200
    data = page.json()
    assert data["contact_name"] == "Bee" and data["owner_name"] == "Riya Sharma (demo)"
    assert data["status"] == "PENDING" and len(data["purposes"]) == 7
    assert "Green Avenue" not in page.text, "never the address or the home"
    api.csrf = data["csrf_token"]
    assert api.post(f"/consent/{raw}", {"decision": "confirm"}, csrf=False).status_code == 403
    unanswered = api.post(f"/consent/{raw}", {})
    assert error_of(unanswered)["fields"] == {"decision": "Confirm or opt out."}
    done = api.post(f"/consent/{raw}", {"decision": "confirm"})
    assert done.status_code == 200, done.text
    assert done.json()["decision"] == "confirm" and "/consent/" in done.json()["opt_out_link"]
    assert query(app, EmergencyContact, id=contact_id)[0].consent_status == "CONFIRMED"
    assert api.get(f"/consent/{raw}").status_code == 404, "the link worked once"
    opt_out = done.json()["opt_out_link"].rsplit("/", 1)[1]
    assert api.get(f"/consent/{opt_out}").json()["status"] == "CONFIRMED"
    declined = api.post(f"/consent/{opt_out}", {"decision": "decline"})
    assert declined.json() == {"decision": "decline", "telegram_link": None,
                               "opt_out_link": None}


def test_contact_sos_page(sos_world):
    app, seeded, _ = sos_world
    rid = open_request(app, seeded, approved=True, snapshot=True)
    token = mint(app, purpose="contact_page", request_id=rid, subject_type="contact",
                 subject_id=seeded.contact_id)
    page_api = Api(app)
    page = page_api.get(f"/contact-page/{token}")
    assert page.status_code == 200
    data = page.json()
    assert data["contact_name"] == "Arjun (demo)" and data["resident"] == "Riya Sharma (demo)"
    assert data["address"].startswith("H.No. 212") and data["map_url"]
    assert data["can_acknowledge"] and not data["can_approve"]
    assert data["call"]["href"] == f"tel:{DESK_PHONE}"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert data["request"]["duress"] is False and data["resident_phone"]
    snapshot = page_api.get(f"/contact-page/{token}/snapshot.jpg")
    assert snapshot.content == b"\xff\xd8jpeg"
    page_api.csrf = data["csrf_token"]
    handled = page_api.post(f"/contact-page/{token}", {"action": "acknowledge"})
    assert handled.status_code == 200 and handled.json()["request"]["state"] == "ACKNOWLEDGED"
    assert handled.json()["message"] == ("Thank you. The resident and the SENTRIX desk can see "
                                         "it.")
    assert any(r.event == "contact_page.acknowledge" for r in query(app, AuditLog))
    assert error_of(Api(app).get("/contact-page/nonsense"))["code"] == "not_found"


def test_contact_page_under_duress(sos_world):
    app, seeded, _ = sos_world
    rid = open_request(app, seeded)
    owner = api_as(app, seeded.owner_id)
    owner.post(f"/sos/{rid}/answer", {"action": "decline", "pin": DURESS_PIN})
    token = mint(app, purpose="contact_page", request_id=rid, subject_type="contact",
                 subject_id=seeded.contact_id)
    data = Api(app).get(f"/contact-page/{token}").json()
    assert data["request"]["duress"] is True
    assert data["request"]["banner"] == f"Do not go to the home. Call {DESK_PHONE}."
    assert data["resident_phone"] is None, "never call the resident first"


# ── the Desk ──────────────────────────────────────────────────────────────────
def test_desk_queue_card_and_answers(sos_world):
    app, seeded, _ = sos_world
    rid = open_request(app, seeded, approved=True, snapshot=True)
    open_request(app, seeded)                         # still prompting: not the Desk's
    desk = api_as(app, seeded.operator_id, totp=True)
    queue = desk.get("/desk/queue").json()
    assert [c["id"] for c in queue["requests"]] == [rid] and queue["unacknowledged"] == 1
    assert queue["requests"][0]["resident"] == "Riya Sharma (demo)"
    card = desk.get(f"/desk/requests/{rid}")
    data = card.json()
    assert data["address"].startswith("H.No. 212") and data["resident_phone"]
    assert data["home"]["adults"] == 2 and data["room"] == "Living room"
    assert data["call"]["href"] == f"tel:{DESK_PHONE}"
    assert [o["value"] for o in data["outcomes"]][0] == "help_arrived"
    assert desk.get(f"/desk/requests/{rid}/snapshot.jpg").content == b"\xff\xd8jpeg"
    assert_no_secrets(app, card)
    assert desk.post(f"/desk/requests/{rid}/acknowledge", csrf=False).status_code == 403
    ack = desk.post(f"/desk/requests/{rid}/acknowledge")
    assert ack.status_code == 200 and ack.json()["request"]["acknowledged"] is True
    note = desk.post(f"/desk/requests/{rid}/called-112")
    assert note.status_code == 200
    events = desk.get(f"/desk/requests/{rid}").json()["events"]
    assert any(e["detail"].get("kind") == "desk_called_112" for e in events)
    no_outcome = desk.post(f"/desk/requests/{rid}/resolve", {})
    assert no_outcome.status_code == 422
    assert error_of(no_outcome)["fields"] == {"outcome": "Choose an outcome."}
    closed = desk.post(f"/desk/requests/{rid}/resolve", {"outcome": "help_arrived",
                                                         "note": "all well"})
    assert closed.status_code == 200 and closed.json()["request"]["state"] == "CLOSED"


def test_desk_role_rules(sos_world):
    app, seeded, _ = sos_world
    rid = open_request(app, seeded, approved=True)
    pending = open_request(app, seeded)
    owner = api_as(app, seeded.owner_id)
    assert error_of(owner.get("/desk/queue"))["code"] == "forbidden"
    no_totp = api_as(app, seeded.operator_id)
    assert error_of(no_totp.get("/desk/queue"))["code"] == "totp_required"
    desk = api_as(app, seeded.operator_id, totp=True)
    assert desk.get(f"/desk/requests/{pending}").status_code == 404, "not on the help path"
    assert error_of(desk.post(f"/sos/{rid}/answer", {"action": "approve"}))["code"] == \
        "forbidden"


# ── the offline mirror ────────────────────────────────────────────────────────
def test_device_mirror(sos_world):
    app, seeded, _ = sos_world
    rid = open_request(app, seeded)
    owner = api_as(app, seeded.owner_id)
    mirror = owner.get(f"/devices/{seeded.device_id}/mirror").json()
    assert mirror["online"] is False and mirror["can_label"] is True
    assert mirror["incidents"][0]["incident_type"] == "weapon"
    assert [r["id"] for r in mirror["requests"]] == [rid]
    desk = api_as(app, seeded.operator_id, totp=True)
    assert desk.get(f"/devices/{seeded.device_id}/mirror").status_code == 403, \
        "the Desk only while a request is on the help path"
    stranger = api_as(app, seeded.extra["stranger_id"])
    assert stranger.get(f"/devices/{seeded.device_id}/mirror").status_code == 404


def test_help_now_after_duress_opens_a_new_request_like_a_decline(sos_world):
    app, seeded, _ = sos_world
    owner = api_as(app, seeded.owner_id)
    real = open_request(app, seeded)
    owner.post(f"/sos/{real}/answer", {"action": "decline", "pin": PIN})
    real_later = owner.post(f"/sos/{real}/answer", {"action": "help_now"}).json()
    coerced = open_request(app, seeded)
    owner.post(f"/sos/{coerced}/answer", {"action": "decline", "pin": DURESS_PIN})
    later = owner.post(f"/sos/{coerced}/answer", {"action": "help_now"})
    assert later.status_code == 200
    body = later.json()
    assert body["request_id"] != coerced, "a new linked request, as after a real decline"
    assert body["request"]["parent_request_id"] == coerced
    assert body["request"]["state"] == real_later["request"]["state"] == "DISPATCHED"
    assert [e["event"] for e in owner.get(f"/sos/{coerced}").json()["events"]] == \
        [e["event"] for e in owner.get(f"/sos/{real}").json()["events"]]
    desk = api_as(app, seeded.operator_id, totp=True)
    assert desk.get(f"/desk/requests/{coerced}").json()["request"]["duress"] is True
    # The new request looks ordinary to the household but carries the banner.
    child = desk.get(f"/desk/requests/{body['request_id']}").json()["request"]
    assert child["duress"] is True and child["banner"]
