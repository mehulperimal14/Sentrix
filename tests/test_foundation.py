"""The portal/SOS foundation (docs/7_PORTAL_AND_SOS.md): contracts every track
codes against, and the safety rules that must hold before any of them land.

Nothing here opens a camera or microphone, sends a message, or touches
``var/sentrix.db``: databases are temporary files, transports are fakes.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from sentrix.alerting import guard
from sentrix.paths import ROOT_DIR
from sentrix.storage import database

SWITCHES = ("SENTRIX_TEST_MODE", "SENTRIX_LIVE_ALERTS", "SENTRIX_TEST_RECIPIENTS",
            *guard.CHANNEL_SWITCHES.values())
TEAM_PHONE = "+919812345610"


@pytest.fixture
def clean_switches(monkeypatch):
    for name in SWITCHES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'device.db'}")
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal",
                        sessionmaker(bind=engine, autocommit=False, autoflush=False))
    return engine


# ── configuration ─────────────────────────────────────────────────────────────
class TestDefaults:
    def test_fresh_process_is_test_mode_with_every_service_off(self):
        """Read in a clean interpreter, so the import-time defaults are what a
        newcomer gets (no .env, no flags)."""
        env = {k: v for k, v in os.environ.items()
               if not (k.startswith("SENTRIX_") or k.startswith("PORTAL_")
                       or k.startswith("TWILIO") or k == "ALERT_PHONE_NUMBER")}
        code = ("import json; from sentrix.config import settings as s; "
                "print(json.dumps({'test_mode': s.test_mode, 'live': s.channels.live_alerts, "
                "'sos': s.sos.enabled, 'link': s.link.enabled, "
                "'channels': s.channels.enabled, 'modem': s.channels.modem_enabled, "
                "'gpio': s.gpio.enabled, 'node': s.node.enabled, "
                "'twilio': s.channels.twilio_enabled, 'clip': s.clip.mode, "
                "'docs': s.web.api_docs, 'single_l3': s.sos.contacts_for_single_source_l3, "
                "'safety': s.sos.safety_defaults}))")
        out = subprocess.run([sys.executable, "-c", code], env=env, cwd=ROOT_DIR,
                             capture_output=True, text=True, timeout=120, check=True)
        flags = json.loads(out.stdout.strip().splitlines()[-1])
        assert flags["test_mode"] is True and flags["safety"] is True
        for key in ("live", "sos", "link", "channels", "modem", "gpio", "node",
                    "twilio", "docs", "single_l3"):
            assert flags[key] is False, key
        assert flags["clip"] == "tick"

    def test_config_never_publishes_secrets(self):
        from sentrix.config import settings

        def keys(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    yield str(key).lower()
                    yield from keys(value)

        names = list(keys(settings.to_dict()))
        for secret in ("token", "api_key", "password", "secret", "recipients"):
            assert not [n for n in names if secret in n], secret

    def test_gpio_pins_follow_the_roadmap_pin_table(self):
        from sentrix.config import settings
        g = settings.gpio
        assert (g.siren_pin, g.led_green_pin, g.led_amber_pin, g.led_red_pin,
                g.buzzer_pin, g.pir_pin, g.door_pin, g.mains_sense_pin) == \
            (17, 5, 12, 13, 26, 23, 24, 16)


# ── send guard ────────────────────────────────────────────────────────────────
class TestSendGuard:
    def test_default_is_a_dry_run(self, clean_switches):
        decision = guard.may_send("modem_sms", TEAM_PHONE)
        assert not decision and "LIVE_ALERTS" in decision.reason
        assert decision.test_mode is True

    def test_one_switch_is_not_enough(self, clean_switches):
        clean_switches.setenv("SENTRIX_LIVE_ALERTS", "1")
        assert not guard.may_send("modem_sms", TEAM_PHONE)
        clean_switches.delenv("SENTRIX_LIVE_ALERTS")
        clean_switches.setenv("SENTRIX_MODEM_LIVE", "1")
        assert not guard.may_send("modem_sms", TEAM_PHONE)

    def test_test_mode_sends_only_to_the_allowlist(self, clean_switches):
        clean_switches.setenv("SENTRIX_LIVE_ALERTS", "1")
        clean_switches.setenv("SENTRIX_TELEGRAM_LIVE", "1")
        clean_switches.setenv("SENTRIX_MODEM_LIVE", "1")
        clean_switches.setenv("SENTRIX_TEST_RECIPIENTS", "98123 45610, tg:4242")
        assert guard.may_send("modem_sms", TEAM_PHONE)            # 10-digit entry -> +91
        assert guard.may_send("telegram", "4242")
        refused = guard.may_send("modem_sms", "+919800000001")
        assert not refused and "SENTRIX_TEST_RECIPIENTS" in refused.reason
        assert refused.recipient_masked == "+91 98xxxxxx01"

    def test_emergency_numbers_are_never_contacted(self, clean_switches):
        for name in ("SENTRIX_LIVE_ALERTS", "SENTRIX_MODEM_LIVE", "SENTRIX_TWILIO_ENABLED"):
            clean_switches.setenv(name, "1")
        clean_switches.setenv("SENTRIX_TEST_MODE", "0")
        assert guard.may_send("modem_sms", TEAM_PHONE)            # live, not test mode
        for number in ("112", "+91112", "100", "101", "108"):
            assert not guard.may_send("modem_sms", number)
            assert not guard.may_send("twilio_call", number)

    def test_banner_and_assert(self, clean_switches):
        clean_switches.setenv("SENTRIX_LIVE_ALERTS", "1")
        clean_switches.setenv("SENTRIX_EMAIL_LIVE", "1")
        clean_switches.setenv("SENTRIX_TEST_RECIPIENTS", "team@example.in")
        text = guard.prepare_text("email", "Do you need assistance?")
        assert text.startswith(guard.TEST_PREFIX)
        assert guard.prepare_text("modem_sms", "x").startswith(guard.TEST_PREFIX_GSM)
        assert guard.prepare_text("email", text) == text           # idempotent
        guard.assert_may_send("email", "Team@Example.in", text)
        with pytest.raises(guard.SendRefused):
            guard.assert_may_send("email", "team@example.in", "no banner")
        with pytest.raises(guard.SendRefused):
            guard.assert_may_send("email", "someone@else.in", text)
        assert not guard.may_send("pigeon", "x")

    def test_status_has_counts_not_addresses(self, clean_switches):
        clean_switches.setenv("SENTRIX_TEST_RECIPIENTS", f"{TEAM_PHONE},a@b.in")
        status = guard.status()
        assert status["test_recipients"] == 2 and TEAM_PHONE not in json.dumps(status)


class FakeTwilio:
    created: list = []

    def __init__(self, sid, token):
        FakeTwilio.created.append(sid)
        self.messages = SimpleNamespace(create=self._message)
        self.calls = SimpleNamespace(create=self._call)
        self.sent: list = []

    def _message(self, **kwargs):
        self.sent.append(("sms", kwargs))

    def _call(self, **kwargs):
        self.sent.append(("call", kwargs))


class TestLegacyNotifier:
    @pytest.fixture
    def twilio_env(self, clean_switches):
        twilio_rest = pytest.importorskip("twilio.rest")
        FakeTwilio.created = []
        clean_switches.setattr(twilio_rest, "Client", FakeTwilio)
        for name, value in (("TWILIO_ACCOUNT_SID", "AC_test"), ("TWILIO_AUTH_TOKEN", "t"),
                            ("TWILIO_PHONE_NUMBER", "+15550000000"),
                            ("ALERT_PHONE_NUMBER", TEAM_PHONE)):
            clean_switches.setenv(name, value)
        return clean_switches

    @staticmethod
    def result():
        return SimpleNamespace(tci=0.9, incident_type="weapon", reason="weapon <corroborated>",
                               features=SimpleNamespace(weapon=0.9, fire=0.0, audio=0.0))

    def test_keys_alone_send_nothing(self, twilio_env):
        from sentrix.alerting.notifier import Notifier
        notifier = Notifier()
        assert FakeTwilio.created == [], "no Twilio client without SENTRIX_TWILIO_ENABLED"
        assert not notifier.online
        assert notifier.send_sms(self.result(), 4, "HIGH") is False
        assert notifier.make_call(self.result(), 5, "CRITICAL") is False
        assert notifier.send_raw("dispatch") is False

    def test_enabled_but_not_live_sends_nothing(self, twilio_env):
        from sentrix.alerting.notifier import Notifier
        twilio_env.setenv("SENTRIX_TWILIO_ENABLED", "1")
        notifier = Notifier()
        assert FakeTwilio.created == ["AC_test"]
        assert notifier.send_sms(self.result(), 4, "HIGH") is False
        assert notifier._client.sent == []

    def test_both_switches_and_allowlist_send_with_the_banner(self, twilio_env):
        from sentrix.alerting.notifier import Notifier
        for name in ("SENTRIX_TWILIO_ENABLED", "SENTRIX_LIVE_ALERTS"):
            twilio_env.setenv(name, "1")
        twilio_env.setenv("SENTRIX_TEST_RECIPIENTS", TEAM_PHONE)
        notifier = Notifier()
        assert notifier.online
        assert notifier.send_sms(self.result(), 4, "HIGH") is True
        assert notifier.make_call(self.result(), 5, "CRITICAL") is True
        (kind, sms), (_, call) = notifier._client.sent
        assert sms["body"].startswith(guard.TEST_PREFIX_GSM) and sms["to"] == TEAM_PHONE
        assert "&lt;corroborated&gt;" in call["twiml"], "TwiML text is escaped"


# ── device database ───────────────────────────────────────────────────────────
class TestDeviceSchema:
    def test_old_database_gains_tables_and_columns_and_keeps_rows(self, temp_db):
        from sentrix.storage.models import DispatchRecord, EnrolledFace, IncidentRecord
        new_cols = {"incidents": {"verdict_by", "verdict_at", "request_id", "trigger_json"},
                    "dispatch_packages": {"request_id", "chosen_authority", "sent_at",
                                          "sent_by", "sent_to_json", "error",
                                          "expires_at", "updated_at"},
                    "enrolled_faces": {"member_id", "consent_id", "enrolled_by"}}
        with temp_db.begin() as conn:
            for model in (IncidentRecord, DispatchRecord, EnrolledFace):
                table = model.__table__
                cols = ", ".join(f"{c.name} {c.type.compile(temp_db.dialect)}"
                                 for c in table.columns if c.name not in new_cols[table.name])
                conn.exec_driver_sql(f"CREATE TABLE {table.name} ({cols})")
            conn.exec_driver_sql("INSERT INTO incidents (id, peak_level) VALUES ('old-1', 3)")
        database.init_db()
        tables = set(inspect(temp_db).get_table_names())
        assert {"assistance_requests", "request_events", "link_outbox", "profile_cache",
                "notifications", "remote_audit"} <= tables
        with temp_db.begin() as conn:
            for table, cols in new_cols.items():
                present = {r[1] for r in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
                assert cols <= present, table
            assert conn.exec_driver_sql("SELECT count(*) FROM incidents").scalar() == 1
        database.init_db()                                      # idempotent

    def test_incident_api_is_capped_and_pages(self, temp_db):
        database.init_db()
        assert database.recent_incidents(10_000) == []          # capped, no error
        assert database.recent_incidents(5, offset=10) == []


def _incident(manager=None, level=3, kind="weapon"):
    from sentrix.pipeline.incidents import IncidentManager
    manager = manager or IncidentManager()
    incident, _ = manager.update(level, 0.5, kind, now=0.0, features={}, reason="r",
                                 trigger={"weapon": {"state": "confirmed"}})
    return incident


def _package(pid, incident_id):
    from sentrix.alerting.dispatch import DispatchPackage
    return DispatchPackage(id=pid, created_at="2026-10-14T00:00:00+00:00",
                           incident_id=incident_id, incident_type="weapon", level=3,
                           tci=0.5, user_name="u", user_address="a", user_phone="",
                           camera_location="c", evidence_bundle=None, evidence_hash=None,
                           authority="POLICE", status="PENDING", payload_json="{}")


class TestVerdictBySosState:
    """A false-alarm click must never quietly cancel help (docs/7 §7.7)."""

    @staticmethod
    def setup_request(state, flags=None):
        from sentrix.sos import states as S
        from sentrix.storage import requests
        from sentrix.storage.models import AssistanceRequestRecord, DispatchRecord
        database.init_db()
        incident = _incident()
        database.save_incident(incident, "s1", ongoing=True)
        req = requests.create_request("req-1", incident_id=incident.id,
                                      incident_type="weapon", level=3,
                                      evidence_state="confirmed",
                                      policy={"window_seconds": 1800}, expires_at=None,
                                      test_mode=True, flags=flags)
        assert req["state"] == S.DRAFTED
        assert database.link_incident_request(incident.id, "req-1")
        database.save_dispatch(_package("p1", incident.id))
        with database.session_scope() as session:
            session.get(AssistanceRequestRecord, "req-1").state = state
            session.get(DispatchRecord, "p1").request_id = "req-1"
        return incident

    def test_no_request_keeps_the_old_rule(self, temp_db):
        database.init_db()
        incident = _incident()
        database.save_incident(incident, "s1")
        database.save_dispatch(_package("p0", incident.id))
        assert database.set_verdict(incident.id, "FALSE", by="lc:device_admin")
        assert database.get_dispatch("p0")["status"] == "CANCELLED"
        row = database.recent_incidents()[0]
        assert row["verdict_by"] == "lc:device_admin" and row["verdict_at"]

    def test_pending_request_is_cancelled_with_its_package(self, temp_db):
        from sentrix.storage import requests
        incident = self.setup_request("PENDING")
        assert database.set_verdict(incident.id, "FALSE", by="portal:u1")
        assert requests.get_request("req-1")["state"] == "CANCELLED"
        assert database.get_dispatch("p1")["status"] == "CANCELLED"
        events = requests.events("req-1")
        assert [e["event"] for e in events] == ["created", "cancel"]
        assert requests.verify_chain("req-1")["ok"]

    def test_help_path_marks_resident_safe_and_keeps_help(self, temp_db):
        from sentrix.storage import requests
        incident = self.setup_request("DISPATCHED")
        assert database.set_verdict(incident.id, "FALSE", by="portal:u1")
        req = requests.get_request("req-1")
        assert req["state"] == "DISPATCHED" and req["flags"]["resident_safe"] is True
        assert database.get_dispatch("p1")["status"] == "PENDING"

    def test_duress_changes_nothing(self, temp_db):
        from sentrix.storage import requests
        incident = self.setup_request("APPROVED", flags={"duress": True})
        assert database.set_verdict(incident.id, "FALSE")
        assert requests.get_request("req-1")["state"] == "APPROVED"
        assert database.get_dispatch("p1")["status"] == "PENDING"

    def test_listeners_see_writes_and_cannot_break_them(self, temp_db):
        seen = []
        database.add_listener("incident", lambda d: seen.append(("incident", d["id"])))
        database.add_listener("incident", lambda d: 1 / 0)
        try:
            database.init_db()
            incident = _incident()
            database.save_incident(incident, "s1", ongoing=True)
        finally:
            from sentrix import listeners
            listeners.clear()
        assert seen == [("incident", incident.id)]
        assert database.recent_incidents()[0]["id"] == incident.id


class TestRequestStore:
    def test_transitions_follow_the_contract_table(self, temp_db):
        from sentrix.storage import requests
        database.init_db()
        requests.create_request("r", incident_id="i", incident_type="fire", level=3,
                                evidence_state="confirmed", policy={}, expires_at=None,
                                test_mode=True)
        assert requests.create_request("r", incident_id="i", incident_type="fire",
                                       level=3, evidence_state="confirmed", policy={},
                                       expires_at=None, test_mode=True)["version"] == 1
        assert requests.transition("r", "prompted")["state"] == "PENDING"
        assert requests.transition("r", "resolve") is None, "not allowed from PENDING"
        assert requests.transition("r", "approve", actor_type="owner",
                                   channel="lc", authority="FIRE")["authority"] == "FIRE"
        assert requests.open_request()["id"] == "r"
        requests.transition("r", "delivered")
        requests.transition("r", "resolve", actor_type="desk", outcome="help_arrived")
        assert requests.open_request() is None
        assert requests.verify_chain("r") == {"ok": True, "entries": 5, "problems": [],
                                              "origins": {"device": 5}}


class TestLinkAndNotificationStores:
    def test_outbox_sos_first_dedupe_and_ack(self, temp_db):
        from sentrix.storage import link_store
        database.init_db()
        a = link_store.enqueue("incident.upsert", {"id": "i"})
        b = link_store.enqueue("sos.created", {"id": "r"}, priority=link_store.PRIORITY_SOS,
                               dedupe_key="sos.created:r")
        assert link_store.enqueue("sos.created", {}, dedupe_key="sos.created:r") is None
        assert [e["seq"] for e in link_store.pending()] == [b, a]
        assert link_store.ack(upto=a) == 1 and link_store.depth() == 1

    def test_notification_outbox_round_trip(self, temp_db):
        from sentrix.alerting.channels import ChannelRouter, Message, Recipient
        from sentrix.alerting.channels.base import Channel, FakeTransport
        from sentrix.storage.notifications import SqlOutbox
        database.init_db()
        fake = FakeTransport()
        router = ChannelRouter({"email": Channel("email", fake, policy_factory=_live_policy)},
                               SqlOutbox(), clock=lambda: 1000.0)
        owner = Recipient("owner", "Riya", "email", "team@example.in")
        message = Message(template="sos.prompt", step="t0", text="hello", request_id="r1")
        assert router.enqueue(owner, message)
        assert router.enqueue(owner, message) is None
        assert router.pump(now=1000.0) == 1 and len(fake.sent) == 1
        assert fake.sent[0][1].text.startswith(guard.TEST_PREFIX)


def _live_policy():
    return guard.SendPolicy(test_mode=True, live_alerts=True,
                            switches={ch: True for ch in guard.CHANNEL_SWITCHES},
                            allowlist=frozenset({"team@example.in", TEAM_PHONE}))


class TestChannelRouter:
    def test_dry_run_is_suppressed_before_the_transport(self, clean_switches):
        from sentrix.alerting.channels import ChannelRouter, Message, Recipient
        from sentrix.alerting.channels.base import SUPPRESSED, Channel, FakeTransport
        fake = FakeTransport()
        router = ChannelRouter({"modem_sms": Channel("modem_sms", fake)})
        router.enqueue(Recipient("owner", "R", "modem_sms", TEAM_PHONE),
                       Message(template="sos.prompt", step="t0", text="x"))
        router.pump()
        assert fake.attempts == 0
        assert router.outbox.items()[0].status == SUPPRESSED

    def test_retries_with_backoff_then_succeeds(self):
        from sentrix.alerting.channels import ChannelRouter, Message, Recipient
        from sentrix.alerting.channels.base import FAILED, SENT, Channel, FakeTransport
        fake = FakeTransport(fail_times=2)
        router = ChannelRouter({"modem_sms": Channel("modem_sms", fake,
                                                     policy_factory=_live_policy)},
                               backoff=(30.0, 90.0), clock=lambda: 0.0)
        router.enqueue(Recipient("contact:1", "A", "modem_sms", TEAM_PHONE),
                       Message(template="sos.full", step="t0", text="SOS", request_id="r"))
        assert router.pump(now=0.0) == 1
        item = router.outbox.items()[0]
        assert item.status == FAILED and item.next_attempt_at == 30.0
        assert router.pump(now=10.0) == 0                       # not due yet
        router.pump(now=30.0)
        assert router.outbox.items()[0].next_attempt_at == 120.0
        router.pump(now=120.0)
        assert router.outbox.items()[0].status == SENT and fake.attempts == 3


# ── services and measurement safety ───────────────────────────────────────────
class FakeService:
    def __init__(self, name, log):
        self.name, self.log = name, log

    def start(self):
        self.log.append(("start", self.name))

    def stop(self):
        self.log.append(("stop", self.name))

    def health(self):
        return {"ok": True}


class TestServices:
    def test_start_in_order_stop_in_reverse_and_isolate_failures(self, monkeypatch):
        from sentrix import services
        registry = services.ServiceRegistry()
        log: list = []
        module = SimpleNamespace(
            build_a=lambda ctx: FakeService("a", log), build_off=lambda ctx: None,
            build_bad=lambda ctx: 1 / 0)
        monkeypatch.setitem(sys.modules, "fake_services_mod", module)
        ctx = services.ServiceContext(settings=None, state=None, get_pipeline=lambda: None,
                                      registry=registry)
        built = registry.load_builtin(ctx, factories=(
            ("a", "fake_services_mod:build_a"), ("off", "fake_services_mod:build_off"),
            ("bad", "fake_services_mod:build_bad"),
            ("missing", "sentrix.not_a_module_yet:build_service")))
        registry.register(FakeService("b", log))
        assert built == ["a"] and registry.names() == ["a", "b"]
        registry.start_all()
        assert registry.running() == ["a", "b"]
        health = registry.health()
        assert health["a"] == {"status": "running", "ok": True}
        assert health["bad"]["status"] == "failed" and "missing" not in health
        registry.stop_all()
        assert log == [("start", "a"), ("start", "b"), ("stop", "b"), ("stop", "a")]

    def test_builtin_factories_are_absent_or_off_by_default(self):
        from sentrix import services
        registry = services.ServiceRegistry()
        ctx = services.ServiceContext(settings=None, state=None, get_pipeline=lambda: None,
                                      registry=registry)
        assert registry.load_builtin(ctx) == []

    def test_measurement_refuses_a_real_sos_service_or_running_services(self, monkeypatch):
        from sentrix import services
        from sentrix.sos import NullSosService
        services.assert_measurement_safe(SimpleNamespace(sos=NullSosService()))
        with pytest.raises(services.MeasurementUnsafe):
            services.assert_measurement_safe(SimpleNamespace(sos=object()))
        registry = services.ServiceRegistry()
        registry.register(FakeService("link", []))
        registry.start_all()
        monkeypatch.setattr(services, "registry", registry)
        with pytest.raises(services.MeasurementUnsafe):
            services.assert_measurement_safe(SimpleNamespace(sos=NullSosService()))

    def test_eval_harness_and_tools_keep_the_null_sos_service(self):
        harness = (ROOT_DIR / "sentrix" / "evaluation" / "harness.py").read_text(encoding="utf-8")
        assert "pipeline.sos = NullSosService()" in harness
        assert "assert_measurement_safe(pipeline)" in harness
        for tool in ("bench_live.py", "replay.py"):
            source = (ROOT_DIR / "tools" / tool).read_text(encoding="utf-8")
            assert "assert_measurement_safe(pipeline)" in source, tool

    def test_a_new_pipeline_defaults_to_the_null_service(self):
        from sentrix.pipeline.engine import Pipeline
        source = Path(sys.modules[Pipeline.__module__].__file__).read_text(encoding="utf-8")
        assert "self.sos = NullSosService()" in source


# ── the pipeline's SOS hook ───────────────────────────────────────────────────
class RecordingSos:
    name = "sos"

    def __init__(self, kinds=("weapon", "fire")):
        self.kinds, self.triggers, self.levels = kinds, [], []

    def wants(self, incident_type, level):
        return incident_type in self.kinds and level >= 3

    def trigger(self, trigger):
        self.triggers.append(trigger)

    def on_level(self, change):
        self.levels.append(change)


def _fake_pipeline(sos):
    from sentrix.pipeline.engine import Pipeline
    from sentrix.pipeline.incidents import IncidentManager
    fake = Pipeline.__new__(Pipeline)
    fake.cfg = __import__("sentrix.config", fromlist=["settings"]).settings
    fake.incidents = IncidentManager()
    fake.sos = sos
    fake.session_id = "s1"
    fake._committed_level = 1
    fake._tasks = queue.Queue(maxsize=64)
    fake.submitted = []
    fake._submit = lambda func, *a, **k: fake.submitted.append(func.__name__)
    fake._evidence_context = lambda result: {}
    return fake


def _result(kind="weapon"):
    extras = {"evidence": {kind: {"state": "confirmed"}}, "detections": {"all": []}}
    return SimpleNamespace(incident_type=kind, tci=0.5, reason=f"{kind} confirmed",
                           features=SimpleNamespace(extras=extras))


def _drain(fake):
    while not fake._tasks.empty():
        func, args, kwargs = fake._tasks.get_nowait()
        func(*args, **kwargs)


class TestPipelineSosHook:
    def test_one_trigger_per_incident_after_snapshot_and_evidence(self):
        import numpy as np
        from sentrix.pipeline.escalation import plan_for
        sos = RecordingSos()
        fake = _fake_pipeline(sos)
        incident = _incident(fake.incidents)
        frame = np.zeros((4, 4, 3), np.uint8)
        for _ in range(3):
            fake._act(incident, plan_for(3), frame, _result(), 3, "ELEVATED", 0.0)
        _drain(fake)
        assert fake.submitted[:2] == ["_capture_snapshot", "_capture_evidence"]
        assert len(sos.triggers) == 1 and "sos" in incident.actions
        trigger = sos.triggers[0]
        assert (trigger.incident_id, trigger.incident_type, trigger.evidence_state,
                trigger.level) == (incident.id, "weapon", "confirmed", 3)

    def test_unwanted_types_never_claim_so_a_later_weapon_still_triggers(self):
        import numpy as np
        from sentrix.pipeline.escalation import plan_for
        sos = RecordingSos(kinds=("weapon",))
        fake = _fake_pipeline(sos)
        incident = _incident(fake.incidents)
        frame = np.zeros((4, 4, 3), np.uint8)
        fake._act(incident, plan_for(3), frame, _result("intrusion"), 3, "ELEVATED", 0.0)
        assert "sos" not in incident.actions and fake._tasks.empty()
        fake._act(incident, plan_for(3), frame, _result("weapon"), 3, "ELEVATED", 1.0)
        _drain(fake)
        assert len(sos.triggers) == 1

    def test_l2_never_offers_sos(self):
        from sentrix.pipeline.escalation import plan_for
        assert not plan_for(2).sos and plan_for(3).sos and plan_for(5).sos

    def test_level_changes_reach_the_service_and_the_history(self):
        from sentrix.pipeline.engine import Pipeline
        from sentrix.pipeline.state import state
        sos = RecordingSos()
        fake = _fake_pipeline(sos)
        incident = _incident(fake.incidents)
        before = len(state.level_history())
        Pipeline._level_changed(fake, 3, "ELEVATED", _result(), incident, 0.0)
        Pipeline._level_changed(fake, 1, "SECURE", _result(), incident, 9.0)
        assert [(c.previous_level, c.level) for c in sos.levels] == [(1, 3), (3, 1)]
        history = state.level_history()
        assert len(history) == min(before + 2, 50) and history[-1]["level"] == 1


# ── live state ────────────────────────────────────────────────────────────────
class TestStateBlocks:
    def test_new_blocks_have_safe_defaults(self):
        from sentrix.pipeline.state import SystemState
        payload = json.loads(SystemState().ws_payload())
        assert payload["sos"]["active"] is False and "duress" not in payload["sos"]
        assert payload["link"]["connected"] is False
        assert payload["channels"]["outbound_ready"] is False
        assert payload["node"]["cpu_temp_c"] is None
        assert payload["remote_viewers"] == 0 and payload["test_mode"] is True
        assert payload["mode"]["armed"] is False

    def test_remote_viewers_and_reset(self):
        from sentrix.pipeline.state import SystemState
        live = SystemState()
        live.set_remote_viewers(2)
        assert live.remote_viewers == 2 and live.get()["remote_viewers"] == 2
        live.update(sos={"active": True})
        live.reset()
        assert live.remote_viewers == 0 and live.get()["sos"]["active"] is False


class TestRuntimeMode:
    def test_override_arms_the_intrusion_rule_and_clears(self):
        from sentrix import mode
        from sentrix.config import settings
        from sentrix.fusion.features import FeatureVector
        from sentrix.fusion.tci import TCIEngine
        stranger = FeatureVector(person_present=True, authorized=False,
                                 identity_degraded=False)
        try:
            mode.set_armed(True, by="test")
            engine = TCIEngine()
            results = [engine.compute(stranger, now=float(t)) for t in range(6)]
            assert results[-1].incident_type == "intrusion"
            assert mode.snapshot()["source"] == "runtime"
        finally:
            mode.clear_override()
        assert mode.is_armed() == settings.threat.armed


# ── link contract ─────────────────────────────────────────────────────────────
class TestTunnelAuth:
    def test_only_this_process_can_vouch_for_a_portal_user(self):
        from starlette.datastructures import Headers
        from sentrix.link import tunnel_auth as ta
        headers = ta.build_headers(user_id="u1", user_name="Riya <b>", role="owner",
                                   base="/d/dev_1", step_up_at=100.0, request_id="t1",
                                   csrf="c1")
        identity = ta.verify(Headers(headers))
        assert identity.role == "owner" and identity.base == "/d/dev_1"
        assert identity.user_name == "Riya b" and identity.csrf == "c1"
        assert identity.actor == "portal:u1" and identity.stepped_up_within(300, 200.0)
        forged = dict(headers, **{ta.HEADER_SECRET: "guess"})
        assert ta.verify(Headers(forged)) is None
        assert ta.verify(Headers(dict(headers, **{ta.HEADER_ROLE: "company_admin"}))) is None
        assert ta.verify(Headers(dict(headers, **{ta.HEADER_BASE: "/evil"}))) is None
        with pytest.raises(ValueError):
            ta.build_headers(user_id="a", user_name="a", role="company_admin", base="/d/x")

    def test_base_for_request(self):
        from sentrix.link import tunnel_auth as ta
        identity = ta.verify(ta.build_headers(user_id="u", user_name="u", role="member",
                                              base="/d/abc"))
        assert ta.base_for(SimpleNamespace(scope={ta.SCOPE_KEY: identity})) == "/d/abc"
        assert ta.base_for(SimpleNamespace(scope={})) == ""


class TestProtocol:
    def test_envelope_round_trip_and_version_check(self):
        from sentrix.link import protocol as P
        message = P.envelope(P.HEARTBEAT, camera_online=True)
        decoded = P.decode(P.encode(message))
        assert decoded["type"] == "heartbeat" and decoded["v"] == 1
        with pytest.raises(P.ProtocolError):
            P.decode(json.dumps(dict(message, v=2)))
        assert P.COMMANDS and "shell" not in " ".join(P.COMMANDS)
        assert P.SOS_EVENT_KINDS <= set(P.EVENT_KINDS)

    def test_binary_frames(self):
        from sentrix.link import protocol as P
        header, jpeg = P.unpack_binary(P.pack_frame(7, 1234, b"\xff\xd8jpeg"))
        assert (header.kind, header.a, header.b, jpeg) == (P.KIND_FRAME, 7, 1234, b"\xff\xd8jpeg")
        header, data = P.unpack_binary(P.pack_body_chunk(3, 2, b"abc", last=True))
        assert header.kind == P.KIND_BODY and header.flags & P.FLAG_LAST and data == b"abc"
        assert P.HEADER_BYTES == 16
        with pytest.raises(ValueError):
            P.unpack_binary(b"\x01")


class TestSosStates:
    def test_table_is_closed_and_terminal_states_absorb(self):
        from sentrix.sos import states as S
        for (src, event), dst in S.TRANSITIONS.items():
            assert event in S.EVENTS and dst in S.STATES
            assert src is None or src in S.STATES
            assert src not in S.TERMINAL_STATES
        for terminal in S.TERMINAL_STATES:
            for event in (S.APPROVE, S.DECLINE, S.EXPIRE, S.RESOLVE, S.CHECKING):
                assert S.next_state(terminal, event) is None
        assert S.next_state(S.PENDING, S.CHECKING) == S.PENDING
        assert S.next_state(S.APPROVED, S.EXPIRE) is None, "approved never expires"

    def test_reconnect_rule(self):
        from sentrix.sos import states as S
        assert S.reconcile(S.APPROVED, S.EXPIRED) == S.APPROVED      # help path wins
        assert S.reconcile(S.EXPIRED, S.ACKNOWLEDGED) == S.ACKNOWLEDGED
        assert S.reconcile(S.DISPATCHED, S.ACKNOWLEDGED) == S.ACKNOWLEDGED
        assert S.reconcile(S.PENDING, S.EXPIRED) == S.EXPIRED       # cloud terminal
        assert S.reconcile(S.DECLINED, S.EXPIRED) == S.EXPIRED
        assert S.reconcile(S.DECLINED, S.PENDING) == S.DECLINED

    def test_owner_view_hides_duress(self):
        from sentrix.sos import states as S
        view = S.owner_view(S.DISPATCHED, {"duress": True, "repeat": True})
        assert view == {"state": S.DECLINED, "label": "Marked as false alarm",
                        "flags": {"repeat": True}}
        assert S.STATE_LABELS[S.EXPIRED] == "Unanswered"


class TestRedaction:
    def test_camera_credentials_never_leave(self):
        from sentrix.utils.redact import mask_email, mask_phone, redact_url
        assert redact_url("capture:rtsp://admin:hunter2@192.168.1.9:554/s1") == \
            "capture:rtsp://***@192.168.1.9:554/s1"
        assert redact_url("multi:capture:rtsp://u:p@a/x,file:clip.mp4") == \
            "multi:capture:rtsp://***@a/x,file:clip.mp4"
        assert redact_url("http://cam/video?user=a&password=b") == \
            "http://cam/video?user=a&password=***"
        assert redact_url("capture:0") == "capture:0" and redact_url(None) == ""
        assert mask_phone("+919812345610") == "+91 98xxxxxx10"
        assert mask_email("riya.sharma@example.in") == "r***@example.in"

    def test_capture_source_name_is_redacted(self):
        from sentrix.io import sources
        source = sources.CaptureSource.__new__(sources.CaptureSource)
        # Only the naming line matters; the reader thread is never started.
        source.source = "rtsp://admin:hunter2@10.0.0.2/s1"
        name = f"capture:{sources.redact_url(str(source.source))}"
        assert "hunter2" not in name
        text = Path(sources.__file__).read_text(encoding="utf-8")
        assert 'self.name = f"capture:{redact_url(str(source))}"' in text


# ── Local Console app ─────────────────────────────────────────────────────────
class TestLocalConsoleApp:
    def test_api_docs_are_off_and_extensions_load(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from sentrix.app import app
        from sentrix.web import extensions
        client = TestClient(app)                              # no lifespan: no pipeline
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404
        probe = FastAPI()
        assert extensions.install(probe) == [*extensions.ROUTER_MODULES,
                                             *extensions.MIDDLEWARE_MODULES]

    def test_health_fields(self, clean_switches):
        from sentrix import services
        fields = services.health_fields()
        assert fields["test_mode"] is True and fields["send_guard"]["live_alerts"] is False
        assert set(fields) == {"services", "send_guard", "test_mode", "mode"}


# ── Portal skeleton ───────────────────────────────────────────────────────────
@pytest.fixture
def portal(tmp_path):
    from sentrix_portal.app import create_app
    from sentrix_portal.settings import PortalSettings
    settings = PortalSettings(database_url=f"sqlite:///{(tmp_path / 'portal.db').as_posix()}",
                              secret_key="k" * 64, data_key="d" * 64, test_mode=True)
    return create_app(settings)


class TestPortalSkeleton:
    def test_healthz_and_every_table(self, portal):
        from fastapi.testclient import TestClient
        from sentrix_portal.db import Base
        response = TestClient(portal).get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "db": "ok", "test_mode": True,
                                   "version": "0.1.0"}
        tables = set(inspect(portal.state.db.engine).get_table_names())
        assert set(Base.metadata.tables) <= tables
        assert {"users", "sessions", "households", "devices", "assistance_requests",
                "request_events", "notifications", "action_tokens", "audit_log",
                "emergency_contacts", "consents", "incidents_mirror"} <= tables
        assert TestClient(portal).get("/docs").status_code == 404

    def test_production_settings_need_secrets(self):
        from sentrix_portal.settings import PortalSettings
        with pytest.raises(RuntimeError):
            PortalSettings(test_mode=False).checked()
        assert PortalSettings(test_mode=True).checked().secret_key

    def test_passwords_sessions_csrf_and_audit(self, portal):
        from sentrix_portal import security
        from sentrix_portal.models import Household, User
        assert security.verify_password(security.hash_password("s3cret pass"), "s3cret pass")
        assert not security.verify_password(security.hash_password("a"), "b")
        assert not security.verify_password(None, "b")
        code = security.new_code()
        assert len(code) == 9 and code[4] == "-"
        assert security.code_digest(code.lower().replace("-", " ")) == security.code_digest(code)
        assert security.unseal(security.seal("+919812345610")) == "+919812345610"
        with portal.state.db.session() as db:
            home = Household(customer_code="SX-0001", display_name="Demo")
            db.add(home)
            db.flush()
            user = User(username="riya", role="owner", household_id=home.id,
                        status="active", password_hash=security.hash_password("pw"))
            db.add(user)
            db.flush()
            raw, row = security.create_session(db, user, ip="1.2.3.4")
            assert row.id_hash != raw and security.load_session(db, raw)[1].id == user.id
            principal = security.principal_of(
                SimpleNamespace(cookies={"portal_session": raw}), db)
            assert security.verify_csrf(principal, principal.csrf_token)
            assert not security.verify_csrf(principal, "nope")
            security.audit(db, "login", actor_type="user", actor_id=user.id, ip="1.2.3.4")
            security.audit(db, "logout", actor_type="user", actor_id=user.id)
            assert security.verify_audit_chain(db) == {"ok": True, "entries": 2,
                                                       "problems": []}
            assert security.revoke_session(db, raw)
            assert security.load_session(db, raw) is None

    def test_require_role(self, portal):
        from fastapi import Depends
        from fastapi.testclient import TestClient
        from sentrix_portal import security
        from sentrix_portal.models import User

        @portal.get("/api/_probe")
        def probe(p=Depends(security.require_role("owner"))):
            return {"user": p.username}

        @portal.post("/api/_probe")
        def probe_post(p=Depends(security.require_role("owner"))):
            return {"ok": True}

        with portal.state.db.session() as db:
            owner = User(username="o", role="owner", status="active")
            admin = User(username="a", role="company_admin", status="active")
            db.add_all([owner, admin])
            db.flush()
            owner_raw, owner_row = security.create_session(db, owner)
            admin_raw, _ = security.create_session(db, admin)
            csrf = owner_row.csrf_token
        client = TestClient(portal)
        assert client.get("/api/_probe").status_code == 401
        client.cookies.set("portal_session", owner_raw)
        assert client.get("/api/_probe").json() == {"user": "o"}
        assert client.post("/api/_probe").status_code == 403, "CSRF required"
        assert client.post("/api/_probe", headers={"X-CSRF-Token": csrf}).status_code == 200
        client.cookies.set("portal_session", admin_raw)
        assert client.get("/api/_probe").status_code == 403
