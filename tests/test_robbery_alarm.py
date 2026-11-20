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

from test_sos_engine import advance, event_names, fire_trigger, the_request, world  # noqa: F401

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
        assert "owner" in recipients and "c1" not in recipients

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

    def test_default_classes_follow_the_switch(self, monkeypatch):
        sys.path.insert(0, str(ROOT / "tools"))
        import calibrate_room as cr
        from sentrix import config
        assert cr.default_classes() == ("weapon", "fire")
        monkeypatch.setattr(config, "settings",
                            dataclasses.replace(config.settings, threat=ON))
        assert cr.default_classes() == ("weapon", "fire", "robbery")
        assert cr.Rules.from_settings().person_classes == ("weapon", "robbery")
