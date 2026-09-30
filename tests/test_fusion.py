"""Regression tests for the TCI fusion layer.

Each test pins a defect found in a previous implementation. They are written
so a failure names the regression rather than just an assertion.

Since M1 (CLAUDE.md) the TCI is the strongest confirmed per-class evidence.
A hand-built FeatureVector without an evidence block speaks the TCI scale
directly: ``weapon`` and ``fire`` *are* class evidence.
"""

from __future__ import annotations

import dataclasses

import pytest

from sentrix.config import settings
from sentrix.fusion.features import (CONTRACT_V1, CONTRACT_V2, FeatureVector,
                                     contract_for)
from sentrix.fusion.policy import LevelPolicy, level_for
from sentrix.fusion.tci import TCIEngine


def steady(features: FeatureVector, seconds: float = 12.0, hz: float = 6.0,
           armed: bool = False):
    """Run one feature vector to EMA convergence and return the final result."""
    engine = TCIEngine()
    if armed:
        engine.threat = dataclasses.replace(settings.threat, armed=True)
    dt, now, result = 1.0 / hz, 0.0, None
    for _ in range(int(seconds * hz)):
        now += dt
        result = engine.compute(features, now=now)
    return result


class TestEmptyScene:
    """The headline bug: an idle room scored 0.425 and sat at level 2 forever.

    Root cause was train/serve skew — the booster was fitted on raw scores in
    [0, 1] but served through an unfitted sigmoid that compressed inputs to
    [0.378, 0.731], so the model never saw its own training distribution.
    """

    def test_empty_room_is_quiet(self):
        result = steady(FeatureVector())
        assert result.tci < 0.10, (
            f"empty scene scored {result.tci:.3f}; previously 0.425 due to the "
            "unfitted input calibration")
        assert level_for(result.tci) == 1

    def test_empty_room_at_night_is_quiet(self):
        result = steady(FeatureVector(is_night=1.0))
        assert result.tci < 0.15
        assert level_for(result.tci) == 1

    def test_synthetic_booster_is_retired(self):
        """The shipped booster was fitted to a formula in which identity meant
        familiarity; it scored residents ~5x higher than strangers (D6)."""
        assert TCIEngine().model_source == "evidence"


class TestPresenceIsContext:
    """Every unenrolled teammate used to become an L2 incident with a snapshot:
    presence added +0.15, and one saturated motion frame fired the intrusion
    override (CLAUDE.md §5, D2)."""

    def test_presence_alone_does_not_raise_the_score_at_home(self):
        person = steady(FeatureVector(vision=0.85, motion=1.0, identity=0.6,
                                      person_present=True))
        assert person.tci < settings.policy.thresholds[0]
        assert person.incident_type == "normal"

    def test_armed_mode_raises_a_sustained_stranger(self):
        result = steady(FeatureVector(vision=0.85, identity=0.6, person_present=True),
                        armed=True)
        assert result.incident_type == "intrusion"
        assert result.raw_tci == pytest.approx(settings.threat.intrusion_level)
        assert level_for(result.raw_tci) == 3

    def test_armed_mode_waits_for_the_dwell(self):
        engine = TCIEngine()
        engine.threat = dataclasses.replace(settings.threat, armed=True)
        stranger = FeatureVector(vision=0.85, identity=0.6, person_present=True)
        first = engine.compute(stranger, now=0.0)
        assert first.incident_type == "normal", "one frame of a stranger is not an intrusion"

    def test_armed_intrusion_survives_missed_detections(self):
        """80 % person recall in the dark must not restart the dwell (F3)."""
        engine = TCIEngine()
        engine.threat = dataclasses.replace(settings.threat, armed=True)
        stranger = FeatureVector(vision=0.85, identity=0.6, person_present=True)
        missed = FeatureVector()
        best = 0.0
        for i in range(40):                                     # 20 s at 2 Hz
            features = missed if i % 5 == 4 else stranger
            best = max(best, engine.compute(features, now=i / 2).raw_tci)
        assert best == pytest.approx(settings.threat.intrusion_level)

    def test_armed_intrusion_clears_once_the_person_is_gone(self):
        engine = TCIEngine()
        engine.threat = dataclasses.replace(settings.threat, armed=True)
        stranger = FeatureVector(vision=0.85, identity=0.6, person_present=True)
        for i in range(10):
            engine.compute(stranger, now=i / 2)
        result = None
        for i in range(10, 20):
            result = engine.compute(FeatureVector(), now=i / 2)
        assert result.incident_type == "normal" and result.raw_tci == 0.0

    def test_full_range_is_reachable(self):
        low = steady(FeatureVector()).tci
        high = steady(FeatureVector(fire=0.9)).tci
        assert low < 0.10 and high > 0.85


class TestDirectEvidence:
    def test_fire_is_critical(self):
        result = steady(FeatureVector(fire=0.88))
        assert result.tci >= 0.85 and result.incident_type == "fire"

    def test_weapon_is_critical(self):
        result = steady(FeatureVector(vision=0.8, weapon=0.86, person_present=True))
        assert result.tci >= 0.85 and result.incident_type == "weapon"

    def test_authorised_resident_alone_scores_nothing(self):
        result = steady(FeatureVector(vision=0.9, motion=0.3,
                                      person_present=True, authorized=True))
        assert result.tci < 0.02 and result.incident_type == "normal"

    def test_armed_resident_is_not_floored(self):
        """Authorisation must never suppress a weapon. The production path is
        covered end to end in test_evidence.TestIdentityNeverRelaxesEvidence;
        here, the TCI engine itself ignores authorisation for threat classes."""
        armed = steady(FeatureVector(vision=0.9, weapon=0.86,
                                     person_present=True, authorized=True))
        stranger = steady(FeatureVector(vision=0.9, weapon=0.86, person_present=True))
        assert armed.tci == stranger.tci >= 0.85

    def test_degraded_identity_cannot_authorise(self):
        """If identity is failing closed, an 'authorised' flag counts as a
        stranger, so armed mode still raises an intrusion."""
        result = steady(FeatureVector(vision=0.9, identity=0.6, person_present=True,
                                      authorized=True, identity_degraded=True),
                        armed=True)
        assert result.incident_type == "intrusion"

    def test_reason_comes_from_the_evidence(self):
        evidence = {"weapon": {"score": 0.52, "reason": "weapon confirmed: scene 0.6/0.5"},
                    "fire": {"score": 0.01, "reason": "fire unconfirmed"}}
        features = FeatureVector(weapon=0.52, fire=0.01, extras={"evidence": evidence})
        result = TCIEngine().compute(features, now=0.0)
        assert result.incident_type == "weapon"
        assert result.reason == "weapon confirmed: scene 0.6/0.5"
        assert "detector" not in result.reason, "reasons must name the real source"


class TestNoDisplayLag:
    """The first live test reported a laggy TCI: the display EMA trailed the
    committed level by more than a second at every onset ("TCI 0.37, L3").
    Evidence is confirmed over observations before it reaches the TCI, so the
    displayed value is now the decided value (M1 review F6)."""

    def test_displayed_tci_is_the_decided_tci(self):
        engine = TCIEngine()
        engine.compute(FeatureVector(), now=0.0)
        result = engine.compute(FeatureVector(fire=0.9), now=0.2)
        assert result.tci == pytest.approx(result.raw_tci) == pytest.approx(0.9)

    def test_frame_rate_does_not_change_the_value(self):
        features = FeatureVector(weapon=0.5, person_present=True)
        slow = steady(features, seconds=15.0, hz=3.0).tci
        fast = steady(features, seconds=15.0, hz=30.0).tci
        assert slow == pytest.approx(fast)

    def test_level_and_gauge_agree_at_onset(self):
        """At 6 Hz the committed level and the gauge are in the same band."""
        engine, policy = TCIEngine(), LevelPolicy()
        for i in range(12):
            result = engine.compute(FeatureVector(fire=0.55), now=i / 6)
            state = policy.update(result.raw_tci, now=i / 6)
            if state.level > 1:
                assert level_for(result.tci) >= state.level


class TestFeatureContract:
    def test_contracts_are_distinct_and_resolvable(self):
        assert len(CONTRACT_V2) == 8 and len(CONTRACT_V1) == 5
        assert contract_for(5) == "v1" and contract_for(8) == "v2"

    def test_unknown_width_is_rejected(self):
        with pytest.raises(ValueError):
            contract_for(7)

    def test_features_are_clamped(self):
        features = FeatureVector(vision=5.0, audio=-2.0)
        assert features.vision == 1.0 and features.audio == 0.0

    def test_array_matches_contract_order(self):
        features = FeatureVector(vision=0.1, audio=0.2, motion=0.3,
                                 behaviour=0.4, identity=0.5, weapon=0.6,
                                 fire=0.7, is_night=1.0)
        array = features.to_array("v2")
        assert array.shape == (1, 8)
        assert list(array[0]) == pytest.approx([0.1, 0.2, 0.3, 0.4, 0.5, 0.6,
                                                0.7, 1.0])

    def test_weapon_reaches_the_score(self):
        """With the 5-feature booster, weapon/fire/behaviour previously could
        not influence TCI at all below the override thresholds."""
        without = steady(FeatureVector(vision=0.7, person_present=True)).tci
        with_knife = steady(FeatureVector(vision=0.7, weapon=0.45,
                                          person_present=True)).tci
        assert with_knife > without, (
            "a sub-threshold weapon changed nothing; weapon is not reaching "
            "the fusion score")


class TestPolicyHysteresis:
    def test_costly_levels_require_dwell(self):
        """L2/L3 trust already-confirmed evidence; L4/L5 (SMS, siren) wait."""
        policy = LevelPolicy()
        assert policy.update(0.95, now=0.0).level == 3, "L3 is immediate"
        assert policy.update(0.95, now=0.1).level == 3, "L5 needs 0.6 s"
        assert policy.update(0.95, now=1.0).level == 5

    def test_single_spike_cannot_reach_the_costly_levels(self):
        """Spikes are rejected upstream (k-of-n confirmation); the policy's own
        guard keeps one tick from sending an SMS or sounding the siren."""
        policy = LevelPolicy()
        now = 0.0
        for _ in range(20):
            now += 1 / 6
            policy.update(0.05, now=now)
        now += 1 / 6
        assert policy.update(0.95, now=now).level < 4          # one spike
        now += 1 / 6
        assert policy.update(0.05, now=now).level < 4

    def test_deescalation_is_slower_than_escalation(self):
        assert (settings.policy.deescalate_dwell_seconds >
                max(settings.policy.escalate_dwell_seconds))

    def test_deescalation_holds_the_alarm(self):
        policy = LevelPolicy()
        now = 0.0
        for _ in range(20):
            now += 1 / 6
            policy.update(0.95, now=now)
        assert policy.level == 5
        now += 1.0
        assert policy.update(0.05, now=now).level == 5, (
            "alarm cleared within 1 s; an intruder stepping out of frame would "
            "silence the system")

    def test_ramp_across_bands_commits(self):
        """0.635 -> 0.758 used to restart the dwell and stay at L2 (live
        incident 63be946a). The threshold stays crossed, so it must commit."""
        policy = LevelPolicy()
        states = [policy.update(t, now=n) for t, n in
                  ((0.30, 0.0), (0.635, 0.5), (0.758, 1.0), (0.70, 1.5), (0.72, 2.0))]
        assert [s.level for s in states] == [2, 3, 3, 3, 4]

    def test_flapping_below_the_level_de_escalates(self):
        """A TCI flapping between bands 1 and 2 held L3 for 20.9 s live."""
        policy = LevelPolicy()
        now = 0.0
        for _ in range(12):
            now += 0.5
            policy.update(0.5, now=now)
        assert policy.level == 3
        for i in range(24):
            now += 0.5
            state = policy.update(0.19 if i % 2 else 0.21, now=now)
        assert state.level == 2

    def test_one_frame_at_low_fps_cannot_commit_l4(self):
        """At 0.5 Hz a single observation 'lasts' 2 s; time alone is not enough
        for the levels that send an SMS or sound the siren."""
        policy = LevelPolicy()
        policy.update(0.05, now=0.0)
        assert policy.update(0.95, now=2.0).level == 3
        assert policy.update(0.05, now=4.0).level == 3

    def test_confirmed_evidence_commits_l3_at_the_first_confirmed_tick(self):
        """Persistence is counted once: L3 at 0.5 Hz took 4 s (review S-F4)."""
        policy = LevelPolicy()
        policy.update(0.10, now=0.0)                   # first crossing, unconfirmed
        assert policy.update(0.50, now=2.0).level == 3

    def test_deescalation_countdown_is_reported(self):
        policy = LevelPolicy()
        for i in range(10):
            policy.update(0.9, now=i * 0.2)
        state = policy.update(0.05, now=2.0)
        assert state.level == 5
        assert state.deescalate_in == pytest.approx(
            settings.policy.deescalate_dwell_seconds - 0.2, abs=1e-6)

    def test_bands_are_monotonic(self):
        thresholds = settings.policy.thresholds
        assert list(thresholds) == sorted(thresholds)
        assert level_for(0.0) == 1 and level_for(1.0) == 5
