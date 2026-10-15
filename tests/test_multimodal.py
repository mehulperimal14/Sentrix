"""Multimodal, time-aligned fusion (decision D15).

Each test pins one rule the owner asked for:

* the TCI combines every input, and each one's contribution is recorded;
* audio counts only when its window describes the frame (stale = unavailable,
  never "calm");
* a fight needs video *and* aligned aggression audio to leave L2, and banter
  (laughter, calm talk) keeps a visual fight low;
* an unrecognised person reaches L2 only, in home and armed mode;
* a weapon box with an aligned gunshot is corroborated (L4).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from sentrix.config import settings
from sentrix.fusion.evidence import (AUDIO, VIDEO_CLASSIFIER, VIDEO_DETECTOR,
                                     ClipConfirmation, ClipObservation, EvidenceAccumulator)
from sentrix.fusion.features import FeatureVector
from sentrix.fusion.policy import LevelPolicy, level_for
from sentrix.fusion.tci import TCIEngine
from sentrix.perception.audio import (AudioEngine, AudioResult, FileAudio, alignment,
                                      audio_votes)
from sentrix.perception.sound_tagger import (CUE_LABELS, CUES, LABELS_NAME, WEIGHTS_NAME,
                                             SoundTagger, cues_from_scores, load_labels)
from sentrix.paths import model_path

CFG = settings.threat
L2, L3, L4, L5 = settings.policy.thresholds
CUE_BARS = settings.audio.cue_thresholds
NO_SCENE = SimpleNamespace(available=False, reset=lambda: None)


# ── helpers ───────────────────────────────────────────────────────────────────
def scene(**probs):
    return {"scene": {c: (p, CFG.scene_threshold_floor[c]) for c, p in probs.items()}}


def detector(weapon=0.0, fire=0.0):
    return {"detector": {"weapon": (weapon, settings.detection.weapon_conf),
                         "fire": (fire, settings.detection.fire_conf)}}


def heard(end: float, **cues) -> AudioResult:
    full = {c: 0.0 for c in CUES}
    full.update(cues)
    return AudioResult(0.0, "test", 1.0, cues=full, start=end - 4.0, end=end,
                       model="panns_cnn10")


def audio(end: float, fresh: bool = True, **cues) -> dict:
    return audio_votes(heard(end, **cues), fresh)


def drive(ticks, *, persons=2, motion=0.2, person=True, authorized=True, armed=False,
          hz=2.0):
    """Evidence -> TCI -> policy, as Pipeline.decide wires them. Returns
    (levels, last result, last evidence)."""
    acc, tci, policy = EvidenceAccumulator(), TCIEngine(), LevelPolicy()
    if armed:
        tci.threat = dataclasses.replace(settings.threat, armed=True)
    levels, result, ev = [], None, None
    for i, sources in enumerate(ticks):
        now = i / hz
        ev = acc.update(now, sources, person_recent=person,
                        context={"persons": persons, "motion": motion})
        features = FeatureVector(
            weapon=ev["weapon"].score, fire=ev["fire"].score, person_present=person,
            authorized=authorized, person_count=persons, motion=motion,
            extras={"evidence": {c: e.to_dict() for c, e in ev.items()},
                    "fusion_context": {"persons_recent": persons, "motion": motion},
                    "person_recent": person,
                    "audio": {"status": "aligned"} if any(
                        s.startswith("audio") for s in sources) else {"status": "disabled"}})
        result = tci.compute(features, now)
        levels.append(policy.update(result.raw_tci, now).level)
    return levels, result, ev


def fight_ticks(n=12, **cues):
    """Scene p(assault) over threshold plus (optionally) one fresh audio
    window per second."""
    ticks = []
    for i in range(n):
        sources = scene(assault=0.9)
        if cues and i % 2 == 0:
            sources |= audio(end=i / 2.0, **cues)
        elif cues:
            sources |= audio(end=(i - 1) / 2.0, fresh=False, **cues)
        ticks.append(sources)
    return ticks


# ── time alignment ────────────────────────────────────────────────────────────
class TestAlignment:
    def test_a_window_covering_the_frame_is_aligned(self):
        assert alignment(heard(10.0), 9.0)["status"] == "aligned"
        assert alignment(heard(10.0), 11.0)["status"] == "aligned"   # 1.0 s late

    def test_a_window_older_than_the_tolerance_is_stale(self):
        tolerance = settings.audio.align_tolerance_seconds
        record = alignment(heard(10.0), 10.0 + tolerance + 0.1)
        assert record["status"] == "stale" and record["lag_s"] > tolerance

    def test_a_window_from_the_future_is_not_this_frame(self):
        assert alignment(heard(20.0), 10.0)["status"] == "future"

    def test_untimed_or_missing_audio_is_not_aligned(self):
        assert alignment(AudioResult(), 1.0)["status"] == "untimed"
        assert alignment(None, 1.0)["status"] == "none"

    def test_engine_hands_out_a_stale_result_as_unavailable(self):
        engine = AudioEngine.__new__(AudioEngine)
        import threading
        engine._lock, engine._result, engine.available = threading.Lock(), heard(5.0), True
        result, record = engine.result_for(5.5)
        assert result is not None and record["status"] == "aligned"
        result, record = engine.result_for(9.0)
        assert result is None and record["status"] == "stale"

    def test_stale_calm_audio_does_not_lower_a_fight(self):
        """Banter heard 10 s ago says nothing about the fight now: the
        pipeline drops unaligned audio, so no audio_benign vote exists."""
        engine = AudioEngine.__new__(AudioEngine)
        import threading
        engine._lock, engine.available = threading.Lock(), True
        engine._result = heard(0.0, benign_social=0.95)
        pipeline = SimpleNamespace(audio=engine)
        from sentrix.pipeline.engine import Pipeline
        result, record = Pipeline.audio_for(pipeline, 10.0)
        assert result is None and record["status"] == "stale"

    def test_file_audio_follows_the_live_grid(self):
        rate = settings.windows.audio_sample_rate
        seen = []

        class Classifier:
            model_name = "fake"

            def classify_window(self, samples, rms=None, **stamp):
                seen.append((len(samples), stamp["end"]))
                return AudioResult(0.0, "x", 1.0, cues={}, start=stamp["start"],
                                   end=stamp["end"])

        track = FileAudio(np.zeros(rate * 10, np.float32), rate, Classifier())
        assert track.result_for(3.5)[0] is None              # no full 4 s window yet
        result, record = track.result_for(6.7)
        assert result.end == pytest.approx(6.0) and record["status"] == "aligned"
        track.result_for(6.9)                                # same window: cached
        assert seen == [(4 * rate, 6.0)]


# ── the sound model ───────────────────────────────────────────────────────────
class TestSoundModel:
    def test_cue_labels_exist_in_the_audioset_label_file(self):
        labels = set(load_labels(model_path("audio", LABELS_NAME)))
        for cue, names in CUE_LABELS.items():
            missing = [n for n in names if n not in labels]
            assert not missing, f"{cue}: {missing}"

    def test_calm_talk_is_benign_but_shouting_is_not(self):
        labels = load_labels(model_path("audio", LABELS_NAME))
        index = {n: i for i, n in enumerate(labels)}
        scores = np.zeros(len(labels), np.float32)
        scores[index["Speech"]] = 0.9
        assert cues_from_scores(scores, labels)["benign_social"] == pytest.approx(0.9)
        scores[index["Shout"]] = 0.8
        cues = cues_from_scores(scores, labels)
        assert cues["aggression"] == pytest.approx(0.8)
        assert cues["benign_social"] < 0.2
        assert cues_from_scores(scores, labels, calm_talk=False)["benign_social"] == 0.0

    def test_missing_weights_degrade_gracefully(self, tmp_path):
        tagger = SoundTagger(tmp_path / "missing.pth", tmp_path / "missing.csv")
        assert not tagger.available

    @pytest.mark.skipif(not model_path("audio", WEIGHTS_NAME).exists(),
                        reason="PANNs weights not present (tools/fetch_models.py)")
    def test_the_tagger_runs_on_a_window(self):
        tagger = SoundTagger(model_path("audio", WEIGHTS_NAME), model_path("audio", LABELS_NAME),
                             threads=1)
        assert tagger.available
        rng = np.random.default_rng(0)
        cues, top, score = tagger.tag(rng.normal(0, 0.05, 16000 * 4).astype(np.float32),
                                      16000)
        assert set(cues) == set(CUES) and all(0.0 <= v <= 1.0 for v in cues.values())
        assert isinstance(top, str) and 0.0 <= score <= 1.0

    def test_a_quiet_window_is_heard_as_nothing(self):
        engine = AudioEngine(capture=False)
        result = engine.classify_window(np.zeros(16000 * 4, np.float32), start=0.0, end=4.0)
        assert result.model == "quiet" and result.end == 4.0
        assert all(v == 0.0 for v in result.cues.values())

    def test_votes_map_cues_to_classes(self):
        votes = audio_votes(heard(1.0, weapon_sound=0.1, aggression=0.9))
        assert votes["audio"]["weapon"][:2] == (pytest.approx(0.1), CUE_BARS["weapon_sound"])
        assert votes["audio"]["assault"][0] == pytest.approx(0.9)
        assert votes["audio_aggression"]["weapon"][0] == pytest.approx(0.9)
        assert votes["audio_benign"]["assault"][0] == 0.0

    def test_a_scream_alone_is_not_a_weapon(self):
        levels, _, ev = drive([audio(end=i / 2.0, aggression=0.9) for i in range(10)],
                              persons=1)
        assert ev["weapon"].score < L2 and ev["weapon"].rule == "corroboration only"

    def test_a_scream_corroborates_a_visible_gun(self):
        ticks = [detector(weapon=0.8) | audio(end=i / 2.0, aggression=0.9) for i in range(10)]
        levels, _, ev = drive(ticks)
        assert ev["weapon"].state == "corroborated" and max(levels) >= 4


# ── fight vs banter ───────────────────────────────────────────────────────────
class TestFightVersusBanter:
    def test_a_visual_fight_alone_stays_at_l2(self):
        levels, result, ev = drive(fight_ticks())
        assert max(levels) == 2 and ev["assault"].rule == "experimental"

    def test_fight_plus_aligned_aggression_reaches_l3(self):
        levels, result, ev = drive(fight_ticks(aggression=0.8))
        assault = ev["assault"]
        assert max(levels) == 3 and assault.state == "corroborated"
        assert set(assault.modalities) == {VIDEO_CLASSIFIER, AUDIO}
        assert result.incident_type == "assault"

    def test_banter_keeps_a_visual_fight_low(self):
        levels, result, ev = drive(fight_ticks(benign_social=0.9))
        assert max(levels) == 1 and ev["assault"].rule == "banter"
        assert ev["assault"].score <= CFG.banter_cap < L2

    def test_aggression_beats_banter(self):
        levels, _, ev = drive(fight_ticks(aggression=0.8, benign_social=0.6))
        assert max(levels) == 3 and ev["assault"].rule == "fight corroborated"

    def test_one_person_is_not_a_fight(self):
        levels, _, ev = drive(fight_ticks(aggression=0.8), persons=1)
        assert max(levels) == 1
        assert any("fewer than" in (v.gated or "") for v in ev["assault"].votes)

    def test_a_still_room_does_not_leave_l2(self):
        levels, _, _ = drive(fight_ticks(aggression=0.8), motion=0.0)
        assert max(levels) == 2

    def test_no_audio_is_neutral(self):
        """Unavailable audio neither raises nor lowers: the experimental cap."""
        levels, _, ev = drive(fight_ticks())
        assert max(levels) == 2 and ev["assault"].rule == "experimental"

    def test_aggression_alone_is_at_most_l2(self):
        ticks = [audio(end=i / 2.0, aggression=0.9) for i in range(12)]
        levels, _, _ = drive(ticks)
        assert max(levels) <= 2

    def test_the_cap_is_configurable(self):
        cfg = dataclasses.replace(CFG, fight_max_level=2)
        acc = EvidenceAccumulator(cfg)
        ev = None
        for i, sources in enumerate(fight_ticks(aggression=0.8)):
            ev = acc.update(i / 2.0, sources, True, {"persons": 2, "motion": 0.2})
        assert ev["assault"].score <= CFG.experimental_cap

    def test_l4_needs_a_second_visual_source(self):
        cfg = dataclasses.replace(CFG, fight_max_level=4)
        acc = EvidenceAccumulator(cfg)
        ev = None
        for i in range(12):
            sources = scene(assault=0.9) | {"violence": {"assault": (0.9, 0.5)}}
            sources |= audio(end=i / 2.0, aggression=0.8)
            ev = acc.update(i / 2.0, sources, True, {"persons": 2, "motion": 0.2})
        assert ev["assault"].score >= L4


# ── weapon and fire with audio ────────────────────────────────────────────────
class TestWeaponAndFireWithAudio:
    def test_weapon_detector_plus_aligned_gunshot_is_l4(self):
        ticks = [detector(weapon=0.8) | audio(end=i / 2.0, weapon_sound=0.7)
                 for i in range(10)]
        levels, result, ev = drive(ticks)
        weapon = ev["weapon"]
        assert weapon.state == "corroborated" and set(weapon.modalities) == {VIDEO_DETECTOR, AUDIO}
        assert max(levels) >= 4 and result.incident_type == "weapon"

    def test_weapon_detector_alone_is_l3(self):
        levels, _, ev = drive([detector(weapon=0.8)] * 10)
        assert max(levels) == 3 and ev["weapon"].state == "confirmed"

    def test_three_modalities_reach_l5(self):
        ticks = [detector(weapon=0.6) | scene(weapon=0.6) | audio(end=i / 2.0, weapon_sound=0.5)
                 for i in range(10)]
        levels, _, ev = drive(ticks)
        assert len(ev["weapon"].modalities) == 3 and max(levels) == 5

    def test_a_gunshot_alone_is_at_most_l2(self):
        levels, _, ev = drive([audio(end=i / 2.0, weapon_sound=0.9) for i in range(10)])
        assert max(levels) <= 2 and ev["weapon"].rule == "audio only"

    def test_an_aligned_smoke_alarm_corroborates_fire(self):
        ticks = [detector(fire=0.7) | audio(end=i / 2.0, fire_sound=0.8) for i in range(10)]
        levels, _, ev = drive(ticks)
        assert ev["fire"].state == "corroborated" and max(levels) >= 4

    def test_one_audio_window_cannot_confirm_itself(self):
        """The same window repeated across ticks is not fresh: no confirmation."""
        ticks = [detector(weapon=0.8) | audio(end=0.0, fresh=(i == 0), weapon_sound=0.9)
                 for i in range(6)]
        _, _, ev = drive(ticks)
        audio_vote = next(v for v in ev["weapon"].votes if v.source == "audio")
        assert not audio_vote.confirmed and ev["weapon"].state == "confirmed"

    def test_a_weapon_sound_needs_a_person_too(self):
        ticks = [audio(end=i / 2.0, weapon_sound=0.9) for i in range(6)]
        _, _, ev = drive(ticks, person=False)
        assert not ev["weapon"].confirmed

    def test_audio_votes_can_be_switched_off(self):
        acc = EvidenceAccumulator(dataclasses.replace(CFG, audio_votes=False))
        ev = None
        for i in range(10):
            ev = acc.update(i / 2.0, detector(weapon=0.8) | audio(end=i / 2.0, weapon_sound=0.9),
                            True)
        assert ev["weapon"].state == "confirmed"
        assert not any(v.source == "audio" for v in ev["weapon"].votes)


# ── unknown people: L2 only ───────────────────────────────────────────────────
class TestUnknownPeopleAreL2Only:
    @pytest.mark.parametrize("armed", [False, True])
    def test_a_lingering_stranger_is_l2_in_both_modes(self, armed):
        levels, result, _ = drive([{}] * 60, persons=1, authorized=False, armed=armed)
        assert max(levels) == 2 and result.incident_type == "intrusion"
        assert "L2 only" in result.reason

    def test_intrusion_level_can_never_be_configured_into_l3(self):
        engine = TCIEngine()
        engine.threat = dataclasses.replace(settings.threat, armed=True, intrusion_level=0.6)
        result = None
        for i in range(20):
            result = engine.compute(FeatureVector(person_present=True), now=i / 2.0)
        assert level_for(result.raw_tci) == 2

    def test_a_stranger_with_a_gun_is_a_weapon_alarm(self):
        levels, result, _ = drive([detector(weapon=0.8)] * 30, persons=1, authorized=False,
                                  armed=True)
        assert max(levels) == 3 and result.incident_type == "weapon"

    def test_armed_sensor_activity_is_l2_context(self):
        engine = TCIEngine()
        engine.threat = dataclasses.replace(settings.threat, armed=True)
        result = engine.compute(FeatureVector(extras={"sensors": {"door": 2.0}}), now=1.0)
        assert result.incident_type == "intrusion" and level_for(result.raw_tci) == 2
        rows = {r["input"]: r for r in result.contributions}
        assert rows["sensors"]["effect"].startswith("raises")

    def test_sensors_at_home_are_context_only(self):
        result = TCIEngine().compute(FeatureVector(extras={"sensors": {"pir": 1.0}}), now=1.0)
        assert result.incident_type == "normal"


# ── contributions ─────────────────────────────────────────────────────────────
class TestContributions:
    def test_every_input_is_listed_for_the_leading_class(self):
        ticks = [detector(weapon=0.8) | scene(weapon=0.3) | audio(end=i / 2.0, weapon_sound=0.7)
                 for i in range(10)]
        _, result, _ = drive(ticks)
        inputs = [r["input"] for r in result.contributions]
        for name in ("video detector", "scene model", "audio", "people", "motion",
                     "identity", "sensors"):
            assert name in inputs, name
        rows = {r["input"]: r for r in result.contributions}
        assert rows["video detector"]["effect"] == "raises"
        assert rows["audio"]["effect"] == "raises"
        assert rows["scene model"]["effect"] == "below confirmation"
        assert rows["people"]["effect"] == "gate ok"

    def test_banter_is_shown_as_lowering(self):
        _, result, _ = drive(fight_ticks(benign_social=0.9))
        assert result.contributions == [] or result.incident_type == "normal"
        from sentrix.fusion.tci import contributions
        features = FeatureVector(extras={"evidence": {"assault": drive(
            fight_ticks(benign_social=0.9))[2]["assault"].to_dict()}})
        rows = contributions(features, "assault")
        assert any(r["input"] == "audio (banter)" and r["effect"] == "lowers" for r in rows)

    def test_unavailable_audio_says_why(self):
        from sentrix.fusion.tci import contributions
        features = FeatureVector(extras={"audio": {"status": "stale", "lag_s": 3.2}})
        rows = {r["input"]: r for r in contributions(features, "weapon")}
        assert rows["audio"]["effect"] == "unavailable" and "stale" in rows["audio"]["detail"]


# ── clip mode ─────────────────────────────────────────────────────────────────
class TestClipModeAudio:
    def test_clip_audio_confirms_on_its_one_window(self):
        observations = [ClipObservation("scene", "assault", 0.9, 0.6, t, None, True)
                        for t in (0.5, 1.5, 2.5)]
        observations += [ClipObservation(s, c, v, th, 4.0, 4.0, True)
                         for s, per in audio_votes(heard(4.0, aggression=0.8)).items()
                         for c, (v, th, _) in per.items()]
        ev = ClipConfirmation(2).update(observations, {"persons": 2, "motion": 0.2})
        assert ev["assault"].state == "corroborated" and level_for(ev["assault"].score) == 3

    def test_clip_banter_holds_the_fight(self):
        observations = [ClipObservation("scene", "assault", 0.9, 0.6, t, None, True)
                        for t in (0.5, 1.5, 2.5)]
        observations += [ClipObservation(s, c, v, th, 4.0, 4.0, True)
                         for s, per in audio_votes(heard(4.0, benign_social=0.9)).items()
                         for c, (v, th, _) in per.items()]
        ev = ClipConfirmation(2).update(observations, {"persons": 2, "motion": 0.2})
        assert ev["assault"].rule == "banter" and level_for(ev["assault"].score) == 1


# ── the pipeline wiring ───────────────────────────────────────────────────────
class TestPipelineWiring:
    def test_observe_records_audio_alignment_and_votes(self, tmp_path):
        from test_clip_engine import FakeDetector, armed_weapon, make_pipeline

        class Mic:
            available = True
            model_name = "fake"

            def result_for(self, at, tolerance=None):
                result = heard(at, weapon_sound=0.8)
                return result, alignment(result, at)

        pipeline = make_pipeline(tmp_path, detector=FakeDetector(armed_weapon(0.0)),
                                 scene=NO_SCENE, audio=Mic())
        frame = np.zeros((120, 160, 3), np.uint8)
        features = None
        for i in range(6):
            features = pipeline.observe(frame, i / 2.0)
        extras = features.extras
        assert extras["audio"]["status"] == "aligned"
        assert "audio" in extras["sources"] and "audio_benign" in extras["sources"]
        weapon = extras["evidence"]["weapon"]
        assert weapon["state"] == "corroborated"
        assert set(weapon["modalities"]) == {VIDEO_DETECTOR, AUDIO}
        out = pipeline.decide(frame, features, 3.0)
        assert out["level"] >= 3
        import json
        pipeline.events.close()
        row = json.loads(pipeline.events.path.read_text().splitlines()[-1])
        assert row["audio"]["status"] == "aligned" and "audio" in row["votes"]
        assert row["fusion"]["class"] == "weapon" and row["fusion"]["contributions"]

    def test_stale_audio_is_unavailable_in_the_features(self, tmp_path):
        from test_clip_engine import make_pipeline

        class Mic:
            available = True
            model_name = "fake"

            def result_for(self, at, tolerance=None):
                result = heard(at - 30.0, benign_social=0.9)
                record = alignment(result, at)
                return (result if record["status"] == "aligned" else None), record

        pipeline = make_pipeline(tmp_path, scene=NO_SCENE, audio=Mic())
        features = pipeline.observe(np.zeros((120, 160, 3), np.uint8), 40.0)
        assert features.audio_label == "audio unavailable"
        assert features.extras["audio"]["status"] == "stale"
        assert "audio" not in features.extras["sources"]

    def test_a_pir_event_reaches_the_features(self, tmp_path):
        from test_clip_engine import make_pipeline
        pipeline = make_pipeline(tmp_path, scene=NO_SCENE)
        pipeline.note_sensor("pir", 9.0)
        features = pipeline.observe(np.zeros((120, 160, 3), np.uint8), 10.0)
        assert features.extras["sensors"] == {"pir": 1.0}
