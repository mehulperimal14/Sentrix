"""Track T-CH: Telegram, Brevo, Resend and Twilio transports.

HTTP is ``httpx.MockTransport`` (nothing leaves the process; the conftest
socket guard would refuse it anyway); Twilio is a fake client factory.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import httpx
import pytest

from sentrix.alerting import guard
from sentrix.alerting.channels import email as E
from sentrix.alerting.channels import telegram as T
from sentrix.alerting.channels import twilio as W
from sentrix.alerting.channels.base import (Button, Channel, ChannelRouter, Message,
                                            Recipient, callback_data, new_callback_nonce)

TOKEN = "123456:AAH-secret_token"


class Recorder:
    """An httpx MockTransport handler that records requests and replies from
    a list (the last reply repeats)."""

    def __init__(self, *replies):
        self.replies = list(replies) or [(200, {"ok": True, "result": {"message_id": 7}})]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        status, body = reply
        return httpx.Response(status, json=body)

    def json(self, index=-1) -> dict:
        return json.loads(self.requests[index].content)


def telegram_api(recorder: Recorder, token=TOKEN) -> T.TelegramApi:
    return T.TelegramApi(token, client=httpx.Client(transport=httpx.MockTransport(recorder)))


def sos_message(step="t0", **kw) -> Message:
    nonce = new_callback_nonce()
    buttons = (Button("Alert my contacts and the SENTRIX desk", "approve",
                      callback_data=callback_data(nonce, "approve")),
               Button("I'm checking", "checking", callback_data=callback_data(nonce, "checking")),
               Button("False alarm (PIN)", "decline_page",
                      url="https://portal.example.in/a/tok"))
    return Message(template="sos.prompt", step=step, text="Possible HANDGUN in Living room.",
                   request_id="r1", buttons=buttons, **kw)


OWNER_TG = Recipient("owner", "Riya", "telegram", "tg:12345", "owner")


# ── Telegram ──────────────────────────────────────────────────────────────────
class TestTelegramTransport:
    def test_send_message_payload(self):
        recorder = Recorder()
        result = T.TelegramTransport(telegram_api(recorder)).send(OWNER_TG, sos_message())
        assert result.ok and result.provider_id == "tg:12345:7"
        request = recorder.requests[0]
        assert request.url.path == f"/bot{TOKEN}/sendMessage"
        body = recorder.json()
        assert body["chat_id"] == 12345 and "parse_mode" not in body
        assert body["link_preview_options"] == {"is_disabled": True}
        rows = body["reply_markup"]["inline_keyboard"]
        assert [list(r[0]) for r in rows] == [["text", "callback_data"],
                                              ["text", "callback_data"], ["text", "url"]]
        assert all(len(r[0].get("callback_data", "").encode()) <= 64 for r in rows)

    def test_only_text_and_buttons_never_media(self):
        recorder = Recorder()
        T.TelegramTransport(telegram_api(recorder)).send(OWNER_TG, sos_message(
            html="<img src=x>", subject="s"))
        methods = {r.url.path.rsplit("/", 1)[-1] for r in recorder.requests}
        assert methods == {"sendMessage"}
        assert set(recorder.json()) <= {"chat_id", "text", "reply_markup",
                                        "link_preview_options", "disable_web_page_preview"}

    def test_refused_url_button_is_folded_into_the_text(self):
        recorder = Recorder((400, {"ok": False, "error_code": 400,
                                   "description": "Bad Request: BUTTON_URL_INVALID"}),
                            (200, {"ok": True, "result": {"message_id": 8}}))
        message = sos_message()
        result = T.TelegramTransport(telegram_api(recorder)).send(OWNER_TG, message)
        assert result.ok and len(recorder.requests) == 2
        retry = recorder.json(1)
        assert "False alarm (PIN): https://portal.example.in/a/tok" in retry["text"]
        assert all("url" not in row[0] for row in retry["reply_markup"]["inline_keyboard"])

    @pytest.mark.parametrize("status,retryable", [(429, True), (500, True), (403, False),
                                                  (400, False), (401, False)])
    def test_error_mapping(self, status, retryable):
        recorder = Recorder((status, {"ok": False, "error_code": status,
                                      "description": "Forbidden: bot was blocked by the user",
                                      "parameters": {"retry_after": 3}}))
        result = T.TelegramTransport(telegram_api(recorder)).send(
            OWNER_TG, Message(template="x", step="s", text="t"))
        assert not result.ok and result.retryable is retryable

    def test_network_errors_are_retryable_and_never_show_the_token(self):
        recorder = Recorder(httpx.ConnectError(f"failed for /bot{TOKEN}/sendMessage"))
        result = T.TelegramTransport(telegram_api(recorder)).send(
            OWNER_TG, Message(template="x", step="s", text="t"))
        assert not result.ok and result.retryable and TOKEN not in result.error

    def test_groups_and_bad_addresses_are_refused_without_a_request(self):
        recorder = Recorder()
        transport = T.TelegramTransport(telegram_api(recorder))
        for address in ("tg:-1001234", "tg:abc", "12345"):
            result = transport.send(Recipient("owner", "R", "telegram", address),
                                    Message(template="x", step="s", text="t"))
            assert not result.ok and not result.retryable
        assert recorder.requests == []
        assert T.chat_id_of("tg:42") == 42 and T.chat_id_of("tg:0") is None

    def test_missing_token_is_reported_not_raised(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        transport = T.TelegramTransport(T.TelegramApi(client=httpx.Client(
            transport=httpx.MockTransport(Recorder()))))
        assert transport.state() == (False, "TELEGRAM_BOT_TOKEN not set")
        result = transport.send(OWNER_TG, Message(template="x", step="s", text="t"))
        assert not result.ok and not result.retryable

    def test_text_is_capped_at_telegrams_limit(self):
        recorder = Recorder()
        T.TelegramTransport(telegram_api(recorder)).send(
            OWNER_TG, Message(template="x", step="s", text="a" * 5000))
        assert len(recorder.json()["text"]) == T.MAX_TEXT

    def test_through_the_guard_in_test_mode(self, monkeypatch):
        recorder = Recorder()
        router = ChannelRouter({"telegram": Channel("telegram",
                                                    T.TelegramTransport(telegram_api(recorder)))})
        router.enqueue(OWNER_TG, sos_message())
        router.pump()
        assert recorder.requests == [], "dry run: Telegram is never called"
        for name in ("SENTRIX_LIVE_ALERTS", "SENTRIX_TELEGRAM_LIVE"):
            monkeypatch.setenv(name, "1")
        monkeypatch.setenv("SENTRIX_TEST_RECIPIENTS", "tg:12345")
        router.enqueue(OWNER_TG, sos_message(step="r120"))
        router.pump()
        assert recorder.json()["text"].startswith(guard.TEST_PREFIX)

    def test_httpx_log_lines_never_carry_the_token(self, caplog):
        record = logging.LogRecord("httpx", logging.INFO, __file__, 1,
                                   'HTTP Request: POST https://api.telegram.org/bot%s/getMe "200"',
                                   (TOKEN,), None)
        for f in logging.getLogger("httpx").filters:
            f.filter(record)
        assert TOKEN not in record.getMessage() and "/bot***" in record.getMessage()


class TestTelegramApi:
    def test_webhook_and_updates_payloads(self):
        recorder = Recorder((200, {"ok": True, "result": True}))
        api = telegram_api(recorder)
        api.set_webhook("https://portal.example.in/hooks/telegram", "s3cr3t_token")
        body = recorder.json()
        assert body["secret_token"] == "s3cr3t_token"
        assert body["allowed_updates"] == list(T.ALLOWED_UPDATES)
        with pytest.raises(ValueError):
            api.set_webhook("http://portal.localhost/hooks/telegram", "s3cr3t_token")
        with pytest.raises(ValueError):
            api.set_webhook("https://p.example.in/hooks/telegram", "bad secret!")
        recorder.replies = [(200, {"ok": True, "result": [{"update_id": 9}]})]
        assert api.get_updates(offset=9, timeout=1) == [{"update_id": 9}]
        assert recorder.json()["offset"] == 9

    def test_async_client(self):
        recorder = Recorder((200, {"ok": True, "result": True}))

        async def go():
            api = T.AsyncTelegramApi(TOKEN, client=httpx.AsyncClient(
                transport=httpx.MockTransport(recorder)))
            await api.answer_callback("cb1", "Help requested")
            await api.leave_chat(-100)
            await api.aclose()

        asyncio.run(go())
        assert [r.url.path.rsplit("/", 1)[-1] for r in recorder.requests] == [
            "answerCallbackQuery", "leaveChat"]
        assert recorder.json(0) == {"callback_query_id": "cb1", "show_alert": False,
                                    "text": "Help requested"}


class TestTelegramInbound:
    def test_callback(self):
        inbound = T.parse_update({"update_id": 5, "callback_query": {
            "id": "cb", "from": {"id": 12345, "first_name": "Riya"}, "data": "n:a",
            "message": {"chat": {"id": 12345, "type": "private"}}}})
        assert (inbound.kind, inbound.callback_id, inbound.from_id, inbound.data) == (
            "callback", "cb", 12345, "n:a")
        assert inbound.private and inbound.update_id == 5

    def test_start_with_and_without_code(self):
        def start(text, chat_type="private"):
            return T.parse_update({"update_id": 1, "message": {
                "text": text, "chat": {"id": 7, "type": chat_type}, "from": {"id": 7}}})
        assert start("/start abc_DEF-123").code == "abc_DEF-123"
        assert start("/start@SentrixBot abc").code == "abc"
        assert start("/start").code is None
        assert start("/start bad code!").code == ""
        assert start("hello").kind == "other"
        assert not start("/start x", "group").private

    def test_added_to_a_group(self):
        inbound = T.parse_update({"update_id": 2, "my_chat_member": {
            "chat": {"id": -100, "type": "supergroup"}, "from": {"id": 1},
            "new_chat_member": {"status": "member"}}})
        assert inbound.kind == "group"
        left = T.parse_update({"update_id": 3, "my_chat_member": {
            "chat": {"id": -100, "type": "group"}, "new_chat_member": {"status": "left"}}})
        assert left.kind == "other"

    @pytest.mark.parametrize("junk", [None, [], "x", {"callback_query": "x"},
                                      {"message": {"chat": {"id": "abc"}}}])
    def test_malformed_updates_never_raise(self, junk):
        assert T.parse_update(junk).kind in ("other", "callback")

    def test_deep_link_and_webhook_secret(self, monkeypatch):
        assert T.deep_link("@SentrixDemoBot", "abc-123") == \
            "https://t.me/SentrixDemoBot?start=abc-123"
        with pytest.raises(ValueError):
            T.deep_link("SentrixDemoBot", "has space")
        monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
        assert not T.webhook_secret_ok("anything")
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "abc")
        assert T.webhook_secret_ok("abc") and not T.webhook_secret_ok("abd")
        assert not T.webhook_secret_ok(None)


# ── e-mail ────────────────────────────────────────────────────────────────────
OWNER_MAIL = Recipient("owner", "Riya Sharma", "email", "Owner@Example.com", "owner")


def mail_client(recorder):
    return httpx.Client(transport=httpx.MockTransport(recorder))


class TestEmail:
    def test_brevo_payload(self):
        recorder = Recorder((201, {"messageId": "<abc@brevo>"}))
        provider = E.BrevoProvider(client=mail_client(recorder), api_key="brevo-key",
                                   sender=("SENTRIX", "alerts@example.in"))
        result = E.EmailTransport(provider).send(OWNER_MAIL, sos_message())
        assert result.ok and result.provider_id == "<abc@brevo>"
        request = recorder.requests[0]
        assert str(request.url) == E.BREVO_URL and request.headers["api-key"] == "brevo-key"
        body = recorder.json()
        assert body["sender"] == {"name": "SENTRIX", "email": "alerts@example.in"}
        assert body["to"] == [{"email": "owner@example.com", "name": "Riya Sharma"}]
        assert "False alarm (PIN): https://portal.example.in/a/tok" in body["textContent"]
        assert 'href="https://portal.example.in/a/tok"' in body["htmlContent"]
        assert "callback" not in body["htmlContent"], "Telegram-only buttons are not links"

    def test_resend_payload_and_idempotency_key(self):
        recorder = Recorder((200, {"id": "re_123"}))
        provider = E.ResendProvider(client=mail_client(recorder), api_key="re-key",
                                    sender=("SENTRIX Alerts", "alerts@example.in"))
        message = sos_message()
        result = E.EmailTransport(provider).send(OWNER_MAIL, message)
        assert result.ok and result.provider_id == "re_123"
        request = recorder.requests[0]
        assert request.headers["authorization"] == "Bearer re-key"
        assert request.headers["idempotency-key"] == message.dedupe_key(OWNER_MAIL)
        body = recorder.json()
        assert body["from"] == "SENTRIX Alerts <alerts@example.in>"
        assert body["to"] == ["owner@example.com"] and body["text"] and body["html"]

    @pytest.mark.parametrize("status,retryable", [(400, False), (401, False), (429, True),
                                                  (503, True)])
    def test_error_mapping(self, status, retryable):
        recorder = Recorder((status, {"message": "nope"}))
        provider = E.ResendProvider(client=mail_client(recorder), api_key="k",
                                    sender=("S", "a@example.in"))
        result = provider.send("x@example.in", "", "s", "t", "<p>t</p>")
        assert not result.ok and result.retryable is retryable and "nope" in result.error

    def test_network_error_is_retryable(self):
        recorder = Recorder(httpx.ReadTimeout("slow"))
        provider = E.BrevoProvider(client=mail_client(recorder), api_key="k",
                                   sender=("S", "a@example.in"))
        result = provider.send("x@example.in", "", "s", "t", "<p>t</p>")
        assert not result.ok and result.retryable

    def test_unconfigured_providers(self, monkeypatch):
        monkeypatch.delenv("SENTRIX_EMAIL_FROM", raising=False)
        monkeypatch.delenv("SENTRIX_EMAIL_PROVIDER", raising=False)
        assert E.provider_from_env() is None and E.provider_from_env("smtp") is None
        transport = E.EmailTransport()
        assert transport.state()[0] is False
        assert not transport.send(OWNER_MAIL, sos_message()).retryable
        provider = E.provider_from_env("brevo")
        assert provider.configured() == (False, "BREVO_API_KEY not set")
        monkeypatch.setenv("BREVO_API_KEY", "k")
        assert provider.configured() == (False, "SENTRIX_EMAIL_FROM not set")
        monkeypatch.setenv("SENTRIX_EMAIL_FROM", "SENTRIX <alerts@example.in>")
        assert provider.configured() == (True, "brevo")
        assert E.sender_from_env() == ("SENTRIX", "alerts@example.in")

    def test_bodies_escape_and_carry_the_banner(self):
        text = guard.prepare_text("email", "Riya <script>alert(1)</script> & co\n\nSecond",
                                  policy=guard.SendPolicy())
        subject, plain, document = E.render_bodies(Message(
            template="sos.prompt", step="t0", text=text,
            buttons=(Button("Open", "open", url="https://p.example.in/a/t?x=1&y=2"),)))
        assert subject.startswith("[SENTRIX DEMO - TEST] SENTRIX: Riya")
        assert "<script>" not in document and "&lt;script&gt;" in document
        assert guard.TEST_PREFIX in document and plain.startswith(guard.TEST_PREFIX)
        assert 'href="https://p.example.in/a/t?x=1&amp;y=2"' in document
        assert "confirmation page" in document

    def test_callers_html_gets_the_banner_too(self):
        text = guard.prepare_text("email", "hi", policy=guard.SendPolicy())
        _, _, document = E.render_bodies(Message(template="x", step="s", text=text,
                                                 html="<p>hi</p>", subject="Consent"))
        assert document.index(guard.TEST_PREFIX) < document.index("<p>hi</p>")
        assert E.render_bodies(Message(template="x", step="s", text="hi",
                                       subject="Consent"))[0] == "Consent"


# ── Twilio ────────────────────────────────────────────────────────────────────
class FakeTwilioClient:
    def __init__(self, error=None):
        self.error = error
        self.sent = []
        self.messages = SimpleNamespace(create=self._create("sms"))
        self.calls = SimpleNamespace(create=self._create("call"))

    def _create(self, kind):
        def create(**kwargs):
            if self.error is not None:
                raise self.error
            self.sent.append((kind, kwargs))
            return SimpleNamespace(sid=f"{kind.upper()}123")
        return create


def creds():
    return W.TwilioCredentials("AC1", "tok", "+15550000000")


class TwilioError(Exception):
    def __init__(self, status, code, msg):
        super().__init__(msg)
        self.status, self.code, self.msg = status, code, msg


class TestTwilio:
    def test_sms_and_call(self):
        client = FakeTwilioClient()
        built = []
        factory = lambda c: built.append(c.sid) or client  # noqa: E731
        sms = W.TwilioSmsTransport(credentials=creds, client_factory=factory)
        call = W.TwilioCallTransport(credentials=creds, client_factory=factory)
        target = Recipient("owner", "R", "twilio_sms", "98123 45610")
        assert sms.send(target, Message(template="legacy.l4_sms", step="l4:1",
                                        text="L4")).provider_id == "SMS123"
        assert call.send(target, Message(template="legacy.l5_call", step="l5:1",
                                         text="weapon <confirmed> & more")).ok
        (_, first), (_, second) = client.sent
        assert first == {"body": "L4", "from_": "+15550000000", "to": "+919812345610"}
        assert "&lt;confirmed&gt; &amp; more" in second["twiml"]
        assert built == ["AC1", "AC1"]

    def test_no_client_without_credentials(self):
        built = []
        transport = W.TwilioSmsTransport(credentials=W.TwilioCredentials,
                                         client_factory=lambda c: built.append(c))
        assert transport.state()[0] is False
        result = transport.send(Recipient("owner", "R", "twilio_sms", "+919812345610"),
                                Message(template="x", step="s", text="t"))
        assert not result.ok and not result.retryable and built == []

    @pytest.mark.parametrize("status,code,retryable", [(400, 21211, False), (429, 20429, True),
                                                       (500, 20500, True), (None, None, True)])
    def test_error_mapping(self, status, code, retryable):
        error = TwilioError(status, code, "The 'To' number is not valid")
        transport = W.TwilioSmsTransport(credentials=creds,
                                         client_factory=lambda c: FakeTwilioClient(error))
        result = transport.send(Recipient("owner", "R", "twilio_sms", "+919812345610"),
                                Message(template="x", step="s", text="t"))
        assert not result.ok and result.retryable is retryable

    def test_keys_alone_send_nothing_through_the_router(self, monkeypatch):
        client = FakeTwilioClient()
        router = ChannelRouter({"twilio_sms": Channel("twilio_sms", W.TwilioSmsTransport(
            credentials=creds, client_factory=lambda c: client))})
        router.enqueue(Recipient("owner", "R", "twilio_sms", "+919812345610"),
                       Message(template="legacy.l4_sms", step="l4:1", text="x"))
        router.pump()
        assert client.sent == [] and router.outbox.items()[0].status == "SUPPRESSED"
