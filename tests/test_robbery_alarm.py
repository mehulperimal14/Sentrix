"""D26: ``SENTRIX_ROBBERY_ALARM`` promotes a confirmed robbery to Level 3.

The demo's staged scene is a masked theft. Robbery used to be experimental
(L2 at most, no SOS request). With the switch on, a robbery the scene
classifier confirmed (k of n, a person in view) lands in the L3 band and opens
an SOS request for the owner only; it is still one visual modality, so it
never reaches L4/L5, and an unrecognised person alone is still an L2 (D2).
Off by default: nothing changes unless the switch is set.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from sentrix.config import settings
from sentrix.fusion.evidence import ClipConfirmation, ClipObservation, EvidenceAccumulator
from sentrix.fusion.features import FeatureVector
from sentrix.fusion.policy import level_for
from sentrix.fusion.tci import TCIEngine, contributions, default_reason
from sentrix.sos import machine as M
from sentrix.sos import states as S
from sentrix.sos.policy import Rules, contacts_delay
from sentrix.storage import requests

from test_sos_engine import (OWNER, advance, event_names, fire_trigger,  # noqa: F401
                             the_request, world)

ROOT = Path(__file__).resolve().parents[1]
OFF = settings.threat
ON = dataclasses.replace(settings.threat, robbery_alarm=True)
FLOOR = settings.threat.scene_threshold_floor["robbery"]          # 0.60
L2, L3, L4 = settings.policy.thresholds[0], settings.policy.thresholds[1], \
    settings.policy.thresholds[2]
RULES_ON = Rules(test_mode=True, alarm_classes=Rules().alarm_classes + ("robbery",))


def robbery(p: float, threshold: float = FLOOR) -> dict:
    return {"scene": {"robbery": (p, threshold)}}


def run(ticks, cfg, person=True, hz=2.0):
    acc = EvidenceAccumulator(cfg)
    evidence = None
    for i, sources in enumerate(ticks):
        evidence = acc.update(i / hz, sources, person_recent=person)
    return evidence


def tci_of(evidence, cfg, **features):
    engine = TCIEngine()
    engine.threat = cfg
    vector = FeatureVector(extras={"evidence": {c: e.to_dict() for c, e in evidence.items()},
                                   "person_recent": True}, **features)
    return engine.compute(vector, now=0.0)


# ── the switch ────────────────────────────────────────────────────────────────
class TestSwitch:
    def test_off_by_default(self):
        assert settings.threat.robbery_alarm is False
        assert "robbery" in settings.threat.experimental_classes
        assert "robbery" not in settings.sos.alarm_classes
        assert settings.threat.person_gated_classes == ("weapon",)

    def test_on_moves_robbery_out_of_the_experimental_classes(self):
        assert ON.experimental_classes == ("assault",)
        assert ON.person_gated_classes == ("weapon", "robbery")

    def test_switching_a_copy_off_puts_robbery_back_under_the_cap(self):
        """replace(ON, robbery_alarm=False) must not leave robbery neither
        capped nor person-gated (an L3 from an empty room)."""
        off = dataclasses.replace(ON, robbery_alarm=False)
        assert off.experimental_classes == ("assault", "robbery")
        assert off.person_gated_classes == ("weapon",)
        assert off.fail_closed_person_classes == ()
        ev = run([robbery(0.95), robbery(0.60), robbery(0.95), robbery(0.60)], off,
                 person=False)["robbery"]
        assert ev.experimental and level_for(ev.score) == 2
        assert dataclasses.replace(off, robbery_alarm=True).experimental_classes == ("assault",)

    def test_conftest_forces_the_switch_off(self):
        """The demo Pi's .env sets SENTRIX_ROBBERY_ALARM=1: the suite must not
        depend on it (tests/conftest.py)."""
        assert os.environ["SENTRIX_ROBBERY_ALARM"] == "0"

    def test_one_environment_key_sets_both_lists(self):
        """The device reads SENTRIX_ROBBERY_ALARM once (sentrix/config.py)."""
        code = ("from sentrix.config import settings as s; "
                "print(s.threat.robbery_alarm, s.threat.experimental_classes, "
                "s.sos.alarm_classes)")
        env = {**os.environ, "SENTRIX_ROBBERY_ALARM": "1"}
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, text=True,
                             capture_output=True, timeout=120, check=True).stdout
        assert out.strip() == ("True ('assault',) "
                               "('weapon', 'fire', 'assault', 'robbery')")


# ── evidence, tick mode ───────────────────────────────────────────────────────
class TestTickEvidence:
    def test_off_a_confirmed_robbery_stays_in_l2(self):
        ev = run([robbery(0.95)] * 4, OFF)["robbery"]
        assert ev.confirmed and ev.experimental
        assert level_for(ev.score) == 2 and "experimental" in ev.reason

    def test_on_a_confirmed_robbery_with_a_person_is_l3(self):
        ev = run([robbery(0.9)] * 3, ON)["robbery"]
        assert ev.confirmed and not ev.experimental and ev.rule == "single"
        assert L3 <= ev.score <= ON.single_source_cap < L4
        assert level_for(ev.score) == 3 and "experimental" not in ev.reason

    def test_on_one_tick_never_confirms(self):
        ev = run([robbery(0.99)], ON)["robbery"]
        assert not ev.confirmed and ev.score < L2

    def test_on_a_robbery_needs_a_person(self):
        """An empty room never alarms: no person seen in the last 2 s."""
        ev = run([robbery(0.99)] * 5, ON, person=False)["robbery"]
        assert not ev.confirmed and ev.score < L2
        assert ev.votes[0].gated == "no person in view"

    def test_on_no_person_detector_never_confirms_a_robbery(self):
        """The person model failed to load or threw (person_recent None): the
        weapon gate opens, the robbery gate stays shut (fail closed)."""
        ev = run([robbery(0.99)] * 5, ON, person=None)["robbery"]
        assert not ev.confirmed and ev.score < L2
        assert "person gate off" not in ev.reason
        assert ev.votes[0].gated == "no person seen: person detector unavailable"

    def test_on_the_weapon_gate_still_opens_without_a_person_detector(self):
        ev = run([{"scene": {"weapon": (0.9, 0.5)}}] * 3, ON, person=None)["weapon"]
        assert ev.confirmed and level_for(ev.score) == 3
        assert "person gate off: person detector unavailable" in ev.reason

    def test_off_the_person_gate_is_unchanged(self):
        """Off, robbery keeps its old (ungated) experimental L2."""
        ev = run([robbery(0.95)] * 4, OFF, person=False)["robbery"]
        assert ev.confirmed and level_for(ev.score) == 2

    def test_on_the_scene_model_alone_never_reaches_l4(self):
        """One visual modality (D3): certainty for a minute is still L3."""
        ev = run([robbery(0.999)] * 120, ON)["robbery"]
        assert ev.score <= ON.single_source_cap and level_for(ev.score) == 3
        # Scene and violence are one modality (both classifiers), not two.
        both = run([{"scene": {"robbery": (0.999, FLOOR)},
                     "violence": {"robbery": (0.999, 0.5)}}] * 5, ON)["robbery"]
        assert both.state == "confirmed" and level_for(both.score) == 3

    def test_on_the_room_calibration_bar_applies(self):
        """A calibrated bar (var/calibration/scene.json) is the vote's threshold."""
        ev = run([robbery(0.70, threshold=0.78)] * 5, ON)["robbery"]
        assert not ev.confirmed and level_for(ev.score) == 1

    def test_on_detector_mode_keeps_the_scene_vote(self):
        """No detector head knows robbery: detector mode must not mute it."""
        cfg = dataclasses.replace(ON, sources="detector")
        ev = run([robbery(0.9)] * 3, cfg)["robbery"]
        assert ev.confirmed and level_for(ev.score) == 3


# ── evidence, clip mode ───────────────────────────────────────────────────────
class TestClipEvidence:
    @staticmethod
    def obs(p, at, gate=True, threshold=FLOOR):
        return ClipObservation("scene", "robbery", p, threshold, at, None, gate)

    def test_on_two_keyframes_with_a_person_are_l3(self):
        ev = ClipConfirmation(2, cfg=ON).update([self.obs(0.9, 1.0), self.obs(0.85, 2.0)])
        assert ev["robbery"].confirmed and level_for(ev["robbery"].score) == 3

    def test_on_one_keyframe_or_no_person_is_not(self):
        one = ClipConfirmation(2, cfg=ON).update([self.obs(0.9, 1.0), self.obs(0.3, 2.0)])
        assert not one["robbery"].confirmed
        alone = ClipConfirmation(2, cfg=ON).update([self.obs(0.9, 1.0, gate=False),
                                                    self.obs(0.9, 2.0, gate=False)])
        assert not alone["robbery"].confirmed
        assert alone["robbery"].votes[0].gated == "no person in view"

    def test_on_no_person_detector_never_confirms_in_a_clip(self):
        ev = ClipConfirmation(2, cfg=ON).update([self.obs(0.9, 1.0, gate=None),
                                                 self.obs(0.9, 2.0, gate=None)])
        assert not ev["robbery"].confirmed and ev["robbery"].score < L2
        assert "person gate off" not in ev["robbery"].reason

    def test_off_clip_robbery_stays_in_l2(self):
        ev = ClipConfirmation(2, cfg=OFF).update([self.obs(0.95, 1.0), self.obs(0.95, 2.0)])
        assert ev["robbery"].experimental and level_for(ev["robbery"].score) == 2


# ── the TCI: type, headline, unknown people ──────────────────────────────────
class TestTci:
    def test_on_a_robbery_incident_at_l3_without_experimental(self):
        result = tci_of(run([robbery(0.9)] * 3, ON), ON, person_present=True)
        assert result.incident_type == "robbery" and level_for(result.raw_tci) == 3
        assert "experimental" not in result.reason
        people = next(r for r in result.contributions if r["input"] == "people")
        assert "a theft needs a person" in people["detail"]
        assert people["effect"] == "gate ok"

    def test_default_headline_follows_the_switch(self):
        assert default_reason("robbery", OFF) == "Possible theft (experimental)"
        assert default_reason("robbery", ON) == "Possible theft"
        assert default_reason("weapon", ON) == "Possible weapon"
        assert default_reason("assault", ON) == "Possible assault (experimental)"

    def test_hand_built_vector_uses_the_l3_band_on_and_l2_off(self):
        vector = FeatureVector(behaviour=0.95, behaviour_label="robbery")
        engine = TCIEngine()
        assert level_for(engine.compute(vector, now=0.0).raw_tci) == 2
        engine.threat = ON
        result = engine.compute(vector, now=0.0)
        assert result.incident_type == "robbery" and level_for(result.raw_tci) == 3
        assert result.reason == "Possible theft"

    def test_on_an_unrecognised_person_alone_is_still_l2(self):
        """D2: the switch is about robbery evidence, never about a face."""
        engine = TCIEngine()
        engine.threat = ON
        stranger = FeatureVector(vision=0.85, motion=1.0, identity=0.6, person_present=True)
        result = None
        for i in range(int((ON.intrusion_home_dwell_seconds + 5) * 6)):
            result = engine.compute(stranger, now=i / 6)
        assert result.incident_type == "intrusion" and level_for(result.raw_tci) == 2

    def test_on_an_unconfirmed_robbery_beside_a_stranger_stays_l2(self):
        evidence = run([robbery(0.95), robbery(0.1), robbery(0.1)], ON)
        assert not evidence["robbery"].confirmed
        result = tci_of(evidence, ON, person_present=True)
        assert level_for(result.raw_tci) <= 2

    def test_contributions_on_show_an_unknown_gate_as_failed(self):
        ev = run([robbery(0.95)] * 3, ON, person=None)
        rows = contributions(FeatureVector(extras={"evidence": {c: e.to_dict()
                                                                for c, e in ev.items()},
                                                   "person_recent": None}),
                             "robbery", threat=ON)
        people = next(r for r in rows if r["input"] == "people")
        assert people["effect"] == "gate failed"

    def test_contributions_off_show_no_robbery_gate(self):
        ev = run([robbery(0.95)] * 3, OFF)
        rows = contributions(FeatureVector(extras={"evidence": {c: e.to_dict()
                                                                for c, e in ev.items()}}),
                             "robbery", threat=OFF)
        people = next(r for r in rows if r["input"] == "people")
        assert people["effect"] == "context"


# ── SOS ───────────────────────────────────────────────────────────────────────
POLICY = {"window_seconds": 1800, "safety_defaults": True,
          "contacts_for_single_source_l3": True,
          "contacts": [{"id": "c1", "name": "Aman", "phone": "+919812345611"}]}


class TestSosRules:
    def test_off_robbery_never_opens(self):
        assert not M.wants("robbery", 3, Rules())

    def test_on_robbery_at_l3_is_an_alarm(self):
        assert M.wants("robbery", 3, RULES_ON, "confirmed")
        assert not M.wants("robbery", 2, RULES_ON, "confirmed")
        facts = M.TriggerFacts(incident_id="i1", level=3, incident_type="robbery",
                               evidence_state="confirmed", at=0.0, armed=False)
        assert M.classify(facts, RULES_ON) == "alarm"

    def test_single_source_robbery_alerts_the_owner_only(self):
        """No safety default sends a scene-only robbery to the contacts, even
        with the single-source weapon gate (P8) open."""
        assert contacts_delay(POLICY, incident_type="robbery", peak_level=3,
                              rules=RULES_ON) is None


@pytest.fixture
def robbery_world(world):
    """The SOS engine's test world with robbery among the alarm classes."""
    world.engine.rules = dataclasses.replace(world.engine.rules,
                                             alarm_classes=RULES_ON.alarm_classes)
    return world


class TestSosEngine:
    def test_on_a_robbery_request_prompts_the_owner_only(self, robbery_world):
        fire_trigger(robbery_world, "robbery")
        req = the_request()
        assert req["state"] == S.PENDING and req["incident_type"] == "robbery"
        assert "possible THEFT (one camera check)" in req["sos_text_full"]
        advance(robbery_world, 1801)
        assert "contacts_notified" not in event_names(req["id"])
        recipients = {ref for ref, _, _ in robbery_world.channels.keys(req["id"])}
        assert "owner" in recipients and "contact:c1" not in recipients

    def test_a_fire_takes_over_an_open_robbery_request(self, robbery_world):
        """A robbery request (owner only) must not swallow a fire: the fire
        takes it over and its T+2 min safety default reaches the contacts."""
        fire_trigger(robbery_world, "robbery")
        req = the_request()
        fire_trigger(robbery_world, "fire")                    # a new incident
        assert len(requests.list_requests(5)) == 1
        req = requests.get_request(req["id"])
        assert req["incident_type"] == "fire" and len(req["incident_ids"]) == 2
        assert "FIRE" in req["sos_text_full"] and "THEFT" not in req["sos_text_full"]
        attached = [e for e in requests.events(req["id"]) if e["event"] == "attached"]
        assert attached[-1]["detail"]["retyped_from"] == "robbery"
        owner_steps = {step for ref, _, step in robbery_world.channels.keys(req["id"])
                       if ref == "owner"}
        assert "retype_fire" in owner_steps
        advance(robbery_world, 200)
        events = requests.events(req["id"])
        assert [e["detail"]["reason"] for e in events
                if e["event"] == "contacts_notified"] == ["confirmed_fire"]

    def test_a_fire_in_the_same_incident_takes_over_too(self, robbery_world):
        incident = fire_trigger(robbery_world, "robbery")
        req = the_request()
        fire_trigger(robbery_world, "fire", incident_id=incident)
        req = requests.get_request(req["id"])
        assert req["incident_type"] == "fire" and req["incident_ids"] == [incident]
        advance(robbery_world, 200)
        assert "contacts_notified" in event_names(req["id"])

    def test_the_fire_delay_counts_from_the_takeover(self, robbery_world):
        """The owner gets the fire's own two minutes, and a fresh window."""
        fire_trigger(robbery_world, "robbery")
        req = the_request()
        advance(robbery_world, 600)
        assert "contacts_notified" not in event_names(req["id"])
        fire_trigger(robbery_world, "fire")
        taken = requests.get_request(req["id"])
        assert taken["expires_at"] > req["expires_at"]
        advance(robbery_world, 100)
        assert "contacts_notified" not in event_names(req["id"])
        advance(robbery_world, 30)
        assert "contacts_notified" in event_names(req["id"])

    def test_on_the_help_path_the_desk_and_contacts_hear_of_the_fire(self, robbery_world):
        fire_trigger(robbery_world, "robbery")
        req = the_request()
        result = robbery_world.engine.respond(req["id"], "approve", actor=OWNER,
                                              channel="portal", authority="POLICE")
        assert result["ok"], result
        fire_trigger(robbery_world, "fire")
        req = requests.get_request(req["id"])
        assert req["incident_type"] == "fire"
        steps = {(ref, step) for ref, _, step in robbery_world.channels.keys(req["id"])}
        assert ("contact:c1", "retype_fire") in steps and ("desk", "retype_fire") in steps
        assert ("owner", "retype_fire") not in steps

    def test_a_robbery_never_takes_over_another_class(self):
        snap = M.Snapshot(id="r1", state=S.PENDING, incident_type="fire")
        facts = M.TriggerFacts("i2", "robbery", 3, "confirmed", 0.0)
        assert not M.takes_over(snap, facts, RULES_ON)
        steps = M.attach_steps(snap, facts, RULES_ON)
        assert [s.event for s in steps] == ["attached"]
        assert "incident_type" not in steps[0].fields
        robbery_snap = dataclasses.replace(snap, incident_type="robbery")
        assert M.takes_over(robbery_snap, dataclasses.replace(facts, incident_type="fire"),
                            RULES_ON)

    def test_off_a_robbery_trigger_opens_nothing(self, world):
        fire_trigger(world, "robbery")
        assert requests.list_requests(5) == []


class TestPipelineHook:
    def test_on_the_pipeline_hands_a_robbery_l3_to_sos(self, temp_db):
        from test_sos_engine import Clock
        from test_sos_replay import drain, hook_pipeline, make_engine, result_of
        from sentrix.pipeline.escalation import plan_for
        from sentrix.storage import database
        engine = make_engine(Clock())
        try:
            engine.rules = dataclasses.replace(engine.rules,
                                               alarm_classes=RULES_ON.alarm_classes)
            fake = hook_pipeline(engine)
            incident, _ = fake.incidents.update(3, 0.5, "robbery", now=0.0, features={},
                                                reason="robbery confirmed")
            database.save_incident(incident, "sess-hook", ongoing=True)
            fake._act(incident, plan_for(3), np.zeros((4, 4, 3), np.uint8),
                      result_of("robbery", 3), 3, "ELEVATED", 0.0)
            assert "sos:robbery" in incident.actions
            drain(fake)
            engine.work()
            [req] = requests.list_requests(5)
            assert req["incident_type"] == "robbery" and req["state"] == "PENDING"
        finally:
            engine.stop()


# ── tools/calibrate_room.py ───────────────────────────────────────────────────
class TestCalibration:
    def test_robbery_bar_from_a_quiet_log_with_the_person_gate(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import calibrate_room as cr
        from test_calibrate_room import clip
        rules = cr.Rules(person_classes=("weapon", "robbery"))
        rows = [clip("q", 10.0, [(1, -2.0, True, {"robbery": 0.52}),
                                 (2, -1.0, True, {"robbery": 0.55})]),
                # Nobody in view: the device would not count these.
                clip("q", 20.0, [(3, -2.0, False, {"robbery": 0.80}),
                                 (4, -1.0, False, {"robbery": 0.80})])]
        log = cr.collect(rows, ["robbery"], rules)["robbery"]
        assert [m.confirm for m in log.moments] == pytest.approx([0.52, 0.0])
        report = cr.propose(log, 0.60)
        # max(floor 0.60, 0.52 + 0.10, single max 0.80 + 0.01)
        assert report["proposed"] == pytest.approx(0.81)
        ungated = cr.collect(rows, ["robbery"], cr.Rules())["robbery"]
        assert max(m.confirm for m in ungated.moments) == pytest.approx(0.80)

    def test_the_device_env_file_sets_the_robbery_gate(self, tmp_path):
        """On the Pi the switch lives in ~/Sentrix/.env and the script arrives
        on stdin (the D23 usage): it must replay the device's own robbery gate."""
        from test_calibrate_room import clip
        (tmp_path / ".env").write_text("SENTRIX_ROBBERY_ALARM=1\n", encoding="utf-8")
        events = tmp_path / "events"
        events.mkdir()
        probs = {"robbery": 0.30, "weapon": 0.20, "fire": 0.10}
        rows = [clip("q", 10.0, [(1, -2.0, True, probs), (2, -1.0, True, probs)])]
        (events / "events_20261121_000000_test.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "SENTRIX_ROBBERY_ALARM"}
        env.update(PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
        report = tmp_path / "report.json"
        out = subprocess.run(
            [sys.executable, "-", "--events", str(events), "--json", str(report)],
            input=(ROOT / "tools" / "calibrate_room.py").read_text(encoding="utf-8"),
            cwd=tmp_path, env=env, encoding="utf-8", capture_output=True, timeout=300)
        assert "a person needed for: weapon, robbery" in out.stdout, out.stdout + out.stderr
        data = json.loads(report.read_text(encoding="utf-8"))
        assert data["sources"]["person_gate"] == ["weapon", "robbery"]
        assert [c["class"] for c in data["classes"]] == ["weapon", "fire", "robbery"]

    def test_default_classes_follow_the_switch(self, monkeypatch):
        sys.path.insert(0, str(ROOT / "tools"))
        import calibrate_room as cr
        from sentrix import config
        assert cr.default_classes() == ("weapon", "fire")
        monkeypatch.setattr(config, "settings",
                            dataclasses.replace(config.settings, threat=ON))
        assert cr.default_classes() == ("weapon", "fire", "robbery")
        rules = cr.Rules.from_settings()
        assert rules.person_classes == ("weapon", "robbery")
        assert rules.fail_closed == ("robbery",)

    def test_a_tick_with_no_person_detector_never_confirms_a_robbery(self):
        """The tool replays the device's fail-closed robbery gate (person None)."""
        sys.path.insert(0, str(ROOT / "tools"))
        import calibrate_room as cr
        from test_calibrate_room import tick
        rules = cr.Rules(person_classes=("weapon", "robbery"), fail_closed=("robbery",))
        rows = [tick("t", 100.0 + i, {"robbery": 0.9, "weapon": 0.9}, person=None)
                for i in range(3)]
        logs = cr.collect(rows, ["robbery", "weapon"], rules)
        assert max(m.confirm for m in logs["robbery"].moments) == 0.0
        assert max(m.confirm for m in logs["weapon"].moments) == pytest.approx(0.9)


# ── the Portal's device-offline rule ──────────────────────────────────────────
class TestPortalOfflineRule:
    """D26: a robbery reaches contacts through the Portal's offline rule only
    where the Portal itself runs with SENTRIX_ROBBERY_ALARM=1 (the Render
    Portal of the demo does not), with or without a request already open."""

    def test_offline_opens_request_follows_the_portals_own_key(self, monkeypatch):
        from sentrix_portal.services import sos as portal_sos
        from test_link_portal import robbery_portal
        assert not portal_sos.offline_opens_request("robbery", 3)
        assert portal_sos.offline_opens_request("weapon", 3)
        robbery_portal(monkeypatch)
        assert portal_sos.offline_opens_request("robbery", 3)
        assert not portal_sos.offline_opens_request("robbery", 2)

    def test_offline_alerts_contacts_of_an_open_request(self, monkeypatch):
        from types import SimpleNamespace
        from sentrix_portal.services.sos import offline_alerts_contacts
        from test_link_portal import robbery_portal

        def row(kind, **flags):
            return SimpleNamespace(incident_type=kind, peak_level=3, flags=flags)

        for kind in ("weapon", "fire", "assault", "intrusion", "duress"):
            assert offline_alerts_contacts(row(kind), 1), kind
        assert not offline_alerts_contacts(row("robbery"), 3)
        assert offline_alerts_contacts(row("robbery", duress=True), 3)
        robbery_portal(monkeypatch)
        assert offline_alerts_contacts(row("robbery"), 3)
