"""Multimodal, time-aligned fusion (decision D15) and the review fixes to it.

Each test pins one rule:

* the TCI combines every input, and each one's contribution is recorded;
* audio counts only when its window describes the frame (stale, unclassified
  or the device's own sound = unavailable, never "calm");
* an audio window is a new observation only if it does not overlap the last
  one counted, so one impulse cannot confirm itself (review F4);
* a fight needs video *and* aligned aggression audio to leave L2; banter
  (laughter, very calm talk) keeps it at L2 - never below; TV/music make the
  audio neutral (review F3); ``fight_max_level`` is a hard cap and L4 needs
  two visual modalities (review F5);
* an unrecognised person reaches L2 only, in home and armed mode;
* a weapon box with an aligned gunshot is corroborated (L4); three
  modalities reach L5 only with audio well over its bar (review F4);
* the sound model never changes torch's process-wide threads (review F2).
"""

from __future__ import annotations

import dataclasses
import math
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from sentrix.config import settings
from sentrix.fusion.evidence import (AUDIO, VIDEO_CLASSIFIER, VIDEO_DETECTOR,
                                     ClipConfirmation, ClipObservation, EvidenceAccumulator)
from sentrix.fusion.features import FeatureVector
from sentrix.fusion.policy import LevelPolicy, level_for
from sentrix.fusion.tci import TCIEngine, contributions
from sentrix.io import self_output
from sentrix.paths import model_path
from sentrix.perception.audio import (AudioEngine, AudioResult, FileAudio, alignment,
                                      audio_votes, block_end_monotonic, neutral_reason)
from sentrix.perception.sound_tagger import (CUE_LABELS, CUES, LABELS_NAME, WEIGHTS_NAME,
                                             SoundTagger, cues_from_scores, load_labels)
from sentrix.pipeline.engine import Pipeline

CFG = settings.threat
L2, L3, L4, L5 = settings.policy.thresholds
CUE_BARS = settings.audio.cue_thresholds
NO_SCENE = SimpleNamespace(available=False, reset=lambda: None)


@pytest.fixture(autouse=True)
def _no_self_output():
    self_output.clear()
    yield
    self_output.clear()


# ── helpers ───────────────────────────────────────────────────────────────────
def scene(**probs):
    return {"scene": {c: (p, CFG.scene_threshold_floor[c]) for c, p in probs.items()}}


def detector(weapon=0.0, fire=0.0, **extra):
    votes = {"weapon": (weapon, settings.detection.weapon_conf),
             "fire": (fire, settings.detection.fire_conf)}
    votes.update({c: (v, 0.5) for c, v in extra.items()})
    return {"detector": votes}


def heard(end: float, **cues) -> AudioResult:
    full = {c: 0.0 for c in CUES}
    full.update(cues)
    return AudioResult(0.0, "test", 1.0, cues=full, start=end - 4.0, end=end,
                       model="panns_cnn10")


def audio(end: float, fresh: bool = True, **cues) -> dict:
    return audio_votes(heard(end, **cues), fresh)


class _Counter:
    """The pipeline's own rule for which audio windows are new observations."""

    def __init__(self) -> None:
        self._audio_last_end = None
        self._audio_counted_end = None

    def is_new(self, result: AudioResult) -> bool:
        return Pipeline._audio_is_new(self, result)


def audio_stream(n: int, hz: float = 2.0, cues_at=None, **cues) -> list[dict]:
    """Per-tick audio votes as the live loop sees them: a 4 s window ending on
    every whole second, counted only when it does not overlap the last counted
    one. ``cues_at(window_end) -> cues`` overrides the constant ``cues``."""
    counter, out = _Counter(), []
    for i in range(n):
        end = float(math.floor(i / hz))
        values = cues_at(end) if cues_at is not None else cues
        result = heard(end, **values)
        out.append(audio_votes(result, counter.is_new(result)))
    return out


def merge(visual: list[dict], sounds: list[dict]) -> list[dict]:
    return [v | a for v, a in zip(visual, sounds)]


def drive(ticks, *, persons=2, motion=0.2, person=True, authorized=True, armed=False,
          hz=2.0, cfg=None):
    """Evidence -> TCI -> policy, as Pipeline.decide wires them. Returns
    (levels, last result, last evidence)."""
    acc, tci, policy = EvidenceAccumulator(cfg), TCIEngine(), LevelPolicy()
    if armed:
        tci.threat = dataclasses.replace(settings.threat, armed=True)
    levels, result, ev = [], None, None
    for i, sources in enumerate(ticks):
        now = i / hz
        ev = acc.update(now, sources, person_recent=person,
                        context={"persons": persons, "motion": motion})
        features = FeatureVector(
            weapon=ev["weapon"].score, fire=ev["fire"].score, person_present=person,
            authorized=authorized, person_count=persons or 0, motion=motion,
            extras={"evidence": {c: e.to_dict() for c, e in ev.items()},
                    "fusion_context": {"persons_recent": persons, "motion": motion},
                    "person_recent": person,
                    "audio": {"status": "aligned"} if any(
                        s.startswith("audio") for s in sources) else {"status": "disabled"}})
        result = tci.compute(features, now)
        levels.append(policy.update(result.raw_tci, now).level)
    return levels, result, ev


N = 24      # 12 s at 2 Hz: three non-overlapping audio windows


def fight_ticks(n=N, **cues):
    """Scene p(assault) over threshold plus (optionally) the live audio stream."""
    visual = [scene(assault=0.9) for _ in range(n)]
    return merge(visual, audio_stream(n, **cues)) if cues else visual


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

    def test_untimed_unclassified_or_missing_audio_is_not_aligned(self):
        assert alignment(AudioResult(model="panns_cnn10"), 1.0)["status"] == "untimed"
        assert alignment(None, 1.0)["status"] == "none"
        # Review F7: no model / failed inference is never "heard, nothing there".
        assert alignment(AudioResult(start=0.0, end=1.0), 1.0)["status"] == "unclassified"
        assert alignment(AudioResult(start=0.0, end=1.0, model="error"),
                         1.0)["status"] == "unclassified"

    def test_an_engine_with_no_model_hands_out_nothing(self):
        """Review F7: a loud window with no model loaded used to come back as
        an aligned, empty result."""
        engine = AudioEngine(capture=False)
        engine.tagger, engine._model = None, None
        loud = np.random.default_rng(0).normal(0, 0.2, 16000 * 4).astype(np.float32)
        engine._publish(engine.classify_window(loud, start=0.0, end=4.0))
        result, record = engine.result_for(4.0)
        assert result is None and record["status"] == "unclassified"

    def test_engine_hands_out_a_stale_result_as_unavailable(self):
        engine = AudioEngine.__new__(AudioEngine)
        engine._lock, engine._result, engine.available = threading.Lock(), heard(5.0), True
        result, record = engine.result_for(5.5)
        assert result is not None and record["status"] == "aligned"
        result, record = engine.result_for(9.0)
        assert result is None and record["status"] == "stale"

    def test_stale_calm_audio_does_not_touch_a_fight(self):
        engine = AudioEngine.__new__(AudioEngine)
        engine._lock, engine.available = threading.Lock(), True
        engine._result = heard(0.0, benign_social=0.95)
        result, record = Pipeline.audio_for(SimpleNamespace(audio=engine), 10.0)
        assert result is None and record["status"] == "stale"

    def test_file_audio_follows_the_live_grid(self):
        rate = settings.windows.audio_sample_rate
        seen = []

        class Classifier:
            model_name = "fake"

            def classify_window(self, samples, rms=None, **stamp):
                seen.append((len(samples), stamp["end"]))
                return AudioResult(0.0, "x", 1.0, cues={}, start=stamp["start"],
                                   end=stamp["end"], model="fake")

        track = FileAudio(np.zeros(rate * 10, np.float32), rate, Classifier())
        assert track.result_for(3.5)[0] is None              # no full 4 s window yet
        result, record = track.result_for(6.7)
        assert result.end == pytest.approx(6.0) and record["status"] == "aligned"
        track.result_for(6.9)                                # same window: cached
        assert seen == [(4 * rate, 6.0)]


class TestOverlappingWindows:
    """Review F4: 4 s windows every 1 s overlap, so one impulse sits in
    four consecutive windows."""

    def test_only_non_overlapping_windows_are_new_observations(self):
        counter = _Counter()
        new = [counter.is_new(heard(float(end))) for end in range(1, 14)]
        assert [end for end, is_new in zip(range(1, 14), new) if is_new] == [1, 5, 9, 13]
        assert counter.is_new(heard(13.0)) is False          # the same window again

    def test_one_impulse_cannot_confirm_audio(self):
        """A replica-gun click at t = 4.5 s is in the windows ending 5-8 s; the
        old "every new window is fresh" rule confirmed it 2-of-3 on its own."""
        def click(end):
            return {"weapon_sound": 0.9 if end - 4.0 <= 4.5 <= end else 0.0}

        ticks = merge([detector(weapon=0.8)] * 30, audio_stream(30, cues_at=click))
        levels, _, ev = drive(ticks)
        sound = next(v for v in ev["weapon"].votes if v.source == "audio")
        assert not sound.confirmed and ev["weapon"].state == "confirmed"
        assert max(levels) == 3

    def test_a_sustained_sound_still_confirms(self):
        ticks = merge([detector(weapon=0.8)] * N, audio_stream(N, weapon_sound=0.7))
        levels, _, ev = drive(ticks)
        assert ev["weapon"].state == "corroborated" and max(levels) >= 4


# ── the sound model ───────────────────────────────────────────────────────────
class TestSoundModel:
    def test_cue_labels_exist_in_the_audioset_label_file(self):
        labels = set(load_labels(model_path("audio", LABELS_NAME)))
        for cue, names in CUE_LABELS.items():
            missing = [n for n in names if n not in labels]
            assert not missing, f"{cue}: {missing}"

    def test_cues_separate_laughter_calm_talk_shouting_and_tv(self):
        labels = load_labels(model_path("audio", LABELS_NAME))
        index = {n: i for i, n in enumerate(labels)}
        scores = np.zeros(len(labels), np.float32)
        scores[index["Speech"]] = 0.9
        cues = cues_from_scores(scores, labels)
        assert cues["calm_talk"] == pytest.approx(0.9) and cues["benign_social"] == 0.0
        scores[index["Shout"]] = 0.8
        cues = cues_from_scores(scores, labels)
        assert cues["aggression"] == pytest.approx(0.8) and cues["calm_talk"] < 0.2
        scores[index["Television"]] = 0.7
        assert cues_from_scores(scores, labels)["background"] == pytest.approx(0.7)
        scores[index["Giggle"]] = 0.6
        assert cues_from_scores(scores, labels)["benign_social"] == pytest.approx(0.6)
        assert cues_from_scores(scores, labels, calm_talk=False)["calm_talk"] == 0.0

    def test_missing_weights_degrade_gracefully(self, tmp_path):
        tagger = SoundTagger(tmp_path / "missing.pth", tmp_path / "missing.csv")
        assert not tagger.available

    @pytest.mark.skipif(not model_path("audio", WEIGHTS_NAME).exists(),
                        reason="PANNs weights not present (tools/fetch_models.py)")
    @pytest.mark.parametrize("backend", ["onnx", "torch"])
    def test_the_tagger_never_changes_torch_threads(self, backend, tmp_path):
        """Review F2: torch.set_num_threads(2) in the tagger halved DINOv2."""
        import torch
        before = torch.get_num_threads()
        onnx = None if backend == "onnx" else tmp_path / "absent.onnx"
        tagger = SoundTagger(model_path("audio", WEIGHTS_NAME),
                             model_path("audio", LABELS_NAME), threads=1, onnx=onnx)
        assert tagger.available
        if backend == "onnx" and tagger.backend != "onnx":
            pytest.skip("onnxruntime or the exported model is not present")
        assert tagger.backend == backend
        rng = np.random.default_rng(0)
        cues, top, score = tagger.tag(rng.normal(0, 0.05, 16000 * 4).astype(np.float32),
                                      16000)
        assert set(cues) == set(CUES) and all(0.0 <= v <= 1.0 for v in cues.values())
        assert isinstance(top, str) and 0.0 <= score <= 1.0
        assert torch.get_num_threads() == before

    def test_building_the_audio_engine_keeps_torch_threads(self):
        import torch
        before = torch.get_num_threads()
        AudioEngine(capture=False)
        assert torch.get_num_threads() == before

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

    def test_banter_vote_is_laughter_or_very_calm_talk(self):
        laugh = audio_votes(heard(1.0, benign_social=0.5))["audio_benign"]["assault"]
        assert laugh[:2] == (pytest.approx(0.5), CUE_BARS["benign_social"])
        talk = audio_votes(heard(1.0, calm_talk=0.9))["audio_benign"]["assault"]
        assert talk[:2] == (pytest.approx(0.9), CUE_BARS["calm_talk"])
        assert CUE_BARS["calm_talk"] > CUE_BARS["benign_social"]

    def test_tv_or_music_makes_the_window_neutral(self):
        """Review F3: a film's screams or gunfire say nothing about the room."""
        loud = heard(1.0, background=0.8, aggression=0.9, weapon_sound=0.9)
        assert neutral_reason(loud) is not None and audio_votes(loud) == {}

    def test_a_scream_alone_is_not_a_weapon(self):
        _, _, ev = drive(audio_stream(N, aggression=0.9), persons=1)
        assert ev["weapon"].score < L2 and ev["weapon"].rule == "corroboration only"

    def test_a_scream_corroborates_a_visible_gun(self):
        levels, _, ev = drive(merge([detector(weapon=0.8)] * N, audio_stream(N, aggression=0.9)))
        assert ev["weapon"].state == "corroborated" and max(levels) >= 4


class TestSelfOutput:
    """Review F3: the device's own speaker, siren or buzzer is not the room."""

    def test_a_window_over_the_speaker_does_not_vote(self):
        engine = AudioEngine(capture=False)
        self_output.mark("speaker", 2.0, at=10.0)
        loud = np.random.default_rng(0).normal(0, 0.2, 16000 * 4).astype(np.float32)
        result = engine.classify_window(loud, start=8.0, end=12.0)
        assert result.model == "self"
        assert alignment(result, 12.0)["status"] == "self_output"
        later = engine.classify_window(np.zeros(16000 * 4, np.float32), start=20.0, end=24.0)
        assert later.model == "quiet"

    def test_the_speaker_marks_its_playback(self):
        from sentrix.io.speaker import Player, Speaker
        heard_during = []

        class Probe(Player):
            name, real = "probe", True

            def play(self, text):
                import time
                now = time.monotonic()
                heard_during.append(self_output.overlapping(now - 0.1, now))

        speaker = Speaker(dataclasses.replace(settings.speaker, enabled=True), player=Probe(),
                          audit=lambda *a, **k: None, background=False)
        assert speaker.say("hello").ok
        assert heard_during == ["speaker"]

    def test_a_siren_marks_its_sound(self):
        from sentrix.io.actuators import SoundSiren
        siren = SoundSiren.__new__(SoundSiren)
        siren._play = lambda duration: None
        siren.activate(1.0)
        import time
        now = time.monotonic()
        assert self_output.overlapping(now, now + 0.5) == "siren"


# ── fight vs banter ───────────────────────────────────────────────────────────
class TestFightVersusBanter:
    def test_a_visual_fight_alone_stays_at_l2(self):
        levels, _, ev = drive(fight_ticks())
        assert max(levels) == 2 and ev["assault"].rule == "experimental"

    def test_fight_plus_aligned_aggression_reaches_l3(self):
        levels, result, ev = drive(fight_ticks(aggression=0.8))
        assault = ev["assault"]
        assert max(levels) == 3 and assault.state == "corroborated"
        assert set(assault.modalities) == {VIDEO_CLASSIFIER, AUDIO}
        assert result.incident_type == "assault"

    def test_banter_keeps_a_visual_fight_at_l2_never_below(self):
        """Review F3: banter used to hide a fight at L1; it now holds it at L2."""
        levels, _, ev = drive(fight_ticks(benign_social=0.9))
        assert max(levels) == 2 and ev["assault"].rule == "banter"
        assert L2 <= ev["assault"].score <= CFG.experimental_cap

    def test_very_calm_talk_is_banter_too(self):
        _, _, ev = drive(fight_ticks(calm_talk=0.95))
        assert ev["assault"].rule == "banter"

    def test_ordinary_talk_is_not_banter(self):
        _, _, ev = drive(fight_ticks(calm_talk=0.6))
        assert ev["assault"].rule == "experimental"

    def test_mixed_aggression_and_laughter_holds_at_l2(self):
        levels, _, ev = drive(fight_ticks(aggression=0.8, benign_social=0.6))
        assert max(levels) == 2 and ev["assault"].rule == "mixed audio"

    def test_a_fight_with_the_tv_on_is_not_hidden(self):
        """Review F3: TV/music made it banter (L1); now the audio is neutral
        and the visual fight stays visible at L2."""
        levels, _, ev = drive(fight_ticks(background=0.8, aggression=0.9))
        assert max(levels) == 2 and ev["assault"].rule == "experimental"
        assert not any(v.source.startswith("audio") for v in ev["assault"].votes)

    def test_one_person_is_not_a_fight(self):
        levels, _, ev = drive(fight_ticks(aggression=0.8), persons=1)
        assert max(levels) == 1
        assert any("fewer than" in (v.gated or "") for v in ev["assault"].votes)

    def test_a_still_room_does_not_leave_l2(self):
        levels, _, _ = drive(fight_ticks(aggression=0.8), motion=0.0)
        assert max(levels) == 2

    def test_no_audio_is_neutral(self):
        levels, _, ev = drive(fight_ticks())
        assert max(levels) == 2 and ev["assault"].rule == "experimental"

    def test_aggression_alone_is_at_most_l2(self):
        levels, _, _ = drive(audio_stream(N, aggression=0.9))
        assert max(levels) <= 2

    def test_the_cap_is_configurable(self):
        cfg = dataclasses.replace(CFG, fight_max_level=2)
        levels, _, ev = drive(fight_ticks(aggression=0.8), cfg=cfg)
        assert max(levels) == 2 and ev["assault"].score < L3

    def test_scene_and_violence_are_one_modality_even_at_max_level_4(self):
        """Review F5: two classifier *sources* used to count as two visual
        sources for the fight's L4."""
        cfg = dataclasses.replace(CFG, fight_max_level=4)
        visual = [scene(assault=0.9) | {"violence": {"assault": (0.9, 0.5)}}] * N
        levels, _, ev = drive(merge(visual, audio_stream(N, aggression=0.8)), cfg=cfg)
        assert max(levels) == 3 and ev["assault"].score < L4

    def test_l4_needs_two_visual_modalities_and_never_passes_the_cap(self):
        cfg = dataclasses.replace(CFG, fight_max_level=4)
        visual = [scene(assault=0.99) | detector(assault=0.99)] * N
        levels, _, ev = drive(merge(visual, audio_stream(N, aggression=0.99)), cfg=cfg)
        assert max(levels) == 4 and L4 <= ev["assault"].score < L5


# ── weapon and fire with audio ────────────────────────────────────────────────
class TestWeaponAndFireWithAudio:
    def test_weapon_detector_plus_aligned_gunshot_is_l4(self):
        ticks = merge([detector(weapon=0.8)] * N, audio_stream(N, weapon_sound=0.7))
        levels, result, ev = drive(ticks)
        weapon = ev["weapon"]
        assert weapon.state == "corroborated" and set(weapon.modalities) == {VIDEO_DETECTOR, AUDIO}
        assert max(levels) >= 4 and result.incident_type == "weapon"

    def test_weapon_detector_alone_is_l3(self):
        levels, _, ev = drive([detector(weapon=0.8)] * N)
        assert max(levels) == 3 and ev["weapon"].state == "confirmed"

    def test_three_modalities_reach_l5_with_clear_audio(self):
        ticks = merge([detector(weapon=0.6) | scene(weapon=0.6)] * N,
                      audio_stream(N, weapon_sound=0.7))
        levels, _, ev = drive(ticks)
        assert len(ev["weapon"].modalities) == 3 and max(levels) == 5

    def test_three_modalities_with_faint_audio_stay_l4(self):
        """Review F4: a sound barely over its bar must not lift L4 to L5."""
        ticks = merge([detector(weapon=0.6) | scene(weapon=0.6)] * N,
                      audio_stream(N, weapon_sound=0.35))
        levels, _, ev = drive(ticks)
        assert len(ev["weapon"].modalities) == 3 and max(levels) == 4

    def test_a_gunshot_alone_is_at_most_l2(self):
        levels, _, ev = drive(audio_stream(N, weapon_sound=0.9))
        assert max(levels) <= 2 and ev["weapon"].rule == "audio only"

    def test_an_aligned_smoke_alarm_corroborates_fire(self):
        levels, _, ev = drive(merge([detector(fire=0.7)] * N, audio_stream(N, fire_sound=0.8)))
        assert ev["fire"].state == "corroborated" and max(levels) >= 4

    def test_one_audio_window_cannot_confirm_itself(self):
        ticks = [detector(weapon=0.8) | audio(end=0.0, fresh=(i == 0), weapon_sound=0.9)
                 for i in range(6)]
        _, _, ev = drive(ticks)
        audio_vote = next(v for v in ev["weapon"].votes if v.source == "audio")
        assert not audio_vote.confirmed and ev["weapon"].state == "confirmed"

    def test_a_weapon_sound_needs_a_person_too(self):
        _, _, ev = drive(audio_stream(N, weapon_sound=0.9), person=False)
        assert not ev["weapon"].confirmed

    def test_audio_votes_can_be_switched_off(self):
        cfg = dataclasses.replace(CFG, audio_votes=False)
        _, _, ev = drive(merge([detector(weapon=0.8)] * N, audio_stream(N, weapon_sound=0.9)),
                         cfg=cfg)
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
        ticks = merge([detector(weapon=0.8) | scene(weapon=0.3)] * N,
                      audio_stream(N, weapon_sound=0.7))
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

    def test_banter_is_shown_as_holding_at_l2(self):
        ev = drive(fight_ticks(benign_social=0.9))[2]
        rows = contributions(FeatureVector(extras={"evidence": {"assault": ev["assault"].to_dict()}}),
                             "assault")
        assert any(r["input"] == "audio (banter)" and r["effect"] == "holds at L2" for r in rows)

    def test_unavailable_audio_says_why(self):
        features = FeatureVector(extras={"audio": {"status": "stale", "lag_s": 3.2}})
        rows = {r["input"]: r for r in contributions(features, "weapon")}
        assert rows["audio"]["effect"] == "unavailable" and "stale" in rows["audio"]["detail"]

    def test_an_unknown_people_count_is_not_shown_as_a_passed_gate(self):
        """Review F12: the fight gate did not apply (person detector down), so
        the dashboard must not say "gate ok" from this tick's count."""
        features = FeatureVector(person_count=3,
                                 extras={"fusion_context": {"persons_recent": None,
                                                            "motion": 0.3}})
        rows = {r["input"]: r for r in contributions(features, "assault")}
        assert "unknown" in rows["people"]["detail"]
        assert not rows["people"]["effect"].startswith("gate ok")


# ── clip mode ─────────────────────────────────────────────────────────────────
def clip_audio(half_cues: list[dict]) -> list[ClipObservation]:
    out = []
    for i, cues in enumerate(half_cues):
        end = 2.0 * (i + 1)
        result = AudioResult(0.0, "t", 1.0, cues={c: 0.0 for c in CUES} | cues,
                             start=end - 2.0, end=end, model="panns_cnn10")
        out += [ClipObservation(s, c, v, th, end, 2.0, True)
                for s, per in audio_votes(result).items() for c, (v, th, _) in per.items()]
    return out


FIGHT_FRAMES = [ClipObservation("scene", "assault", 0.9, 0.6, t, None, True)
                for t in (0.5, 1.5, 2.5, 3.5)]


class TestClipModeAudio:
    def test_both_halves_of_aggression_corroborate_a_fight(self):
        ev = ClipConfirmation(2).update(
            FIGHT_FRAMES + clip_audio([{"aggression": 0.8}, {"aggression": 0.8}]),
            {"persons": 2, "motion": 0.2})
        assert ev["assault"].state == "corroborated" and level_for(ev["assault"].score) == 3

    def test_one_half_is_an_impulse_and_does_not_confirm(self):
        """Review F4 in clip mode: the halves do not overlap; one is not enough."""
        ev = ClipConfirmation(2).update(
            FIGHT_FRAMES + clip_audio([{"aggression": 0.9}, {}]),
            {"persons": 2, "motion": 0.2})
        assert ev["assault"].state != "corroborated" and level_for(ev["assault"].score) == 2

    def test_clip_banter_holds_the_fight_at_l2(self):
        ev = ClipConfirmation(2).update(
            FIGHT_FRAMES + clip_audio([{"benign_social": 0.9}, {}]),
            {"persons": 2, "motion": 0.2})
        assert ev["assault"].rule == "banter" and level_for(ev["assault"].score) == 2


# ── capture and the pipeline wiring ───────────────────────────────────────────
class TestCapture:
    """Review F11: one persistent stream; blocks stamped with their capture time."""

    def test_block_end_uses_the_adc_time(self):
        info = SimpleNamespace(inputBufferAdcTime=99.0, currentTime=100.0)
        assert block_end_monotonic(info, 16000, 16000, now=50.0) == pytest.approx(50.0)
        info = SimpleNamespace(inputBufferAdcTime=99.5, currentTime=100.0)
        assert block_end_monotonic(info, 8000, 16000, now=50.0) == pytest.approx(50.0)
        late = SimpleNamespace(inputBufferAdcTime=97.0, currentTime=100.0)
        assert block_end_monotonic(late, 16000, 16000, now=50.0) == pytest.approx(48.0)

    def test_zero_timestamps_fall_back_to_now(self):
        info = SimpleNamespace(inputBufferAdcTime=0.0, currentTime=0.0)
        assert block_end_monotonic(info, 16000, 16000, now=7.0) == 7.0

    def test_blocks_queue_and_classify_without_a_device(self):
        engine = AudioEngine(capture=False)
        rng = np.random.default_rng(0)
        info = SimpleNamespace(inputBufferAdcTime=0.0, currentTime=0.0)
        for _ in range(5):
            engine._on_block(rng.normal(0, 0.1, (16000, 1)).astype(np.float32), 16000, info,
                             None)
        assert engine._blocks.qsize() == 5
        result = None
        for i in range(5):
            samples, _, wall = engine._blocks.get_nowait()
            result = engine.process_block(samples, 10.0 + i, wall) or result
        assert result is not None and result.end == 14.0 and result.start == 10.0


class TestPipelineWiring:
    def test_observe_records_audio_alignment_and_votes(self, tmp_path):
        from test_clip_engine import FakeDetector, armed_weapon, make_pipeline

        class Mic:
            available = True
            model_name = "fake"

            def result_for(self, at, tolerance=None):
                result = heard(float(math.floor(at)), weapon_sound=0.8)
                return result, alignment(result, at)

        pipeline = make_pipeline(tmp_path, detector=FakeDetector(armed_weapon(0.0)),
                                 scene=NO_SCENE, audio=Mic())
        frame = np.zeros((120, 160, 3), np.uint8)
        features = None
        for i in range(N):
            features = pipeline.observe(frame, i / 2.0)
        extras = features.extras
        assert extras["audio"]["status"] == "aligned" and "counted" in extras["audio"]
        assert "audio" in extras["sources"] and "audio_benign" in extras["sources"]
        weapon = extras["evidence"]["weapon"]
        assert weapon["state"] == "corroborated"
        assert set(weapon["modalities"]) == {VIDEO_DETECTOR, AUDIO}
        out = pipeline.decide(frame, features, N / 2.0)
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

    def test_sensor_events_from_another_thread_are_safe(self, tmp_path):
        """Review F10: the GPIO thread writes while inference reads/resets."""
        from test_clip_engine import make_pipeline
        pipeline = make_pipeline(tmp_path, scene=NO_SCENE)
        stop, errors = threading.Event(), []

        def gpio():
            i = 0
            while not stop.is_set():
                pipeline.note_sensor(f"s{i % 50}", float(i))
                i += 1

        thread = threading.Thread(target=gpio)
        thread.start()
        try:
            for i in range(2000):
                pipeline.sensor_context(float(i))
                if i % 200 == 0:
                    with pipeline._sensor_lock:
                        pipeline._sensor_at.clear()
        except RuntimeError as exc:             # "dictionary changed size during iteration"
            errors.append(exc)
        finally:
            stop.set()
            thread.join()
        assert not errors
