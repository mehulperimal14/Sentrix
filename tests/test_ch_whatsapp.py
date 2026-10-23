"""WhatsApp through Meta's Cloud API (D17): the send guard, the transport
(templates, session messages, errors), the webhook parser and signature, and
on the Portal the SOS recipients, quick replies, links and delivery reports.

The Graph API is ``httpx.MockTransport``; the SOS entry point
(``services.sos.respond``) is a recorder. Nothing leaves the machine.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from sentrix.alerting import guard
from sentrix.alerting.channels import whatsapp as W
from sentrix.alerting.channels.base import (DELIVERED, FAILED, SENT, Button, FakeTransport,
                                            Message, Recipient, callback_data,
                                            new_callback_nonce)
from sentrix_portal.services import notify

OWNER = "+919812345610"
CONTACT = "+919812345611"
STRANGER = "+919812345699"
NUMBER_ID = "1234567890"
SECRET = "app-secret"


def live_policy():
    return guard.SendPolicy(test_mode=True, live_alerts=True,
                            switches={c: True for c in guard.CHANNEL_SWITCHES},
                            allowlist=frozenset({OWNER, CONTACT}))


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "EAAG-secret-token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", NUMBER_ID)
    monkeypatch.setenv("WHATSAPP_APP_SECRET", SECRET)
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify-me")


class Graph:
    """Records Graph API calls; answers each with the next scripted reply."""

    def __init__(self, *replies):
        self.calls: list[dict] = []
        self.headers: list[dict] = []
        self.urls: list[str] = []
        self.replies = list(replies)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(json.loads(request.content))
        self.headers.append(dict(request.headers))
        self.urls.append(str(request.url))
        if self.replies:
            status, body = self.replies.pop(0)
            return httpx.Response(status, json=body)
        return httpx.Response(200, json={"messages": [{"id": f"wamid.{len(self.calls)}"}]})


def transport(graph: Graph) -> W.WhatsAppTransport:
    client = httpx.Client(transport=httpx.MockTransport(graph))
    return W.WhatsAppTransport(W.WhatsAppApi(client=client))


def error(code: int, status: int = 400) -> tuple[int, dict]:
    return status, {"error": {"message": "nope", "code": code,
                              "error_data": {"details": f"details {code}"}}}


def prompt_buttons(nonce="n" * 16, page="https://portal.example/sos/req-1"):
    return (Button("Alert my contacts and the SENTRIX desk", "approve",
                   callback_data=callback_data(nonce, "approve")),
            Button("I'm checking", "checking", callback_data=callback_data(nonce, "checking")),
            Button("False alarm (PIN)", "decline_page", url=page))


def owner(address=OWNER):
    return Recipient("owner", "Riya", "whatsapp", address, "owner")


def banner(text: str) -> str:
    return guard.prepare_text("whatsapp", text, policy=live_policy())


# ── the send guard ────────────────────────────────────────────────────────────
class TestGuard:
    def test_its_own_switch_and_the_allowlist(self):
        policy = guard.SendPolicy(test_mode=True, live_alerts=True,
                                  switches={"whatsapp": False}, allowlist=frozenset({OWNER}))
        assert not guard.may_send("whatsapp", OWNER, policy=policy)
        assert "SENTRIX_WHATSAPP_LIVE" in guard.may_send("whatsapp", OWNER, policy=policy).reason
        assert guard.may_send("whatsapp", OWNER, policy=live_policy())
        assert not guard.may_send("whatsapp", STRANGER, policy=live_policy())

    def test_numbers_are_phones(self):
        assert guard.normalize_address("whatsapp", "98123 45610") == OWNER
        refused = guard.may_send("whatsapp", "112", policy=live_policy())
        assert not refused and "emergency" in refused.reason
        assert not guard.may_send("whatsapp", "tg:12345", policy=live_policy())

    def test_dry_run_by_default(self):
        assert not guard.may_send("whatsapp", OWNER)


# ── the transport ─────────────────────────────────────────────────────────────
class TestTemplates:
    def test_owner_prompt_uses_the_prompt_template(self, configured):
        graph = Graph()
        text = banner("Possible weapon at H-212.\n\nOpen the request:   now")
        result = transport(graph).send(owner(), Message("sos.prompt", "t0", text,
                                                        request_id="req-1",
                                                        buttons=prompt_buttons()))
        assert result.ok and result.provider_id == "wamid.1"
        (call,) = graph.calls
        assert graph.urls[0] == f"https://graph.facebook.com/v23.0/{NUMBER_ID}/messages"
        assert graph.headers[0]["authorization"] == "Bearer EAAG-secret-token"
        assert call["to"] == "919812345610" and call["type"] == "template"
        template = call["template"]
        assert template["name"] == "sentrix_prompt" and template["language"] == {"code": "en"}
        body, *buttons = template["components"]
        param = body["parameters"][0]["text"]
        assert param == "Possible weapon at H-212. · Open the request: now"
        assert guard.TEST_PREFIX not in param, "the template body carries the banner"
        assert [b["parameters"][0]["payload"] for b in buttons] == [
            "n" * 16 + ":a", "n" * 16 + ":c", "open:2"]
        assert [b["index"] for b in buttons] == ["0", "1", "2"]

    def test_contact_alert_and_plain_notice(self, configured):
        graph = Graph()
        nonce = new_callback_nonce()
        contact_buttons = (Button("I'm handling it", "acknowledge",
                                  callback_data=callback_data(nonce, "acknowledge")),
                           Button("Open", "open", url="https://portal.example/c/x"))
        send = transport(graph).send
        assert send(owner(CONTACT), Message("sos.full", "contacts_t0", banner("Help!"),
                                            buttons=contact_buttons)).ok
        assert send(owner(), Message("sos.resolved", "resolved", banner("Closed."))).ok
        contact_call, notice_call = graph.calls
        assert contact_call["template"]["name"] == "sentrix_contact"
        payloads = [c["parameters"][0]["payload"]
                    for c in contact_call["template"]["components"][1:]]
        assert payloads == [f"{nonce}:k", "open:1"]
        assert notice_call["template"]["name"] == "sentrix_notice"
        assert len(notice_call["template"]["components"]) == 1

    def test_a_template_button_without_a_matching_action_says_none(self):
        payloads = W.template_payloads(W.PROMPT, prompt_buttons()[:1])
        assert payloads == ["n" * 16 + ":a", W.UNUSED, W.UNUSED]

    def test_names_and_language_from_the_environment(self, configured, monkeypatch):
        monkeypatch.setenv("WHATSAPP_TEMPLATE_NOTICE", "sx_notice_v2")
        monkeypatch.setenv("WHATSAPP_TEMPLATE_LANG", "en_US")
        monkeypatch.setenv("WHATSAPP_API_VERSION", "24.0")
        graph = Graph()
        transport(graph).send(owner(), Message("sos.resolved", "resolved", banner("x")))
        assert graph.calls[0]["template"]["name"] == "sx_notice_v2"
        assert graph.calls[0]["template"]["language"] == {"code": "en_US"}
        assert "/v24.0/" in graph.urls[0]

    def test_template_bodies_obey_whatsapp_rules(self):
        for spec in W.TEMPLATES.values():
            body = spec["body"]
            assert body.startswith(guard.TEST_PREFIX) and body.count("{{1}}") == 1
            assert not body.endswith("}}") and not body.startswith("{{")
            assert len(spec["buttons"]) <= 3
            assert all(len(label) <= W.MAX_BUTTON_TITLE for label, _ in spec["buttons"])


class TestSessionMessages:
    def test_session_mode_sends_reply_buttons_and_folds_links(self, configured,
                                                              monkeypatch):
        monkeypatch.setenv("WHATSAPP_MODE", "session")
        graph = Graph()
        assert transport(graph).send(owner(), Message("sos.prompt", "t0", banner("Weapon?"),
                                                      buttons=prompt_buttons())).ok
        (call,) = graph.calls
        assert call["type"] == "interactive"
        interactive = call["interactive"]
        assert interactive["body"]["text"].startswith(guard.TEST_PREFIX)
        assert "False alarm (PIN): https://portal.example/sos/req-1" in interactive["body"]["text"]
        titles = [b["reply"]["title"] for b in interactive["action"]["buttons"]]
        assert titles == ["Alert contacts", "I'm checking"]
        assert interactive["action"]["buttons"][0]["reply"]["id"] == "n" * 16 + ":a"

    def test_replies_are_always_session_text(self, configured):
        graph = Graph()
        transport(graph).send(owner(), Message("whatsapp.reply", "wa:1", banner("Noted.")))
        assert graph.calls[0]["type"] == "text"
        assert graph.calls[0]["text"]["body"].startswith(guard.TEST_PREFIX)

    def test_auto_falls_back_when_the_template_is_refused(self, configured, monkeypatch):
        monkeypatch.setenv("WHATSAPP_MODE", "auto")
        graph = Graph(error(132001))
        result = transport(graph).send(owner(), Message("sos.prompt", "t0", banner("x"),
                                                        buttons=prompt_buttons()))
        assert result.ok and [c["type"] for c in graph.calls] == ["template", "interactive"]

    def test_template_mode_does_not_fall_back(self, configured):
        graph = Graph(error(132001))
        result = transport(graph).send(owner(), Message("sos.prompt", "t0", banner("x")))
        assert not result.ok and not result.retryable and len(graph.calls) == 1


class TestErrors:
    @pytest.mark.parametrize("reply, retryable", [
        (error(131047), False),          # outside the 24-hour window
        (error(131030), False),          # not in the test number's recipient list
        (error(190, 401), False),        # expired token
        (error(130429, 429), True),      # rate limit
        (error(131000, 500), True),
    ])
    def test_retryable_or_not(self, configured, reply, retryable):
        result = transport(Graph(reply)).send(owner(), Message("sos.prompt", "t0", "x"))
        assert not result.ok and result.retryable is retryable
        assert "EAAG-secret-token" not in (result.error or "")

    def test_not_configured(self):
        result = W.WhatsAppTransport().send(owner(), Message("sos.prompt", "t0", "x"))
        assert not result.ok and not result.retryable
        assert W.WhatsAppTransport().state()[0] is False and not W.configured()

    def test_network_error_is_retryable(self, configured):
        def broken(request):
            raise httpx.ConnectError("down")
        client = httpx.Client(transport=httpx.MockTransport(broken))
        result = W.WhatsAppTransport(W.WhatsAppApi(client=client)).send(
            owner(), Message("sos.prompt", "t0", "x"))
        assert not result.ok and result.retryable

    def test_a_refused_guard_never_reaches_meta(self, configured):
        from sentrix.alerting.channels.base import Channel
        graph = Graph()
        channel = Channel("whatsapp", transport(graph))      # default policy: dry run
        result = channel.deliver(owner(), Message("sos.prompt", "t0", "x"))
        assert result.suppressed and graph.calls == []


def test_flatten():
    text = "a\tb\n\n  c     d\r\ne" + "x" * 1000
    flat = W.flatten(text, limit=40)
    assert "\n" not in flat and "\t" not in flat and "     " not in flat
    assert flat.startswith("a b · c d · e") and len(flat) == 40 and flat.endswith("…")


# ── inbound ───────────────────────────────────────────────────────────────────
def webhook(*, messages=(), statuses=(), number_id=NUMBER_ID):
    return {"object": "whatsapp_business_account", "entry": [{"id": "waba", "changes": [{
        "field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "15550001234", "phone_number_id": number_id},
            "contacts": [{"profile": {"name": "Riya"}, "wa_id": OWNER[1:]}],
            "messages": list(messages), "statuses": list(statuses)}}]}]}


def button(payload, *, sender=OWNER, mid="wamid.in1", context="wamid.out1"):
    return {"from": sender[1:], "id": mid, "timestamp": "1", "type": "button",
            "button": {"payload": payload, "text": "Alert contacts"},
            "context": {"from": "15550001234", "id": context}}


class TestParse:
    def test_every_kind(self):
        body = webhook(messages=[
            button("abc:a"),
            {"from": OWNER[1:], "id": "wamid.in2", "type": "interactive",
             "interactive": {"type": "button_reply", "button_reply": {"id": "abc:c",
                                                                      "title": "x"}}},
            {"from": OWNER[1:], "id": "wamid.in3", "type": "text", "text": {"body": "hi"}},
            {"from": OWNER[1:], "id": "wamid.in4", "type": "image"}],
            statuses=[{"id": "wamid.out1", "status": "failed", "recipient_id": OWNER[1:],
                       "errors": [{"code": 131047, "title": "Re-engagement message"}]}])
        events = W.parse_webhook(body)
        assert [e.kind for e in events] == ["button", "button", "text", "other", "status"]
        first = events[0]
        assert (first.sender, first.payload, first.context_id, first.name) == (
            OWNER, "abc:a", "wamid.out1", "Riya")
        assert events[1].payload == "abc:c" and events[2].text == "hi"
        assert events[4].status == "failed" and events[4].error.startswith("131047")

    def test_not_whatsapp(self):
        assert W.parse_webhook({"object": "page"}) == []
        assert W.parse_webhook(None) == []

    def test_signature(self):
        body = b'{"a": 1}'
        good = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        assert W.signature_ok(body, good, SECRET)
        assert not W.signature_ok(body + b" ", good, SECRET)
        assert not W.signature_ok(body, good, "")
        assert not W.signature_ok(body, "sha1=abc", SECRET)

    def test_subscription_handshake(self, configured):
        assert W.verify_subscription("subscribe", "verify-me", "123") == "123"
        assert W.verify_subscription("subscribe", "wrong", "123") is None
        assert W.verify_subscription("other", "verify-me", "123") is None


# ── the Portal ────────────────────────────────────────────────────────────────
def run(coro):
    return asyncio.run(coro)


def later(hours=1):
    return datetime.now(timezone.utc) + timedelta(hours=hours)


@pytest.fixture(autouse=True)
def fresh_notify():
    notify.reset()
    yield
    notify.reset()
    notify._worker = None


@pytest.fixture
def fakes():
    transports = {name: FakeTransport(name) for name in ("telegram", "whatsapp", "email")}
    notify.configure(transports=dict(transports), policy_factory=live_policy)
    return transports


class SosRecorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, db, request_id, action, *, actor, channel, authority=None,
                       pin=None, detail=None):
        self.calls.append({"request_id": request_id, "action": action, "actor": actor,
                           "channel": channel})
        return {"ok": True, "code": "ok", "status": 200, "message": "", "request": None}


@pytest.fixture
def sos(monkeypatch):
    from sentrix_portal.services import sos as sos_service
    recorder = SosRecorder()
    monkeypatch.setattr(sos_service, "respond", recorder)
    return recorder


@pytest.fixture
def home(portal_app):
    """A household with an owner (mobile number) and a confirmed contact."""
    from sentrix_portal import security
    from sentrix_portal.models import EmergencyContact, Household, User
    with portal_app.state.db.session() as db:
        house = Household(customer_code="SX-0007", display_name="Demo")
        db.add(house)
        db.flush()
        user = User(username="riya", display_name="Riya", role="owner", household_id=house.id,
                    status="active", phone_enc=security.seal(OWNER))
        person = EmergencyContact(household_id=house.id, name="Asha", consent_status="CONFIRMED",
                                  phone_enc=security.seal(CONTACT))
        db.add_all([user, person])
        db.flush()
        return {"household_id": house.id, "user_id": user.id, "contact_id": person.id}


def token(portal_app, home, *, subject="user", purpose="action_link"):
    from sentrix_portal.models import ActionToken
    nonce = new_callback_nonce()
    subject_id = home["user_id"] if subject == "user" else home["contact_id"]
    with portal_app.state.db.session() as db:
        row = ActionToken(token_hash=hashlib.sha256(nonce.encode()).hexdigest(),
                          purpose=purpose, household_id=home["household_id"],
                          request_id="req-1", subject_type=subject, subject_id=subject_id,
                          nonce=nonce, expires_at=later())
        db.add(row)
        db.flush()
        return nonce, row.id


def replies(portal_app) -> list[tuple[str, str]]:
    from sentrix_portal.models import Notification
    from sentrix_portal import security
    with portal_app.state.db.session() as db:
        rows = (db.query(Notification).filter_by(channel="whatsapp", template="whatsapp.reply")
                .order_by(Notification.created_at).all())
        return [(security.unseal(r.address_enc), r.body) for r in rows]


class TestRecipients:
    def test_owner_and_contact_on_whatsapp_once_configured(self, portal_app, home,
                                                           configured):
        from sentrix_portal.models import AssistanceRequest
        from sentrix_portal.services import sos as sos_service
        with portal_app.state.db.session() as db:
            _, recipients = sos_service._owner_recipients_for(db, home["household_id"])
            assert [(r.channel, r.address) for r in recipients] == [("whatsapp", OWNER)]
            row = AssistanceRequest(id="req-1", household_id=home["household_id"],
                                    state="PENDING", incident_type="weapon")
            db.add(row)
            db.flush()
            contacts = sos_service._contact_recipients(db, row)
            assert [(r.channel, r.address) for _, rs in contacts for r in rs] == [
                ("whatsapp", CONTACT)]

    def test_nothing_on_whatsapp_unless_configured(self, portal_app, home):
        from sentrix_portal.services import sos as sos_service
        with portal_app.state.db.session() as db:
            _, recipients = sos_service._owner_recipients_for(db, home["household_id"])
            assert recipients == []

    def test_the_owner_can_switch_it_off(self, portal_app, home, configured):
        from sentrix_portal.models import Household
        from sentrix_portal.services import sos as sos_service
        with portal_app.state.db.session() as db:
            db.get(Household, home["household_id"]).channels = {"whatsapp": False}
            db.flush()
            assert sos_service._owner_recipients_for(db, home["household_id"])[1] == []

    def test_a_new_contact_gets_the_consent_link_on_whatsapp(self, portal_app, home,
                                                            configured, fakes):
        from sentrix_portal import security
        from sentrix_portal.models import EmergencyContact, Notification
        from sentrix_portal.services import accounts
        with portal_app.state.db.session() as db:
            person = db.get(EmergencyContact, home["contact_id"])
            link, _ = accounts.request_contact_consent(db, person, owner_name="Riya")
        with portal_app.state.db.session() as db:
            (row,) = db.query(Notification).filter_by(channel="whatsapp",
                                                      template="contact.consent").all()
            assert link in row.body and security.unseal(row.address_enc) == CONTACT

    def test_the_owner_prompt_carries_the_three_buttons(self, portal_app, home, configured,
                                                        fakes):
        from sentrix_portal.models import AssistanceRequest, Notification
        from sentrix_portal.services import sos as sos_service
        with portal_app.state.db.session() as db:
            row = AssistanceRequest(id="req-1", household_id=home["household_id"],
                                    state="PENDING", incident_type="weapon")
            db.add(row)
            db.flush()
            sos_service._send_owner(db, row, step="t0", template="sos.prompt",
                                    text="Possible weapon.", subject="SENTRIX",
                                    with_buttons=True)
        with portal_app.state.db.session() as db:
            (note,) = db.query(Notification).filter_by(channel="whatsapp").all()
            buttons = note.meta["buttons"]
            assert [b["action"] for b in buttons] == ["approve", "checking", "decline_page"]
            assert buttons[0]["callback_data"].endswith(":a")
            assert buttons[2]["url"].endswith("/sos/req-1")


class TestInbound:
    def test_the_owner_approves_from_their_number(self, portal_app, home, fakes, sos,
                                                  configured):
        nonce, token_id = token(portal_app, home)
        outcome = run(notify.handle_whatsapp(portal_app, webhook(messages=[
            button(callback_data(nonce, "approve"))])))
        assert outcome == [{"handled": "answered", "action": "approve", "code": "ok",
                            "text": notify._ANSWERS["approve"]}]
        (call,) = sos.calls
        assert call["channel"] == "whatsapp" and call["actor"]["id"] == home["user_id"]
        assert replies(portal_app) == [(OWNER, notify._ANSWERS["approve"])]
        from sentrix_portal.models import ActionToken, AuditLog
        with portal_app.state.db.session() as db:
            assert db.get(ActionToken, token_id).used_at is not None
            audit = db.query(AuditLog).filter_by(event="whatsapp.reply").one()
            assert audit.detail["ok"] is True and OWNER not in json.dumps(audit.detail)

    def test_another_number_cannot_answer(self, portal_app, home, fakes, sos, configured):
        nonce, _ = token(portal_app, home)
        outcome = run(notify.handle_whatsapp(portal_app, webhook(messages=[
            button(callback_data(nonce, "approve"), sender=STRANGER)])))
        assert outcome[0]["handled"] == "refused" and sos.calls == []

    def test_a_contact_acknowledges(self, portal_app, home, fakes, sos, configured):
        nonce, _ = token(portal_app, home, subject="contact", purpose="contact_page")
        run(notify.handle_whatsapp(portal_app, webhook(messages=[
            button(callback_data(nonce, "acknowledge"), sender=CONTACT)])))
        assert sos.calls[0]["actor"]["type"] == "contact"

    def test_redelivered_messages_act_once(self, portal_app, home, fakes, sos, configured):
        nonce, _ = token(portal_app, home)
        body = webhook(messages=[button(callback_data(nonce, "checking"))])
        run(notify.handle_whatsapp(portal_app, body))
        assert run(notify.handle_whatsapp(portal_app, body)) == [{"handled": "duplicate"}]
        assert len(sos.calls) == 1

    def test_open_answers_with_the_link_for_its_recipient_only(self, portal_app, home, fakes,
                                                               configured):
        with portal_app.state.db.session() as db:
            notify.enqueue(db, owner(), Message("sos.prompt", "t0", banner("Weapon?"),
                                                request_id="req-1",
                                                buttons=prompt_buttons()))
        notify.worker_router(portal_app.state.db).pump()
        from sentrix_portal.models import Notification
        with portal_app.state.db.session() as db:
            sent = db.query(Notification).filter_by(channel="whatsapp").one()
            sent_id = sent.provider_id
        run(notify.handle_whatsapp(portal_app, webhook(messages=[
            button("open:2", context=sent_id)])))
        run(notify.handle_whatsapp(portal_app, webhook(messages=[
            button("open:2", context=sent_id, sender=STRANGER, mid="wamid.in9")])))
        got = replies(portal_app)
        assert got[0] == (OWNER, "False alarm (PIN): https://portal.example/sos/req-1")
        assert got[1][0] == STRANGER and "not valid" in got[1][1]

    def test_a_text_gets_the_help_reply(self, portal_app, fakes, configured):
        outcome = run(notify.handle_whatsapp(portal_app, webhook(messages=[
            {"from": OWNER[1:], "id": "wamid.t1", "type": "text", "text": {"body": "hi"}}])))
        assert outcome == [{"handled": "help"}]
        assert replies(portal_app) == [(OWNER, notify._WA_HELP)]

    def test_another_sender_number_is_ignored(self, portal_app, fakes, sos, configured):
        outcome = run(notify.handle_whatsapp(portal_app, webhook(
            messages=[button("x:a")], number_id="999")))
        assert outcome == [{"handled": "ignored", "reason": "another sender number"}]

    @pytest.mark.parametrize("status, final", [("delivered", DELIVERED), ("failed", FAILED)])
    def test_delivery_reports(self, portal_app, home, fakes, configured, status, final):
        with portal_app.state.db.session() as db:
            notify.enqueue(db, owner(), Message("sos.prompt", "t0", banner("Weapon?"),
                                                request_id="req-1"))
        notify.worker_router(portal_app.state.db).pump()
        from sentrix_portal.models import Notification
        with portal_app.state.db.session() as db:
            row = db.query(Notification).filter_by(channel="whatsapp").one()
            assert row.status == SENT
            sent_id = row.provider_id
        run(notify.handle_whatsapp(portal_app, webhook(statuses=[
            {"id": sent_id, "status": status, "recipient_id": OWNER[1:],
             "errors": [{"code": 131026, "title": "undeliverable"}] if status == "failed"
             else []}])))
        with portal_app.state.db.session() as db:
            assert db.query(Notification).filter_by(channel="whatsapp").one().status == final


class TestWebhookRoutes:
    def test_not_configured_is_not_found(self, portal_app):
        client = TestClient(portal_app)
        assert client.get("/hooks/whatsapp").status_code == 404
        assert client.post("/hooks/whatsapp", json={}).status_code == 404

    def test_handshake(self, portal_app, configured):
        client = TestClient(portal_app)
        ok = client.get("/hooks/whatsapp", params={"hub.mode": "subscribe",
                                                   "hub.verify_token": "verify-me",
                                                   "hub.challenge": "1158201444"})
        assert ok.status_code == 200 and ok.text == "1158201444"
        assert client.get("/hooks/whatsapp", params={
            "hub.mode": "subscribe", "hub.verify_token": "nope",
            "hub.challenge": "1"}).status_code == 403

    def test_signed_bodies_only(self, portal_app, home, fakes, sos, configured):
        nonce, _ = token(portal_app, home)
        body = json.dumps(webhook(messages=[button(callback_data(nonce, "approve"))])).encode()
        client = TestClient(portal_app)
        unsigned = client.post("/hooks/whatsapp", content=body,
                               headers={"content-type": "application/json"})
        assert unsigned.status_code == 401 and sos.calls == []
        signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        signed = client.post("/hooks/whatsapp", content=body,
                             headers={"content-type": "application/json",
                                      W.SIGNATURE_HEADER: signature})
        assert signed.status_code == 200 and signed.json()["handled"] == ["answered"]
        assert len(sos.calls) == 1
