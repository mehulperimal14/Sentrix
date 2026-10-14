"""The fast lane: YOLO at 320 px between clips, L3 on two consecutive hits.

Scripted heads keyed on the frame time; no model is loaded.
"""

from __future__ import annotations

import pytest

from sentrix.config import settings
from sentrix.fusion.policy import level_for
from sentrix.perception.detector import DetectionResult
from sentrix.pipeline.clip import ClipRunner
from sentrix.pipeline.fastlane import FastLane, FastLaneHit, fast_lane_evidence
from sentrix.runtime.yolo import Box
from test_clip_engine import (FakeDetector, FakeScene, clip_cfg, frame_at, make_pipeline,
                              person, run, time_of)

WEAPON_BOX = (60, 40, 76, 56)
PERSON_BOX = (30, 10, 100, 115)


class FakeHead:
    backend = "fake"
    imgsz = 320

    def __init__(self, script) -> None:
        self.script = script
        self.calls: list[float] = []

    def predict(self, frame, conf, iou=0.45, classes=None, max_det=50):
        t = time_of(frame)
        self.calls.append(t)
        return [b for b in self.script(t) if b.conf >= conf]


def pistol_box(conf=0.8, box=WEAPON_BOX, label="pistol"):
    return Box(label, conf, box, 0)


def lane(weapon=lambda t: [], fire=lambda t: [], persons=lambda t: [], with_person=True,
         **cfg) -> FastLane:
    heads = {"weapon": FakeHead(weapon), "fire": FakeHead(fire)}
    if with_person:
        heads["person"] = FakeHead(persons)
    return FastLane(heads, clip_cfg(**cfg))


def someone(t):
    return [Box("person", 0.9, PERSON_BOX, 0)]


class TestHits:
    def test_two_consecutive_hits_on_one_track_raise(self):
        fast = lane(weapon=lambda t: [pistol_box()], persons=someone)
        first = fast.analyse(frame_at(1.0), 1.0)
        second = fast.analyse(frame_at(1.27), 1.27)
        assert first.hits == []
        assert [h.cls for h in second.hits] == ["weapon"]
        hit = second.hits[0]
        assert hit.hits == 2 and hit.first_at == 1.0 and hit.at == 1.27

    def test_a_gap_starts_the_count_again(self):
        script = {1.0: [pistol_box()], 1.27: [], 1.53: [pistol_box()], 1.8: [pistol_box()]}
        fast = lane(weapon=lambda t: script.get(t, []), persons=someone)
        results = [fast.analyse(frame_at(t), t).hits for t in (1.0, 1.27, 1.53, 1.8)]
        assert [bool(h) for h in results] == [False, False, False, True]

    def test_a_box_that_jumps_across_the_frame_is_a_new_track(self):
        boxes = {1.0: (10, 10, 26, 26), 1.27: (130, 90, 146, 106)}
        fast = lane(weapon=lambda t: [pistol_box(box=boxes[t])], persons=someone)
        assert not fast.analyse(frame_at(1.0), 1.0).hits
        assert not fast.analyse(frame_at(1.27), 1.27).hits

    def test_a_weapon_needs_a_person(self):
        fast = lane(weapon=lambda t: [pistol_box()], persons=lambda t: [])
        for t in (1.0, 1.27, 1.53):
            seen = fast.analyse(frame_at(t), t)
            assert seen.person_recent is False and not seen.hits

    def test_the_clip_engines_person_counts(self):
        fast = lane(weapon=lambda t: [pistol_box()], with_person=False)
        fast.analyse(frame_at(1.0), 1.0, person_seen_at=0.5)
        assert fast.analyse(frame_at(1.27), 1.27, person_seen_at=0.5).hits
        stale = lane(weapon=lambda t: [pistol_box()], with_person=False)
        stale.analyse(frame_at(5.0), 5.0, person_seen_at=0.5)
        assert not stale.analyse(frame_at(5.27), 5.27, person_seen_at=0.5).hits

    def test_without_any_person_source_the_gate_is_off(self):
        fast = lane(weapon=lambda t: [pistol_box()], with_person=False)
        fast.analyse(frame_at(1.0), 1.0)
        seen = fast.analyse(frame_at(1.27), 1.27)
        assert seen.person_recent is None and seen.hits

    def test_stick_and_smoke_never_count(self):
        fast = lane(weapon=lambda t: [pistol_box(label="stick", conf=0.99)],
                    fire=lambda t: [Box("smoke", 0.99, (0, 0, 40, 40), 1)], persons=someone)
        for t in (1.0, 1.27, 1.53):
            assert not fast.analyse(frame_at(t), t).hits

    def test_boxes_below_the_detection_floor_do_not_count(self):
        low = settings.detection.weapon_conf - 0.05
        fast = lane(weapon=lambda t: [pistol_box(conf=low)], persons=someone)
        for t in (1.0, 1.27, 1.53):
            assert not fast.analyse(frame_at(t), t).hits

    def test_fire_in_one_region(self):
        flame = [Box("fire", 0.9, (100, 70, 140, 110), 0)]
        fast = lane(fire=lambda t: flame, with_person=False)
        fast.analyse(frame_at(1.0), 1.0)
        assert [h.cls for h in fast.analyse(frame_at(1.27), 1.27).hits] == ["fire"]

    def test_every_nth_frame(self):
        fast = lane(fast_lane_every_n=4)
        assert [s for s in range(12) if fast.due(s)] == [0, 4, 8]

    def test_stats(self):
        fast = lane(weapon=lambda t: [pistol_box()], persons=someone)
        for t in (1.0, 1.27):
            fast.analyse(frame_at(t), t)
        stats = fast.stats()
        assert stats["calls"] == 2 and stats["raises"] == 1 and stats["ms_p50"] is not None


class TestEvidence:
    def hit(self, cls="weapon", value=0.99):
        return FastLaneHit(cls, "pistol", value, 0.55, WEAPON_BOX, 1, 2, 1.0, 1.27)

    def test_one_source_stays_in_the_l3_band(self):
        """The fast lane is one detector: L3 (snapshot, evidence), never L4 (D3)."""
        ev = fast_lane_evidence([self.hit(value=1.0)])
        assert ev["weapon"].state == "confirmed"
        assert level_for(ev["weapon"].score) == 3
        assert ev["weapon"].score <= settings.threat.single_source_cap
        assert ev["fire"].score == 0.0
        assert "fastlane" in ev["weapon"].reason and "frames" in ev["weapon"].reason

    def test_scene_only_sources_mute_the_fast_lane(self, monkeypatch):
        import dataclasses
        cfg = dataclasses.replace(settings.threat, sources="scene")
        assert not fast_lane_evidence([self.hit()], cfg)["weapon"].votes


class TestRunner:
    def test_l3_before_the_first_clip_then_the_clip_takes_over(self, tmp_path):
        """A pistol drawn at 0.5 s: the fast lane commits L3 within about half a
        second; the first clip (2 s) confirms and types it."""
        def armed(t):
            return [pistol_box()] if t >= 0.5 else []

        detector = FakeDetector(lambda t: DetectionResult(persons=[person(1)]))
        scene = FakeScene(lambda t: {"weapon": 0.8} if t >= 0.5 else {})
        pipeline = make_pipeline(tmp_path, detector, scene)
        fast = lane(weapon=armed, persons=someone)
        runner = ClipRunner(pipeline, fast_lane=fast)
        decisions = run(runner, 4.2)
        first = decisions[0]
        assert first["kind"] == "fast_lane" and first["level"] == 3
        assert 0.5 < first["t"] <= 1.1 and first["incident_type"] == "weapon"
        assert any(d["kind"] == "clip" and d["level"] >= 3 for d in decisions)
        assert runner.fast_decisions >= 1

    def test_only_every_nth_buffered_frame_reaches_the_fast_lane(self, tmp_path):
        fast = lane(fast_lane_every_n=4)
        runner = ClipRunner(make_pipeline(tmp_path), fast_lane=fast)
        run(runner, 2.0)
        assert len(fast.heads["weapon"].calls) == pytest.approx(30 / 4, abs=1)

    def test_enable_and_disable(self, tmp_path, monkeypatch):
        runner = ClipRunner(make_pipeline(tmp_path), fast_lane=False)
        assert runner.fast_lane is None
        built = lane()
        monkeypatch.setattr(FastLane, "build", classmethod(lambda cls, cfg=None, **k: built))
        assert runner.enable_fast_lane(True) and runner.fast_lane is built
        assert not runner.enable_fast_lane(False) and runner.fast_lane is None

    def test_build_needs_an_alarm_head(self, monkeypatch):
        import sentrix.runtime as runtime
        monkeypatch.setattr(runtime, "build_head", lambda *a, **k: None)
        assert FastLane.build(clip_cfg()) is None
