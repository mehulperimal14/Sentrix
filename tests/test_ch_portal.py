"""Track T-CH on the Portal: the notifications outbox, the worker, the
Telegram bot (``/start`` linking, buttons) and ``POST /hooks/telegram``.

Transports are fakes; the Telegram API is ``httpx.MockTransport``; the SOS
entry point (``services.sos.respond``, T-LINK's) is replaced by a recorder.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from sentrix.alerting import guard
from sentrix.alerting.channels import telegram as T
from sentrix.alerting.channels.base import (SENT, SUPPRESSED, Button, FakeTransport, Message,
                                            Recipient, callback_data, new_callback_nonce)
from sentrix_portal.services import notify

TOKEN = "123456:AAH-secret_token"
CHAT = 12345


def live_policy():
    return guard.SendPolicy(test_mode=True, live_alerts=True,
                            switches={c: True for c in guard.CHANNEL_SWITCHES},
                            allowlist=frozenset({f"tg:{CHAT}", "owner@example.com"}))


@pytest.fixture(autouse=True)
def fresh_notify():
    notify.reset()
    yield
    notify.reset()
    notify._worker = None


@pytest.fixture
def fakes():
    transports = {"telegram": FakeTransport("telegram"), "email": FakeTransport("email")}
    notify.configure(transports=dict(transports), policy_factory=live_policy)
    return transports


class TelegramMock:
    """Records Bot API calls; answers ok (getUpdates: from ``updates``)."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.updates: list[list[dict]] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        if method == "getUpdates":
            if self.updates:
                return httpx.Response(200, json={"ok": True, "result": self.updates.pop(0)})
            await asyncio.sleep(0.05)
            return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.Response(200, json={"ok": True, "result": True})

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]


@pytest.fixture
def bot(portal_app):
    mock = TelegramMock()
    portal_app.state.telegram_api = T.AsyncTelegramApi(
        TOKEN, client=httpx.AsyncClient(transport=httpx.MockTransport(mock)))
    return mock


def household(db, **kw):
    from sentrix_portal.models import Household
    home = Household(customer_code="SX-0007", display_name="Demo", **kw)
    db.add(home)
    db.flush()
    return home


def owner_user(db, home, role="owner"):
    from sentrix_portal.models import User
    user = User(username=f"riya-{role}", display_name="Riya", role=role,
                household_id=home.id, status="active")
    db.add(user)
    db.flush()
    return user


def contact(db, home, status="CONFIRMED"):
    from sentrix_portal.models import EmergencyContact
    person = EmergencyContact(household_id=home.id, name="Asha", consent_status=status)
    db.add(person)
    db.flush()
    return person


def later(hours=1):
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def run(coro):
    return asyncio.run(coro)


# ── outbox ────────────────────────────────────────────────────────────────────
class TestPortalOutbox:
    def test_enqueue_in_the_callers_transaction_sealed_and_scoped(self, portal_app, fakes):
        from sentrix_portal.models import Notification
        with portal_app.state.db.session() as db:
            home = household(db)
            person = contact(db, home)
            recipient = Recipient(f"contact:{person.id}", "Asha", "telegram", f"tg:{CHAT}",
                                  "contact")
            message = Message(template="sos.full", step="contacts_t0", text="SOS",
                              request_id="r1")
            assert notify.enqueue(db, recipient, message)
            assert notify.enqueue(db, recipient, message) is None, "duplicate key"
            other = Recipient("owner", "Riya", "email", "owner@example.com")
            assert notify.enqueue(db, other, message), "the transaction is still usable"
        with portal_app.state.db.session() as db:
            row = db.query(Notification).filter_by(channel="telegram").one()
            assert row.household_id == home.id and row.recipient_masked == "tg:xx345"
            assert str(CHAT) not in (row.address_enc or "")
            assert db.query(Notification).count() == 2

    def test_a_rolled_back_caller_takes_its_message_with_it(self, portal_app, fakes):
        from sentrix_portal.models import Notification
        with pytest.raises(RuntimeError):
            with portal_app.state.db.session() as db:
                notify.enqueue(db, Recipient("owner", "R", "telegram", f"tg:{CHAT}"),
                               Message(template="x", step="s", text="t"))
                raise RuntimeError("the state change failed")
        with portal_app.state.db.session() as db:
            assert db.query(Notification).count() == 0

    def test_worker_pump_delivers_and_listeners_hear(self, portal_app, fakes):
        heard = []
        notify.add_listener(lambda item: heard.append((item.recipient.channel, item.status)))
        buttons = (Button("I'm handling it", "acknowledge",
                          callback_data=callback_data(new_callback_nonce(), "acknowledge")),
                   Button("Open", "open", url="https://p.example.in/c/tok"))
        with portal_app.state.db.session() as db:
            notify.enqueue(db, Recipient("contact:c1", "Asha", "telegram", f"tg:{CHAT}",
                                         "contact"),
                           Message(template="sos.full", step="contacts_t0", text="SOS",
                                   request_id="r1", buttons=buttons, urgency="HIGH"))
            notify.enqueue(db, Recipient("contact:c2", "Ravi", "telegram", "tg:999"),
                           Message(template="sos.full", step="contacts_t0", text="SOS",
                                   request_id="r1"))
        assert notify.worker_router(portal_app.state.db).pump() == 2
        assert sorted(heard) == [("telegram", SENT), ("telegram", SUPPRESSED)]
        recipient, message = fakes["telegram"].sent[0]
        assert recipient.role == "contact" and recipient.name == "Asha"
        assert message.buttons == buttons and message.urgency == "HIGH"
        assert message.text.startswith(guard.TEST_PREFIX)
        counts = notify.status(portal_app.state.db)["outbox"]
        assert counts[SENT] == 1 and counts[SUPPRESSED] == 1 and counts["depth"] == 0

    def test_cancel_in_the_callers_transaction(self, portal_app, fakes):
        with portal_app.state.db.session() as db:
            notify.enqueue(db, Recipient("owner", "R", "email", "owner@example.com"),
                           Message(template="sos.reminder", step="r120", text="t",
                                   request_id="r9"))
        with portal_app.state.db.session() as db:
            assert notify.cancel(db, "r9", "approved") == 1
        assert notify.worker_router(portal_app.state.db).pump() == 0
        assert fakes["email"].sent == []

    def test_default_guard_is_a_dry_run(self, portal_app):
        notify.configure(transports={"telegram": FakeTransport("telegram")})
        with portal_app.state.db.session() as db:
            notify.enqueue(db, Recipient("owner", "R", "telegram", f"tg:{CHAT}"),
                           Message(template="x", step="s", text="t"))
        notify.worker_router(portal_app.state.db).pump()
        recent = notify.worker_router(portal_app.state.db).recent()
        assert recent[0]["status"] == SUPPRESSED and "dry run" in recent[0]["error"]

    def test_channels_and_status(self, portal_app, monkeypatch):
        monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
        names = set(notify.build_channels())
        assert names == {"telegram", "email"}
        for name, value in (("TWILIO_ACCOUNT_SID", "AC1"), ("TWILIO_AUTH_TOKEN", "t"),
                            ("TWILIO_PHONE_NUMBER", "+15550000000")):
            monkeypatch.setenv(name, value)
        assert "twilio_sms" in notify.build_channels()
        block = notify.status()
        assert block["channels"]["telegram"]["ready"] is False and block["test_mode"]
        assert block["worker"] == {"running": False}

    def test_missing_arguments_are_refused_quietly(self, portal_app):
        assert notify.enqueue(None, None, None) is None


# ── /start linking ────────────────────────────────────────────────────────────
def start_update(code=None, chat_type="private", update_id=1, chat=CHAT):
    text = "/start" + (f" {code}" if code else "")
    return {"update_id": update_id, "message": {"text": text, "from": {"id": chat},
                                                "chat": {"id": chat, "type": chat_type}}}


def link_token(db, home, subject_type, subject_id, code="link-code-1", **kw):
    from sentrix_portal import security
    from sentrix_portal.models import ActionToken
    token = ActionToken(token_hash=security.token_digest(code), purpose="telegram_link",
                        household_id=home.id, subject_type=subject_type,
                        subject_id=subject_id, expires_at=kw.pop("expires_at", later()), **kw)
    db.add(token)
    db.flush()
    return token


class TestStartLinking:
    def test_links_a_user_chat_once(self, portal_app, fakes, bot):
        from sentrix_portal.models import AuditLog, Household, Notification, TelegramLink
        with portal_app.state.db.session() as db:
            home = household(db)
            user = owner_user(db, home)
            link_token(db, home, "user", user.id)
            home_id, user_id = home.id, user.id
        outcome = run(notify.handle_update(portal_app, start_update("link-code-1")))
        assert outcome == {"handled": "linked", "subject_type": "user"}
        with portal_app.state.db.session() as db:
            link = db.query(TelegramLink).one()
            assert (link.subject_id, link.chat_id, link.status) == (user_id, str(CHAT), "active")
            assert db.query(AuditLog).filter_by(event="telegram.linked").count() == 1
            assert db.get(Household, home_id).profile_version == 1
            reply = db.query(Notification).filter_by(template="telegram.linked").one()
            assert reply.recipient_ref == f"user:{user_id}" and "photos" in reply.body
        again = run(notify.handle_update(portal_app, start_update("link-code-1", update_id=2)))
        assert again["handled"] == "refused"

    def test_links_a_contact_and_records_the_chat_on_the_contact(self, portal_app, fakes, bot):
        from sentrix_portal.models import EmergencyContact, TelegramLink
        with portal_app.state.db.session() as db:
            home = household(db)
            person = contact(db, home)
            link_token(db, home, "contact", person.id)
            person_id = person.id
        run(notify.handle_update(portal_app, start_update("link-code-1")))
        with portal_app.state.db.session() as db:
            assert db.get(EmergencyContact, person_id).telegram_chat_id == str(CHAT)
            assert db.query(TelegramLink).filter_by(subject_type="contact").count() == 1

    def test_relinking_revokes_the_old_chat(self, portal_app, fakes, bot):
        from sentrix_portal.models import TelegramLink
        with portal_app.state.db.session() as db:
            home = household(db)
            user = owner_user(db, home)
            link_token(db, home, "user", user.id, code="first")
            link_token(db, home, "user", user.id, code="second")
        run(notify.handle_update(portal_app, start_update("first", update_id=1, chat=111)))
        run(notify.handle_update(portal_app, start_update("second", update_id=2, chat=222)))
        with portal_app.state.db.session() as db:
            states = {l.chat_id: l.status for l in db.query(TelegramLink)}
        assert states == {"111": "revoked", "222": "active"}

    @pytest.mark.parametrize("kw", [{"expires_at": datetime.now(timezone.utc) - timedelta(1)},
                                    {"revoked_at": datetime.now(timezone.utc)},
                                    {"used_at": datetime.now(timezone.utc)}])
    def test_unusable_codes_are_refused(self, portal_app, fakes, bot, kw):
        from sentrix_portal.models import Notification, TelegramLink
        with portal_app.state.db.session() as db:
            home = household(db)
            user = owner_user(db, home)
            link_token(db, home, "user", user.id, **kw)
        outcome = run(notify.handle_update(portal_app, start_update("link-code-1")))
        assert outcome["handled"] == "refused"
        with portal_app.state.db.session() as db:
            assert db.query(TelegramLink).count() == 0
            assert db.query(Notification).one().template == "telegram.link_failed"

    def test_start_without_a_code_explains(self, portal_app, fakes, bot):
        from sentrix_portal.models import Notification
        assert run(notify.handle_update(portal_app, start_update()))["handled"] == "help"
        with portal_app.state.db.session() as db:
            assert db.query(Notification).one().template == "telegram.help"

    def test_groups_are_left_never_linked(self, portal_app, fakes, bot):
        from sentrix_portal.models import TelegramLink
        with portal_app.state.db.session() as db:
            home = household(db)
            link_token(db, home, "user", owner_user(db, home).id)
        outcome = run(notify.handle_update(portal_app, start_update(
            "link-code-1", chat_type="group", chat=-100)))
        assert outcome["handled"] == "left_group" and bot.methods() == ["leaveChat"]
        added = {"update_id": 9, "my_chat_member": {"chat": {"id": -200, "type": "supergroup"},
                                                     "new_chat_member": {"status": "member"}}}
        assert run(notify.handle_update(portal_app, added))["handled"] == "left_group"
        with portal_app.state.db.session() as db:
            assert db.query(TelegramLink).count() == 0


# ── buttons ───────────────────────────────────────────────────────────────────
class SosRecorder:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"ok": True, "code": "ok", "status": 200, "message": "",
                                 "request": None}

    async def __call__(self, db, request_id, action, *, actor, channel, authority=None,
                       pin=None, detail=None):
        self.calls.append({"request_id": request_id, "action": action, "actor": actor,
                           "channel": channel, "pin": pin})
        return dict(self.result)


@pytest.fixture
def sos(monkeypatch):
    from sentrix_portal.services import sos as sos_service
    recorder = SosRecorder()
    monkeypatch.setattr(sos_service, "respond", recorder)
    return recorder


def callback_update(data, *, sender=CHAT, chat_type="private", update_id=10):
    return {"update_id": update_id, "callback_query": {
        "id": f"cb{update_id}", "data": data, "from": {"id": sender, "first_name": "Riya"},
        "message": {"chat": {"id": sender, "type": chat_type}, "message_id": 4}}}


@pytest.fixture
def linked(portal_app):
    """An owner with a linked chat and an action token with a nonce."""
    from sentrix_portal.models import ActionToken, TelegramLink
    nonce = new_callback_nonce()
    with portal_app.state.db.session() as db:
        home = household(db)
        user = owner_user(db, home)
        db.add(TelegramLink(subject_type="user", subject_id=user.id, household_id=home.id,
                            chat_id=str(CHAT), status="active"))
        token = ActionToken(token_hash="h" * 64, purpose="action_link", household_id=home.id,
                            request_id="req-1", subject_type="user", subject_id=user.id,
                            nonce=nonce, expires_at=later())
        db.add(token)
        db.flush()
        return {"nonce": nonce, "user_id": user.id, "token_id": token.id,
                "household_id": home.id}


def answers(bot):
    return [body.get("text") for method, body in bot.calls if method == "answerCallbackQuery"]


class TestCallbacks:
    def test_linked_owner_approves(self, portal_app, bot, sos, linked):
        from sentrix_portal.models import ActionToken, AuditLog
        data = callback_data(linked["nonce"], "approve")
        outcome = run(notify.handle_update(portal_app, callback_update(data)))
        assert outcome["handled"] == "answered" and outcome["action"] == "approve"
        (call,) = sos.calls
        assert call["request_id"] == "req-1" and call["channel"] == "telegram"
        assert call["actor"]["type"] == "owner" and call["actor"]["id"] == linked["user_id"]
        assert call["pin"] is None
        assert answers(bot) == [notify._ANSWERS["approve"]]
        with portal_app.state.db.session() as db:
            assert db.get(ActionToken, linked["token_id"]).used_at is not None
            row = db.query(AuditLog).filter_by(event="telegram.callback").one()
            assert row.detail["ok"] is True and row.detail["action"] == "approve"

    def test_a_second_approve_is_refused_but_checking_is_not_consuming(
            self, portal_app, bot, sos, linked):
        nonce = linked["nonce"]
        run(notify.handle_update(portal_app, callback_update(callback_data(nonce, "checking"),
                                                             update_id=1)))
        run(notify.handle_update(portal_app, callback_update(callback_data(nonce, "checking"),
                                                             update_id=2)))
        run(notify.handle_update(portal_app, callback_update(callback_data(nonce, "approve"),
                                                             update_id=3)))
        refused = run(notify.handle_update(portal_app, callback_update(
            callback_data(nonce, "approve"), update_id=4)))
        assert [c["action"] for c in sos.calls] == ["checking", "checking", "approve"]
        assert refused["reason"] == "already used" and answers(bot)[-1] == "Already answered."

    def test_only_the_linked_chat_may_press(self, portal_app, bot, sos, linked):
        from sentrix_portal.models import AuditLog
        outcome = run(notify.handle_update(portal_app, callback_update(
            callback_data(linked["nonce"], "approve"), sender=99999)))
        assert outcome["reason"] == "sender is not the linked chat" and sos.calls == []
        with portal_app.state.db.session() as db:
            row = db.query(AuditLog).filter_by(event="telegram.callback").one()
            assert row.detail["ok"] is False

    def test_private_chats_only(self, portal_app, bot, sos, linked):
        outcome = run(notify.handle_update(portal_app, callback_update(
            callback_data(linked["nonce"], "approve"), chat_type="group")))
        assert outcome["reason"] == "not a private chat" and sos.calls == []

    def test_expired_or_revoked_tokens(self, portal_app, bot, sos, linked):
        from sentrix_portal.models import ActionToken
        with portal_app.state.db.session() as db:
            db.get(ActionToken, linked["token_id"]).revoked_at = datetime.now(timezone.utc)
        outcome = run(notify.handle_update(portal_app, callback_update(
            callback_data(linked["nonce"], "approve"))))
        assert outcome["reason"] == "expired or revoked" and sos.calls == []

    def test_unknown_or_malformed_data(self, portal_app, bot, sos, linked):
        unknown = run(notify.handle_update(portal_app, callback_update(
            callback_data(new_callback_nonce(), "approve"), update_id=1)))
        malformed = run(notify.handle_update(portal_app, callback_update("x:z", update_id=2)))
        assert unknown["reason"] == "unknown nonce" and malformed["reason"].startswith("malformed")
        assert sos.calls == [] and len(answers(bot)) == 2

    def test_a_refused_answer_keeps_the_token(self, portal_app, bot, linked, monkeypatch):
        from sentrix_portal.models import ActionToken
        from sentrix_portal.services import sos as sos_service
        monkeypatch.setattr(sos_service, "respond", SosRecorder(
            {"ok": False, "code": "conflict", "status": 409, "message": "x", "request": None}))
        outcome = run(notify.handle_update(portal_app, callback_update(
            callback_data(linked["nonce"], "approve"))))
        assert outcome["handled"] == "failed"
        assert answers(bot) == ["This request was already answered."]
        with portal_app.state.db.session() as db:
            assert db.get(ActionToken, linked["token_id"]).used_at is None

    def test_contacts_act_as_contacts(self, portal_app, bot, sos):
        from sentrix_portal.models import ActionToken
        nonce = new_callback_nonce()
        with portal_app.state.db.session() as db:
            home = household(db)
            person = contact(db, home)
            person.telegram_chat_id = str(CHAT)
            db.add(ActionToken(token_hash="c" * 64, purpose="contact_page",
                               household_id=home.id, request_id="req-2",
                               subject_type="contact", subject_id=person.id, nonce=nonce,
                               expires_at=later()))
            person_id = person.id
        run(notify.handle_update(portal_app, callback_update(callback_data(nonce,
                                                                           "acknowledge"))))
        assert sos.calls[0]["actor"] == {"type": "contact", "id": person_id, "name": "Asha",
                                         "step_up_at": None}

    def test_redelivered_updates_act_once(self, portal_app, bot, sos, linked):
        update = callback_update(callback_data(linked["nonce"], "checking"), update_id=77)
        run(notify.handle_update(portal_app, update))
        assert run(notify.handle_update(portal_app, update)) == {"handled": "duplicate"}
        assert len(sos.calls) == 1


# ── the webhook ───────────────────────────────────────────────────────────────
class TestWebhook:
    def test_not_configured_is_not_found(self, portal_app, monkeypatch):
        monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
        response = TestClient(portal_app).post("/hooks/telegram", json={"update_id": 1})
        assert response.status_code == 404

    def test_secret_token_is_checked(self, portal_app, monkeypatch):
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "hook-secret")
        client = TestClient(portal_app)
        assert client.post("/hooks/telegram", json={"update_id": 1}).status_code == 401
        assert client.post("/hooks/telegram", json={"update_id": 1}, headers={
            T.SECRET_HEADER: "wrong"}).status_code == 401

    def test_accepted_update_is_handled(self, portal_app, monkeypatch, bot, sos, linked):
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "hook-secret")
        client = TestClient(portal_app)
        response = client.post("/hooks/telegram", headers={T.SECRET_HEADER: "hook-secret"},
                               json=callback_update(callback_data(linked["nonce"], "approve")))
        assert response.status_code == 200 and response.json()["handled"] == "answered"
        assert len(sos.calls) == 1

    def test_bad_bodies(self, portal_app, monkeypatch):
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "hook-secret")
        client = TestClient(portal_app)
        headers = {T.SECRET_HEADER: "hook-secret", "content-type": "application/json"}
        assert client.post("/hooks/telegram", content=b"{", headers=headers).status_code == 400
        assert client.post("/hooks/telegram", content=b"[1]",
                           headers=headers).status_code == 400
        big = json.dumps({"update_id": 1, "pad": "x" * 300_000}).encode()
        assert client.post("/hooks/telegram", content=big, headers=headers).status_code == 413
        ignored = client.post("/hooks/telegram", json={"update_id": 5}, headers=headers)
        assert ignored.status_code == 200 and ignored.json()["handled"] == "ignored"

    def test_handler_errors_still_answer_200(self, portal_app, monkeypatch):
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "hook-secret")

        async def boom(app, update):
            raise RuntimeError("db down")

        monkeypatch.setattr(notify, "handle_update", boom)
        response = TestClient(portal_app).post("/hooks/telegram", json={"update_id": 1},
                                               headers={T.SECRET_HEADER: "hook-secret"})
        assert response.status_code == 200 and response.json()["handled"] == "error"


# ── the background module ─────────────────────────────────────────────────────
class TestWorker:
    def test_polling_and_delivery(self, portal_app, fakes, bot, sos, linked, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
        bot.updates = [[callback_update(callback_data(linked["nonce"], "checking"),
                                        update_id=500)]]

        async def scenario():
            stop = notify.start(portal_app)
            try:
                with portal_app.state.db.session() as db:
                    notify.enqueue(db, Recipient("owner", "R", "telegram", f"tg:{CHAT}"),
                                   Message(template="sos.prompt", step="t0", text="hi",
                                           request_id="req-1"))
                for _ in range(200):
                    if fakes["telegram"].sent and sos.calls and len(
                            [m for m in bot.methods() if m == "getUpdates"]) >= 2:
                        break
                    await asyncio.sleep(0.02)
                health = portal_app.state.notify.health()
            finally:
                await stop()
            return health

        health = run(scenario())
        assert health["running"] and health["telegram"] == "polling"
        assert len(fakes["telegram"].sent) == 1 and sos.calls[0]["action"] == "checking"
        offsets = [body.get("offset") for method, body in bot.calls if method == "getUpdates"]
        assert offsets[0] is None and offsets[1] == 501
        assert notify._worker is None

    def test_webhook_mode_registers_the_https_url(self, portal_app, bot, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "hook-secret")
        portal_app.state.settings = replace(portal_app.state.settings, telegram_mode="webhook",
                                            base_url="https://portal.example.in")

        async def scenario():
            stop = notify.start(portal_app)
            for _ in range(100):
                if "setWebhook" in bot.methods():
                    break
                await asyncio.sleep(0.01)
            state = portal_app.state.notify.telegram_state
            await stop()
            return state

        assert run(scenario()) == "webhook"
        (body,) = [b for m, b in bot.calls if m == "setWebhook"]
        assert body["url"] == "https://portal.example.in/hooks/telegram"
        assert body["secret_token"] == "hook-secret" and "getUpdates" not in bot.methods()

    def test_no_token_means_no_telegram_work(self, portal_app, bot, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

        async def scenario():
            stop = notify.start(portal_app)
            await asyncio.sleep(0.05)
            state = portal_app.state.notify.telegram_state
            await stop()
            return state

        assert run(scenario()).startswith("off") and bot.calls == []

    def test_is_a_background_module(self):
        from sentrix_portal.app import BACKGROUND_MODULES
        assert "sentrix_portal.services.notify" in BACKGROUND_MODULES
