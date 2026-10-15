"""Track T-CH: the pre-portal notifier (L4 SMS, L5 call, dispatch "Send now")
routed through the channel router, and still a no-op by default.
"""

from __future__ import annotations

import queue
import time
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from sentrix.alerting import guard
from sentrix.alerting.channels import router as R
from sentrix.alerting.channels.base import (DELIVERED, SENT, SUPPRESSED, FakeTransport)

TEAM = "+919812345610"


def tci_result(kind="weapon"):
    return SimpleNamespace(tci=0.9, incident_type=kind, reason="weapon <corroborated>",
                           features=SimpleNamespace(weapon=0.9, fire=0.0, audio=0.0,
                                                    extras={"detections": {"all": []}}))


def live(monkeypatch, *channels):
    monkeypatch.setenv("SENTRIX_LIVE_ALERTS", "1")
    for channel in channels:
        monkeypatch.setenv(guard.CHANNEL_SWITCHES[channel], "1")
    monkeypatch.setenv("SENTRIX_TEST_RECIPIENTS", TEAM)


@pytest.fixture
def channels(temp_db, monkeypatch):
    """A device channels service with fake phone transports, as the running
    ``channels`` service; ALERT_PHONE_NUMBER is the team phone."""
    from sentrix.config import settings
    monkeypatch.setenv("ALERT_PHONE_NUMBER", TEAM)
    fakes = {name: FakeTransport(name) for name in
             ("modem_sms", "modem_call", "twilio_sms", "twilio_call")}
    service = R.build_device_channels(
        replace(settings, channels=replace(settings.channels, enabled=True)),
        transports=dict(fakes))
    monkeypatch.setattr(R, "current_service", lambda: service)
    service.fakes = fakes
    return service


def outbox(service):
    return service.recent(50)


class TestNoOpByDefault:
    def test_nothing_is_built_or_sent(self, monkeypatch):
        twilio_rest = pytest.importorskip("twilio.rest")
        built = []
        monkeypatch.setattr(twilio_rest, "Client", lambda *a: built.append(a))
        for name, value in (("TWILIO_ACCOUNT_SID", "AC1"), ("TWILIO_AUTH_TOKEN", "t"),
                            ("TWILIO_PHONE_NUMBER", "+15550000000"),
                            ("ALERT_PHONE_NUMBER", TEAM)):
            monkeypatch.setenv(name, value)
        from sentrix.alerting.notifier import Notifier
        notifier = Notifier()
        assert R.current_service() is None
        assert notifier.send_sms(tci_result(), 4, "HIGH") is False
        assert notifier.make_call(tci_result(), 5, "CRITICAL") is False
        assert notifier.send_raw("[SENTRIX EMERGENCY - POLICE]") is False
        assert notifier.online is False and built == []

    def test_router_without_a_phone_channel_keeps_the_direct_path(self, temp_db, monkeypatch):
        from sentrix.alerting.notifier import Notifier
        from sentrix.config import settings
        service = R.build_device_channels(
            replace(settings, channels=replace(settings.channels, enabled=True)),
            transports={"telegram": FakeTransport("telegram")})
        monkeypatch.setattr(R, "current_service", lambda: service)
        assert Notifier().send_sms(tci_result(), 4, "HIGH") is False
        assert outbox(service) == []


class TestRoutedThroughTheRouter:
    def test_l4_sms_goes_to_the_owner_by_modem_first(self, channels, monkeypatch):
        from sentrix.alerting.notifier import Notifier
        live(monkeypatch, "modem_sms", "twilio_sms")
        assert Notifier().send_sms(tci_result(), 4, "HIGH") is True
        channels.step()
        (row,) = outbox(channels)
        assert row["template"] == "legacy.l4_sms" and row["channel"] == "modem_sms"
        assert row["recipient_ref"] == "owner" and row["status"] == SENT
        recipient, message = channels.fakes["modem_sms"].sent[0]
        assert recipient.address == TEAM and message.text.startswith(guard.TEST_PREFIX_GSM)
        assert "Level 4 HIGH" in message.text and channels.fakes["twilio_sms"].sent == []

    def test_dry_run_records_a_suppressed_copy(self, channels):
        from sentrix.alerting.notifier import Notifier
        assert Notifier().send_sms(tci_result(), 4, "HIGH") is True
        channels.step()
        (row,) = outbox(channels)
        assert row["status"] == SUPPRESSED and "dry run" in row["error"]
        assert all(fake.attempts == 0 for fake in channels.fakes.values())

    def test_the_old_sms_cooldown_is_kept(self, channels):
        from sentrix.alerting.notifier import Notifier
        notifier = Notifier()
        assert notifier.send_sms(tci_result(), 4, "HIGH") is True
        assert notifier.send_sms(tci_result(), 4, "HIGH") is False, "300 s cooldown"
        assert len(outbox(channels)) == 1
        cooldown = channels.router.cooldowns["legacy.l4_sms"]
        assert cooldown == 300.0 and channels.router.cooldowns["legacy.l5_call"] == 600.0

    def test_profile_owner_wins_over_alert_phone(self, channels, monkeypatch):
        from sentrix.alerting.notifier import Notifier
        from sentrix.link import profile
        monkeypatch.setattr(profile, "current", lambda: {
            "owner": {"name": "Riya", "phone": "+919811111111"}})
        Notifier().send_sms(tci_result(), 4, "HIGH")
        assert outbox(channels)[0]["recipient_masked"] == "+91 98xxxxxx11"

    def test_no_owner_number_sends_nothing(self, channels, monkeypatch):
        from sentrix.alerting.notifier import Notifier
        monkeypatch.delenv("ALERT_PHONE_NUMBER")
        assert Notifier().send_sms(tci_result(), 4, "HIGH") is False
        assert outbox(channels) == []

    def test_l5_call_prefers_a_ready_channel(self, channels, monkeypatch):
        from sentrix.alerting.notifier import Notifier
        live(monkeypatch, "twilio_call")
        assert Notifier().make_call(tci_result(), 5, "CRITICAL") is True
        channels.step()
        (row,) = outbox(channels)
        assert row["template"] == "legacy.l5_call" and row["channel"] == "twilio_call"
        assert row["status"] == SENT
        _, message = channels.fakes["twilio_call"].sent[0]
        assert "Level 5" in message.text and "weapon <corroborated>" in message.text

    def test_dispatch_send_waits_for_its_attempt(self, channels, monkeypatch):
        from sentrix.alerting.notifier import Notifier
        notifier = Notifier()
        assert notifier.send_raw("[SENTRIX EMERGENCY - POLICE]") is False, "dry run"
        live(monkeypatch, "modem_sms")
        assert notifier.send_raw("[SENTRIX EMERGENCY - FIRE]") is True
        rows = outbox(channels)
        assert {r["status"] for r in rows} == {SUPPRESSED, SENT}
        assert {r["template"] for r in rows} == {"legacy.dispatch"}

    def test_online_follows_the_router(self, channels, monkeypatch):
        from sentrix.alerting.notifier import Notifier
        notifier = Notifier()
        assert notifier.online is False
        live(monkeypatch, "twilio_sms")
        assert notifier.online is True
        monkeypatch.setenv("SENTRIX_TEST_RECIPIENTS", "+919800000000")
        assert notifier.online is False, "the owner is not allowlisted"

    def test_a_router_error_never_raises_into_the_pipeline(self, channels, monkeypatch):
        from sentrix.alerting.notifier import Notifier

        def broken(recipient, message):
            raise RuntimeError("disk full")

        monkeypatch.setattr(channels, "enqueue", broken)
        assert Notifier().send_sms(tci_result(), 4, "HIGH") is False


class TestDispatchThroughTheRouter:
    def test_send_now_outcomes(self, channels, monkeypatch):
        from sentrix.alerting.dispatch import DispatchPackage, dispatch_service
        from sentrix.storage import database
        database.save_dispatch(DispatchPackage(
            id="p1", created_at="2026-10-15T00:00:00+00:00", incident_id="i1",
            incident_type="fire", level=4, tci=0.8, user_name="Riya", user_address="H212",
            user_phone="", camera_location="Living room", evidence_bundle=None,
            evidence_hash=None, authority="FIRE", status="PENDING", payload_json="{}"))
        outcome = dispatch_service.send("p1", "FIRE")
        assert outcome.status == "failed"
        assert database.get_dispatch("p1")["status"] == "PENDING"
        live(monkeypatch, "modem_sms")
        assert dispatch_service.send("p1", "FIRE").status == "sent"
        assert database.get_dispatch("p1")["status"] == "SENT"
        _, message = channels.fakes["modem_sms"].sent[0]
        assert "[SENTRIX EMERGENCY - FIRE]" in message.text


class TestPipelineClaims:
    """``IncidentManager.claim``: each legacy message once per incident."""

    @staticmethod
    def fake_pipeline(wants=False):
        from sentrix.config import settings
        from sentrix.pipeline.engine import Pipeline
        from sentrix.pipeline.incidents import IncidentManager
        fake = Pipeline.__new__(Pipeline)
        fake.cfg = settings
        fake.incidents = IncidentManager()
        fake.sos = SimpleNamespace(wants=lambda kind, level, state=None: wants,
                                   trigger=lambda t: None)
        fake.session_id = "s1"
        fake._tasks = queue.Queue(maxsize=64)
        fake._submit = lambda func, *args, **kw: (
            func(*args, **kw) if getattr(func, "__name__", "") in ("send_sms", "make_call")
            else None)
        fake._evidence_context = lambda result: {}
        fake.siren = SimpleNamespace(activate=lambda: None)
        return fake

    @staticmethod
    def incident(manager):
        incident, _ = manager.update(4, 0.8, "weapon", now=0.0, features={}, reason="r",
                                     trigger={"weapon": {"state": "corroborated"}})
        return incident

    def test_one_sms_and_one_call_per_incident(self, channels):
        from sentrix.pipeline.escalation import plan_for
        fake = self.fake_pipeline()
        incident = self.incident(fake.incidents)
        frame = np.zeros((4, 4, 3), np.uint8)
        for level, now in ((4, 0.0), (4, 1.0), (5, 2.0), (5, 3.0), (4, 4.0)):
            fake._act(incident, plan_for(level), frame, tci_result(), level, "HIGH", now)
        templates = sorted(r["template"] for r in outbox(channels))
        assert templates == ["legacy.l4_sms", "legacy.l5_call"]
        assert {"sms", "call"} <= incident.actions

    def test_sos_owned_incidents_send_no_legacy_message(self, channels):
        from sentrix.pipeline.escalation import plan_for
        fake = self.fake_pipeline(wants=True)
        incident = self.incident(fake.incidents)
        fake._submit_or_run = lambda func, *args: None
        fake._act(incident, plan_for(5), np.zeros((4, 4, 3), np.uint8), tci_result(), 5,
                  "CRITICAL", 0.0)
        assert outbox(channels) == [] and "sos" in incident.actions


def test_running_service_in_the_registry_is_found(temp_db, monkeypatch):
    """Without monkeypatching ``current_service``: registered and started."""
    from sentrix import services
    from sentrix.alerting.notifier import Notifier
    from sentrix.config import settings
    monkeypatch.setenv("ALERT_PHONE_NUMBER", TEAM)
    fake = FakeTransport("twilio_sms")
    service = R.build_device_channels(
        replace(settings, channels=replace(settings.channels, enabled=True)),
        transports={"twilio_sms": fake})
    services.registry.register(service)
    assert R.current_service() is None, "registered but not running"
    services.registry.start_all()
    try:
        assert R.current_service() is service
        live(monkeypatch, "twilio_sms")
        assert Notifier().send_sms(tci_result(), 4, "HIGH") is True
        deadline = time.time() + 10
        while time.time() < deadline and service.recent()[0]["status"] not in (SENT,
                                                                                DELIVERED):
            time.sleep(0.02)
        assert service.recent()[0]["status"] in (SENT, DELIVERED)
        assert len(fake.sent) == 1
    finally:
        services.registry.clear()
