"""The device SOS engine (track T-SOS) on a temporary database, with an
injectable clock: time-travel through whole requests, the PIN and duress
rules, verdict semantics by request state, recovery after a restart, the
cloud sync, link events, and the pipeline hook. Messages only ever reach
``FakeTransport`` (the send guard runs in front of them)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from sentrix import listeners, mode
from sentrix.actor import Actor
from sentrix.alerting import guard
from sentrix.alerting.channels.base import (Channel, ChannelRouter, FakeTransport,
                                            MemoryOutbox)
from sentrix.sos import LevelChange, SosTrigger, pins
from sentrix.sos import states as S
from sentrix.sos.policy import Reach
from sentrix.sos.service import SosEngine, build_service, preflight
from sentrix.storage import database, requests

T0 = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc).timestamp()
OWNER_PHONE, CONTACT_PHONE, DESK_PHONE = "+919812345610", "+919812345611", "+919800000001"
SOS_PIN, DURESS_PIN = "246810", "135791"
OWNER = Actor("owner", id="u1", name="Riya Sharma (demo)")
MEMBER = Actor("member", id="m1", name="Dev (demo)")
OFFLINE = Reach(online=False, standalone=False, remote_prompt=False)
ONLINE = Reach(online=True, standalone=False, remote_prompt=True)
STANDALONE = Reach(online=False, standalone=True, remote_prompt=False)


class Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Channels:
    """A ChannelsService over the real router, every transport a fake."""

    name = "channels"

    def __init__(self, clock: Clock, allow: set[str]) -> None:
        policy = guard.SendPolicy(test_mode=True, live_alerts=True,
                                  switches={c: True for c in guard.CHANNEL_SWITCHES},
                                  allowlist=frozenset(allow))
        self.transports = {n: FakeTransport(n) for n in guard.CHANNEL_SWITCHES}
        self.router = ChannelRouter({n: Channel(n, t, policy_factory=lambda: policy)
                                     for n, t in self.transports.items()},
                                    MemoryOutbox(), clock=clock)

    def enqueue(self, recipient, message):
        return self.router.enqueue(recipient, message)

    def ready(self, channel):
        return self.router.ready(channel)

    def add_listener(self, fn):
        self.router.add_listener(fn)

    def remove_listener(self, fn):
        self.router.remove_listener(fn)

    def status(self):
        return self.router.status()

    def start(self):
        pass

    def stop(self):
        pass

    def health(self):
        return {}

    def items(self, request_id=None):
        return [i for i in self.router.outbox.items()
                if request_id is None or i.message.request_id == request_id]

    def keys(self, request_id=None):
        return sorted((i.recipient.ref, i.recipient.channel, i.message.step)
                      for i in self.items(request_id))

    def sent(self):
        return [(r.ref, t.name, m.step, m.text) for t in self.transports.values()
                for r, m in t.sent]


class FakeEvents:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict]] = []

    def audit(self, event, **detail):
        self.rows.append((event, detail))

    def names(self):
        return [name for name, _ in self.rows]


class FakePipeline:
    def __init__(self) -> None:
        self.events = FakeEvents()
        self.session_id = "sess-test"
        self.incidents = SimpleNamespace(current=None)
        self.closed: list[str] = []
        self._overlay = None

    def request_close_incident(self, reason="demo"):
        self.closed.append(reason)
        return True


def settings_with(**sos_changes):
    """The live settings with some ``settings.sos`` fields changed."""
    from dataclasses import replace
    from sentrix.config import settings
    copy = SimpleNamespace(**{f: getattr(settings, f) for f in settings.__dataclass_fields__})
    copy.sos = replace(settings.sos, **sos_changes)
    copy.test_mode = settings.test_mode
    return copy


def cheap_hash(pin: str) -> str:
    from argon2 import PasswordHasher
    return PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1).hash(pin)


HASHES = {}


def make_profile(**sos):
    if not HASHES:
        HASHES.update(pin=cheap_hash(SOS_PIN), duress=cheap_hash(DURESS_PIN))
    return {
        "schema": 1, "version": 5, "customer_code": "SX-0007", "test_mode": True,
        "owner": {"user_id": "u1", "name": "Riya Sharma (demo)", "phone": OWNER_PHONE},
        "contacts": [{"id": "c1", "name": "Aman (demo)", "relation": "brother",
                      "phone": CONTACT_PHONE, "telegram": False, "priority": 1,
                      "consent_status": "CONFIRMED"},
                     {"id": "c2", "name": "Not yet", "phone": "+919812345699",
                      "consent_status": "PENDING"}],
        "desk": {"name": "SENTRIX Desk (simulated)", "phone": DESK_PHONE, "simulated": True},
        "address": {"house": "H.No. 212", "street": "Green Avenue", "city": "Patiala",
                    "pincode": "147001"},
        "cameras": [{"camera_id": "CAM_0", "room_label": "Living room"}],
        "sos": {"window_seconds": 1800, "safety_defaults": True,
                "pin_hash": HASHES["pin"], "duress_pin_hash": HASHES["duress"],
                "pin_version": 1, **sos},
        "consents": {"sos_snapshot": True, "medical_notes": False},
    }


@pytest.fixture
def world(temp_db, tmp_path):
    from sentrix.pipeline.state import SystemState
    clock = Clock()
    channels = Channels(clock, allow={OWNER_PHONE, DESK_PHONE})
    holder = {"profile": make_profile(), "reach": STANDALONE}
    published: list[tuple[str, dict, str | None]] = []
    pipeline = FakePipeline()
    state = SystemState()
    engine = SosEngine(clock=clock, state=state, channels=lambda: channels,
                       pipeline=lambda: pipeline, profile=lambda: holder["profile"],
                       publish=lambda kind, payload, dedupe_key=None:
                       published.append((kind, payload, dedupe_key)),
                       reach=lambda: holder["reach"],
                       limiter=pins.PinLimiter(5, 600, clock=clock), snapshot_dir=tmp_path)
    engine._sweeper.start = lambda: None          # the tests drive work() themselves
    engine.start()
    ns = SimpleNamespace(clock=clock, channels=channels, holder=holder,
                         published=published, pipeline=pipeline, state=state,
                         engine=engine, tmp=tmp_path)
    yield ns
    ns.engine.stop()                              # a test may have "restarted" it
    engine.stop()


def new_incident(kind="weapon", level=3) -> str:
    from sentrix.pipeline.incidents import IncidentManager
    incident, _ = IncidentManager().update(level, 0.5, kind, now=0.0, features={},
                                           reason=f"{kind} confirmed",
                                           trigger={kind: {"state": "confirmed"}})
    database.save_incident(incident, "sess-test", ongoing=True)
    return incident.id


def fire_trigger(w, kind="weapon", level=3, evidence="confirmed", incident_id=None,
                 snapshot=None) -> str:
    incident_id = incident_id or new_incident(kind, level)
    w.engine.trigger(SosTrigger(incident_id=incident_id, level=level, incident_type=kind,
                                evidence_state=evidence, tci=0.5, reason=f"{kind} confirmed",
                                session_id="sess-test", at_wall=w.clock(),
                                snapshot_path=snapshot))
    w.engine.work()
    return incident_id


def advance(w, seconds: float, tick: float = 3600.0) -> None:
    """The sweeper and the channel worker: jump to the next due timer (what
    ``work()`` returns, as the sweeper thread sleeps), at most ``tick``."""
    end = w.clock.now + seconds
    while True:
        w.channels.router.pump()
        next_in = w.engine.work()
        if w.clock.now >= end:
            break
        w.clock.now = min(end, w.clock.now + min(tick, max(0.5, next_in or tick)))


def the_request():
    found = requests.list_requests(5)
    assert found, "no request was opened"
    return found[0]


def event_names(request_id, restricted=True):
    return [e["event"] for e in requests.events(request_id, include_restricted=restricted)]


def package_of(request_id):
    from sentrix.storage.models import DispatchRecord
    with database.session_scope() as session:
        row = session.query(DispatchRecord).filter_by(request_id=request_id).first()
        return database.get_dispatch(row.id) if row else None


def incident_row(incident_id):
    return next(r for r in database.recent_incidents(50) if r["id"] == incident_id)


# ── opening ───────────────────────────────────────────────────────────────────
class TestOpening:
    def test_l3_opens_a_pending_request_with_its_package(self, world):
        incident = fire_trigger(world)
        req = the_request()
        assert req["state"] == S.PENDING and req["incident_id"] == incident
        assert req["expires_at"] == datetime.fromtimestamp(T0 + 1800, timezone.utc).isoformat()
        assert req["policy"]["window_seconds"] == 1800
        assert [c["id"] for c in req["policy"]["contacts"]] == ["c1"]
        assert req["test_mode"] and req["sos_text_full"].startswith(guard.TEST_PREFIX)
        assert "possible WEAPON (one camera check)" in req["sos_text_full"]
        package = package_of(req["id"])
        assert package["status"] == "PENDING" and package["user_name"] == "Riya Sharma (demo)"
        assert package["user_phone"] == OWNER_PHONE and package["authority"] == "POLICE"
        assert req["package_id"] == package["id"]
        assert incident_row(incident)["request_id"] == req["id"]
        assert event_names(req["id"]) == ["created", "prompted"]
        assert requests.verify_chain(req["id"])["ok"]

    def test_the_lc_block_offers_call_112_first(self, world):
        fire_trigger(world, "fire")
        block = world.state.get()["sos"]
        assert block["active"] and block["state"] == S.PENDING
        assert block["actions"] == ["call_112", "approve", "checking", "decline"]
        assert block["recommended_authority"] == "FIRE"
        assert block["desk_number"] == DESK_PHONE and block["remaining_s"] == 1800
        assert "duress" not in json.dumps(block)

    def test_owner_is_texted_at_t0_when_no_remote_prompt_is_possible(self, world):
        fire_trigger(world)
        req = the_request()
        assert ("owner", "modem_sms", "t0") in world.channels.keys(req["id"])
        world.channels.router.pump()
        sent = [s for s in world.channels.sent() if s[2] == "t0"]
        assert sent and sent[0][3].startswith(guard.TEST_PREFIX_GSM)

    def test_online_the_portal_prompts_and_the_device_sends_nothing(self, world):
        world.holder["reach"] = ONLINE
        fire_trigger(world)
        assert world.channels.keys() == []
        kinds = [k for k, _, _ in world.published]
        assert kinds[:2] == ["sos.created", "sos.transition"]

    @pytest.mark.parametrize("kind,level", [("assault", 3), ("robbery", 3), ("weapon", 2),
                                            ("intrusion", 3), ("normal", 3)])
    def test_nothing_else_opens_a_request(self, world, kind, level):
        fire_trigger(world, kind, level)
        assert requests.list_requests(5) == []

    def test_one_open_request_later_incidents_attach(self, world):
        first = fire_trigger(world)
        second = fire_trigger(world, "fire")            # e.g. the 300 s rollover
        assert len(requests.list_requests(5)) == 1
        req = the_request()
        assert req["incident_ids"] == [first, second]
        assert incident_row(second)["request_id"] == req["id"]
        assert "attached" in event_names(req["id"])
        fire_trigger(world, incident_id=second)           # idempotent per incident
        assert event_names(req["id"]).count("attached") == 1

    def test_level_rise_is_urgent_restarts_the_window_and_alerts_contacts(self, world):
        incident = fire_trigger(world)
        world.clock.now += 1500
        world.engine.on_level(LevelChange(level=4, previous_level=3, incident_id=incident,
                                          incident_type="weapon",
                                          evidence_state="corroborated", tci=0.8,
                                          at_wall=world.clock(), at_mono=0.0))
        world.engine.work()
        req = the_request()
        assert req["urgency"] == "HIGH" and req["peak_level"] == 4
        assert req["expires_at"] == datetime.fromtimestamp(T0 + 1500 + 1800,
                                                           timezone.utc).isoformat()
        assert "confirmed by two different models" in req["sos_text_full"]
        names = event_names(req["id"])
        assert "level_rise" in names and "contacts_notified" in names
        keys = world.channels.keys(req["id"])
        assert ("owner", "modem_sms", "rise4") in keys
        assert ("contact:c1", "modem_sms", "contacts_t0") in keys

    def test_a_level_rise_reported_before_the_trigger_is_not_lost(self, world):
        incident = new_incident()
        world.engine.on_level(LevelChange(level=4, previous_level=3, incident_id=incident,
                                          incident_type="weapon",
                                          evidence_state="corroborated", tci=0.8,
                                          at_wall=world.clock(), at_mono=0.0))
        fire_trigger(world, incident_id=incident)
        req = the_request()
        assert req["peak_level"] == 4 and req["urgency"] == "HIGH"
        assert "contacts_notified" in event_names(req["id"])


# ── the owner's timeline ──────────────────────────────────────────────────────
class TestTimeline:
    def test_reminders_lapse_and_expiry(self, world):
        world.holder["reach"] = ONLINE
        incident = fire_trigger(world)
        req = the_request()
        advance(world, 1801)
        req = requests.get_request(req["id"])
        assert req["state"] == S.EXPIRED
        steps = [e["detail"].get("step") for e in requests.events(req["id"])
                 if e["event"] in ("reminder", "lapse_warning")]
        assert steps == ["r120", "r300", "r600", "r1200", "lapse"]
        assert package_of(req["id"])["status"] == "EXPIRED"
        assert incident_row(incident)["verdict"] is None, "unanswered is not reviewed"
        assert world.state.get()["sos"]["last"]["label"] == "Unanswered"
        # Online, the device texts the owner only once unseen for 10 min.
        assert world.channels.keys(req["id"]) == [("owner", "modem_sms", "r1200"),
                                                  ("owner", "modem_sms", "r600")]

    def test_steps_are_never_sent_twice(self, world):
        fire_trigger(world, "fire")
        req = the_request()
        world.clock.now += 650
        for _ in range(5):
            world.engine.work()
            world.engine.sweep()
        keys = world.channels.keys(req["id"])
        assert len(keys) == len(set(keys))
        names = [e["detail"].get("step") for e in requests.events(req["id"])
                 if e["event"] == "reminder"]
        assert names == ["r600"], "overdue reminders collapse into one"

    def test_checking_pauses_reminders_only(self, world):
        world.holder["reach"] = ONLINE
        fire_trigger(world, "fire")
        req = the_request()
        world.clock.now += 100
        result = world.engine.respond(req["id"], "checking", actor=OWNER, channel="portal")
        assert result["ok"] and result["request"]["seen_at"]
        advance(world, 150)                                  # T+250: r120 snoozed
        events = requests.events(req["id"])
        assert not [e for e in events if e["event"] == "reminder"]
        assert [e["detail"]["reason"] for e in events
                if e["event"] == "contacts_notified"] == ["confirmed_fire"]
        advance(world, 200)                                  # T+450: r300 after the snooze
        assert [e["detail"]["step"] for e in requests.events(req["id"])
                if e["event"] == "reminder"] == ["r300"]

    def test_the_demo_window_in_test_mode(self, world):
        world.engine.settings = settings_with(demo_window_seconds=75)
        fire_trigger(world)
        req = the_request()
        assert req["policy"]["window_seconds"] == 75
        advance(world, 80)
        assert requests.get_request(req["id"])["state"] == S.EXPIRED

    def test_a_window_shorter_than_the_fire_delay_still_alerts_the_contacts(self, world):
        # Live smoke run: with the 90 s demo window a confirmed fire expired
        # before the T+2 min safety default, so the contacts were never told.
        world.holder["reach"] = ONLINE
        world.engine.settings = settings_with(demo_window_seconds=90)
        fire_trigger(world, "fire")
        req = the_request()
        advance(world, 95)
        assert requests.get_request(req["id"])["state"] == S.EXPIRED
        events = requests.events(req["id"])
        names = [e["event"] for e in events]
        assert names.index("contacts_notified") < names.index("expire")
        assert [e["detail"]["reason"] for e in events
                if e["event"] == "contacts_notified"] == ["confirmed_fire"]
        published = [p for kind, p, _ in world.published if kind == "sos.event"
                     and p.get("event", {}).get("event") == "contacts_notified"]
        assert published or any("contacts_notified" in repr(p) for _, p, _ in
                                world.published), "the Portal is told to page them"


# ── answers ───────────────────────────────────────────────────────────────────
class TestAnswers:
    def test_approve_then_delivered_by_the_modem_then_acknowledged_and_resolved(self, world):
        incident = fire_trigger(world)
        req = the_request()
        world.clock.now += 40
        result = world.engine.respond(req["id"], "approve", actor=OWNER, channel="lc",
                                      authority="MEDICAL")
        assert result["ok"] and result["request"]["state"] == S.APPROVED
        assert package_of(req["id"])["chosen_authority"] == "MEDICAL"
        keys = world.channels.keys(req["id"])
        assert ("desk", "modem_sms", "desk_t0") in keys
        assert ("contact:c1", "modem_sms", "contacts_t0") in keys
        advance(world, 1)                 # the Desk SMS is SENT (allowlisted)
        req = requests.get_request(req["id"])
        assert req["state"] == S.DISPATCHED
        package = package_of(req["id"])
        assert package["status"] == "SENT" and package["sent_by"] == "portal:u1"
        assert package["chosen_authority"] == "MEDICAL" and package["sent_at"]
        assert package["sent_to"][0] == "SENTRIX Desk"
        assert "+91 98xxxxxx11" in package["sent_to"][1], "recipients are masked"
        operator = Actor("operator", id="d1", name="Desk One")
        assert world.engine.cloud_event(req["id"], "acknowledge", actor=operator)["ok"]
        done = world.engine.cloud_event(req["id"], "resolve", actor=operator,
                                        detail={"outcome": "help_arrived"})
        assert done["ok"] and done["request"]["state"] == S.CLOSED
        assert incident_row(incident)["verdict"] == "TRUE"
        assert requests.verify_chain(req["id"])["ok"]

    def test_an_approved_request_never_expires(self, world):
        world.holder["reach"] = ONLINE
        fire_trigger(world)
        req = the_request()
        world.engine.respond(req["id"], "approve", actor=MEMBER, channel="portal")
        world.engine.cloud_event(req["id"], "delivered", actor=Actor.cloud())
        advance(world, 3 * 3600, tick=60)
        state = requests.get_request(req["id"])["state"]
        assert state in S.HELP_PATH and state != S.CLOSED
        repages = [e["detail"]["step"] for e in requests.events(req["id"])
                   if e["event"] == "repage"]
        assert repages == ["repage1", "repage2", "repage3"], "test mode caps at 3"
        assert requests.get_request(req["id"])["flags"]["unacknowledged"]

    def test_a_dry_run_never_counts_as_delivered(self, world):
        world.channels = Channels(world.clock, allow=set())     # everything SUPPRESSED
        world.engine._channels_getter = lambda: world.channels
        world.channels.add_listener(world.engine._on_outbox)
        fire_trigger(world)
        req = the_request()
        world.engine.respond(req["id"], "approve", actor=OWNER, channel="lc")
        advance(world, 130)
        req = requests.get_request(req["id"])
        assert req["state"] == S.FAILED
        assert world.state.get()["sos"]["label"] == "NOT DELIVERED - call 112 now"
        statuses = {e["detail"]["status"] for e in requests.events(req["id"])
                    if e["event"] == "notification"}
        assert statuses == {"SUPPRESSED"}
        assert package_of(req["id"])["status"] == "PENDING"

    def test_decline_needs_the_pin_and_cancels(self, world):
        incident = fire_trigger(world, "fire")
        req = the_request()
        advance(world, 125)                                 # contacts alerted (fire T+2)
        wrong = world.engine.respond(req["id"], "decline", actor=OWNER, channel="lc",
                                     pin="000000")
        assert wrong["code"] == "pin_invalid" and wrong["status"] == 403
        assert "pin_failed" in event_names(req["id"])
        details = json.dumps([e["detail"] for e in requests.events(req["id"])])
        assert "000000" not in details, "a PIN never reaches the audit trail"
        ok = world.engine.respond(req["id"], "decline", actor=OWNER, channel="lc", pin=SOS_PIN)
        assert ok["ok"] and ok["request"]["state"] == S.DECLINED
        assert incident_row(incident)["verdict"] == "FALSE"
        assert package_of(req["id"])["status"] == "CANCELLED"
        assert ("contact:c1", "modem_sms", "resolved") in world.channels.keys(req["id"])
        last = world.state.get()["sos"]["last"]
        assert last["state"] == S.DECLINED and last["actions"] == ["help_now"]

    def test_five_wrong_pins_lock_the_decline_but_never_help(self, world):
        fire_trigger(world)
        req = the_request()
        for _ in range(5):
            world.engine.respond(req["id"], "decline", actor=OWNER, channel="lc", pin="111111")
        locked = world.engine.respond(req["id"], "decline", actor=OWNER, channel="lc",
                                      pin=SOS_PIN)
        assert locked["code"] == "pin_locked" and locked["status"] == 429
        assert world.engine.respond(req["id"], "approve", actor=OWNER, channel="lc")["ok"]

    def test_duress_looks_like_a_decline_and_calls_for_help(self, world):
        incident = fire_trigger(world)
        req = the_request()
        result = world.engine.respond(req["id"], "decline", actor=OWNER, channel="portal",
                                      pin=DURESS_PIN)
        assert result["ok"] and result["message"] == "Marked as false alarm"
        shown = result["request"]
        assert shown["state"] == S.DECLINED and "duress" not in shown["flags"]
        full = requests.get_request(req["id"])
        assert full["state"] == S.APPROVED and full["flags"]["duress"]
        # Owners, members and the LC see a decline everywhere.
        assert world.engine.current("owner") is None
        block = world.state.get()["sos"]
        assert not block["active"] and block["last"]["label"] == "Marked as false alarm"
        owner_rows = world.engine.events_view(req["id"], "owner")
        assert [r["event"] for r in owner_rows] == ["created", "prompted", "decline"]
        assert [r["n"] for r in owner_rows] == [1, 2, 3]
        assert "duress" not in json.dumps(owner_rows)
        from sentrix.alerting.dispatch import owner_view
        assert owner_view(package_of(req["id"]))["status"] == "CANCELLED"
        assert package_of(req["id"])["status"] == "PENDING"
        # The Desk and the contacts get the duress banner.
        keys = world.channels.keys(req["id"])
        assert ("desk", "modem_sms", "duress") in keys
        assert ("contact:c1", "modem_sms", "duress") in keys
        assert not [k for k in keys if k[0] == "owner" and k[2] != "t0"]
        desk = world.engine.current("desk")
        assert desk["duress_banner"].endswith("Do not go to the home. Call 112.")
        # Every later event is restricted; the chain still verifies.
        events = requests.events(req["id"], include_restricted=True)
        assert all(e["restricted"] for e in events[2:])
        assert requests.verify_chain(req["id"])["ok"]
        # A FALSE label afterwards changes nothing about the help.
        assert world.engine.false_verdict(incident, pin=SOS_PIN, actor=OWNER,
                                          channel="lc")["ok"]
        assert requests.get_request(req["id"])["state"] == S.APPROVED
        assert incident_row(incident)["verdict"] == "FALSE"

    def test_duress_pin_on_a_closed_request_answers_like_one_and_gets_help(self, world):
        world.holder["reach"] = ONLINE
        fire_trigger(world)
        old = the_request()
        advance(world, 1801)
        assert requests.get_request(old["id"])["state"] == S.EXPIRED
        real = world.engine.respond(old["id"], "decline", actor=OWNER, channel="lc",
                                    pin=SOS_PIN)
        duress = world.engine.respond(old["id"], "decline", actor=OWNER, channel="lc",
                                      pin=DURESS_PIN)
        assert real["code"] == duress["code"] == "conflict"
        assert real["message"] == duress["message"] and real["request"] == duress["request"]
        silent = [r for r in requests.list_requests(5) if r["id"] != old["id"]]
        assert len(silent) == 1 and silent[0]["state"] == S.APPROVED
        assert silent[0]["flags"]["silent"] and silent[0]["parent_request_id"] == old["id"]
        assert world.engine.history() == [world.engine.project(
            requests.get_request(old["id"]))]

    def test_safe_on_the_help_path_keeps_help_open(self, world):
        world.holder["reach"] = ONLINE
        fire_trigger(world)
        req = the_request()
        world.engine.respond(req["id"], "approve", actor=OWNER, channel="lc")
        world.engine.cloud_event(req["id"], "delivered", actor=Actor.cloud())
        result = world.engine.respond(req["id"], "safe", actor=OWNER, channel="lc",
                                      pin=SOS_PIN)
        assert result["ok"]
        req = requests.get_request(req["id"])
        assert req["state"] == S.DISPATCHED and req["flags"]["resident_safe"]
        assert world.state.get()["sos"]["actions"] == ["call_112"]

    def test_help_now_after_expiry_opens_a_linked_approved_request(self, world):
        world.holder["reach"] = ONLINE
        fire_trigger(world)
        old = the_request()
        advance(world, 1801, tick=60)
        assert requests.get_request(old["id"])["state"] == S.EXPIRED
        result = world.engine.respond(old["id"], "help_now", actor=OWNER, channel="lc",
                                      authority="POLICE")
        assert result["ok"]
        new = result["request"]
        assert new["id"] != old["id"] and new["parent_request_id"] == old["id"]
        assert new["state"] == S.APPROVED and new["incident_ids"] == old["incident_ids"]
        assert "help_requested_later" in event_names(old["id"])
        assert package_of(new["id"])["status"] == "PENDING"
        assert package_of(old["id"])["status"] == "EXPIRED"

    def test_help_now_while_another_request_is_open_answers_that_one(self, world):
        world.holder["reach"] = ONLINE
        fire_trigger(world)
        old = the_request()
        advance(world, 1801)
        fire_trigger(world, "fire")
        current = the_request()
        assert current["id"] != old["id"] and current["state"] == S.PENDING
        result = world.engine.respond(old["id"], "help_now", actor=OWNER, channel="lc")
        assert result["ok"] and result["request"]["id"] == current["id"]
        assert result["request"]["state"] == S.APPROVED
        assert len(requests.open_requests()) == 1

    def test_a_contact_approves_only_under_safety_defaults(self, world):
        fire_trigger(world)
        req = the_request()
        stranger = Actor("contact", id="c2", name="Not yet")   # PENDING consent
        assert world.engine.respond(req["id"], "approve", actor=stranger,
                                    channel="contact_page")["code"] == "forbidden_role"
        contact = Actor("contact", id="c1", name="Aman")
        result = world.engine.respond(req["id"], "approve", actor=contact,
                                      channel="contact_page")
        assert result["ok"] and requests.get_request(req["id"])["responded_by"] == "contact:c1"

    def test_policy_changes_apply_to_future_requests_only(self, world):
        fire_trigger(world, "fire")
        req = the_request()
        world.holder["profile"] = make_profile(safety_defaults=False, window_seconds=3600)
        advance(world, 125)
        assert "contacts_notified" in event_names(req["id"])
        assert requests.get_request(req["id"])["policy"]["window_seconds"] == 1800


# ── verdicts by request state ─────────────────────────────────────────────────
class TestFalseVerdict:
    def test_no_request_keeps_the_pre_portal_rule(self, world):
        from sentrix.alerting.dispatch import DispatchPackage
        incident = new_incident()
        database.save_dispatch(DispatchPackage(
            id="legacy", created_at="2026-10-14T00:00:00+00:00", incident_id=incident,
            incident_type="weapon", level=4, tci=0.7, user_name="u", user_address="a",
            user_phone="", camera_location="c", evidence_bundle=None, evidence_hash=None,
            authority="POLICE", status="PENDING", payload_json="{}"))
        assert world.engine.false_verdict(incident, pin=None, actor=OWNER, channel="lc")["ok"]
        assert database.get_dispatch("legacy")["status"] == "CANCELLED"
        assert world.engine.false_verdict("nope", pin=None, actor=OWNER,
                                          channel="lc")["code"] == "not_found"

    def test_prompting_needs_the_pin_then_cancels(self, world):
        incident = fire_trigger(world, "fire")
        req = the_request()
        advance(world, 125)
        assert world.engine.false_verdict(incident, pin=None, actor=OWNER,
                                          channel="lc")["code"] == "pin_invalid"
        result = world.engine.false_verdict(incident, pin=SOS_PIN, actor=OWNER, channel="lc")
        assert result["ok"] and result["request"]["state"] == S.CANCELLED
        assert package_of(req["id"])["status"] == "CANCELLED"
        assert ("contact:c1", "modem_sms", "resolved") in world.channels.keys(req["id"])
        assert any(k == "sos.transition" and p["event"]["event"] == "cancel"
                   for k, p, _ in world.published)

    def test_an_attached_incident_cancels_the_requests_package(self, world):
        fire_trigger(world)
        second = fire_trigger(world, "fire")
        req = the_request()
        assert world.engine.false_verdict(second, pin=SOS_PIN, actor=OWNER, channel="lc")["ok"]
        assert requests.get_request(req["id"])["state"] == S.CANCELLED
        assert package_of(req["id"])["status"] == "CANCELLED"

    def test_help_path_marks_safe_and_keeps_the_package(self, world):
        world.holder["reach"] = ONLINE
        incident = fire_trigger(world)
        req = the_request()
        world.engine.respond(req["id"], "approve", actor=OWNER, channel="lc")
        world.engine.cloud_event(req["id"], "delivered", actor=Actor.cloud())
        assert world.engine.false_verdict(incident, pin=SOS_PIN, actor=OWNER, channel="lc")["ok"]
        req = requests.get_request(req["id"])
        assert req["state"] == S.DISPATCHED and req["flags"]["resident_safe"]
        assert package_of(req["id"])["status"] == "SENT"
        assert incident_row(incident)["verdict"] == "FALSE"

    def test_duress_pin_on_a_verdict_approves_silently(self, world):
        incident = fire_trigger(world)
        req = the_request()
        result = world.engine.false_verdict(incident, pin=DURESS_PIN, actor=OWNER, channel="lc")
        assert result["ok"] and result["request"]["state"] == S.DECLINED
        assert requests.get_request(req["id"])["state"] == S.APPROVED
        assert incident_row(incident)["verdict"] == "FALSE"

    def test_terminal_request_label_only_without_pin(self, world):
        world.holder["reach"] = ONLINE
        incident = fire_trigger(world)
        advance(world, 1801, tick=60)
        assert world.engine.false_verdict(incident, pin=None, actor=OWNER, channel="lc")["ok"]
        assert incident_row(incident)["verdict"] == "FALSE"

    def test_the_desk_does_not_label_through_here(self, world):
        incident = fire_trigger(world)
        assert world.engine.false_verdict(incident, pin=SOS_PIN,
                                          actor=Actor("operator", id="d"),
                                          channel="desk")["code"] == "forbidden_role"


# ── repeats ───────────────────────────────────────────────────────────────────
class TestRepeats:
    def test_reprompt_after_decline_at_most_once_per_ten_minutes(self, world):
        fire_trigger(world)
        first = the_request()
        world.engine.respond(first["id"], "decline", actor=OWNER, channel="lc", pin=SOS_PIN)
        world.clock.now += 60
        fire_trigger(world)
        repeat = the_request()
        assert repeat["id"] != first["id"] and repeat["flags"]["repeat"]
        assert world.state.get()["sos"]["repeat"]
        world.engine.respond(repeat["id"], "decline", actor=OWNER, channel="lc", pin=SOS_PIN)
        world.clock.now += 60
        suppressed = fire_trigger(world)
        assert len(requests.list_requests(10)) == 2
        assert "sos_suppressed" in world.pipeline.events.names()
        # Stronger evidence than what was declined always prompts.
        world.engine.on_level(LevelChange(level=4, previous_level=3, incident_id=suppressed,
                                          incident_type="weapon",
                                          evidence_state="corroborated", tci=0.8,
                                          at_wall=world.clock(), at_mono=0.0))
        world.engine.work()
        assert len(requests.list_requests(10)) == 3
        world.clock.now += 700
        world.engine.respond(the_request()["id"], "decline", actor=OWNER, channel="lc",
                             pin=SOS_PIN)
        world.clock.now += 700
        fire_trigger(world)
        assert not the_request()["flags"].get("repeat")


# ── armed intrusion ───────────────────────────────────────────────────────────
def intrusion_level(w, incident, level=3):
    w.engine.on_level(LevelChange(level=level, previous_level=2, incident_id=incident,
                                  incident_type="intrusion", evidence_state="none", tci=0.45,
                                  at_wall=w.clock(), at_mono=0.0))
    w.engine.work()


class TestIntrusion:
    def test_opens_after_the_entry_delay_and_alerts_contacts_at_once(self, world):
        mode.set_armed(True, by="test")
        incident = new_incident("intrusion")
        intrusion_level(world, incident)
        assert requests.list_requests(5) == []
        assert world.engine.health()["held_intrusions"] == 1
        advance(world, 31, tick=5)
        req = the_request()
        assert req["incident_type"] == "intrusion" and req["state"] == S.PENDING
        assert "contacts_notified" in event_names(req["id"])

    def test_the_sos_pin_disarms_within_the_delay(self, world):
        mode.set_armed(True, by="test")
        intrusion_level(world, new_incident("intrusion"))
        assert world.engine.set_mode(False, pin="000000", actor=OWNER)["code"] == "pin_invalid"
        assert mode.is_armed()
        assert world.engine.set_mode(False, pin=SOS_PIN, actor=OWNER)["ok"]
        assert not mode.is_armed()
        advance(world, 40, tick=5)
        assert requests.list_requests(5) == []

    def test_an_authorised_face_disarms_visibly(self, world):
        mode.set_armed(True, by="test")
        intrusion_level(world, new_incident("intrusion"))
        world.state.update(authorized=True, identity_degraded=False, person_count=1)
        world.pipeline._overlay = SimpleNamespace(detections={"persons": [{"identity": "Riya"}]})
        advance(world, 31, tick=5)
        assert requests.list_requests(5) == []
        assert not mode.is_armed() and mode.snapshot()["changed_by"] == "face:Riya"

    def test_an_alarm_over_before_the_delay_opens_nothing(self, world):
        mode.set_armed(True, by="test")
        incident = new_incident("intrusion")
        intrusion_level(world, incident)
        intrusion_level(world, incident, level=1)
        advance(world, 31, tick=5)
        assert requests.list_requests(5) == []

    def test_home_mode_never_holds_an_intrusion(self, world):
        intrusion_level(world, new_incident("intrusion"))
        advance(world, 31, tick=5)
        assert requests.list_requests(5) == [] and world.engine.health()["held_intrusions"] == 0


class TestMode:
    def test_arm_needs_no_pin_disarm_does(self, world):
        assert world.engine.set_mode(True, pin=None, actor=MEMBER)["ok"] and mode.is_armed()
        assert world.engine.set_mode(False, pin=None, actor=MEMBER)["code"] == "pin_invalid"
        assert world.engine.set_mode(False, pin=SOS_PIN, actor=MEMBER)["ok"]
        assert world.engine.set_mode(True, pin=None,
                                     actor=Actor("contact", id="c1"))["code"] == "forbidden_role"

    def test_duress_pin_disarms_and_opens_a_silent_request(self, world):
        mode.set_armed(True, by="test")
        result = world.engine.set_mode(False, pin=DURESS_PIN, actor=OWNER)
        assert result["ok"] and result["message"] == "Disarmed" and not mode.is_armed()
        req = the_request()
        assert req["incident_type"] == "duress" and req["state"] == S.APPROVED
        assert req["flags"]["duress"] and req["flags"]["silent"]
        assert ("desk", "modem_sms", "duress") in world.channels.keys(req["id"])
        # The household never sees it: no request, no incident row, no history.
        assert world.engine.current("owner") is None and world.engine.history() == []
        assert world.engine.events_view(req["id"], "owner") == []
        assert req["incident_id"] is None and database.recent_incidents(10) == []
        assert not world.state.get()["sos"]["active"] and world.state.get()["sos"]["last"] is None
        assert all(e["restricted"] for e in requests.events(req["id"], include_restricted=True))
        assert world.engine.current("desk")["duress_banner"]

    def test_duress_pin_disarm_with_an_open_request_approves_it(self, world):
        fire_trigger(world)
        req = the_request()
        assert world.engine.set_mode(False, pin=DURESS_PIN, actor=OWNER)["ok"]
        full = requests.get_request(req["id"])
        assert full["state"] == S.APPROVED and full["flags"]["duress"]
        assert len(requests.list_requests(5)) == 1


# ── restart ───────────────────────────────────────────────────────────────────
class TestRecovery:
    def restart(self, world):
        world.engine.stop()
        fresh = SosEngine(clock=world.clock, state=world.state,
                          channels=lambda: world.channels, pipeline=lambda: world.pipeline,
                          profile=lambda: world.holder["profile"],
                          publish=lambda *a, **k: None, reach=lambda: world.holder["reach"],
                          limiter=pins.PinLimiter(5, 600, clock=world.clock))
        fresh._sweeper.start = lambda: None
        world.engine = fresh
        fresh.start()
        return fresh

    def test_overdue_pending_expires_while_the_device_was_off(self, world):
        fire_trigger(world)
        req = the_request()
        world.clock.now += 4000
        self.restart(world)
        req = requests.get_request(req["id"])
        assert req["state"] == S.EXPIRED
        expire = [e for e in requests.events(req["id"]) if e["event"] == "expire"][0]
        assert expire["detail"]["reason"] == "expired while the device was off"

    def test_a_draft_inside_its_window_is_prompted(self, world):
        incident = new_incident()
        requests.create_request("draft-1", incident_id=incident, incident_type="weapon",
                                level=3, evidence_state="confirmed",
                                policy={"window_seconds": 1800},
                                expires_at=datetime.fromtimestamp(T0 + 1800, timezone.utc),
                                test_mode=True, at=datetime.fromtimestamp(T0, timezone.utc))
        world.clock.now += 60
        assert self.restart(world).recover() == {"expired": 0, "prompted": 0,
                                                 "reannounced": 1, "resumed": 0}
        assert requests.get_request("draft-1")["state"] == S.PENDING

    def test_approved_resumes_and_cloud_held_requests_wait(self, world):
        fire_trigger(world)
        req = the_request()
        world.engine.respond(req["id"], "approve", actor=OWNER, channel="lc")
        requests.create_request("cloud-1", incident_id=None, incident_type="intrusion",
                                level=3, evidence_state="none", policy={}, expires_at=None,
                                test_mode=True, origin="cloud")
        counts = self.restart(world).recover()
        assert counts["resumed"] == 1
        assert requests.get_request("cloud-1")["state"] == S.DRAFTED


# ── the cloud ─────────────────────────────────────────────────────────────────
def cloud_chain(request_id, specs):
    """Valid cloud-origin events (hash-chained like the Portal's)."""
    from sentrix.storage.requests import _chain_hash
    prev, out = "0" * 64, []
    for seq, (event, frm, to, detail) in enumerate(specs, 1):
        fields = {"request_id": request_id, "seq": seq,
                  "at": datetime.fromtimestamp(T0 + seq, timezone.utc).isoformat(),
                  "from_state": frm, "to_state": to, "event": event, "actor_type": "cloud",
                  "actor_id": None, "channel": "system", "detail": detail,
                  "restricted": False, "origin": "cloud"}
        digest = _chain_hash(prev, fields)
        out.append({**fields, "prev_hash": prev, "hash": digest})
        prev = digest
    return out


class TestCloud:
    def test_sync_imports_the_cloud_chain_and_merges(self, world):
        fire_trigger(world)
        req = the_request()
        chain = cloud_chain(req["id"], [("device_offline", "PENDING", "PENDING", {}),
                                        ("expire", "PENDING", "EXPIRED", {})])
        result = world.engine.sync(req["id"], cloud_state="EXPIRED", cloud_version=4,
                                   authority_side="cloud", events=chain)
        assert result["ok"]
        merged = requests.get_request(req["id"])
        assert merged["state"] == S.EXPIRED and merged["authority_side"] == "device"
        assert package_of(req["id"])["status"] == "EXPIRED"
        assert requests.events(req["id"], origin="cloud")[-1]["event"] == "expire"
        conflict = [e for e in requests.events(req["id"], origin="device")
                    if e["event"] == "conflict"][0]
        assert conflict["detail"]["from"] == "PENDING" and conflict["detail"]["to"] == "EXPIRED"
        assert requests.verify_chain(req["id"])["ok"]
        again = world.engine.sync(req["id"], cloud_state="EXPIRED", cloud_version=4,
                                  authority_side="cloud", events=chain)
        assert "2 duplicate" in again["message"]

    def test_help_path_wins_over_a_cloud_lapse(self, world):
        world.holder["reach"] = ONLINE
        fire_trigger(world)
        req = the_request()
        world.engine.respond(req["id"], "approve", actor=OWNER, channel="lc")
        world.engine.sync(req["id"], cloud_state="EXPIRED", cloud_version=3,
                          authority_side="cloud", events=[])
        assert requests.get_request(req["id"])["state"] == S.APPROVED

    def test_a_request_the_cloud_opened_while_offline(self, world):
        incident = fire_trigger(world)
        own = the_request()
        chain = cloud_chain("cloud-req", [("created", None, "DRAFTED",
                                           {"incident_id": incident,
                                            "incident_type": "weapon", "level": 3}),
                                          ("prompted", "DRAFTED", "PENDING", {})])
        result = world.engine.sync("cloud-req", cloud_state="PENDING", cloud_version=2,
                                   authority_side="cloud", events=chain)
        assert result["ok"]
        cloud = requests.get_request("cloud-req")
        assert cloud["state"] == S.PENDING and cloud["flags"]["device_offline"]
        mine = requests.get_request(own["id"])
        assert mine["state"] == S.CANCELLED
        merge = [e for e in requests.events(own["id"]) if e["event"] == "cancel"][0]
        assert merge["detail"]["merged_into"] == "cloud-req"
        assert requests.verify_chain("cloud-req")["ok"]

    def test_a_gap_is_buffered_until_filled(self, world):
        fire_trigger(world)
        req = the_request()
        chain = cloud_chain(req["id"], [("seen", "PENDING", "PENDING", {}),
                                        ("seen", "PENDING", "PENDING", {})])
        assert "1 gap" in world.engine.sync(req["id"], cloud_state="PENDING", cloud_version=2,
                                            authority_side="device",
                                            events=chain[1:])["message"]
        world.engine.sync(req["id"], cloud_state="PENDING", cloud_version=2,
                          authority_side="device", events=chain[:1])
        assert len(requests.events(req["id"], origin="cloud")) == 2

    def test_cloud_facts_are_checked(self, world):
        fire_trigger(world)
        req = the_request()
        assert world.engine.cloud_event(req["id"], "resolve", actor=Actor.cloud(),
                                        detail={"outcome": "help_arrived"})["code"] == "conflict"
        assert world.engine.cloud_event(req["id"], "seen", actor=OWNER)["code"] == "forbidden_role"
        assert world.engine.cloud_event("nope", "seen", actor=Actor.cloud())["code"] == "not_found"
        assert world.engine.cloud_event(req["id"], "seen", actor=Actor.cloud())["ok"]
        assert requests.get_request(req["id"])["seen_at"]


# ── link events ───────────────────────────────────────────────────────────────
class TestLinkEvents:
    def test_every_device_event_is_published_once_in_seq_order(self, world):
        fire_trigger(world)
        req = the_request()
        world.engine.respond(req["id"], "decline", actor=OWNER, channel="lc", pin=DURESS_PIN)
        world.engine.work()
        sos = [(k, p, key) for k, p, key in world.published if k.startswith("sos.")
               and k != "sos.snapshot"]
        seqs = [p["event"]["seq"] for _, p, _ in sos]
        assert seqs == list(range(1, len(seqs) + 1))
        assert [key for _, _, key in sos] == [f"sos:{req['id']}:device:{n}" for n in seqs]
        assert sos[0][0] == "sos.created" and sos[1][0] == "sos.transition"
        restricted = [p for _, p, _ in sos if p["event"]["restricted"]]
        assert restricted and restricted[-1]["request"]["flags"]["duress"], \
            "the cloud gets the full flags (the Desk needs duress)"
        assert all("snapshot_path" not in p["request"] and "session_id" not in p["request"]
                   for _, p, _ in sos)

    def test_the_consented_snapshot_is_sent_once_and_small(self, world):
        import cv2
        import numpy as np
        gradient = np.tile(np.linspace(0, 255, 1280, dtype=np.uint8), (720, 1))
        cv2.imwrite(str(world.tmp / "snap_1.jpg"), np.dstack([gradient] * 3))
        fire_trigger(world, snapshot="snap_1.jpg")
        world.engine.work()
        snaps = [p for k, p, _ in world.published if k == "sos.snapshot"]
        assert len(snaps) == 1
        assert snaps[0]["width"] == 640 and len(snaps[0]["data_b64"]) < 256 * 1024 * 4 / 3 + 4

    def test_no_snapshot_without_consent(self, world):
        import cv2
        import numpy as np
        cv2.imwrite(str(world.tmp / "snap_2.jpg"), np.zeros((10, 10, 3), np.uint8))
        world.holder["profile"] = {**make_profile(), "consents": {"sos_snapshot": False}}
        fire_trigger(world, snapshot="snap_2.jpg")
        assert not [k for k, _, _ in world.published if k == "sos.snapshot"]


# ── packages, demo, service ───────────────────────────────────────────────────
class TestPackagesAndDemo:
    def test_send_now_on_a_request_package_approves_it(self, world):
        fire_trigger(world)
        req = the_request()
        package = package_of(req["id"])
        result = world.engine.approve_package(package["id"], "fire", actor=OWNER, channel="lc")
        assert result["ok"] and requests.get_request(req["id"])["state"] == S.APPROVED
        assert package_of(req["id"])["chosen_authority"] == "FIRE"
        assert world.engine.approve_package(package["id"], "ARMY", actor=OWNER,
                                            channel="lc")["code"] == "invalid_args"
        database.update_dispatch_status(package["id"], "SENT")
        assert world.engine.approve_package(package["id"], "FIRE", actor=OWNER,
                                            channel="lc")["status"] == 409

    def test_send_now_on_a_legacy_package_is_the_guarded_send(self, world):
        from sentrix.alerting.dispatch import DispatchPackage
        database.save_dispatch(DispatchPackage(
            id="legacy", created_at="2026-10-14T00:00:00+00:00", incident_id=None,
            incident_type="weapon", level=4, tci=0.7, user_name="u", user_address="a",
            user_phone="", camera_location="c", evidence_bundle=None, evidence_hash=None,
            authority="POLICE", status="PENDING", payload_json="{}"))
        result = world.engine.approve_package("legacy", "POLICE", actor=OWNER, channel="lc")
        assert result["ok"] and database.get_dispatch("legacy")["status"] == "PENDING"

    def test_demo_trigger_is_disclosed_and_test_mode_only(self, world, monkeypatch):
        result = world.engine.demo_trigger("weapon", actor=OWNER)
        assert result["ok"] and result["message"] == "SIMULATED DETECTION"
        req = the_request()
        assert req["flags"]["simulated"] and "SIMULATED DETECTION" in req["sos_text_full"]
        assert "sos_demo_trigger" in world.pipeline.events.names()
        assert world.engine.demo_trigger("assault", actor=OWNER)["code"] == "invalid_args"
        assert world.engine.demo_trigger("intrusion", actor=OWNER)["code"] == "unprocessable"
        closed = world.engine.demo_close_incident(actor=OWNER)
        assert closed["ok"] and "cancelled" in closed["message"]
        assert world.pipeline.closed == ["demo"]
        assert requests.get_request(req["id"])["state"] == S.CANCELLED
        cancel = [e for e in requests.events(req["id"]) if e["event"] == "cancel"][0]
        assert cancel["detail"]["reason"] == "test_reset"
        # The next scripted scene opens a new request instead of attaching.
        assert world.engine.demo_trigger("fire", actor=OWNER)["request"]["id"] != req["id"]
        monkeypatch.setenv("SENTRIX_TEST_MODE", "0")
        assert world.engine.demo_trigger("weapon", actor=OWNER)["code"] == "test_mode_only"
        assert world.engine.demo_close_incident(actor=OWNER)["code"] == "test_mode_only"

    def test_respond_validates_its_input(self, world):
        fire_trigger(world)
        req = the_request()
        assert world.engine.respond(req["id"], "explode", actor=OWNER,
                                    channel="lc")["code"] == "invalid_args"
        assert world.engine.respond("missing", "approve", actor=OWNER,
                                    channel="lc")["code"] == "not_found"
        assert world.engine.respond(req["id"], "approve", actor=Actor("operator", id="d"),
                                    channel="desk")["code"] == "forbidden_role"


class TestService:
    def test_the_engine_is_the_contracts_sos_service(self, world):
        from sentrix.services import Service
        from sentrix.sos import SosService, current_service
        from sentrix import services
        assert isinstance(world.engine, SosService) and isinstance(world.engine, Service)
        services.registry.register(world.engine)
        assert current_service() is world.engine
        health = services.registry.health()["sos"]
        assert health["enabled"] and health["test_mode"] and "duress" not in str(health)

    def test_off_unless_its_flag_is_set(self):
        from sentrix import services
        from sentrix.config import settings
        ctx = services.ServiceContext(settings=settings, state=None, get_pipeline=lambda: None,
                                      registry=services.ServiceRegistry())
        assert build_service(ctx) is None
        ctx.settings = settings_with(enabled=True)
        assert isinstance(build_service(ctx), SosEngine)

    def test_preflight_rows(self, temp_db, monkeypatch):
        from sentrix.config import settings
        assert preflight(settings)[0]["detail"].startswith("off")
        on = settings_with(enabled=True)
        monkeypatch.delenv("SENTRIX_SOS_PIN_HASH", raising=False)
        monkeypatch.setenv("SENTRIX_PASSWORD", "")
        rows = preflight(on)
        assert {r["status"] for r in rows} <= {"ok", "warn", "fail"}
        assert any(r["name"] == "sos.pin" and r["status"] == "fail" for r in rows)

    def test_the_thread_drains_triggers_and_stop_drains_the_rest(self, temp_db):
        from sentrix.pipeline.state import SystemState
        engine = SosEngine(state=SystemState(), channels=lambda: None, pipeline=lambda: None,
                           profile=lambda: make_profile(), publish=lambda *a, **k: None,
                           reach=lambda: ONLINE)
        engine.start()
        try:
            incident = new_incident()
            engine.trigger(SosTrigger(incident_id=incident, level=3, incident_type="fire",
                                      evidence_state="confirmed", tci=0.5, reason="fire",
                                      session_id="s", at_wall=time.time()))
            deadline = time.time() + 5
            while not requests.list_requests(1) and time.time() < deadline:
                time.sleep(0.02)
            assert requests.list_requests(1)
            assert engine.health()["running"]
        finally:
            engine.stop()
        assert not engine.health()["running"]
        assert not listeners.has_listeners("verdict")
        queued = SosEngine(state=SystemState(), channels=lambda: None, pipeline=lambda: None,
                           profile=lambda: make_profile(), publish=lambda *a, **k: None,
                           reach=lambda: ONLINE)
        queued._sweeper.start = lambda: None
        queued.start()
        late = new_incident()
        queued.trigger(SosTrigger(incident_id=late, level=3, incident_type="weapon",
                                  evidence_state="confirmed", tci=0.5, reason="w",
                                  session_id="s", at_wall=time.time()))
        queued.stop()                        # shutdown: the queued trigger is still handled
        assert late in requests.open_request()["incident_ids"]

    def test_memory_per_incident_is_bounded(self, world):
        from sentrix.sos import service
        for n in range(service.INCIDENT_MEMORY + 100):
            service._remember(world.engine._triggered, f"i{n}", None)
        assert len(world.engine._triggered) == service.INCIDENT_MEMORY
        assert "i0" not in world.engine._triggered and f"i{n}" in world.engine._triggered
        world.engine._link_incident("no-such-incident", "r1")
        for _ in range(service.LINK_RETRIES):
            world.engine._retry_links()
        assert world.engine._unlinked == {}

    def test_never_raises_into_a_caller(self, world, monkeypatch):
        fire_trigger(world)
        req = the_request()
        monkeypatch.setattr(world.engine, "_snapshot", lambda *_: 1 / 0)
        assert world.engine.respond(req["id"], "approve", actor=OWNER,
                                    channel="lc")["code"] == "internal"
        assert world.engine.set_mode(True, pin=None, actor=OWNER)["ok"]


# ── adversarial review regressions ────────────────────────────────────────────
class TestReviewRegressions:
    def test_a_duress_approval_made_in_the_cloud_stays_hidden_on_the_lc(self, world):
        incident = fire_trigger(world)
        req = the_request()
        chain = cloud_chain(req["id"], [
            ("device_offline", "PENDING", "PENDING", {}),
            ("approve", "PENDING", "APPROVED", {"duress": True}),
            ("delivered", "APPROVED", "DISPATCHED", {"target": "desk"})])
        assert world.engine.sync(req["id"], cloud_state="DISPATCHED", cloud_version=5,
                                 authority_side="cloud", events=chain)["ok"]
        merged = requests.get_request(req["id"])
        assert merged["state"] == S.DISPATCHED and merged["flags"]["duress"]
        conflict = [e for e in requests.events(req["id"], include_restricted=True)
                    if e["event"] == "conflict"][0]
        assert conflict["restricted"]
        assert "conflict" not in event_names(req["id"], restricted=False)
        shown = world.engine.project(merged, "owner")
        assert shown["state"] == S.DECLINED
        assert incident_row(incident)["verdict"] == "FALSE"

    def test_a_cloud_decline_labels_the_incident_false(self, world):
        incident = fire_trigger(world)
        req = the_request()
        chain = cloud_chain(req["id"], [("decline", "PENDING", "DECLINED", {})])
        world.engine.sync(req["id"], cloud_state="DECLINED", cloud_version=3,
                          authority_side="cloud", events=chain)
        assert requests.get_request(req["id"])["state"] == S.DECLINED
        assert incident_row(incident)["verdict"] == "FALSE"

    def test_a_weapon_during_the_entry_delay_opens_at_once(self, world):
        mode.set_armed(True, by="test")
        incident = new_incident("intrusion")
        intrusion_level(world, incident)
        assert requests.list_requests(5) == []
        fire_trigger(world, "weapon", level=4, evidence="corroborated", incident_id=incident)
        req = the_request()
        assert req["incident_type"] == "weapon" and req["state"] == S.PENDING
        assert world.engine.health()["held_intrusions"] == 0
        advance(world, 31, tick=5)
        assert len(requests.list_requests(5)) == 1

    def test_a_weapon_after_a_dropped_hold_still_opens(self, world):
        mode.set_armed(True, by="test")
        incident = new_incident("intrusion")
        intrusion_level(world, incident)
        assert world.engine.set_mode(False, pin=SOS_PIN, actor=OWNER)["ok"]
        advance(world, 31, tick=5)
        assert requests.list_requests(5) == []
        fire_trigger(world, "weapon", incident_id=incident)
        assert the_request()["incident_type"] == "weapon"

    def test_a_cloud_opened_request_keeps_its_type_and_merges_by_incident_ids(self, world):
        incident = fire_trigger(world, "fire")
        own = the_request()
        chain = cloud_chain("cloud-fire", [
            ("created", None, "DRAFTED", {"why": "device_offline",
                                          "incident_ids": [incident],
                                          "incident_type": "fire", "level": 3,
                                          "policy": {"window_seconds": 2700,
                                                     "safety_defaults": True,
                                                     "contacts": [{"id": "c1"}]}}),
            ("prompted", "DRAFTED", "PENDING", {})])
        assert world.engine.sync("cloud-fire", cloud_state="PENDING", cloud_version=2,
                                 authority_side="cloud", events=chain)["ok"]
        cloud = requests.get_request("cloud-fire")
        assert cloud["incident_type"] == "fire" and cloud["incident_id"] == incident
        assert cloud["policy"]["window_seconds"] == 2700
        assert [c["id"] for c in cloud["policy"]["contacts"]] == ["c1"]
        assert requests.get_request(own["id"])["state"] == S.CANCELLED
        assert len(requests.open_requests()) == 1

    def test_recovery_sends_an_owed_contacts_alert_before_expiring(self, world):
        fire_trigger(world, "fire")
        req = the_request()
        world.clock.now += 60                        # contacts due at T+2 min
        assert not requests.get_request(req["id"])["flags"].get("contacts_notified")
        world.clock.now += 4000                      # the device was off past the window
        TestRecovery().restart(world)
        names = event_names(req["id"])
        assert names.index("contacts_notified") < names.index("expire")
        assert requests.get_request(req["id"])["state"] == S.EXPIRED
