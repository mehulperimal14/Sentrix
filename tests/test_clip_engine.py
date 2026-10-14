"""The clip engine's stage cascade, within-clip confirmation and decisions.

Every model is a scripted fake keyed on the frame's capture time (encoded in
its pixels), so these tests run the real buffer, scheduler, engine, policy,
TCI and incident code without loading a network. Side effects are recorded,
never run.
"""

from __future__ import annotations

import dataclasses
import json
import queue
from collections import deque
from types import SimpleNamespace

import numpy as np
import pytest

from sentrix.config import settings
from sentrix.fusion.evidence import (ClassEvidence, ClipConfirmation, ClipObservation,
                                     EvidenceAccumulator, KeyframeVote, corroborated_score)
from sentrix.fusion.features import FeatureVector
from sentrix.fusion.policy import level_for
from sentrix.fusion.tci import TCIEngine
from sentrix.perception.behaviour import BehaviourEngine
from sentrix.perception.detector import Detection, DetectionResult
from sentrix.perception.identity import FaceMatch, TrackIdentity
from sentrix.perception.motion import MotionEngine
from sentrix.perception.scene import SCENE_CLASSES, SceneResult, decide_label
from sentrix.pipeline.clip import (ClipEngine, ClipPolicy, ClipRunner, evidence_span,
                                   read_clip_bundle)
from sentrix.pipeline.engine import Pipeline
from sentrix.pipeline.events import EventLog, load_dataset
from sentrix.pipeline.incidents import IncidentManager
from sentrix.sos import NullSosService

FPS = 15.0
SIZE = (120, 160)
THRESHOLDS = {"fire": 0.45, "weapon": 0.5, "assault": 0.6, "robbery": 0.6}


# ── frames that carry their own time ──────────────────────────────────────────
def frame_at(t: float, moving: bool = True, flame: bool = False) -> np.ndarray:
    image = np.full((*SIZE, 3), 40, np.uint8)
    if moving:
        x = int(t * 30) % (SIZE[1] - 24)
        image[60:84, x:x + 24] = 200
    if flame:
        image[90:110, 120:150] = (0, 160, 255)
    stamp = int(round(t * 100))
    image[0, 0, 0], image[0, 0, 1], image[0, 0, 2] = stamp % 256, stamp // 256, 7
    return image


def time_of(image: np.ndarray) -> float:
    return (int(image[0, 0, 0]) + 256 * int(image[0, 0, 1])) / 100.0


def clip_cfg(**overrides):
    """ClipConfig as a namespace, so options ClipConfig lacks can be set."""
    return SimpleNamespace(**{**dataclasses.asdict(settings.clip), **overrides})


# ── fakes ─────────────────────────────────────────────────────────────────────
def person(track_id: int, box=(10, 10, 70, 110), conf: float = 0.9) -> Detection:
    return Detection("person", conf, box, track_id=track_id)


def pistol(box=(50, 40, 66, 56), conf: float = 0.8, label: str = "pistol") -> Detection:
    return Detection(label, conf, box)


class FakeDetector:
    """``script(t) -> DetectionResult``; records the times it was called at."""

    available = True

    def __init__(self, script=None) -> None:
        self.script = script or (lambda t: DetectionResult())
        self.calls: list[float] = []

    def detect(self, image):
        t = time_of(image)
        self.calls.append(t)
        return self.script(t)

    def reset_tracks(self) -> None:
        pass


class FakeScene:
    """``script(t) -> {class: p}`` (the rest goes to normal)."""

    available = True

    def __init__(self, script=None) -> None:
        self.script = script or (lambda t: {})
        self.thresholds = dict(THRESHOLDS)
        self.calls: list[float] = []

    def score_frames(self, images):
        out = []
        for image in images:
            t = time_of(image)
            self.calls.append(t)
            p = {c: 0.0 for c in SCENE_CLASSES}
            p.update(self.script(t))
            p["normal"] = max(0.0, 1.0 - sum(v for c, v in p.items() if c != "normal"))
            out.append(np.array([p[c] for c in SCENE_CLASSES], np.float32))
        return np.stack(out)

    def decide(self, probs):
        p = {c: float(probs[i]) for i, c in enumerate(SCENE_CLASSES)}
        return SceneResult(p, decide_label(p, self.thresholds), dict(self.thresholds))

    def reset(self) -> None:
        pass


class FakeIdentity:
    backend = "fake"
    available = True

    def __init__(self, faces=()) -> None:
        self.faces = list(faces)
        self.calls = 0

    def match_faces(self, image):
        self.calls += 1
        return list(self.faces)

    def score(self, result, person_present):
        return settings.identity.unknown_person_score if person_present and not result.authorized else 0.0


class FakeReID:
    def embed(self, crop):
        return None

    def match_or_register(self, embedding, now=None):
        return None

    def reset(self) -> None:
        pass


class FakeSiren:
    name = "fake"

    def __init__(self) -> None:
        self.activations = 0

    def activate(self) -> None:
        self.activations += 1


def make_pipeline(tmp_path, detector=None, scene=None, identity=None, violence=None,
                  audio=None):
    """A ``Pipeline`` with fake perception and recorded side effects."""
    p = Pipeline.__new__(Pipeline)
    p.cfg = settings
    p.source = SimpleNamespace(online=True, release=lambda: None)
    p.detector = detector or FakeDetector()
    p.scene = scene or FakeScene()
    p.violence = violence
    p.identity = identity or FakeIdentity()
    p.track_identity = TrackIdentity()
    p._settled = False
    p.reid = FakeReID()
    p.audio = audio
    p.behaviour = BehaviourEngine()
    p.motion = MotionEngine()
    p.evidence = EvidenceAccumulator()
    p._last_person_at = None
    p.tci = TCIEngine()
    p.policy = ClipPolicy()
    p.incidents = IncidentManager()
    p.siren = FakeSiren()
    p.events = EventLog(directory=tmp_path, source="test")
    p.session_id = p.events.session_id
    p.sos = NullSosService()
    p._committed_level = 1
    p._latencies = deque(maxlen=200)
    p._frame_count = 0
    p._tasks = queue.Queue()
    p._init_live()
    p.submitted = []
    p._submit = lambda func, *args, **kwargs: p.submitted.append((func, args, kwargs))
    p._submit_or_run = lambda func, *args: p.submitted.append((func, args, {}))
    return p


def run(runner: ClipRunner, seconds: float, start: float = 0.0, fps: float = FPS,
        **frame_kwargs) -> list[dict]:
    out = []
    for i in range(int(round(seconds * fps))):
        t = start + i / fps
        out.extend(runner.feed(frame_at(t, **frame_kwargs), t))
    return out


def clips(decisions):
    return [d for d in decisions if d["kind"] == "clip"]


def armed_weapon(start: float = 3.0, end: float = 1e9, track: int = 1):
    """A tracked person holding a pistol between ``start`` and ``end``."""
    def script(t):
        weapons = [pistol()] if start <= t < end else []
        return DetectionResult(persons=[person(track)], weapons=weapons)
    return script


# ── within-clip confirmation ──────────────────────────────────────────────────
class TestClipConfirmation:
    def obs(self, source, cls, value, at, group=None, gate=True, threshold=0.5):
        return ClipObservation(source, cls, value, threshold, at, group, gate)

    def test_two_keyframes_of_one_track_confirm(self):
        ev = ClipConfirmation(2).update([
            self.obs("detector", "weapon", 0.7, 1.0, ("track", 1)),
            self.obs("detector", "weapon", 0.8, 2.0, ("track", 1))])
        weapon = ev["weapon"]
        assert weapon.confirmed and weapon.state == "confirmed"
        assert settings.threat.confirmed_floor <= weapon.score <= settings.threat.single_source_cap
        assert isinstance(weapon.votes[0], KeyframeVote)
        assert weapon.votes[0].span == pytest.approx(1.0) and weapon.votes[0].hits == 2

    def test_two_tracks_do_not_confirm_each_other(self):
        ev = ClipConfirmation(2).update([
            self.obs("detector", "weapon", 0.9, 1.0, ("track", 1)),
            self.obs("detector", "weapon", 0.9, 2.0, ("track", 2))])
        assert not ev["weapon"].confirmed
        assert ev["weapon"].score <= settings.threat.unconfirmed_ceiling

    def test_two_fire_regions_do_not_confirm_each_other(self):
        ev = ClipConfirmation(2).update([
            self.obs("detector", "fire", 0.9, 1.0, ("region", 1)),
            self.obs("detector", "fire", 0.9, 2.0, ("region", 2))])
        assert not ev["fire"].confirmed

    def test_one_keyframe_counts_once(self):
        ev = ClipConfirmation(2).update([
            self.obs("detector", "weapon", 0.9, 1.0, ("track", 1)),
            self.obs("detector", "weapon", 0.8, 1.0, ("track", 1))])
        assert not ev["weapon"].confirmed and ev["weapon"].votes[0].hits == 1

    def test_seen_counts_every_keyframe_the_source_looked_at(self):
        ev = ClipConfirmation(2).update(
            [self.obs("detector", "weapon", 0.0, t) for t in (0.5, 1.0, 1.5, 2.0)]
            + [self.obs("detector", "weapon", 0.9, 1.0, ("track", 1))])
        vote = ev["weapon"].votes[0]
        assert (vote.hits, vote.seen) == (1, 4)
        assert "(1/4 keyframes, track 1)" in ev["weapon"].reason

    def test_weapon_needs_a_person_on_each_keyframe(self):
        ev = ClipConfirmation(2).update([
            self.obs("scene", "weapon", 0.9, 1.0, gate=False),
            self.obs("scene", "weapon", 0.9, 2.0, gate=True)])
        assert not ev["weapon"].confirmed
        assert ev["weapon"].votes[0].gated == "no person in view"

    def test_unknown_person_gate_is_reported(self):
        ev = ClipConfirmation(2).update([
            self.obs("scene", "weapon", 0.9, 1.0, gate=None),
            self.obs("scene", "weapon", 0.9, 2.0, gate=None)])
        assert ev["weapon"].confirmed
        assert "person gate off" in ev["weapon"].reason

    def test_classifier_and_detector_corroborate_as_in_tick_mode(self):
        ev = ClipConfirmation(2).update([
            self.obs("scene", "weapon", 0.8, 1.0), self.obs("scene", "weapon", 0.8, 2.0),
            self.obs("detector", "weapon", 0.8, 1.0, ("track", 1), threshold=0.55),
            self.obs("detector", "weapon", 0.8, 2.0, ("track", 1), threshold=0.55)])
        weapon = ev["weapon"]
        assert weapon.state == "corroborated"
        assert weapon.score == pytest.approx(
            corroborated_score([v for v in weapon.votes if v.confirmed]))
        assert level_for(weapon.score) >= 4

    def test_experimental_classes_stop_in_l2(self):
        ev = ClipConfirmation(2).update([
            self.obs("scene", "assault", 0.99, 1.0, threshold=0.6),
            self.obs("scene", "assault", 0.99, 2.0, threshold=0.6)])
        assert ev["assault"].confirmed and ev["assault"].experimental
        assert ev["assault"].score <= settings.threat.experimental_cap
        assert level_for(ev["assault"].score) <= 2

    def test_invalid_inputs_are_gated(self):
        ev = ClipConfirmation(2).update([
            self.obs("scene", "fire", 0.9, 1.0, threshold=1.0),
            self.obs("scene", "fire", 0.9, 2.0, threshold=1.0)])
        assert not ev["fire"].confirmed and ev["fire"].score == 0.0
        assert "invalid threshold" in ev["fire"].reason

    def test_same_shape_as_the_tick_accumulator(self):
        clip = ClipConfirmation(2).update([])
        tick = EvidenceAccumulator().update(0.0, {}, person_recent=True)
        assert set(clip) == set(tick)
        assert all(isinstance(e, ClassEvidence) for e in clip.values())
        assert set(tick["fire"].to_dict()) == set(clip["fire"].to_dict())

    def test_sources_setting_is_respected(self):
        cfg = dataclasses.replace(settings.threat, sources="scene")
        ev = ClipConfirmation(2, cfg=cfg).update([
            self.obs("detector", "fire", 0.9, 1.0, ("region", 1)),
            self.obs("detector", "fire", 0.9, 2.0, ("region", 1))])
        assert not ev["fire"].votes

    def test_evidence_span_of_corroborated_evidence_is_the_shorter_side(self):
        ev = ClipConfirmation(2).update([
            self.obs("scene", "fire", 0.9, 1.0), self.obs("scene", "fire", 0.9, 3.0),
            self.obs("detector", "fire", 0.9, 2.0, ("region", 1)),
            self.obs("detector", "fire", 0.9, 2.5, ("region", 1))])
        assert ev["fire"].state == "corroborated"
        assert evidence_span(ev["fire"]) == pytest.approx(0.5)


# ── level policy ──────────────────────────────────────────────────────────────
class TestClipPolicy:
    def test_l4_commits_on_one_clip_when_the_keyframes_span_the_dwell(self):
        policy = ClipPolicy()
        policy.evidence_span = 1.0
        assert policy.update(0.75, now=10.0).level == 4

    def test_short_evidence_waits_for_the_next_clip(self):
        policy = ClipPolicy()
        policy.evidence_span = 0.1
        assert policy.update(0.75, now=10.0).level == 3
        policy.evidence_span = 0.0
        assert policy.update(0.75, now=12.0).level == 4

    def test_one_observation_is_enough(self):
        assert ClipPolicy()._min_obs == (1, 1, 1, 1)
        policy = ClipPolicy()
        policy.evidence_span = 2.0
        assert policy.update(0.9, now=5.0).level == 5

    def test_steps_down_after_five_quiet_seconds(self):
        policy = ClipPolicy()
        policy.evidence_span = 1.0
        policy.update(0.5, now=0.0)
        policy.evidence_span = 0.0
        assert policy.update(0.0, now=2.0).level == 3
        assert policy.update(0.0, now=4.0).level == 3
        assert policy.update(0.0, now=6.0).level == 1


# ── the cascade ───────────────────────────────────────────────────────────────
class TestCascade:
    def test_a_held_pistol_reaches_l4_with_corroboration(self, tmp_path):
        scene = FakeScene(lambda t: {"weapon": 0.8} if t >= 3.0 else {"weapon": 0.05})
        pipeline = make_pipeline(tmp_path, FakeDetector(armed_weapon(3.0)), scene)
        decisions = clips(run(ClipRunner(pipeline, fast_lane=False), 9.0))
        assert decisions[0]["level"] == 1 and decisions[0]["incident_type"] == "normal"
        threat = [d for d in decisions if d["t"] >= 4.0]
        assert threat[0]["level"] >= 3 and threat[0]["incident_type"] == "weapon"
        assert max(d["level"] for d in threat) >= 4
        evidence = threat[-1]["clip"].evidence["weapon"]
        assert evidence.state == "corroborated"

    def test_the_tracker_never_runs_backwards_and_frames_are_analysed_once(self, tmp_path):
        detector = FakeDetector(armed_weapon(100.0))
        run(ClipRunner(make_pipeline(tmp_path, detector), fast_lane=False), 12.0)
        assert detector.calls == sorted(detector.calls)
        assert len(detector.calls) == len(set(detector.calls))

    def test_overlapping_clips_reuse_the_shared_half(self, tmp_path):
        detector = FakeDetector(armed_weapon(100.0))
        runner = ClipRunner(make_pipeline(tmp_path, detector), fast_lane=False)
        decisions = clips(run(runner, 12.0))
        later = [d["clip"] for d in decisions[2:]]
        for scores in later:
            assert scores.stages["S1_objects"]["calls"] <= 4
            assert any(k.cached for k in scores.keyframes)
            assert len(scores.keyframes) >= 5

    def test_scene_runs_only_with_a_person_or_a_fire_candidate(self, tmp_path):
        scene = FakeScene()
        pipeline = make_pipeline(tmp_path, FakeDetector(), scene)
        decisions = clips(run(ClipRunner(pipeline, fast_lane=False), 6.0))
        assert scene.calls == []
        assert decisions[-1]["clip"].stages["S2_scene"]["reason"] == \
            "no person or fire candidate"

    def test_a_bright_flame_wakes_the_scene_model_without_a_person(self, tmp_path):
        scene = FakeScene(lambda t: {"fire": 0.9})
        pipeline = make_pipeline(tmp_path, FakeDetector(), scene)
        decisions = clips(run(ClipRunner(pipeline, fast_lane=False), 6.0, flame=True))
        assert scene.calls
        assert decisions[-1]["incident_type"] == "fire" and decisions[-1]["level"] >= 3

    def test_at_most_two_scene_inferences_per_clip(self, tmp_path):
        scene = FakeScene()
        pipeline = make_pipeline(tmp_path, FakeDetector(armed_weapon(100.0)), scene)
        for scores in (d["clip"] for d in clips(run(ClipRunner(pipeline, fast_lane=False), 12.0))):
            assert scores.stages["S2_scene"]["calls"] <= 2

    def test_a_weapon_with_nobody_in_view_never_alarms(self, tmp_path):
        """65 % of the live false weapon alarms had nobody in frame (CLAUDE.md §5)."""
        detector = FakeDetector(lambda t: DetectionResult(weapons=[pistol()]))
        scene = FakeScene(lambda t: {"weapon": 0.95})
        pipeline = make_pipeline(tmp_path, detector, scene)
        decisions = clips(run(ClipRunner(pipeline, fast_lane=False), 8.0))
        assert all(d["level"] == 1 for d in decisions)
        reason = decisions[-1]["clip"].evidence["weapon"].reason
        assert "no person in view" in reason

    def test_stick_boxes_never_vote(self, tmp_path):
        detector = FakeDetector(lambda t: DetectionResult(
            persons=[person(1)], weapons=[pistol(label="stick", conf=0.99)]))
        decisions = clips(run(ClipRunner(make_pipeline(tmp_path, detector), fast_lane=False), 6.0))
        assert all(d["level"] == 1 for d in decisions)

    def test_weapon_groups(self, tmp_path):
        engine = ClipEngine(make_pipeline(tmp_path, FakeDetector(lambda t: DetectionResult(
            persons=[person(1, (0, 0, 60, 120)), person(2, (100, 0, 160, 120))],
            weapons=[pistol((40, 40, 56, 56)), pistol((110, 40, 126, 56)),
                     pistol((70, 100, 80, 110))],
            fires=[Detection("fire", 0.9, (0, 0, 20, 20)),
                   Detection("smoke", 0.9, (30, 0, 50, 20))]))), cfg=clip_cfg())
        from sentrix.pipeline.clip import BufferedFrame
        analysis = engine._analyse_frame(BufferedFrame(0, 1.0, frame_at(1.0)))
        assert analysis.weapon_groups[:2] == [("track", 1), ("track", 2)]
        assert analysis.weapon_groups[2][0] == "region"
        assert [f.label for f in analysis.fires] == ["fire"], "smoke never votes (D1)"
        assert analysis.fire_groups[0][0] == "region"


class TestGate:
    def test_a_still_empty_room_closes_the_gate_until_the_refresh(self, tmp_path):
        detector = FakeDetector()
        cfg = clip_cfg(idle_refresh_seconds=6.0, gate_hold_seconds=3.0)
        runner = ClipRunner(make_pipeline(tmp_path, detector), cfg=cfg, fast_lane=False)
        scores = [d["clip"] for d in clips(run(runner, 14.0, moving=False))]
        opened = [s.gate["open"] for s in scores]
        assert opened[0] is True and "refresh" in scores[0].gate["reasons"]
        assert opened[1] is False and scores[1].stages["S1_objects"]["reason"] == "gate closed"
        assert True in opened[2:], "a full cascade at least every refresh period"

    def test_motion_opens_the_gate(self, tmp_path):
        runner = ClipRunner(make_pipeline(tmp_path), fast_lane=False)
        scores = [d["clip"] for d in clips(run(runner, 8.0, moving=True))]
        assert all(s.gate["open"] for s in scores)
        assert "motion" in scores[-1].gate["reasons"]

    def test_an_alarm_keeps_the_gate_open(self, tmp_path):
        pipeline = make_pipeline(tmp_path)
        engine = ClipEngine(pipeline, cfg=clip_cfg())
        from sentrix.pipeline.clip import BufferedFrame, Clip
        engine._last_full_at = 10.0
        still = Clip(0, 6.0, 10.0, [BufferedFrame(0, 9.0, frame_at(9.0, moving=False))])
        assert engine._gate(still, level=1)[0] is False
        assert engine._gate(still, level=3) == (True, ["alarm L3"])

    def test_a_pir_event_opens_the_gate(self, tmp_path):
        engine = ClipEngine(make_pipeline(tmp_path), cfg=clip_cfg())
        from sentrix.pipeline.clip import BufferedFrame, Clip, SensorEvent
        engine._last_full_at = 10.0
        clip = Clip(0, 6.0, 10.0, [BufferedFrame(0, 9.0, frame_at(9.0, moving=False))],
                    events=[SensorEvent("pir", 8.0)])
        assert engine._gate(clip, 1) == (True, ["sensor"])


class TestIdentityStage:
    def test_identity_runs_once_per_interval_and_at_once_for_a_new_track(self, tmp_path):
        identity = FakeIdentity()
        tracks = {"n": 1}
        detector = FakeDetector(lambda t: DetectionResult(
            persons=[person(i + 1, (10 + 40 * i, 10, 40 + 40 * i, 110))
                     for i in range(tracks["n"])]))
        runner = ClipRunner(make_pipeline(tmp_path, detector, identity=identity),
                            cfg=clip_cfg(identity_interval_seconds=6.0), fast_lane=False)
        run(runner, 10.0)
        assert identity.calls == 2                        # t=2 and t=8 (6 s apart)
        tracks["n"] = 2
        run(runner, 2.2, start=10.0)
        assert identity.calls == 3, "a new person is checked at once"

    def test_a_resident_is_authorised_on_their_track(self, tmp_path):
        identity = FakeIdentity([FaceMatch((20, 20, 40, 40), "alice", 0.9)])
        detector = FakeDetector(lambda t: DetectionResult(persons=[person(1)]))
        runner = ClipRunner(make_pipeline(tmp_path, detector, identity=identity),
                            fast_lane=False)
        scores = clips(run(runner, 6.0))[-1]["clip"]
        assert scores.identity["authorized"] is True and scores.identity["name"] == "alice"
        assert scores.features.extras["detections"]["persons"][0]["identity"] == "alice"


class FakeViolence:
    available = True
    threshold = 0.5

    def __init__(self, score: float) -> None:
        self.score, self.fresh, self.observed = score, False, 0

    def reset(self) -> None:
        self.observed = 0

    def observe(self, image, now=None) -> None:
        self.observed += 1

    def result(self, now=None):
        self.fresh = self.observed > 0
        return self.score, "fighting" if self.score >= self.threshold else "normal"


class TestActionStage:
    def two_people(self, t):
        return DetectionResult(persons=[person(1, (0, 0, 60, 120)), person(2, (90, 0, 150, 120))])

    def test_runs_over_both_halves_with_two_people_moving(self, tmp_path):
        violence = FakeViolence(0.9)
        pipeline = make_pipeline(tmp_path, FakeDetector(self.two_people), violence=violence)
        pipeline.scene = SimpleNamespace(available=False)
        runner = ClipRunner(pipeline, cfg=clip_cfg(action_motion_floor=0.0), fast_lane=False)
        scores = clips(run(runner, 6.0))[-1]["clip"]
        assert scores.stages["S4_action"]["calls"] == 2
        assault = scores.evidence["assault"]
        assert assault.confirmed and assault.score <= settings.threat.experimental_cap

    def test_skipped_for_one_person(self, tmp_path):
        pipeline = make_pipeline(tmp_path, FakeDetector(lambda t: DetectionResult(
            persons=[person(1)])), violence=FakeViolence(0.9))
        scores = clips(run(ClipRunner(pipeline, fast_lane=False), 6.0))[-1]["clip"]
        assert scores.stages["S4_action"] == {"ran": False, "ms": 0.0, "calls": 0,
                                              "reason": "fewer than 2 persons"}

    def test_skipped_without_an_action_model(self, tmp_path):
        scores = clips(run(ClipRunner(make_pipeline(tmp_path), fast_lane=False), 4.0))[-1]["clip"]
        assert "no action model" in scores.stages["S4_action"]["reason"]


class TestAudioStage:
    def test_buffered_audio_is_classified_over_the_window(self, tmp_path):
        seen = []

        class Engine:
            def classify_window(self, samples, rms):
                seen.append(len(samples))
                return SimpleNamespace(score=0.4, label="scream")

        pipeline = make_pipeline(tmp_path, audio=Engine())
        runner = ClipRunner(pipeline, fast_lane=False)
        for i in range(int(6 * FPS)):
            t = i / FPS
            if i % 15 == 0:
                runner.buffer.add_audio(np.full(16000, 0.3, np.float32), end_at=t)
            runner.feed(frame_at(t), t)
        assert seen and max(seen) == 4 * 16000
        assert runner.last_scores.audio["label"] == "scream"
        assert runner.last_scores.features.audio == pytest.approx(0.4)

    def test_quiet_audio_is_not_classified(self, tmp_path):
        class Engine:
            def classify_window(self, samples, rms):
                raise AssertionError("quiet audio must not reach the model")

        runner = ClipRunner(make_pipeline(tmp_path, audio=Engine()), fast_lane=False)
        for i in range(int(4 * FPS)):
            t = i / FPS
            runner.buffer.add_audio(np.zeros(1067, np.float32), end_at=t)
            runner.feed(frame_at(t), t)
        assert runner.last_scores.audio["label"] == "normal_ambient"

    def test_the_audio_engine_window_is_used_when_it_owns_the_microphone(self, tmp_path):
        engine = SimpleNamespace(result=lambda: SimpleNamespace(score=0.2, label="siren"))
        runner = ClipRunner(make_pipeline(tmp_path, audio=engine), fast_lane=False)
        run(runner, 4.0)
        assert runner.last_scores.audio["source"] == "audio engine"
        assert runner.last_scores.features.audio_label == "siren"


# ── decisions, records and evidence ───────────────────────────────────────────
class TestDecisions:
    def test_decision_time_never_runs_backwards(self, tmp_path):
        runner = ClipRunner(make_pipeline(tmp_path), fast_lane=False)
        features = FeatureVector()
        a = runner._decide(frame_at(5.0), features, 5.0, None, 0.0)
        b = runner._decide(frame_at(4.0), features, 4.0, None, 0.0)
        assert a["t"] == 5.0 and b["t"] == 5.0

    def test_clip_rows_are_schema_4_and_skipped_by_dataset_loaders(self, tmp_path):
        scene = FakeScene(lambda t: {"weapon": 0.8})
        pipeline = make_pipeline(tmp_path, FakeDetector(armed_weapon(0.0)), scene)
        run(ClipRunner(pipeline, fast_lane=False), 6.2)
        pipeline.events.close()
        rows = [json.loads(line) for line in pipeline.events.path.read_text().splitlines()]
        clip_rows = [r for r in rows if r.get("kind") == "clip"]
        tick_rows = [r for r in rows if "features" in r]
        assert clip_rows and tick_rows and len(clip_rows) == len(tick_rows)
        row = clip_rows[-1]
        assert row["schema"] == 4 and "features" not in row
        assert {"gate", "stages", "keyframes", "evidence", "decision"} <= set(row)
        assert row["decision_seq"] == tick_rows[-1]["seq"]
        assert all(r["schema"] == 3 for r in tick_rows)
        assert len(load_dataset(tmp_path)) == len(tick_rows)

    def test_the_tick_row_carries_the_clip_votes(self, tmp_path):
        scene = FakeScene(lambda t: {"weapon": 0.8})
        pipeline = make_pipeline(tmp_path, FakeDetector(armed_weapon(0.0)), scene)
        run(ClipRunner(pipeline, fast_lane=False), 4.2)
        pipeline.events.close()
        rows = [json.loads(line) for line in pipeline.events.path.read_text().splitlines()]
        tick = [r for r in rows if "features" in r][-1]
        assert tick["votes"]["scene"]["weapon"][0] == pytest.approx(0.8)
        assert tick["incident_type"] == "weapon"

    def test_a_demo_close_forces_the_scene_to_be_confirmed_afresh(self, tmp_path):
        scene = FakeScene(lambda t: {"weapon": 0.8})
        detector = FakeDetector(armed_weapon(0.0))
        pipeline = make_pipeline(tmp_path, detector, scene)
        runner = ClipRunner(pipeline, fast_lane=False)
        first = clips(run(runner, 4.2))[-1]
        assert runner.level >= 3
        pipeline.request_close_incident("demo")
        run(runner, 2.0, start=4.2)                 # applied after the next decision (6 s)
        assert runner.engine._cache == {} and pipeline.incidents.current is None
        calls = len(detector.calls)
        again = clips(run(runner, 2.0, start=6.2))[-1]
        new = detector.calls[calls:]
        assert new and min(new) > 6.0, "no frame from before the close is reused"
        assert again["incident_id"] != first["incident_id"], "a new incident (and request)"


class TestEvidenceClips:
    def test_pre_and_post_roll_are_sealed_through_the_side_effect_worker(self, tmp_path):
        scene = FakeScene(lambda t: {"weapon": 0.8} if t >= 3.0 else {})
        pipeline = make_pipeline(tmp_path, FakeDetector(armed_weapon(3.0)), scene)
        runner = ClipRunner(pipeline, cfg=clip_cfg(post_roll_seconds=4.0), fast_lane=False)
        run(runner, 12.0)
        seals = [args for func, args, _ in pipeline.submitted if func == ClipRunner._seal]
        kinds = [args[3] for args in seals]
        assert kinds[:2] == ["pre_roll", "post_roll"]
        pre, post = seals[0][1], seals[1][1]
        fps = (len(pre) - 1) / (pre[-1][0] - pre[0][0])
        assert fps <= 5.0 + 1e-6, "sealed at evidence_fps"
        assert post[0][0] > pre[-1][0] and post[-1][0] - post[0][0] <= 4.0
        meta = seals[0][2]
        assert meta["incident_type"] == "weapon" and meta["level"] >= 3

    def test_measurement_pipelines_never_seal(self, tmp_path):
        scene = FakeScene(lambda t: {"weapon": 0.8})
        pipeline = make_pipeline(tmp_path, FakeDetector(armed_weapon(0.0)), scene)
        pipeline._act = lambda *a, **k: None               # the harness's no-op
        run(ClipRunner(pipeline, fast_lane=False), 8.0)
        assert not [f for f, _, _ in pipeline.submitted if f == ClipRunner._seal]

    def test_a_sealed_clip_is_in_the_manifest_chain_and_reads_back(self, tmp_path):
        import cv2

        from sentrix import listeners
        from sentrix.evidence.vault import EvidenceVault
        vault = EvidenceVault(tmp_path / "vault")
        heard = []
        listeners.add_listener("evidence", heard.append)
        result = SimpleNamespace(tci=0.5, incident_type="weapon", reason="r", source="evidence",
                                 features=FeatureVector())
        single = vault.save(frame_at(1.0), result, 3, "ELEVATED", "inc-1", [])
        frames = [(t, frame_at(t)) for t in (0.0, 0.2, 0.4, 0.6)]
        record = vault.save_clip(frames, level=3, status="ELEVATED", tci=0.5,
                                 incident_type="weapon", incident_id="inc-1",
                                 trigger_at=0.6, context={"session": "s1"})
        assert record is not None and record.prev_sha256 != single.prev_sha256
        assert vault.verify_chain()["ok"] and vault.verify_chain()["entries"] == 2
        assert record.extras["clip"]["kind"] == "pre_roll"
        assert record.extras["clip"]["trigger_index"] == 3
        assert [h["bundle_id"] for h in heard] == [single.bundle_id, record.bundle_id]
        back = read_clip_bundle(record.bundle_id, vault)
        assert [round(t, 3) for t, _ in back] == [-0.6, -0.4, -0.2, 0.0]
        decoded = cv2.imdecode(np.frombuffer(back[2][1], np.uint8), cv2.IMREAD_COLOR)
        assert decoded.shape == (*SIZE, 3)
        first = cv2.imdecode(np.frombuffer(vault.decrypt(record.bundle_id), np.uint8),
                             cv2.IMREAD_COLOR)
        assert first is not None, "a plain viewer shows the first frame"

    def test_save_clip_without_frames_is_nothing(self, tmp_path):
        from sentrix.evidence.vault import EvidenceVault
        assert EvidenceVault(tmp_path).save_clip([], level=3, status="ELEVATED") is None
