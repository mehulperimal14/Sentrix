"""Track T-CH: the channel router, its outbox and the device ``channels`` service.

Every transport here is a fake; the send guard is either the real one (dry run,
forced by tests/conftest.py) or an explicit test policy. An injectable clock
drives retries and back-off.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import replace

import pytest

from sentrix.alerting import guard
from sentrix.alerting.channels.base import (DELIVERED, FAILED, QUEUED, SENDING, SENT,
                                            SUPPRESSED, Channel, FakeTransport, MemoryOutbox,
                                            Message, OutboxItem, Recipient, SendResult)
from sentrix.alerting.channels import router as R

TEAM = "+919812345610"
STRANGER = "+919800000000"


def live_policy(*allow: str):
    return lambda: guard.SendPolicy(test_mode=True, live_alerts=True,
                                    switches={c: True for c in guard.CHANNEL_SWITCHES},
                                    allowlist=frozenset(allow or (TEAM,)))


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def owner(channel="modem_sms", address=TEAM, ref="owner"):
    return Recipient(ref, "Riya", channel, address, "owner")


def message(step="t0", request_id="r1", template="sos.prompt", text="Need help?", **kw):
    return Message(template=template, step=step, text=text, request_id=request_id, **kw)


def make_router(transport=None, *, clock=None, outbox=None, policy=None, channel="modem_sms",
                **kw):
    clock = clock or Clock()
    transport = transport or FakeTransport(channel)
    router = R.Router({channel: Channel(channel, transport,
                                        policy_factory=policy or live_policy())},
                      outbox if outbox is not None else MemoryOutbox(), clock=clock, **kw)
    return router, transport, clock


class Flaky:
    """Fails with the given results in order, then succeeds."""

    def __init__(self, *results: SendResult, name="modem_sms", delivers_on_send=False):
        self.name = name
        self.results = list(results)
        self.sent = []
        self.delivers_on_send = delivers_on_send

    def send(self, recipient, message):
        if self.results:
            return self.results.pop(0)
        self.sent.append((recipient, message))
        return SendResult(True, provider_id=f"p{len(self.sent)}")


# ── the send guard (D13) ──────────────────────────────────────────────────────
class TestGuardInFrontOfEveryTransport:
    def test_default_environment_is_a_dry_run(self):
        router, fake, _ = make_router(policy=guard.SendPolicy.from_env)
        router.enqueue(owner(), message())
        router.pump()
        item = router.outbox.items()[0]
        assert item.status == SUPPRESSED and fake.attempts == 0
        assert "dry run" in item.error

    @pytest.mark.parametrize("env,reason", [
        ({"SENTRIX_LIVE_ALERTS": "1"}, "SENTRIX_MODEM_LIVE is off"),
        ({"SENTRIX_MODEM_LIVE": "1"}, "dry run"),
        ({"SENTRIX_LIVE_ALERTS": "1", "SENTRIX_MODEM_LIVE": "1"}, "not in SENTRIX_TEST"),
    ])
    def test_two_switches_and_the_allowlist(self, monkeypatch, env, reason):
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        router, fake, _ = make_router(policy=guard.SendPolicy.from_env)
        router.enqueue(owner(), message())
        router.pump()
        item = router.outbox.items()[0]
        assert item.status == SUPPRESSED and reason in item.error and fake.attempts == 0

    def test_both_switches_and_allowlisted_get_the_banner(self, monkeypatch):
        monkeypatch.setenv("SENTRIX_LIVE_ALERTS", "1")
        monkeypatch.setenv("SENTRIX_MODEM_LIVE", "1")
        monkeypatch.setenv("SENTRIX_TEST_RECIPIENTS", "98123 45610")
        router, fake, _ = make_router(policy=guard.SendPolicy.from_env)
        router.enqueue(owner(), message())
        router.enqueue(owner(address=STRANGER, ref="contact:1"), message())
        router.pump()
        statuses = {i.recipient.ref: i.status for i in router.outbox.items()}
        assert statuses == {"owner": SENT, "contact:1": SUPPRESSED}
        assert fake.sent[0][1].text.startswith(guard.TEST_PREFIX_GSM)

    def test_test_mode_off_still_needs_both_switches(self, monkeypatch):
        monkeypatch.setenv("SENTRIX_TEST_MODE", "0")
        monkeypatch.setenv("SENTRIX_LIVE_ALERTS", "1")
        router, fake, _ = make_router(policy=guard.SendPolicy.from_env)
        router.enqueue(owner(address=STRANGER), message())
        router.pump()
        assert router.outbox.items()[0].status == SUPPRESSED and fake.attempts == 0

    def test_emergency_numbers_are_always_refused(self):
        router, fake, _ = make_router(policy=live_policy("112"))
        router.enqueue(owner(address="112"), message())
        router.pump()
        item = router.outbox.items()[0]
        assert item.status == SUPPRESSED and "emergency" in item.error and fake.attempts == 0


# ── outbox: dedupe, retries, back-off, give-up ────────────────────────────────
class TestOutbox:
    def test_dedupe_per_request_recipient_channel_and_step(self):
        router, _, _ = make_router()
        assert router.enqueue(owner(), message("t0"))
        assert router.enqueue(owner(), message("t0")) is None
        assert router.enqueue(owner(), message("r120"))
        assert router.enqueue(owner(ref="contact:1"), message("t0"))
        assert router.enqueue(owner(), message("t0", request_id="r2"))
        assert len(router.outbox.items()) == 4

    def test_unknown_channel_is_not_queued(self):
        router, _, _ = make_router()
        assert router.enqueue(owner(channel="telegram", address="tg:1"), message()) is None

    def test_retries_back_off_then_fail_then_retry_every_two_minutes(self):
        boom = SendResult(False, error="no signal", retryable=True)
        transport = Flaky(boom, boom, boom, boom)
        router, _, clock = make_router(transport, backoff=(30.0, 90.0))
        router.enqueue(owner(), message())
        heard = []
        router.add_listener(lambda item: heard.append((item.status, item.attempts)))
        expected_next = [1030.0, 1120.0, 1240.0, 1360.0]
        for attempt, due in enumerate(expected_next, 1):
            assert router.pump() == 1
            item = router.outbox.items()[0]
            assert item.status == FAILED and item.attempts == attempt
            assert item.next_attempt_at == due
            clock.now = due - 1
            assert router.pump() == 0                       # not due yet
            clock.now = due
        router.pump()
        assert router.outbox.items()[0].status == SENT and len(transport.sent) == 1
        assert heard[-1] == (SENT, 5) and [s for s, _ in heard[:4]] == [FAILED] * 4

    def test_backoff_steps_grow_exponentially_past_the_configured_ones(self):
        router, _, _ = make_router(backoff=(30.0, 90.0), max_attempts=6,
                                   failed_retry_seconds=600.0)
        assert [router.retry_delay(n) for n in range(1, 7)] == [30.0, 90.0, 270.0, 600.0,
                                                               600.0, 600.0]

    def test_non_retryable_failure_is_final(self):
        router, _, _ = make_router(Flaky(SendResult(False, error="bad number",
                                                    retryable=False)))
        router.enqueue(owner(), message())
        router.pump()
        item = router.outbox.items()[0]
        assert item.status == FAILED and math.isinf(item.next_attempt_at)
        assert router.pump(now=1e12) == 0

    def test_transport_suppression_is_final(self):
        router, _, _ = make_router(Flaky(SendResult(False, error="not in profile",
                                                    retryable=False, suppressed=True)))
        router.enqueue(owner(), message())
        router.pump()
        assert router.outbox.items()[0].status == SUPPRESSED

    def test_gives_up_on_messages_older_than_max_age(self):
        boom = SendResult(False, error="no signal")
        router, transport, clock = make_router(Flaky(*[boom] * 10), max_age_seconds=300.0)
        router.enqueue(owner(), message(created_at=1000.0))
        router.pump()
        clock.now = 1400.0
        assert router.pump() == 0
        item = router.outbox.items()[0]
        assert item.status == FAILED and math.isinf(item.next_attempt_at)
        assert "gave up" in item.error and item.attempts == 1

    def test_delivered_on_send_for_transports_that_say_so(self):
        router, _, _ = make_router(Flaky(name="telegram", delivers_on_send=True),
                                   channel="telegram", policy=live_policy("tg:5"))
        router.enqueue(owner("telegram", "tg:5"), message())
        router.pump()
        item = router.outbox.items()[0]
        assert item.status == DELIVERED and item.sent_at == 1000.0

    def test_sending_is_claimed_before_the_transport_runs(self):
        seen = []
        router = None

        class Spy:
            name = "modem_sms"

            def send(self, recipient, msg):
                seen.append(router.outbox.items()[0].status)
                return SendResult(True)

        router, _, _ = make_router(Spy())
        router.enqueue(owner(), message())
        router.pump()
        assert seen == [SENDING]

    def test_provider_errors_are_scrubbed(self):
        router, _, _ = make_router(Flaky(SendResult(
            False, error="The 'To' number +919812345610 / a.person@example.in is invalid",
            retryable=False)))
        router.enqueue(owner(), message())
        router.pump()
        error = router.outbox.items()[0].error
        assert "9812345610" not in error and "a.person" not in error and "+91 98xx" in error

    def test_a_raising_listener_never_stops_delivery(self):
        router, transport, _ = make_router()
        router.add_listener(lambda item: 1 / 0)
        router.enqueue(owner(), message())
        assert router.pump() == 1 and len(transport.sent) == 1

    def test_channel_missing_at_delivery_waits_for_it(self):
        router, transport, clock = make_router(max_age_seconds=600.0)
        heard = []
        router.add_listener(lambda item: heard.append(item.status))
        router.enqueue(owner(), message(created_at=clock.now))
        channel = router.channels.pop("modem_sms")
        assert router.pump() == 0
        item = router.outbox.items()[0]
        assert item.status == FAILED and item.next_attempt_at == clock.now + 120.0
        clock.now += 120
        router.pump()
        assert heard == [FAILED], "listeners hear the first FAILED only"
        router.register(channel)
        clock.now += 120
        assert router.pump() == 1 and len(transport.sent) == 1 and heard[-1] == SENT

    def test_channel_missing_until_max_age_gives_up(self):
        router, _, clock = make_router(max_age_seconds=200.0)
        router.enqueue(owner(), message(created_at=clock.now))
        del router.channels["modem_sms"]
        router.pump()
        clock.now += 240
        router.pump()
        assert math.isinf(router.outbox.items()[0].next_attempt_at)


class TestCooldownsSendNowCancelReceipts:
    def test_legacy_cooldowns_per_template_recipient_and_channel(self):
        router, _, clock = make_router(cooldowns={R.LEGACY_SMS: 300.0})
        legacy = lambda step: message(step, request_id=None, template=R.LEGACY_SMS)  # noqa: E731
        assert router.enqueue(owner(), legacy("l4:1"))
        clock.now += 299
        assert router.enqueue(owner(), legacy("l4:2")) is None, "inside the 300 s cooldown"
        assert router.enqueue(owner(ref="contact:1"), legacy("l4:3")), "another recipient"
        assert router.enqueue(owner(), message("r120")), "SOS messages have no cooldown"
        clock.now += 2
        assert router.enqueue(owner(), legacy("l4:4"))

    def test_send_now_delivers_on_the_callers_thread(self):
        router, transport, _ = make_router()
        item = router.send_now(owner(), message())
        assert item.status == SENT and len(transport.sent) == 1
        assert router.pump() == 0, "not sent twice"
        assert router.send_now(owner(), message()) is None, "duplicate key"

    def test_cancel_withdraws_a_requests_queued_messages(self):
        router, transport, _ = make_router()
        router.enqueue(owner(), message("t0"))
        router.enqueue(owner(), message("r120"))
        router.enqueue(owner(), message("t0", request_id="r2"))
        heard = []
        router.add_listener(lambda item: heard.append(item.status))
        assert router.cancel("r1", "approved", steps=("r120",)) == 1
        assert router.cancel("r1", "closed") == 1
        router.pump()
        assert heard[:2] == [SUPPRESSED, SUPPRESSED]
        assert [m.request_id for _, m in transport.sent] == ["r2"]
        assert {i.error for i in router.outbox.items() if i.status == SUPPRESSED} == {
            "cancelled: approved", "cancelled: closed"}

    def test_confirm_delivery_marks_the_matching_sent_item(self):
        router, _, _ = make_router(Flaky())
        router.enqueue(owner(), message())
        router.pump()
        heard = []
        router.add_listener(lambda item: heard.append(item.status))
        assert router.confirm_delivery("modem_sms", lambda i: i.provider_id == "nope") is None
        item = router.confirm_delivery("modem_sms", lambda i: i.provider_id == "p1")
        assert item.status == DELIVERED and heard == [DELIVERED]
        assert router.confirm_delivery("modem_sms", lambda i: True) is None, "only SENT ones"

    def test_negative_receipt_fails_the_item_for_good(self):
        router, _, _ = make_router(Flaky())
        router.enqueue(owner(), message())
        router.pump()
        item = router.confirm_delivery("modem_sms", lambda i: True, delivered=False,
                                       error="delivery report: status 0x41")
        assert item.status == FAILED and math.isinf(item.next_attempt_at)

    def test_recover_interrupted_retries_sending_rows(self):
        outbox = MemoryOutbox()
        router, transport, _ = make_router(outbox=outbox)
        router.enqueue(owner(), message())
        item = outbox.items()[0]
        item.status = SENDING
        outbox.update(item)
        assert router.pump() == 0
        assert router.recover_interrupted() == 1
        assert router.pump() == 1 and len(transport.sent) == 1

    def test_with_outbox_shares_channels_listeners_and_lock(self):
        router, _, _ = make_router()
        heard = []
        router.add_listener(lambda item: heard.append(item.id))
        other = MemoryOutbox()
        view = router.with_outbox(other)
        view.enqueue(owner(), message())
        view.pump()
        assert router.outbox.items() == [] and len(other.items()) == 1
        assert heard == [other.items()[0].id] and view._lock is router._lock

    def test_ready_needs_switches_and_an_available_transport(self):
        class Down(FakeTransport):
            def state(self):
                return False, "modem not connected"

        router, _, _ = make_router(Down("modem_sms"))
        assert not router.ready("modem_sms")
        status = router.channel_status()["modem_sms"]
        assert status["live"] and not status["available"] and not status["ready"]
        assert status["detail"] == "modem not connected"
        up, _, _ = make_router()
        assert up.ready("modem_sms")
        dry, _, _ = make_router(policy=guard.SendPolicy.from_env)
        assert not dry.ready("modem_sms")

    def test_notification_payload_is_masked(self):
        item = OutboxItem(id="n1", recipient=owner(), message=message(), status=SENT,
                          attempts=1, sent_at=1000.0)
        payload = R.notification_payload(item)
        assert payload["recipient_masked"] == "+91 98xxxxxx10" and TEAM not in str(payload)
        assert payload["sent_at"].startswith("1970-01-01T00:16:40")
        assert set(payload) >= {"id", "request_id", "channel", "recipient_ref", "step",
                                "status", "error", "attempts", "sent_at"}


# ── the persistent outbox (device SQLite) ─────────────────────────────────────
class TestSqlOutbox:
    def test_round_trip_retry_and_recovery(self, temp_db):
        from sentrix.storage.notifications import SqlOutbox
        boom = SendResult(False, error="no signal")
        outbox = SqlOutbox()
        router, transport, clock = make_router(Flaky(boom), outbox=outbox)
        assert router.enqueue(owner(), message())
        assert router.enqueue(owner(), message()) is None
        router.pump()
        assert outbox.counts()["depth"] == 1 and outbox.counts()[FAILED] == 1
        clock.now += 30
        router.pump()
        sent = outbox.sent_since("modem_sms", 0.0)
        assert [i.status for i in sent] == [SENT] and sent[0].attempts == 2
        recent = outbox.recent()
        assert recent[0]["recipient_masked"] == "+91 98xxxxxx10"
        assert "address" not in recent[0] and TEAM not in str(recent)

    def test_sending_rows_survive_a_crash(self, temp_db):
        from sentrix.storage.notifications import SqlOutbox
        outbox = SqlOutbox()
        router, transport, _ = make_router(outbox=outbox)
        router.enqueue(owner(), message())
        item = outbox.due(2000.0)[0]
        item.status = SENDING
        outbox.update(item)
        restarted, transport, _ = make_router(outbox=SqlOutbox())
        assert restarted.pump() == 0
        assert restarted.recover_interrupted() == 1
        assert restarted.pump() == 1 and len(transport.sent) == 1

    def test_cancel_and_counts(self, temp_db):
        from sentrix.storage.notifications import SqlOutbox
        outbox = SqlOutbox()
        router, _, _ = make_router(outbox=outbox, policy=guard.SendPolicy.from_env)
        router.enqueue(owner(), message("t0"))
        router.enqueue(owner(), message("r120"))
        assert router.cancel("r1", "declined") == 2
        counts = outbox.counts()
        assert counts[SUPPRESSED] == 2 and counts["depth"] == 0
        assert router.pump() == 0

    def test_delivery_receipt_on_sql_rows(self, temp_db):
        from sentrix.storage.notifications import SqlOutbox
        router, _, _ = make_router(Flaky(), outbox=SqlOutbox())
        router.enqueue(owner(), message())
        router.pump()
        assert router.confirm_delivery("modem_sms", lambda i: i.provider_id == "p1")
        assert SqlOutbox().counts()[DELIVERED] == 1


# ── the device service ────────────────────────────────────────────────────────
def device_settings(**channels):
    from sentrix.config import settings
    return replace(settings, channels=replace(settings.channels, enabled=True, **channels))


class TestDeviceService:
    def test_off_by_default(self):
        from sentrix import services
        from sentrix.config import settings
        from sentrix.pipeline.state import SystemState
        ctx = services.ServiceContext(settings=settings, state=SystemState(),
                                      get_pipeline=lambda: None, registry=services.registry)
        assert settings.channels.enabled is False
        assert R.build_service(ctx) is None
        assert R.current_service() is None

    def test_registry_builds_starts_publishes_and_stops(self, temp_db, monkeypatch):
        from sentrix import services
        from sentrix.pipeline.state import SystemState
        live_state = SystemState()
        settings = device_settings()
        fake = FakeTransport("twilio_sms")
        monkeypatch.setattr(R, "build_service", lambda ctx: R.build_device_channels(
            ctx.settings, state=ctx.state, transports={"twilio_sms": fake}))
        registry = services.ServiceRegistry()
        ctx = services.ServiceContext(settings=settings, state=live_state,
                                      get_pipeline=lambda: None, registry=registry)
        assert registry.load_builtin(ctx, factories=(
            ("channels", "sentrix.alerting.channels.router:build_service"),)) == ["channels"]
        registry.start_all()
        try:
            service = registry.get("channels")
            block = live_state.get()["channels"]
            assert block["enabled"] is True and "twilio_sms" in block["channels"]
            assert block["outbound_ready"] is False, "dry run: nothing can leave"
            assert registry.health()["channels"]["worker_alive"] is True
            assert service.enqueue(owner("twilio_sms"), message())
            deadline = time.time() + 10
            while time.time() < deadline and service.recent()[0]["status"] != SUPPRESSED:
                time.sleep(0.05)
            assert service.recent()[0]["status"] == SUPPRESSED and fake.attempts == 0
        finally:
            registry.clear()
        assert live_state.get()["channels"]["enabled"] is False

    def test_status_block_shape(self, temp_db, monkeypatch):
        from sentrix.alerting.channels import modem as M
        from sentrix.config import settings as base
        settings = replace(device_settings(modem_enabled=True),
                           sos=replace(base.sos, enabled=True))
        worker = StubWorker(available=(False, "not registered on the mobile network"))
        service = R.build_device_channels(settings, outbox=MemoryOutbox(), modem_worker=worker)
        block = service.status()
        assert set(block) >= {"enabled", "outbound_ready", "live", "channels",
                              "outbox_depth", "last_error", "modem", "legacy_twilio", "warn"}
        assert set(block["channels"]) >= {"modem_sms", "modem_call"}
        assert block["modem"]["present"] is True and block["modem"]["sms_today"] == 0
        assert block["modem"]["sms_daily_cap"] == base.link.sms_daily_cap
        assert block["warn"] is True, "SOS on and nothing can leave"
        assert isinstance(service.sms_counter, M.DailyCounter)

    def test_telegram_and_email_only_in_standalone_mode(self, monkeypatch):
        from sentrix.config import settings as base
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
        monkeypatch.setenv("SENTRIX_EMAIL_PROVIDER", "resend")
        alone = R.build_device_channels(replace(device_settings(), channels=replace(
            device_settings().channels, email_provider="resend")), outbox=MemoryOutbox())
        assert {"telegram", "email"} <= set(alone.router.channels)
        linked = replace(device_settings(), link=replace(base.link, enabled=True,
                                                         portal_url="https://p.example.in"))
        service = R.build_device_channels(linked, outbox=MemoryOutbox())
        assert not {"telegram", "email"} & set(service.router.channels)

    def test_channels_for_prefers_ready_then_modem(self, monkeypatch):
        service = R.build_device_channels(device_settings(), outbox=MemoryOutbox(),
                                          transports={"twilio_sms": FakeTransport(),
                                                      "modem_sms": FakeTransport()})
        assert service.channels_for("sms") == ["modem_sms", "twilio_sms"]
        monkeypatch.setenv("SENTRIX_LIVE_ALERTS", "1")
        monkeypatch.setenv("SENTRIX_TWILIO_ENABLED", "1")
        assert service.channels_for("sms") == ["twilio_sms", "modem_sms"]
        assert service.channels_for("call") == ["twilio_call"]

    def test_owner_target_prefers_the_profile_then_alert_phone(self, temp_db, monkeypatch):
        from sentrix.link import profile
        monkeypatch.setenv("ALERT_PHONE_NUMBER", TEAM)
        assert R.DeviceChannels.owner_target()[1] == TEAM
        monkeypatch.setattr(profile, "current", lambda: {"owner": {"name": "Riya",
                                                                  "phone": "+919811111111"}})
        assert R.DeviceChannels.owner_target() == ("Riya", "+919811111111")
        monkeypatch.setattr(profile, "current", lambda: {"owner": {"name": "Riya"}})
        assert R.DeviceChannels.owner_target() == ("Riya", TEAM), "fallback target"
        monkeypatch.delenv("ALERT_PHONE_NUMBER")
        assert R.DeviceChannels.owner_target() is None

    def test_send_test_is_guarded_and_reports_the_outcome(self, temp_db, monkeypatch):
        monkeypatch.setenv("ALERT_PHONE_NUMBER", TEAM)
        fake = FakeTransport("twilio_sms")
        service = R.build_device_channels(device_settings(), transports={"twilio_sms": fake})
        result = service.send_test()
        assert result["status"] == SUPPRESSED and fake.attempts == 0
        assert result["recipient"] == "+91 98xxxxxx10" and "dry run" in result["error"]
        with pytest.raises(ValueError):
            service.send_test("telegram")
        with pytest.raises(LookupError):
            service.send_test("modem_sms")
        monkeypatch.setenv("SENTRIX_LIVE_ALERTS", "1")
        monkeypatch.setenv("SENTRIX_TWILIO_ENABLED", "1")
        monkeypatch.setenv("SENTRIX_TEST_RECIPIENTS", TEAM)
        sent = service.send_test()
        assert sent["status"] == SENT and fake.sent[0][1].text.startswith(guard.TEST_PREFIX_GSM)

    def test_suppressed_messages_are_audited(self, temp_db, monkeypatch):
        audits = []
        monkeypatch.setattr(R, "_audit", lambda event, **detail: audits.append((event, detail)))
        service = R.build_device_channels(device_settings(), transports={
            "twilio_sms": FakeTransport()})
        service.enqueue(owner("twilio_sms"), message())
        service.step()
        (event, detail), = audits
        assert event == "channel_suppressed" and detail["recipient"] == "+91 98xxxxxx10"
        assert detail["channel"] == "twilio_sms" and "dry run" in detail["reason"]


class StubWorker:
    """What the service needs from a modem worker (the real one is tested in
    tests/test_ch_modem.py)."""

    backend = "stub"

    def __init__(self, available=(True, "network -71 dBm")):
        self._available = available
        self.started = self.stopped = 0
        self.reports = []

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def available(self):
        return self._available

    def status(self):
        return {"backend": "stub", "present": True, "registered": self._available[0],
                "signal_dbm": -71}

    def count_parts(self, text):
        return 1

    def take_reports(self):
        out, self.reports = self.reports, []
        return out


class TestPreflight:
    def test_rows_are_local_and_well_formed(self, monkeypatch):
        from sentrix.config import settings
        monkeypatch.setenv("ALERT_PHONE_NUMBER", TEAM)
        rows = R.preflight(settings)
        assert rows and all(set(r) == {"status", "name", "detail"} for r in rows)
        assert all(r["status"] in ("ok", "warn", "fail") for r in rows)
        assert any("dry run" in r["detail"] for r in rows)

    def test_warns_when_live_in_test_mode_without_an_allowlist(self, monkeypatch):
        monkeypatch.setenv("SENTRIX_LIVE_ALERTS", "1")
        monkeypatch.setenv("SENTRIX_MODEM_LIVE", "1")
        rows = R.preflight(device_settings(modem_enabled=True))
        assert any(r["status"] == "warn" and "empty SENTRIX_TEST_RECIPIENTS" in r["detail"]
                   for r in rows)
        assert any(r["name"] == "modem" for r in rows)

    def test_test_mode_off_is_a_warning(self, monkeypatch):
        from sentrix.config import settings
        monkeypatch.setenv("SENTRIX_TEST_MODE", "0")
        assert any(r["name"] == "test mode" and r["status"] == "warn"
                   for r in R.preflight(settings))

    def test_doctor_report_includes_channels(self):
        from sentrix import services
        from sentrix.config import settings
        report = services.preflight_report(settings)
        assert "channels" in report and report["channels"][0]["name"] == "channels"


def test_worker_thread_wakes_on_enqueue(temp_db):
    service = R.build_device_channels(device_settings(), transports={
        "twilio_sms": FakeTransport()})
    service.poll_seconds = 30.0                              # only a wake can deliver
    service.start()
    try:
        time.sleep(0.2)
        started = time.time()
        service.enqueue(owner("twilio_sms"), message())
        while time.time() - started < 5 and service.recent()[0]["status"] in (QUEUED,
                                                                               SENDING):
            time.sleep(0.02)
        assert service.recent()[0]["status"] == SUPPRESSED
        assert time.time() - started < 5
    finally:
        service.stop()
    assert not any(t.name == "sentrix-channels" and t.is_alive()
                   for t in threading.enumerate())
