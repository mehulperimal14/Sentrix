"""Regression tests for the TCI fusion layer.

Each test pins a defect found in the previous implementation. They are written
so a failure names the regression rather than just an assertion.
"""

from __future__ import annotations

import math

import pytest

from sentrix.config import settings
from sentrix.fusion.features import (CONTRACT_V1, CONTRACT_V2, FeatureVector,
                                     contract_for)
from sentrix.fusion.policy import LevelPolicy, level_for
from sentrix.fusion.tci import TCIEngine


def steady(features: FeatureVector, seconds: float = 12.0, hz: float = 6.0):
    """Run one feature vector to EMA convergence and return the final result."""
    engine = TCIEngine()
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

    def test_input_calibration_is_off_by_default(self):
        assert settings.fusion.calibrate_inputs is False


class TestDynamicRange:
    """TCI previously spanned only ~[0.40, 0.84]; presence changed it by 0.015."""

    def test_person_raises_score_materially(self):
        idle = steady(FeatureVector()).tci
        person = steady(FeatureVector(vision=0.85, motion=0.15, identity=0.6,
                                      person_present=True)).tci
        assert person - idle > 0.15, (
            f"person raised TCI by only {person - idle:.3f}; TCI is not "
            "responding to occupancy")

    def test_full_range_is_reachable(self):
        low = steady(FeatureVector()).tci
        high = steady(FeatureVector(fire=0.9)).tci
        assert low < 0.10 and high > 0.90


class TestHardOverrides:
    def test_fire_is_critical(self):
        result = steady(FeatureVector(fire=0.88))
        assert result.tci >= 0.85 and result.incident_type == "fire"

    def test_weapon_is_critical(self):
        result = steady(FeatureVector(vision=0.8, weapon=0.85,
                                      person_present=True))
        assert result.tci >= 0.85 and result.incident_type == "weapon"

    def test_authorised_resident_is_floored(self):
        result = steady(FeatureVector(vision=0.9, motion=0.3,
                                      person_present=True, authorized=True))
        assert result.tci <= settings.fusion.authorised_floor + 0.02

    def test_armed_resident_is_not_floored(self):
        """Authorisation must never suppress a weapon."""
        result = steady(FeatureVector(vision=0.9, weapon=0.85,
                                      person_present=True, authorized=True))
        assert result.tci >= 0.85, "an armed authorised user must still alarm"

    def test_degraded_identity_cannot_authorise(self):
        """If identity is failing closed, an 'authorised' flag must not suppress."""
        result = steady(FeatureVector(vision=0.9, motion=0.3, identity=0.6,
                                      person_present=True, authorized=True,
                                      identity_degraded=True))
        assert result.tci > settings.fusion.authorised_floor + 0.05


class TestSmoothing:
    """EMA was a fixed per-frame alpha, so behaviour changed with frame rate."""

    def test_smoothing_is_frame_rate_independent(self):
        features = FeatureVector(vision=0.8, motion=0.5, identity=0.6,
                                 person_present=True)
        slow = steady(features, seconds=15.0, hz=3.0).tci
        fast = steady(features, seconds=15.0, hz=30.0).tci
        assert abs(slow - fast) < 0.02, (
            f"3 Hz gave {slow:.3f} but 30 Hz gave {fast:.3f}; smoothing is "
            "still coupled to the frame rate")

    def test_overrides_update_the_smoother(self):
        """Overrides used to return early without touching previous_tci, so the
        EMA resumed from a stale pre-incident value once the override cleared."""
        engine = TCIEngine()
        for i in range(30):
            engine.compute(FeatureVector(fire=0.9), now=i * 0.2)
        after = engine.compute(FeatureVector(), now=6.2)
        assert after.tci > 0.3, (
            "TCI collapsed instantly after a critical override; the smoother "
            "was not carrying the override state")

    def test_tau_matches_configuration(self):
        tau = settings.fusion.ema_tau_seconds
        dt = 1.0 / 6.0
        assert 0.0 < 1.0 - math.exp(-dt / tau) < 1.0


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
    def test_escalation_requires_dwell(self):
        policy = LevelPolicy()
        assert policy.update(0.95, now=0.0).level == 1, "instant escalation"
        assert policy.update(0.95, now=0.1).level == 1
        assert policy.update(0.95, now=1.0).level == 5

    def test_single_spike_is_rejected(self):
        """One bad frame must not move the committed level."""
        policy = LevelPolicy()
        now = 0.0
        for _ in range(20):
            now += 1 / 6
            policy.update(0.05, now=now)
        now += 1 / 6
        policy.update(0.95, now=now)          # one spike
        now += 1 / 6
        assert policy.update(0.05, now=now).level == 1

    def test_deescalation_is_slower_than_escalation(self):
        assert (settings.policy.deescalate_dwell_seconds >
                settings.policy.escalate_dwell_seconds)

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

    def test_bands_are_monotonic(self):
        thresholds = settings.policy.thresholds
        assert list(thresholds) == sorted(thresholds)
        assert level_for(0.0) == 1 and level_for(1.0) == 5
