"""Regression tests for per-class threat evidence (CLAUDE.md M1).

Each test pins a rule that the first live run proved necessary: every false
alarm there was one tick of the scene classifier a hair over its threshold, in
a room it had never seen, often with nobody in view.
"""

from __future__ import annotations

import dataclasses

import pytest

from sentrix.config import settings
from sentrix.fusion.evidence import (ConfirmationWindow, EvidenceAccumulator, Vote,
                                     combine, source_score, strongest)
from sentrix.fusion.features import FeatureVector
from sentrix.fusion.policy import LevelPolicy, level_for
from sentrix.fusion.tci import TCIEngine

L3, L4, L5 = settings.policy.thresholds[1], settings.policy.thresholds[2], \
    settings.policy.thresholds[3]
CFG = settings.threat


def scene(**probs):
    """A scene source at the configured floors: {class: (p, threshold)}."""
    return {"scene": {c: (p, CFG.scene_threshold_floor[c]) for c, p in probs.items()}}


def detector(weapon=0.0, fire=0.0):
    return {"detector": {"weapon": (weapon, settings.detection.weapon_conf),
                         "fire": (fire, settings.detection.fire_conf)}}


def run(ticks, person=True, acc=None, hz=2.0):
    """Feed a list of source dicts; return the evidence after the last tick."""
    acc = acc or EvidenceAccumulator()
    evidence = None
    for i, sources in enumerate(ticks):
        evidence = acc.update(i / hz, sources, person_recent=person)
    return evidence


class TestConfirmationWindow:
    def test_k_of_n(self):
        window = ConfirmationWindow(2, 3, max_gap_seconds=3.0)
        assert window.push(0.0, True)[2] is False
        assert window.push(0.5, False)[2] is False
        assert window.push(1.0, True)[2] is True
        assert window.push(1.5, False)[2] is False       # oldest hit fell out

    def test_long_gap_resets(self):
        window = ConfirmationWindow(2, 3, max_gap_seconds=3.0)
        window.push(0.0, True)
        hits, seen, confirmed = window.push(10.0, True)
        assert (hits, seen, confirmed) == (1, 1, False)


class TestSingleTickNeverAlarms:
    def test_one_crossing_is_unconfirmed(self):
        """The live 'weapon' alarm was p=0.351 against 0.35 on one tick."""
        ev = run([scene(weapon=0.2), scene(weapon=0.95), scene(weapon=0.2)])
        weapon = ev["weapon"]
        assert not weapon.confirmed
        assert weapon.score <= CFG.unconfirmed_ceiling < settings.policy.thresholds[0]

    def test_two_of_three_confirms_into_l3_only(self):
        ev = run([scene(weapon=0.9), scene(weapon=0.9)])
        weapon = ev["weapon"]
        assert weapon.confirmed
        assert L3 <= weapon.score <= CFG.single_source_cap < L4

    def test_scene_alone_never_reaches_l4(self):
        """D3: the least reliable, out-of-domain source is capped at L3."""
        ev = run([scene(weapon=1.0, fire=1.0)] * 20)
        assert ev["weapon"].score < L4 and ev["fire"].score < L4


class TestCorroboration:
    def test_scene_and_detector_reach_l4(self):
        ev = run([scene(weapon=0.6) | detector(weapon=0.6)] * 3)
        assert ev["weapon"].score >= L4
        assert len([v for v in ev["weapon"].votes if v.confirmed]) == 2

    def test_full_strength_reaches_l5(self):
        ev = run([scene(fire=1.0) | detector(fire=1.0)] * 3)
        assert ev["fire"].score >= L5

    def test_realistic_corroborated_fire_reaches_l5(self):
        """0.9 / 0.9 stopped at L4: noisy-OR of capped scores needed ~0.98 on
        both sides, so no class could ever sound the siren (review S-F3)."""
        ev = run([scene(fire=0.9) | detector(fire=0.9)] * 3)
        assert ev["fire"].score >= L5 and "corroborated" in ev["fire"].reason

    def test_corroboration_at_threshold_is_l4_not_l5(self):
        ev = run([scene(weapon=0.6) | detector(weapon=0.6)] * 3)
        assert L4 <= ev["weapon"].score < L5

    def test_weaker_side_sets_the_level(self):
        ev = run([scene(weapon=0.99) | detector(weapon=0.56)] * 3)
        assert L4 <= ev["weapon"].score < L5

    def test_two_classifiers_are_not_corroboration(self):
        """Scene and violence are both classifiers, not independent (D3)."""
        cfg = dataclasses.replace(CFG, experimental_classes=())
        acc = EvidenceAccumulator(cfg)
        ticks = [{"scene": {"assault": (0.95, 0.6)}, "violence": {"assault": (0.95, 0.5)}}] * 3
        ev = run(ticks, acc=acc)
        assert ev["assault"].confirmed and ev["assault"].score < L4

    def test_detector_alone_is_also_capped(self):
        ev = run([detector(weapon=0.99)] * 5)
        assert ev["weapon"].confirmed and ev["weapon"].score < L4


class TestPersonGate:
    def test_weapon_needs_a_person(self):
        """65 % of the live weapon alarms had nobody in view."""
        ev = run([scene(weapon=0.9)] * 5, person=False)
        weapon = ev["weapon"]
        assert not weapon.confirmed
        assert weapon.votes[0].gated == "no person in view"

    def test_fire_does_not_need_a_person(self):
        ev = run([scene(fire=0.9)] * 3, person=False)
        assert ev["fire"].confirmed


class TestIdentityNeverRelaxesEvidence:
    """Authorisation must never suppress a weapon (review F1/S-F5).

    Frame-level identity cannot tell a resident from a stranger standing next
    to them, so it has no input into the evidence layer; this drives the
    production path Pipeline.process wires: evidence -> TCI -> policy."""

    def test_sustained_scene_weapon_with_a_resident_in_view_commits_l3(self):
        acc, tci, policy = EvidenceAccumulator(), TCIEngine(), LevelPolicy()
        level = 1
        for i in range(8):
            now = i / 2
            ev = acc.update(now, scene(weapon=0.9), person_recent=True)
            features = FeatureVector(weapon=ev["weapon"].score, person_present=True,
                                     authorized=True, person_count=2,
                                     extras={"evidence": {c: e.to_dict()
                                                          for c, e in ev.items()}})
            result = tci.compute(features, now)
            level = policy.update(result.raw_tci, now).level
        assert level == 3 and result.incident_type == "weapon"

    def test_evidence_has_no_identity_input(self):
        import inspect
        assert "authorised" not in inspect.signature(EvidenceAccumulator.update).parameters


class TestPersonDetectorUnavailable:
    """No person model, or a failing pass, is 'unknown', not 'nobody' (F9)."""

    def test_gate_is_skipped_and_says_so(self):
        acc = EvidenceAccumulator()
        ev = None
        for i in range(3):
            ev = acc.update(i * 0.5, scene(weapon=0.9), person_recent=None)
        assert ev["weapon"].confirmed and ev["weapon"].score >= L3
        assert "person gate off" in ev["weapon"].reason

    def test_known_empty_room_still_gates(self):
        ev = run([scene(weapon=0.9)] * 3, person=False)
        assert not ev["weapon"].confirmed


class TestFreshObservationsOnly:
    """One model evaluation must not confirm itself (M1 review F2)."""

    @staticmethod
    def cached(**probs):
        return {"scene": {c: (p, CFG.scene_threshold_floor[c], False)
                          for c, p in probs.items()}}

    def test_cached_value_does_not_count_again(self):
        ev = run([scene(weapon=0.7), self.cached(weapon=0.7), self.cached(weapon=0.7),
                  scene(weapon=0.1)])
        assert not ev["weapon"].confirmed
        assert ev["weapon"].score <= CFG.unconfirmed_ceiling

    def test_confirmed_state_is_carried_between_evaluations(self):
        """A model on every other tick keeps its confirmed vote in between."""
        ev = run([scene(weapon=0.8), self.cached(weapon=0.8), scene(weapon=0.8),
                  self.cached(weapon=0.8)])
        weapon = ev["weapon"]
        assert weapon.confirmed and L3 <= weapon.score < L4
        assert weapon.votes[0].fresh is False and "(cached)" in weapon.reason

    def test_expired_cached_value_is_dropped(self):
        acc = EvidenceAccumulator()
        acc.update(0.0, scene(weapon=0.8), person_recent=True)
        ev = acc.update(10.0, self.cached(weapon=0.8), person_recent=True)
        assert ev["weapon"].votes == [] and ev["weapon"].score == 0.0

    def test_person_gate_applies_to_a_carried_vote(self):
        acc = EvidenceAccumulator()
        for i in range(3):
            acc.update(i * 0.5, scene(weapon=0.8), person_recent=True)
        ev = acc.update(1.5, self.cached(weapon=0.8), person_recent=False)
        assert not ev["weapon"].confirmed
        assert "no person in view" in ev["weapon"].reason


class TestInvalidInputs:
    """A bad value or threshold is visible and inert, never silent (F4-F6)."""

    def test_nan_detector_vote_does_not_erase_a_confirmed_fire(self):
        nan = float("nan")
        ev = run([scene(fire=0.9) | {"detector": {"fire": (nan, 0.5)}}] * 3)
        assert ev["fire"].confirmed and L3 <= ev["fire"].score < L4
        assert "invalid value" in ev["fire"].reason
        assert strongest(ev)[0] == "fire"

    @pytest.mark.parametrize("threshold", [0.0, 1.0, 55.0, float("nan")])
    def test_out_of_range_threshold_scores_nothing(self, threshold):
        ev = run([{"scene": {"weapon": (0.99, threshold)}}] * 4)
        assert ev["weapon"].score == 0.0
        assert "invalid threshold" in ev["weapon"].reason

    def test_k_above_n_is_clamped(self):
        window = ConfirmationWindow(3, 2, max_gap_seconds=3.0)
        window.push(0.0, True)
        assert window.push(0.5, True)[2] is True

    def test_non_positive_gap_does_not_clear_every_push(self):
        window = ConfirmationWindow(2, 3, max_gap_seconds=0.0)
        window.push(0.0, True)
        assert window.push(0.5, True)[2] is True


class TestExperimentalClasses:
    def test_assault_is_capped_in_l2(self):
        """Assault and robbery cannot be validated on this data (D1)."""
        ev = run([scene(assault=0.99, robbery=0.99)] * 5)
        for cls in ("assault", "robbery"):
            assert ev[cls].experimental and ev[cls].confirmed
            assert ev[cls].score <= CFG.experimental_cap < L3
            assert "experimental" in ev[cls].reason


class TestSourcesModeExperimental:
    def test_detector_mode_keeps_classifier_votes_for_experimental_classes(self):
        """The detector has no assault head; detector mode must not blind it."""
        cfg = dataclasses.replace(CFG, sources="detector")
        ev = run([{"scene": {"assault": (0.95, 0.6), "weapon": (0.95, 0.5)}}] * 3,
                 acc=EvidenceAccumulator(cfg))
        assert ev["assault"].confirmed and ev["assault"].score <= CFG.experimental_cap
        assert ev["weapon"].votes == []


class TestSourcesMode:
    def test_detector_mode_ignores_the_classifier(self):
        acc = EvidenceAccumulator(dataclasses.replace(CFG, sources="detector"))
        ev = run([scene(weapon=0.99) | detector(weapon=0.0)] * 3, acc=acc)
        assert [v.source for v in ev["weapon"].votes] == ["detector"]
        assert not ev["weapon"].confirmed


class TestScoring:
    def test_confirmed_score_is_monotonic_and_bounded(self):
        scores = [source_score(Vote("scene", p, 0.5, 2, 3, True)) for p in
                  (0.5, 0.6, 0.8, 1.0)]
        assert scores == sorted(scores)
        assert scores[0] == pytest.approx(CFG.confirmed_floor)
        assert scores[-1] == pytest.approx(CFG.single_source_cap)

    def test_noisy_or(self):
        assert combine([0.5, 0.5]) == pytest.approx(0.75)
        assert combine([]) == 0.0

    def test_strongest_breaks_ties_by_severity(self):
        assert strongest({"weapon": 0.5, "fire": 0.5})[0] == "fire"
        assert strongest({"weapon": 0.0})[0] is None


class TestEndToEnd:
    """Evidence -> TCI -> policy, as Pipeline.process wires them."""

    @staticmethod
    def drive(ticks, hz=2.0, person=True):
        acc, tci, policy = EvidenceAccumulator(), TCIEngine(), LevelPolicy()
        levels, types = [], []
        for i, sources in enumerate(ticks):
            now = i / hz
            ev = acc.update(now, sources, person_recent=person)
            # A recognised resident: these tests are about threat blips, not
            # the unrecognised-person L2 rule (D15; tests/test_multimodal.py).
            features = FeatureVector(weapon=ev["weapon"].score, fire=ev["fire"].score,
                                     person_present=person, authorized=True,
                                     extras={"evidence": {c: e.to_dict()
                                                          for c, e in ev.items()}})
            result = tci.compute(features, now)
            levels.append(policy.update(result.raw_tci, now).level)
            types.append(result.incident_type)
        return levels, types

    def test_sporadic_single_ticks_stay_quiet(self):
        """The live session: weapon p hovering near threshold, crossing on
        isolated ticks. It produced ~171 alarms/hour; now none."""
        pattern = [0.3, 0.55, 0.3, 0.3, 0.52, 0.28, 0.31, 0.3, 0.6, 0.3] * 6
        levels, types = self.drive([scene(weapon=p) for p in pattern])
        assert max(levels) == 1 and set(types) == {"normal"}

    def test_sustained_scene_weapon_is_l3_not_l4(self):
        levels, types = self.drive([scene(weapon=0.8)] * 20)
        assert max(levels) == 3 and "weapon" in types

    def test_corroborated_weapon_escalates_to_l4_quickly(self):
        levels, _ = self.drive([scene(weapon=0.8) | detector(weapon=0.8)] * 10)
        first_l4 = next(i for i, level in enumerate(levels) if level >= 4)
        assert first_l4 <= 4, "confirm (2 ticks) + dwell must stay within ~2 s at 2 Hz"

    def test_empty_room_weapon_blips_stay_quiet(self):
        levels, _ = self.drive([scene(weapon=0.9)] * 20, person=False)
        assert max(levels) == 1

    def test_level_bands_match_the_caps(self):
        assert level_for(CFG.confirmed_floor) == 3
        assert level_for(CFG.single_source_cap) == 3
        assert level_for(CFG.experimental_cap) == 2
        assert level_for(CFG.unconfirmed_ceiling) == 1
