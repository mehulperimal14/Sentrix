"""The foundation after the contract review (docs/7): the interfaces the
parallel tracks call each other through.

Nothing here opens a camera or microphone, sends a message or touches
``var/``: databases are temporary (``temp_db``), transports are fakes, the
socket guard of ``tests/conftest.py`` is on.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import Request

from sentrix.actor import Actor
from sentrix.storage import database

OWNER = Actor("owner", id="u1", name="Riya", step_up_at=None)


def _incident(manager=None, level=3, kind="weapon"):
    from sentrix.pipeline.incidents import IncidentManager
    manager = manager or IncidentManager()
    incident, _ = manager.update(level, 0.5, kind, now=0.0, features={}, reason="r",
                                 trigger={kind: {"state": "confirmed"}})
    return incident


def _package(pid, incident_id, status="PENDING"):
    from sentrix.alerting.dispatch import DispatchPackage
    return DispatchPackage(id=pid, created_at="2026-10-14T00:00:00+00:00",
                           incident_id=incident_id, incident_type="weapon", level=3,
                           tci=0.5, user_name="u", user_address="a", user_phone="",
                           camera_location="c", evidence_bundle=None, evidence_hash=None,
                           authority="POLICE", status=status, payload_json="{}")


def _request(rid="r1", **kwargs):
    from sentrix.storage import requests
    values = dict(incident_id="i1", incident_type="weapon", level=3,
                  evidence_state="confirmed", policy={"window_seconds": 1800},
                  expires_at=None, test_mode=True)
    values.update(kwargs)
    return requests.create_request(rid, **values)


# ── test-session safety (tests/conftest.py) ───────────────────────────────────
class TestSessionSafety:
    def test_live_switches_are_forced_off_before_sentrix_loads(self):
        import os
        from sentrix.alerting import guard
        assert os.environ["SENTRIX_LIVE_ALERTS"] == "0"
        assert os.environ["SENTRIX_TWILIO_ENABLED"] == "0"
        assert os.environ["TWILIO_AUTH_TOKEN"] == ""
        status = guard.status()
        assert status["test_mode"] and not status["live_alerts"]
        assert not any(status["channels_live"].values()) and status["test_recipients"] == 0

    def test_importing_the_app_cannot_turn_them_back_on(self):
        import os
        import sentrix.app  # noqa: F401  (load_dotenv runs at import)
        assert os.environ["SENTRIX_LIVE_ALERTS"] == "0"
        assert os.environ["SENTRIX_TEST_MODE"] == "1"

    def test_socket_guard_allows_loopback_only(self):
        import conftest
        with pytest.raises(OSError):
            socket.create_connection(("192.0.2.1", 443), timeout=0.5)
        with pytest.raises(OSError):
            socket.getaddrinfo("api.twilio.com", 443)
        assert any("192.0.2.1" in entry for entry in conftest.BLOCKED_CONNECTIONS)
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        try:
            client = socket.create_connection(server.getsockname(), timeout=2)
            client.close()
        finally:
            server.close()

    def test_mode_file_is_temporary(self, tmp_path):
        from sentrix import mode
        assert mode.MODE_PATH.parent == tmp_path


# ── actors ────────────────────────────────────────────────────────────────────
class TestActor:
    def test_refs_and_event_types(self):
        assert Actor.device_admin().ref == "lc:device_admin"
        assert OWNER.ref == "portal:u1" and OWNER.event_actor_type == "owner"
        desk = Actor("operator", id="op7")
        assert desk.ref == "desk:op7" and desk.event_actor_type == "desk"
        assert Actor("contact", id="c1").ref == "contact:c1"
        assert Actor.cloud().ref == "cloud" and Actor.system().ref == "system"
        with pytest.raises(ValueError):
            Actor("company_admin")

    def test_wire_format_round_trip_and_legacy_spelling(self):
        actor = Actor("member", id="m2", name="Dev", step_up_at=1760430000.0)
        assert Actor.from_dict(actor.to_dict()) == actor
        legacy = Actor.from_dict({"user_id": "u9", "user_name": "A", "role": "owner",
                                  "step_up_at": ""})
        assert legacy == Actor("owner", id="u9", name="A", step_up_at=None)
        assert actor.stepped_up_within(300, now=1760430100.0)
        assert not actor.stepped_up_within(30, now=1760430100.0)

    def test_guess_from_audit_string(self):
        from sentrix.actor import event_actor_type_for_ref
        assert [event_actor_type_for_ref(r) for r in
                ("lc:device_admin", "desk:op", "portal:u", "contact:c", "cloud", None)] == \
            ["device_admin", "desk", "owner", "contact", "cloud", "system"]


# ── SOS interface ─────────────────────────────────────────────────────────────
class TestSosInterface:
    def test_null_service_satisfies_the_protocol(self):
        from sentrix.sos import NullSosService, SosService
        assert isinstance(NullSosService(), SosService)

    def test_sos_result_status_from_the_code_table(self):
        from sentrix.sos import sos_result
        assert sos_result() == {"ok": True, "code": "ok", "status": 200, "message": "",
                                "request": None}
        assert sos_result("pin_locked")["status"] == 429
        assert sos_result("conflict", "x")["ok"] is False

    def test_sos_only_calls_are_unsupported_without_the_service(self):
        from sentrix.sos import NullSosService
        null = NullSosService()
        for result in (null.respond("r", "approve", actor=OWNER, channel="lc"),
                       null.cloud_event("r", "acknowledge", actor=Actor.cloud()),
                       null.sync("r", cloud_state="PENDING", cloud_version=2,
                                 authority_side="cloud", events=[]),
                       null.demo_trigger("weapon", actor=OWNER),
                       null.demo_close_incident(actor=OWNER)):
            assert result["code"] == "unsupported" and result["status"] == 400
        assert null.current() is None

    def test_null_false_verdict_keeps_the_pre_portal_rule(self, temp_db):
        from sentrix.sos import NullSosService
        incident = _incident()
        database.save_incident(incident, "s1")
        database.save_dispatch(_package("p0", incident.id))
        result = NullSosService().false_verdict(incident.id, pin=None, actor=OWNER,
                                                channel="portal")
        assert result["ok"]
        assert database.get_dispatch("p0")["status"] == "CANCELLED"
        row = database.recent_incidents()[0]
        assert row["verdict"] == "FALSE" and row["verdict_by"] == "portal:u1"
        assert NullSosService().false_verdict("nope", pin=None, actor=OWNER,
                                              channel="lc")["code"] == "not_found"

    def test_null_mode_arms_for_anyone_disarms_for_the_device_admin_only(self):
        from sentrix import mode
        from sentrix.sos import NullSosService
        null = NullSosService()
        assert null.set_mode(True, pin=None, actor=OWNER)["ok"] and mode.is_armed()
        assert null.set_mode(False, pin="123456", actor=OWNER)["code"] == "unsupported"
        assert mode.is_armed()
        assert null.set_mode(False, pin=None, actor=Actor.device_admin())["ok"]
        assert not mode.is_armed()

    def test_null_approve_package_is_the_guarded_legacy_send(self, temp_db):
        from sentrix.sos import NullSosService
        incident = _incident()
        database.save_incident(incident, "s1")
        database.save_dispatch(_package("p1", incident.id))
        result = NullSosService().approve_package("p1", "FIRE", actor=OWNER, channel="lc")
        assert result["ok"]                       # dry run: not sent, still sendable
        package = database.get_dispatch("p1")
        assert package["status"] == "PENDING" and package["chosen_authority"] == "FIRE"
        assert package["error"], "the failed attempt is recorded"
        database.update_dispatch_status("p1", "SENT")
        assert NullSosService().approve_package("p1", "FIRE", actor=OWNER,
                                                channel="lc")["status"] == 409

    def test_current_service_is_the_registered_one(self):
        from sentrix import services
        from sentrix.sos import NullSosService, current_service
        assert isinstance(current_service(), NullSosService)
        fake = SimpleNamespace(name="sos", start=lambda: None, stop=lambda: None,
                               health=lambda: {})
        services.registry.register(fake)
        assert current_service() is fake

    def test_vocabularies(self):
        from sentrix.link import protocol
        from sentrix.sos import RESPOND_ACTIONS, SOS_ACTIONS
        assert SOS_ACTIONS == ("approve", "call_112", "checking", "decline", "safe",
                               "help_now")
        assert RESPOND_ACTIONS == protocol.SOS_RESPOND_ACTIONS
        assert {"acknowledge", "resolve", "delivered"} <= set(protocol.SOS_UPDATE_EVENTS)


# ── dispatch ──────────────────────────────────────────────────────────────────
class TestDispatchOutcome:
    def test_outcomes(self, temp_db):
        from sentrix.alerting.dispatch import dispatch_service
        incident = _incident()
        database.save_incident(incident, "s1")
        database.save_dispatch(_package("p1", incident.id))
        assert dispatch_service.send("missing", "POLICE").status == "not_found"
        assert dispatch_service.send("p1", "NAVY").status == "invalid"
        outcome = dispatch_service.send("p1", "medical", by="portal:u1")
        assert outcome.status == "failed" and not outcome and outcome.code == "ok"
        database.update_dispatch_status("p1", "CANCELLED")
        refused = dispatch_service.send("p1", "POLICE")
        assert refused.status == "conflict" and refused.code == "conflict"


# ── the pipeline hook: SOS owns the messages of its incidents ─────────────────
class TestSosOwnsItsIncidents:
    def _fake(self, wants):
        from sentrix.pipeline.engine import Pipeline
        from sentrix.pipeline.incidents import IncidentManager
        import queue
        fake = Pipeline.__new__(Pipeline)
        fake.cfg = __import__("sentrix.config", fromlist=["settings"]).settings
        fake.incidents = IncidentManager()
        fake.sos = SimpleNamespace(wants=lambda kind, level: wants, trigger=lambda t: None)
        fake.session_id = "s1"
        fake._tasks = queue.Queue(maxsize=64)
        fake.submitted = []
        fake._submit = lambda func, *a, **k: fake.submitted.append(func.__name__)
        fake._evidence_context = lambda result: {}
        fake.siren = SimpleNamespace(activated=0)
        fake.siren.activate = lambda: setattr(fake.siren, "activated", fake.siren.activated + 1)
        return fake

    @staticmethod
    def _result():
        extras = {"evidence": {"weapon": {"state": "corroborated"}},
                  "detections": {"all": []}}
        return SimpleNamespace(incident_type="weapon", tci=0.95, reason="weapon",
                               features=SimpleNamespace(extras=extras))

    def test_no_legacy_sms_call_or_package_once_sos_claimed_it(self):
        import numpy as np
        from sentrix.pipeline.escalation import plan_for
        fake = self._fake(wants=True)
        incident = _incident(fake.incidents)
        frame = np.zeros((4, 4, 3), np.uint8)
        fake._act(incident, plan_for(3), frame, self._result(), 3, "ELEVATED", 0.0)
        fake._act(incident, plan_for(5), frame, self._result(), 5, "CRITICAL", 1.0)
        assert "sos" in incident.actions
        for legacy in ("send_sms", "_create_dispatch", "make_call"):
            assert legacy not in fake.submitted, legacy
        assert fake.siren.activated == 1, "the siren is not a message: it stays"

    def test_without_sos_the_legacy_path_is_unchanged(self):
        import numpy as np
        from sentrix.pipeline.escalation import plan_for
        fake = self._fake(wants=False)
        incident = _incident(fake.incidents)
        fake._act(incident, plan_for(5), np.zeros((4, 4, 3), np.uint8), self._result(),
                  5, "CRITICAL", 0.0)
        assert {"send_sms", "_create_dispatch", "make_call"} <= set(fake.submitted)


# ── request store: lock, versions, per-origin chains ──────────────────────────
class TestRequestConcurrency:
    def test_approve_and_expire_race_exactly_one_wins(self, temp_db):
        from sentrix.storage import requests
        for round_ in range(5):
            rid = f"race-{round_}"
            _request(rid)
            requests.transition(rid, "prompted")
            barrier = threading.Barrier(2)
            outcomes = {}

            def act(event):
                barrier.wait()
                outcomes[event] = requests.try_transition(rid, event, expected_version=2)

            threads = [threading.Thread(target=act, args=(e,)) for e in ("approve", "expire")]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            won = [e for e, r in outcomes.items() if r.ok]
            assert len(won) == 1, outcomes
            final = requests.get_request(rid)
            assert final["state"] == ("APPROVED" if won == ["approve"] else "EXPIRED")
            assert final["version"] == 3
            assert [e["event"] for e in requests.events(rid)] == ["created", "prompted", won[0]]

    def test_stale_version_and_refused_event_write_nothing(self, temp_db):
        from sentrix.storage import requests
        _request("r")
        stale = requests.try_transition("r", "prompted", expected_version=7)
        assert stale.code == "conflict" and "stale" in stale.message
        refused = requests.try_transition("r", "resolve")
        assert refused.code == "conflict" and "not allowed" in refused.message
        assert requests.try_transition("missing", "prompted").code == "not_found"
        assert requests.get_request("r")["version"] == 1
        assert requests.transition("r", "prompted", expected_version=1)["state"] == "PENDING"

    def test_a_crash_before_the_prompt_can_still_expire(self, temp_db):
        from sentrix.storage import requests
        _request("r")
        assert requests.transition("r", "expire")["state"] == "EXPIRED"

    def test_set_fields_is_bookkeeping_without_an_event(self, temp_db):
        from datetime import datetime, timezone
        from sentrix.storage import requests
        _request("r")
        at = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)
        assert requests.set_fields("r", next_timer_at=at, reminder_step=2)
        assert requests.get_request("r")["version"] == 1
        assert len(requests.events("r")) == 1
        assert requests.set_fields("r", expected_version=9, reminder_step=3) is None


def _cloud_events(request_id, n=2):
    """A valid cloud-origin chain, built the way the Portal builds it."""
    import hashlib
    from sentrix.link.protocol import canonical_json
    events, prev = [], "0" * 64
    names = [("created", None, "DRAFTED"), ("prompted", "DRAFTED", "PENDING"),
             ("expire", "PENDING", "EXPIRED")]
    for seq in range(1, n + 1):
        event, from_state, to_state = names[seq - 1]
        fields = {"request_id": request_id, "seq": seq,
                  "at": f"2026-10-14T09:0{seq}:00+00:00", "from_state": from_state,
                  "to_state": to_state, "event": event, "actor_type": "cloud",
                  "actor_id": "cloud", "channel": "system", "detail": {"why": "offline"},
                  "restricted": False, "origin": "cloud"}
        digest = hashlib.sha256(prev.encode() + canonical_json(fields)).hexdigest()
        events.append({**fields, "prev_hash": prev, "hash": digest})
        prev = digest
    return events


class TestPerOriginChains:
    def test_cloud_events_do_not_use_up_device_seqs(self, temp_db):
        from sentrix.storage import requests
        _request("r")
        requests.transition("r", "prompted")
        cloud = _cloud_events("r", 2)
        assert requests.import_event(cloud[0])[1] == "ok"
        assert requests.import_event(cloud[1])[1] == "ok"
        requests.transition("r", "approve", actor_type="owner")
        device = requests.events("r", origin="device")
        assert [e["seq"] for e in device] == [1, 2, 3]
        assert requests.last_seq("r", "cloud") == 2
        chain = requests.verify_chain("r")
        assert chain["ok"] and chain["origins"] == {"device": 3, "cloud": 2}

    def test_import_is_idempotent_ordered_and_verified(self, temp_db):
        from sentrix.storage import requests
        _request("r", origin="cloud")
        cloud = _cloud_events("r", 3)
        assert requests.import_event(cloud[1])[1] == "gap"
        assert requests.import_event(cloud[0])[1] == "ok"
        assert requests.import_event(cloud[0])[1] == "duplicate"
        tampered = {**cloud[1], "detail": {"why": "changed"}}
        assert requests.import_event(tampered)[1] == "invalid"
        assert requests.import_event({"seq": 1})[1] == "invalid"
        assert requests.import_event(cloud[1])[1] == "ok"
        assert requests.verify_chain("r")["ok"]

    def test_cloud_created_request(self, temp_db):
        from sentrix.storage import requests
        row = _request("rc", origin="cloud", incident_id=None,
                       flags={"device_offline": True})
        assert row["authority_side"] == "cloud" and row["flags"]["device_offline"]
        assert row["incident_ids"] == [] and requests.events("rc") == []

    def test_existing_database_gains_the_unique_chain_index(self, temp_db):
        with temp_db.begin() as conn:
            names = {r[1] for r in conn.exec_driver_sql("PRAGMA index_list(request_events)")}
        assert "ux_request_events_origin_seq" in names

    def test_append_event_rejects_unknown_origins(self, temp_db):
        from sentrix.storage import requests
        with database.session_scope() as session, pytest.raises(ValueError):
            requests.append_event(session, "r", event="seen", from_state=None,
                                  to_state=None, origin="elsewhere")


# ── verdicts: actors and the listener payload ─────────────────────────────────
class TestVerdictActors:
    def _setup(self, state="PENDING"):
        from sentrix.storage import requests
        from sentrix.storage.models import AssistanceRequestRecord
        incident = _incident()
        database.save_incident(incident, "s1", ongoing=True)
        _request("req-1", incident_id=incident.id)
        database.link_incident_request(incident.id, "req-1")
        with database.session_scope() as session:
            session.get(AssistanceRequestRecord, "req-1").state = state
        return incident, requests

    def test_desk_verdict_is_recorded_as_desk_and_announced(self, temp_db):
        from sentrix import listeners
        incident, requests = self._setup("DISPATCHED")
        heard = []
        listeners.add_listener("verdict", heard.append)
        assert database.set_verdict(incident.id, "FALSE", by="desk:op7", channel="desk")
        event = requests.events("req-1")[-1]
        assert (event["event"], event["actor_type"], event["actor_id"]) == \
            ("resident_safe", "desk", "desk:op7")
        assert heard[0]["request_id"] == "req-1" and heard[0]["effect"] == "resident_safe"
        assert heard[0]["request_state"] == "DISPATCHED" and heard[0]["request_version"] == 2

    def test_member_verdict_keeps_the_callers_actor_type(self, temp_db):
        incident, requests = self._setup("PENDING")
        member = Actor("member", id="m1")
        assert database.set_verdict(incident.id, "FALSE", by=member.ref, channel="portal",
                                    actor_type=member.event_actor_type)
        assert requests.events("req-1")[-1]["actor_type"] == "member"


# ── change listeners ──────────────────────────────────────────────────────────
class TestListeners:
    def test_close_interrupted_announces_the_closed_rows(self, temp_db):
        from sentrix import listeners
        incident = _incident()
        database.save_incident(incident, "old-session", ongoing=True)
        heard = []
        listeners.add_listener("incident", heard.append)
        assert database.close_interrupted("new-session") == 1
        assert heard[0]["id"] == incident.id and heard[0]["ongoing"] is False
        assert set(heard[0]) >= {"peak_level", "incident_type", "actions", "started_at"}

    def test_audit_rows_reach_listeners(self, tmp_path):
        from sentrix import listeners
        from sentrix.pipeline.events import EventLog
        log = EventLog(directory=tmp_path, source="test")
        heard = []
        listeners.add_listener("audit", heard.append)
        log.audit("gallery_enrolled", person="Riya", file="riya.jpg", by="lc:device_admin")
        log.close()
        assert heard[0]["event"] == "gallery_enrolled" and heard[0]["person"] == "Riya"

    def test_evidence_metadata_never_carries_the_image(self):
        from sentrix import listeners
        from sentrix.evidence.vault import EvidenceRecord, _announce
        heard = []
        listeners.add_listener("evidence", heard.append)
        record = EvidenceRecord(file="b.enc", bundle_id="b", timestamp="t", incident_id="i",
                                camera_id="CAM_0", tci=0.7, level=4, status="HIGH",
                                incident_type="weapon", reason="r", features={},
                                detections=[{"label": "pistol", "conf": 0.8,
                                             "bbox": [1, 2, 3, 4], "track_id": 3}],
                                sha256="s", prev_sha256="p")
        _announce(record)
        assert heard == [{"bundle_id": "b", "incident_id": "i", "created_at": "t",
                          "level": 4, "tci": 0.7, "sha256": "s", "camera_id": "CAM_0",
                          "incident_type": "weapon",
                          "detections": [{"label": "pistol", "conf": 0.8}]}]

    def test_unknown_kind_and_failing_listener(self):
        from sentrix import listeners
        with pytest.raises(ValueError):
            listeners.add_listener("nonsense", print)
        listeners.add_listener("mode", lambda d: 1 / 0)
        listeners.emit("mode", {"armed": True})          # logged, never raised


# ── runtime mode survives a restart ───────────────────────────────────────────
class TestModePersistence:
    def test_set_persists_load_restores_and_announces(self):
        from sentrix import listeners, mode
        heard = []
        listeners.add_listener("mode", heard.append)
        mode.set_armed(True, by="portal:u1")
        saved = json.loads(mode.MODE_PATH.read_text(encoding="utf-8"))
        assert saved["armed"] is True and saved["by"] == "portal:u1"
        assert heard[0]["armed"] is True and heard[0]["by"] == "portal:u1"
        mode.clear_override()                     # "restart": the config value applies
        assert mode.armed_override() is None
        assert mode.load()["armed"] is True and mode.is_armed()
        mode.clear_override(forget=True)
        assert not mode.MODE_PATH.exists() and mode.load()["source"] == "config"

    def test_a_broken_file_keeps_the_configured_mode(self):
        from sentrix import mode
        mode.MODE_PATH.write_text("{not json", encoding="utf-8")
        assert mode.load()["source"] == "config"
        mode.MODE_PATH.write_text('{"armed": "yes"}', encoding="utf-8")
        assert mode.armed_override() is None and mode.load()["source"] == "config"


# ── link: publish and the profile ─────────────────────────────────────────────
class TestPublish:
    def test_nothing_is_stored_while_the_link_is_off(self, temp_db):
        from sentrix import link
        from sentrix.storage import link_store
        assert link.publish("incident.upsert", {"id": "i"}) is None
        assert link_store.depth() == 0

    def test_link_on_sos_first_dedupe_unknown_kind(self, temp_db, monkeypatch):
        from sentrix import link
        from sentrix.link import protocol
        from sentrix.storage import link_store
        monkeypatch.setattr(link, "link_enabled", lambda: True)
        a = link.publish("incident.upsert", {"id": "i"})
        key = protocol.sos_dedupe_key("r1", "device", 1)
        assert key == "sos:r1:device:1"
        b = link.publish("sos.created", {"request": {"id": "r1"}}, dedupe_key=key)
        assert link.publish("sos.created", {}, dedupe_key=key) is None
        assert link.publish("not.a.kind", {}) is None
        assert [e["seq"] for e in link_store.pending()] == [b, a]


def _profile(version=1, **sos):
    return {"schema": 1, "version": version, "owner": {"name": "Riya", "phone": "+919812345610"},
            "contacts": [{"id": "c1", "name": "A", "phone": "+919812345611",
                          "consent_status": "CONFIRMED"},
                         {"id": "c2", "name": "B", "phone": "+919812345612",
                          "consent_status": "PENDING"}],
            "desk": {"name": "Desk", "phone": "+919812345699", "simulated": True},
            "sos": {"window_seconds": 1800, "pin_hash": "$argon2id$x", **sos},
            "consents": {"remote_view": True}}


class TestProfileCache:
    def test_store_current_version_and_restart(self, temp_db):
        from sentrix.link import profile, protocol
        heard = []
        profile.add_listener(heard.append)
        doc = _profile(3)
        ack = profile.store(doc, version=3, sha256=protocol.sha256_hex(doc))
        assert ack == {"version": 3, "ok": True, "error": None}
        assert heard == [doc] and profile.current_version() == 3
        profile.forget_cache()                   # a restart: decrypt from SQLite
        assert profile.current() == doc
        assert (profile.identity_dir() / profile.PROFILE_KEY_FILE).stat().st_size == 32
        assert profile.store(_profile(2), version=2)["error"] == "stale"
        assert profile.current_version() == 3

    def test_refused_profiles(self, temp_db):
        from sentrix.link import profile
        assert not profile.store(_profile(), version=1, sha256="0" * 64)["ok"]
        bad = _profile()
        bad["owner"]["password_hash"] = "$argon2id$..."
        assert "forbidden field owner.password_hash" in profile.store(bad, version=1)["error"]
        leaky = _profile()
        leaky["contacts"][0]["email"] = "a@example.com"
        assert not profile.store(leaky, version=1)["ok"]
        assert not profile.store({"schema": 9}, version=1)["ok"]
        assert profile.current() is None

    def test_wipe(self, temp_db):
        from sentrix.link import profile
        profile.store(_profile(), version=1)
        assert profile.wipe() == 1 and profile.current() is None
        assert profile.current_version() == 0

    def test_standalone_effective_desk_and_allowlist(self, temp_db, monkeypatch):
        from sentrix.link import profile
        monkeypatch.setenv("ALERT_PHONE_NUMBER", "+919812345610")
        monkeypatch.setenv("SENTRIX_DESK_NUMBER", "+919812345698")
        alone = profile.effective()
        assert alone["standalone"] and alone["owner"]["phone"] == "+919812345610"
        assert alone["sos"]["pin_source"] == "console_password" and alone["contacts"] == []
        assert profile.desk_number() == "+919812345698"
        profile.store(_profile(), version=1)
        assert profile.desk_number() == "+919812345699"
        assert profile.sms_allowlist() == {"+919812345610", "+919812345611", "+919812345699"}


# ── channels: the service interface, callbacks, outbox meta ───────────────────
class TestChannelsContract:
    def test_router_listeners_hear_every_status_change(self, fake_router):
        from sentrix.alerting.channels.base import (SENT, SUPPRESSED, ChannelRouter,
                                                    Message, Recipient)
        router, transports = fake_router
        heard = []
        router.add_listener(lambda item: heard.append((item.recipient.ref, item.status)))
        router.add_listener(lambda item: 1 / 0)          # never stops delivery
        router.enqueue(Recipient("owner", "R", "modem_sms", "+919812345610"),
                       Message(template="sos.prompt", step="t0", text="x", request_id="r"))
        router.enqueue(Recipient("contact:9", "S", "modem_sms", "+919800000000"),
                       Message(template="sos.full", step="contacts_t0", text="y",
                               request_id="r"))
        assert router.pump() == 2
        assert sorted(heard) == [("contact:9", SUPPRESSED), ("owner", SENT)]
        assert len(transports["modem_sms"].sent) == 1, "not allowlisted: suppressed"
        assert isinstance(router, ChannelRouter)

    def test_ready_needs_both_switches(self, fake_router):
        from sentrix.alerting.channels.base import Channel, ChannelRouter, FakeTransport
        router, _ = fake_router
        assert router.ready("telegram") and not router.ready("nope")
        dry = ChannelRouter({"email": Channel("email", FakeTransport())})
        assert not dry.ready("email")

    def test_telegram_callback_data_fits_64_bytes(self):
        from sentrix.alerting.channels import base
        nonce = base.new_callback_nonce()
        assert len(nonce) == 16
        for action in ("approve", "checking", "acknowledge"):
            data = base.callback_data(nonce, action)
            assert len(data.encode()) <= base.CALLBACK_DATA_MAX_BYTES
            assert base.parse_callback_data(data) == (nonce, action)
        with pytest.raises(ValueError):
            base.callback_data(nonce, "decline")         # a URL button, never a callback
        assert base.parse_callback_data("x" * 70) is None
        assert base.parse_callback_data(f"{nonce}:z") is None

    def test_sql_outbox_keeps_buttons_html_urgency_and_recipient(self, temp_db):
        from sentrix.alerting.channels.base import Button, Message, OutboxItem, Recipient
        from sentrix.storage.notifications import SqlOutbox
        outbox = SqlOutbox()
        recipient = Recipient("contact:c1", "Asha", "telegram", "tg:12345", role="contact")
        message = Message(template="sos.full", step="contacts_t0", text="SOS",
                          request_id="r1", html="<p>SOS</p>", urgency="HIGH",
                          buttons=(Button("I'm handling it", "acknowledge",
                                          callback_data="n:k"),
                                   Button("Open", "open", url="https://p/c/t")),
                          created_at=1000.0)
        assert outbox.add(OutboxItem(id="n1", recipient=recipient, message=message,
                                     next_attempt_at=1000.0))
        item = outbox.due(2000.0)[0]
        assert item.recipient == recipient
        assert item.message == message

    def test_channels_service_protocol_shape(self):
        from sentrix.alerting.channels.base import ChannelsService
        service = SimpleNamespace(name="channels", enqueue=lambda r, m: "id",
                                  ready=lambda c: False, status=lambda: {},
                                  add_listener=lambda f: None,
                                  remove_listener=lambda f: None, start=lambda: None,
                                  stop=lambda: None, health=lambda: {})
        assert isinstance(service, ChannelsService)

    def test_sos_steps(self):
        from sentrix.sos import states as S
        assert [S.reminder_step(o) for o in (120, 300, 600, 1200)] == \
            ["r120", "r300", "r600", "r1200"]
        assert S.RISE_STEPS == {4: "rise4", 5: "rise5"} and S.repage_step(3) == "repage3"
        assert {S.STEP_T0, S.STEP_LAPSE, S.STEP_LAPSED, S.STEP_CONTACTS_T0,
                S.STEP_RESOLVED, S.STEP_DURESS} == {"t0", "lapse", "lapsed", "contacts_t0",
                                                    "resolved", "duress"}


# ── canonical JSON ────────────────────────────────────────────────────────────
class TestCanonicalJson:
    def test_bytes_are_stable_and_strict(self):
        from sentrix.link.protocol import canonical_json, sha256_hex
        assert canonical_json({"b": 1, "a": "é"}) == '{"a":"é","b":1}'.encode("utf-8")
        assert sha256_hex({"a": 1, "b": 2}) == sha256_hex({"b": 2, "a": 1})
        with pytest.raises(TypeError):
            canonical_json({"at": object()})
        with pytest.raises(ValueError):
            canonical_json({"x": float("nan")})


# ── LC principal ──────────────────────────────────────────────────────────────
def _lc_app():
    from fastapi import Depends, FastAPI
    from fastapi.responses import HTMLResponse
    from sentrix.web import auth, extensions

    app = FastAPI()

    @app.get("/api/who")
    async def who(p=Depends(auth.require_lc())):
        return {"role": p.role, "actor": p.actor, "kind": p.kind, "channel": p.channel}

    @app.get("/page", response_class=HTMLResponse)
    async def page(p=Depends(auth.require_lc())):
        return "<p>ok</p>"

    @app.post("/api/admin-only")
    async def admin_only(p=Depends(auth.require_lc("device_admin"))):
        return {"ok": True}

    @app.get("/api/key")
    async def key(request: Request):
        return {"key": auth.rate_key(request)}

    @app.middleware("http")
    async def fake_hardening(request, call_next):
        # What T-WEB's hardening does: verify the bridge, store the identity.
        from sentrix.link import tunnel_auth
        identity = tunnel_auth.verify(request.headers)
        if identity is not None:
            request.scope[tunnel_auth.SCOPE_KEY] = identity
        return await call_next(request)

    extensions._install_error_handler(app)
    return app


class TestLcPrincipal:
    def test_device_admin_session(self, monkeypatch):
        from fastapi.testclient import TestClient
        from sentrix.web import auth
        from sentrix.web.security import generate_token
        monkeypatch.setenv("SENTRIX_PASSWORD", "correct horse")
        client = TestClient(_lc_app())
        client.cookies.set("lc_session", generate_token("correct horse"))
        assert client.get("/api/who").json() == {"role": "device_admin",
                                                 "actor": "lc:device_admin", "kind": "lc",
                                                 "channel": "lc"}
        assert client.post("/api/admin-only").status_code == 200
        cookie = generate_token("correct horse")
        assert auth.csrf_for_session(cookie) == auth.csrf_for_session(cookie)
        assert auth.csrf_for_session(cookie) != auth.csrf_for_session(cookie + "x")

    def test_legacy_cookie_is_no_longer_a_session(self, monkeypatch):
        # T-WEB's lc_session rename landed; the old cookie skipped the CSRF
        # header, so integration retired it.
        from fastapi.testclient import TestClient
        from sentrix.web.security import generate_token
        monkeypatch.setenv("SENTRIX_PASSWORD", "pw")
        client = TestClient(_lc_app())
        client.cookies.set("session_token", generate_token("pw"))
        assert client.get("/api/who").status_code == 401
        client.cookies.clear()
        client.cookies.set("lc_session", generate_token("pw"))
        assert client.get("/api/who").status_code == 200

    def test_tunnel_identity_and_role_rules(self, monkeypatch):
        from fastapi.testclient import TestClient
        from sentrix.link import tunnel_auth
        monkeypatch.setenv("SENTRIX_PASSWORD", "pw")
        headers = tunnel_auth.build_headers(user_id="u1", user_name="Riya", role="member",
                                            base="/d/dev-1", step_up_at=None, csrf="c")
        client = TestClient(_lc_app())
        body = client.get("/api/who", headers=headers).json()
        assert body == {"role": "member", "actor": "portal:u1", "kind": "tunnel",
                        "channel": "portal"}
        refused = client.post("/api/admin-only", headers=headers)
        assert refused.status_code == 403 and refused.json()["code"] == "forbidden_role"
        assert client.get("/api/key", headers=headers).json() == {"key": "portal:u1"}

    def test_unauthenticated_pages_redirect_apis_and_tunnels_get_json(self, monkeypatch):
        from fastapi.testclient import TestClient
        monkeypatch.setenv("SENTRIX_PASSWORD", "pw")
        client = TestClient(_lc_app(), follow_redirects=False)
        page = client.get("/page", headers={"accept": "text/html"})
        assert page.status_code == 307 and page.headers["location"] == "/login"
        api = client.get("/api/who")
        assert api.status_code == 401 and api.json() == {"detail": "not signed in",
                                                         "code": "unauthorised"}
        tunnelled = TestClient(_lc_app(), follow_redirects=False,
                               client=("tunnel", 0)).get("/page",
                                                         headers={"accept": "text/html"})
        assert tunnelled.status_code == 401, "never a redirect to the device login"

    def test_forged_bridge_headers_are_not_an_identity(self, monkeypatch):
        from fastapi.testclient import TestClient
        from sentrix.link import tunnel_auth
        monkeypatch.setenv("SENTRIX_PASSWORD", "pw")
        forged = {tunnel_auth.HEADER_SECRET: "guess", tunnel_auth.HEADER_ROLE: "owner",
                  tunnel_auth.HEADER_USER_ID: "x", tunnel_auth.HEADER_BASE: "/d/x"}
        assert TestClient(_lc_app()).get("/api/who", headers=forged).status_code == 401

    def test_unknown_roles_are_a_programming_error(self):
        from sentrix.web import auth
        with pytest.raises(ValueError):
            auth.require_lc("company_admin")


class TestLcRoutesAreThinWrappers:
    @pytest.fixture
    def client(self, temp_db, monkeypatch):
        from fastapi.testclient import TestClient
        from sentrix.app import app
        from sentrix.web.security import generate_token
        monkeypatch.setenv("SENTRIX_PASSWORD", "pw")
        from sentrix.web import auth
        client = TestClient(app)                 # no lifespan: no pipeline, no camera
        cookie = generate_token("pw")
        client.cookies.set("lc_session", cookie)
        client.headers["X-CSRF-Token"] = auth.csrf_for_session(cookie)
        return client

    def test_false_verdict_goes_through_the_sos_service(self, client):
        incident = _incident()
        database.save_incident(incident, "s1")
        database.save_dispatch(_package("p0", incident.id))
        assert client.post(f"/api/incidents/{incident.id}/verdict",
                           data={"verdict": "FALSE"}).status_code == 200
        assert database.get_dispatch("p0")["status"] == "CANCELLED"
        assert database.recent_incidents()[0]["verdict_by"] == "lc:device_admin"
        assert client.post("/api/incidents/nope/verdict",
                           data={"verdict": "FALSE"}).status_code == 404

    def test_send_maps_to_409_and_mode_toggles(self, client):
        from sentrix import mode
        incident = _incident()
        database.save_incident(incident, "s1")
        database.save_dispatch(_package("p1", incident.id, status="SENT"))
        refused = client.post("/api/dispatch/p1/send/POLICE")
        assert refused.status_code == 409 and refused.json()["code"] == "conflict"
        database.save_dispatch(_package("p2", incident.id))
        assert client.post("/api/dispatch/p2/send/FIRE").json()["status"] == "failed"
        assert client.post("/api/mode", json={"armed": True}).json()["mode"]["armed"]
        assert mode.is_armed()
        assert client.post("/api/mode", json={"armed": False}).status_code == 200
        assert not mode.is_armed()


class TestFailClosedHardening:
    def test_a_broken_hardening_module_refuses_writes_only(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from sentrix.web import extensions
        monkeypatch.setattr(extensions, "ROUTER_MODULES", ())
        monkeypatch.setattr(extensions, "MIDDLEWARE_MODULES", ("sentrix.web._missing_",))
        app = FastAPI()

        @app.get("/x")
        def read():
            return {"ok": True}

        @app.post("/x")
        def write():
            return {"ok": True}

        assert extensions.install(app) == []
        client = TestClient(app)
        assert client.get("/x").status_code == 200
        refused = client.post("/x")
        assert refused.status_code == 503 and refused.json()["code"] == "internal"


# ── live loop seams ───────────────────────────────────────────────────────────
class _Camera:
    name = "fake"
    online = True

    def __init__(self):
        self.seq = 0

    def latest(self, after_seq=-1):
        import numpy as np
        self.seq += 1
        return np.zeros((8, 8, 3), np.uint8), self.seq, time.monotonic()

    def release(self):
        pass


class TestPipelineSeams:
    def test_run_live_takes_an_inference_strategy(self):
        from sentrix.pipeline.engine import InferenceStrategy, Pipeline
        from sentrix.config import settings
        pipeline = Pipeline.__new__(Pipeline)
        pipeline.cfg = settings
        pipeline.source = _Camera()
        pipeline._init_live()

        class Recording:
            name = "recording"

            def __init__(self):
                self.frames, self.ran, self.woken = 0, False, False

            def on_frame(self, frame, captured_at):
                self.frames += 1

            def run(self, stop):
                self.ran = True
                stop.wait(5)

            def wake(self):
                self.woken = True

        strategy = Recording()
        assert isinstance(strategy, InferenceStrategy)
        stop = threading.Event()
        thread = threading.Thread(target=pipeline.run_live, args=(stop, strategy))
        thread.start()
        time.sleep(0.3)
        stop.set()
        thread.join(5)
        assert strategy.ran and strategy.woken and strategy.frames > 5
        assert pipeline.preview_seq > 5, "capture and preview stay in run_live"

    def test_clip_mode_falls_back_to_ticks_without_the_engine(self, monkeypatch):
        from sentrix.pipeline.engine import Pipeline, TickStrategy
        from sentrix.config import settings
        # The clip engine exists now (T-CLIP): hide it to test the
        # "module missing" branch, not build_strategy's own fallback.
        monkeypatch.setitem(sys.modules, "sentrix.pipeline.clip", None)
        pipeline = Pipeline.__new__(Pipeline)
        pipeline.cfg = SimpleNamespace(clip=SimpleNamespace(mode="clip"),
                                       pipeline=settings.pipeline)
        pipeline._init_live()
        assert isinstance(pipeline._build_strategy(), TickStrategy)

    def test_shutdown_drains_queued_side_effects(self):
        import queue
        from sentrix.pipeline.engine import Pipeline
        fake = Pipeline.__new__(Pipeline)
        fake._tasks = queue.Queue(maxsize=64)
        fake._stop = threading.Event()
        fake.incidents = SimpleNamespace(current=None)
        fake.audio = None
        fake.source = _Camera()
        fake.events = SimpleNamespace(close=lambda: None)
        ran = []
        for n in range(5):
            fake._tasks.put((lambda n=n: (time.sleep(0.05), ran.append(n)), (), {}))
        fake._worker = threading.Thread(target=fake._task_loop, daemon=True)
        fake._worker.start()
        Pipeline.shutdown(fake)
        assert ran == [0, 1, 2, 3, 4] and not fake._worker.is_alive()

    def test_close_request_closes_and_clears_temporal_state(self):
        from sentrix.pipeline.engine import Pipeline
        from sentrix.pipeline.incidents import IncidentManager
        fake = Pipeline.__new__(Pipeline)
        fake.incidents = IncidentManager()
        incident = _incident(fake.incidents)
        incident.saved_at = 0.0                  # persisted, so it is kept when closed
        resets = []
        for name in ("policy", "evidence", "tci"):
            setattr(fake, name, SimpleNamespace(reset=lambda name=name: resets.append(name)))
        fake.events = SimpleNamespace(audit=lambda event, **d: resets.append(event))
        fake.session_id = "s1"
        submitted = []
        fake._submit = lambda func, *a: submitted.append(a[0].id)
        fake._committed_level = 3
        levels = []
        fake._level_changed = lambda level, status, result, inc, now: levels.append(level)
        fake._close_requested = None
        assert Pipeline.request_close_incident(fake, "demo") is True
        Pipeline._apply_close_request(fake, SimpleNamespace(), 5.0)
        assert fake.incidents.current is None and submitted == [incident.id]
        assert resets == ["incident_closed", "policy", "evidence", "tci"]
        assert levels == [1] and fake._close_requested is None

    def test_harness_rejects_unknown_modes_before_building_anything(self):
        from sentrix.evaluation.harness import build_eval_pipeline
        with pytest.raises(ValueError):
            build_eval_pipeline(mode="hybrid")


class TestLifespanOrder:
    def test_services_before_cleanup_and_pipeline_before_services_at_stop(self, monkeypatch,
                                                                          temp_db):
        from fastapi import FastAPI
        from sentrix import app as app_module
        from sentrix import services
        from sentrix.pipeline import engine
        order = []

        class FakePipeline:
            session_id = "now"
            sos = None

            def run_live(self, stop):
                stop.wait(5)

        fake = FakePipeline()
        monkeypatch.setattr(engine, "get_pipeline", lambda: fake)
        monkeypatch.setattr(engine, "shutdown_pipeline", lambda: order.append("pipeline"))
        monkeypatch.setattr(database, "close_interrupted",
                            lambda session: order.append("close_interrupted"))
        monkeypatch.setattr(database, "prune", lambda days: 0)
        seen_extras = {}

        def start_services(pipeline, app, loop):
            seen_extras.update(app=app, loop=loop)
            order.append("services")

        monkeypatch.setattr(app_module, "_start_services", start_services)
        monkeypatch.setattr(services.registry, "clear", lambda: order.append("registry"))
        probe = FastAPI()

        async def run():
            async with app_module.lifespan(probe):
                order.append("serving")

        asyncio.run(run())
        assert order == ["services", "close_interrupted", "serving", "pipeline", "registry"]
        assert seen_extras["app"] is probe and seen_extras["loop"] is not None


# ── siren test ────────────────────────────────────────────────────────────────
class TestSirenTest:
    def test_one_shared_limiter(self):
        from sentrix.io.actuators import ConsoleSiren, siren_test
        calls = []
        siren = ConsoleSiren()
        siren.activate = lambda duration=None: calls.append(duration)
        first = siren_test(siren, actor=Actor.device_admin())
        assert first["ok"] and first["seconds"] == 1.0 and calls == [1.0]
        second = siren_test(siren, actor=OWNER)
        assert second["code"] == "rate_limited" and second["retry_after"] > 0
        assert calls == [1.0]


# ── services: preflight hook for the doctor ───────────────────────────────────
class TestPreflight:
    def test_preflight_rows_and_failures(self, monkeypatch):
        import sys
        import types
        from sentrix import services
        good = types.ModuleType("sentrix_test_good")
        good.preflight = lambda settings: [{"status": "ok", "name": "x", "detail": "d"}]
        bad = types.ModuleType("sentrix_test_bad")
        bad.preflight = lambda settings: 1 / 0
        monkeypatch.setitem(sys.modules, "sentrix_test_good", good)
        monkeypatch.setitem(sys.modules, "sentrix_test_bad", bad)
        report = services.preflight_report(None, (("g", "sentrix_test_good:build_service"),
                                                  ("b", "sentrix_test_bad:build_service"),
                                                  ("m", "sentrix_missing_mod:build_service")))
        assert report["g"] == [{"status": "ok", "name": "x", "detail": "d"}]
        assert report["b"][0]["status"] == "fail" and "m" not in report


# ── Portal ────────────────────────────────────────────────────────────────────
class TestPortalKeys:
    def test_keys_are_generated_once_and_reused(self, tmp_path):
        from sentrix_portal.settings import DATA_KEY_FILE, SECRET_KEY_FILE, PortalSettings
        first = PortalSettings(data_dir=str(tmp_path), test_mode=True).checked()
        again = PortalSettings(data_dir=str(tmp_path), test_mode=True).checked()
        assert first.secret_key == again.secret_key and first.data_key == again.data_key
        assert first.secret_key != first.data_key
        assert (tmp_path / SECRET_KEY_FILE).read_text().strip() == first.secret_key
        assert (tmp_path / DATA_KEY_FILE).exists()
        assert first.extras["key_files_created"] == [SECRET_KEY_FILE, DATA_KEY_FILE]
        assert again.extras["key_files_created"] == []

    def test_production_needs_env_keys_and_tests_get_throwaway_ones(self, tmp_path):
        from sentrix_portal.settings import PortalSettings
        with pytest.raises(RuntimeError):
            PortalSettings(data_dir=str(tmp_path), test_mode=False).checked()
        a, b = PortalSettings.for_tests(tmp_path / "a"), PortalSettings.for_tests(tmp_path / "b")
        assert a.secret_key != b.secret_key and not a.workers
        assert not (tmp_path / "a" / "portal-data" / "secret_key").exists()


class TestPortalLifespanAndHeaders:
    def test_background_modules_start_and_stop_only_with_workers(self, tmp_path, monkeypatch):
        import sys
        import types
        from fastapi.testclient import TestClient
        from sentrix_portal import app as portal_app_module
        from sentrix_portal.settings import PortalSettings
        log = []
        worker = types.ModuleType("sentrix_portal_fake_worker")

        def start(app):
            log.append(("start", app.state.settings.workers))

            async def stop():
                log.append("stop")
            return stop

        worker.start = start
        monkeypatch.setitem(sys.modules, "sentrix_portal_fake_worker", worker)
        monkeypatch.setattr(portal_app_module, "BACKGROUND_MODULES",
                            ("sentrix_portal_fake_worker", "sentrix_portal.services.nonexistent"))
        with TestClient(portal_app_module.create_app(PortalSettings.for_tests(tmp_path / "off"))):
            pass
        assert log == []
        app = portal_app_module.create_app(PortalSettings.for_tests(tmp_path / "on", workers=True))
        with TestClient(app):
            assert app.state.background == ["sentrix_portal_fake_worker"]
        assert log == [("start", True), "stop"]

    def test_csp_on_portal_pages_not_on_the_tunnel(self, portal_app):
        from fastapi.responses import HTMLResponse, Response
        from fastapi.testclient import TestClient

        @portal_app.get("/d/{device_id}/dashboard")
        def tunnelled(device_id: str):
            return Response("<p>console</p>", media_type="text/html",
                            headers={"Content-Security-Policy": "script-src 'nonce-dev'"})

        @portal_app.get("/_nonce", response_class=HTMLResponse)
        def nonce(request: Request):
            return request.state.csp_nonce

        client = TestClient(portal_app)
        page = client.get("/_nonce")
        assert f"'nonce-{page.text}'" in page.headers["content-security-policy"]
        assert page.headers["x-frame-options"] == "DENY"
        console = client.get("/d/dev-1/dashboard")
        assert console.headers["content-security-policy"] == "script-src 'nonce-dev'"
        assert "x-frame-options" not in console.headers
        assert console.headers["x-content-type-options"] == "nosniff"


class TestPortalSecurityFixes:
    def _session(self, portal_app, role="owner"):
        from sentrix_portal import security
        from sentrix_portal.models import User
        with portal_app.state.db.session() as db:
            user = User(username=f"{role}-1", role=role, status="active")
            db.add(user)
            db.flush()
            raw, row = security.create_session(db, user)
            return raw, row.csrf_token

    def test_header_only_csrf_leaves_the_body_for_the_proxy(self, portal_app):
        from fastapi import Depends
        from fastapi.testclient import TestClient
        from sentrix_portal import security

        @portal_app.post("/d/{device_id}/{path:path}")
        async def proxy(device_id: str, path: str, request: Request,
                        p=Depends(security.require_role("owner", csrf="header"))):
            body = await request.body()
            return {"forwarded": body.decode()}

        raw, csrf = self._session(portal_app)
        client = TestClient(portal_app)
        client.cookies.set("portal_session", raw)
        form = {"verdict": "FALSE", "csrf_token": csrf}
        assert client.post("/d/dev/api/x", data=form).status_code == 403, \
            "a form field is not accepted on the proxy"
        ok = client.post("/d/dev/api/x", data={"verdict": "FALSE"},
                         headers={"X-CSRF-Token": csrf})
        assert ok.status_code == 200 and ok.json()["forwarded"] == "verdict=FALSE"

    def test_logged_out_tunnel_bookmark_goes_to_login(self, portal_app):
        from fastapi import Depends
        from fastapi.testclient import TestClient
        from sentrix_portal import security

        @portal_app.get("/d/{device_id}/{path:path}")
        def page(device_id: str, path: str, p=Depends(security.require_role())):
            return {}

        client = TestClient(portal_app, follow_redirects=False)
        html = client.get("/d/dev/dashboard", headers={"accept": "text/html"})
        assert html.status_code == 303 and html.headers["location"] == "/login"
        api = client.get("/d/dev/api/health", headers={"accept": "text/html"})
        assert api.status_code == 401

    def test_audit_chain_verifies_with_non_json_detail(self, portal_app):
        from datetime import datetime, timezone
        from sentrix_portal import security
        with portal_app.state.db.session() as db:
            security.audit(db, "admin.customer_created",
                           detail={"at": datetime(2026, 10, 14, tzinfo=timezone.utc),
                                   "name": "Ríya"})
            assert security.verify_audit_chain(db)["ok"]

    def test_existing_portal_database_gains_new_columns(self, tmp_path):
        from sqlalchemy import create_engine, inspect
        from sentrix_portal.db import Database
        url = f"sqlite:///{(tmp_path / 'old.db').as_posix()}"
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE notifications (id VARCHAR(36) PRIMARY KEY, "
                                 "body TEXT)")
        engine.dispose()
        database = Database(url)
        database.init()
        columns = {c["name"] for c in inspect(database.engine).get_columns("notifications")}
        assert {"meta", "dedupe_key", "status"} <= columns
        database.dispose()


class TestPortalServiceSeams:
    """The frozen Portal service functions, now implemented by T-LINK, T-CH
    and T-ACCT: still safe without a device, a session or a recipient."""

    def test_signatures_and_safe_defaults(self, portal_app):
        from sentrix_portal.models import EmergencyContact, Household
        from sentrix_portal.services import accounts, hub, notify, sos
        assert hub.is_online("dev") is False
        result = asyncio.run(hub.send_command("dev", "diag.ping", {}, OWNER.to_dict(),
                                              timeout=1.0))
        assert result["code"] == "device_offline" and result["queued"] is False
        assert asyncio.run(hub.push_profile(None, "dev")) == 0
        assert asyncio.run(sos.respond(None, "r", "approve", actor=OWNER.to_dict(),
                                       channel="portal"))["code"] == "unsupported"
        assert notify.enqueue(None, None, None) is None
        with portal_app.state.db.session() as db:
            home = Household(customer_code="SX-0001", display_name="Demo")
            db.add(home)
            db.flush()
            db.add_all([EmergencyContact(household_id=home.id, name="A", priority=2,
                                         consent_status="CONFIRMED"),
                        EmergencyContact(household_id=home.id, name="B", priority=1,
                                         consent_status="PENDING")])
            db.flush()
            assert [c.name for c in accounts.confirmed_contacts(db, home.id)] == ["A"]
            assert accounts.bump_profile_version(db, home.id) == 1
            assert accounts.bump_profile_version(db, home.id) == 2
            with pytest.raises(LookupError):
                accounts.build_profile(db, home.id, "no-such-device")
