"""Clip mode on the live loop, in evaluation, and in replay.

``Pipeline.run_live`` with the clip strategy and a fake camera; the
evaluation hook and the replay driver on fake perception. No model, camera or
microphone is opened.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from sentrix.io.sources import FrameSource
from sentrix.perception.detector import DetectionResult
from sentrix.pipeline.clip import (ClipPolicy, ClipRunner, ClipStrategy, build_strategy,
                                   prepare_eval_pipeline, replay_video_clip)
from sentrix.pipeline.engine import InferenceStrategy, Pipeline, TickStrategy
from sentrix.pipeline.state import state
from sentrix.sos import NullSosService
from test_clip_engine import (FakeDetector, FakeScene, armed_weapon, clip_cfg, frame_at,
                              make_pipeline, time_of)
from test_clip_fastlane import lane, pistol_box, someone


class StampedCamera(FrameSource):
    """30 fps frames that carry their time since start (``frame_at``)."""

    name = "fake:stamped"

    def __init__(self, period: float = 1 / 30) -> None:
        self.period, self.started = period, time.monotonic()

    def read(self):
        return True, frame_at(0.0)

    def latest(self, after_seq: int = -1):
        seq = int((time.monotonic() - self.started) / self.period) + 1
        if seq == after_seq:
            return None, seq, 0.0
        at = self.started + seq * self.period
        return frame_at(at - self.started), seq, at


@pytest.fixture
def torch_threads():
    import torch
    before = torch.get_num_threads()
    yield
    torch.set_num_threads(before)


class TestStrategy:
    def test_build_strategy_falls_back_on_a_bare_pipeline(self):
        bare = Pipeline.__new__(Pipeline)
        assert isinstance(build_strategy(bare), TickStrategy)

    def test_build_strategy(self, tmp_path, torch_threads):
        pipeline = make_pipeline(tmp_path)
        pipeline.policy = object()
        strategy = build_strategy(pipeline)
        assert isinstance(strategy, ClipStrategy) and isinstance(strategy, InferenceStrategy)
        assert strategy.name == "clip"
        assert isinstance(pipeline.policy, ClipPolicy)
        assert pipeline.clip_runner is strategy.runner

    def test_run_live_runs_one_clip_per_hop_and_keeps_the_video_rate(self, tmp_path):
        pipeline = make_pipeline(tmp_path, FakeDetector(lambda t: DetectionResult()))
        pipeline.source = StampedCamera()
        runner = ClipRunner(pipeline, cfg=clip_cfg(window_seconds=0.6, hop_seconds=0.3),
                            fast_lane=False)
        strategy = ClipStrategy(pipeline, runner)
        stop = threading.Event()
        loop = threading.Thread(target=pipeline.run_live, args=(stop, strategy), daemon=True)
        loop.start()
        time.sleep(1.8)
        stop.set()
        loop.join(timeout=10.0)
        assert not loop.is_alive()
        assert pipeline.preview_seq >= 35, "preview keeps the camera rate"
        assert 3 <= runner.scheduler.clips <= 7
        status = state.get()["clip"]
        assert status["mode"] == "clip" and status["clips"] >= 3
        assert status["last"]["gate_open"] is not None
        assert state.get()["frame_age_ms"] is not None and pipeline._ai_times
        detector_times = pipeline.detector.calls
        assert detector_times == sorted(detector_times)

    def test_the_fast_lane_runs_on_its_own_thread(self, tmp_path):
        pipeline = make_pipeline(tmp_path, FakeDetector(lambda t: DetectionResult()),
                                 FakeScene())
        pipeline.source = StampedCamera()
        fast = lane(weapon=lambda t: [pistol_box()], persons=someone, fast_lane_every_n=2)
        runner = ClipRunner(pipeline, cfg=clip_cfg(window_seconds=2.0, hop_seconds=1.0),
                            fast_lane=fast)
        strategy = ClipStrategy(pipeline, runner)
        stop = threading.Event()
        loop = threading.Thread(target=pipeline.run_live, args=(stop, strategy), daemon=True)
        loop.start()
        time.sleep(1.5)
        stop.set()
        loop.join(timeout=10.0)
        assert fast.calls >= 5
        assert runner.fast_decisions >= 1 and runner.level >= 3
        assert any(t.name == "sentrix-fastlane" for t in threading.enumerate()) is False

    def test_on_frame_only_buffers(self, tmp_path):
        pipeline = make_pipeline(tmp_path)
        strategy = ClipStrategy(pipeline, ClipRunner(pipeline, fast_lane=False))
        started = time.perf_counter()
        for i in range(60):
            strategy.on_frame(frame_at(i / 30), i / 30)
        per_frame = (time.perf_counter() - started) / 60
        assert per_frame < 0.01 and pipeline.detector.calls == []
        assert len(strategy.runner.buffer) == 30


class TestEvaluationHook:
    def test_the_harness_no_ops_survive(self, tmp_path):
        pipeline = make_pipeline(tmp_path)
        sentinels = SimpleNamespace(act=lambda *a, **k: None, submit=lambda *a, **k: None,
                                    save=lambda *a, **k: None)
        pipeline._act, pipeline._submit = sentinels.act, sentinels.submit
        pipeline._save_open_incident = sentinels.save
        sos = pipeline.sos
        out = prepare_eval_pipeline(pipeline)
        assert out is pipeline
        assert pipeline._act is sentinels.act and pipeline._submit is sentinels.submit
        assert pipeline._save_open_incident is sentinels.save
        assert pipeline.sos is sos and isinstance(pipeline.sos, NullSosService)
        assert isinstance(pipeline.policy, ClipPolicy)
        assert pipeline.clip_runner.fast_lane is None, "SENTRIX_FAST_LANE is off by default"

    def test_the_harness_builds_clip_mode(self, monkeypatch):
        from sentrix.evaluation import harness
        built = {}

        def fake_prepare(pipeline):
            built["pipeline"] = pipeline
            return pipeline

        class Light:
            def __init__(self, source=None, enable_audio=True, event_source="live"):
                self.sos = NullSosService()

        monkeypatch.setattr("sentrix.pipeline.engine.Pipeline", Light)
        monkeypatch.setattr("sentrix.pipeline.clip.prepare_eval_pipeline", fake_prepare)
        pipeline = harness.build_eval_pipeline(mode="clip")
        assert built["pipeline"] is pipeline and pipeline._submit is harness._noop


def write_video(path, seconds: float, fps: float = 15.0):
    import cv2
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"FFV1"), fps, (160, 120))
    if not writer.isOpened():
        pytest.skip("no lossless video encoder")
    for i in range(int(seconds * fps)):
        writer.write(frame_at(i / fps))
    writer.release()
    cap = cv2.VideoCapture(str(path))
    ok, first = cap.read()
    cap.release()
    if not ok or time_of(first) != 0.0:
        pytest.skip("video round trip is not lossless here")


class TestReplay:
    def test_rows_on_the_tick_grid_show_the_committed_state(self, tmp_path):
        video = tmp_path / "armed.avi"
        write_video(video, 9.0)
        scene = FakeScene(lambda t: {"weapon": 0.8} if t >= 3.0 else {"weapon": 0.05})
        pipeline = make_pipeline(tmp_path, FakeDetector(armed_weapon(3.0)), scene)
        prepare_eval_pipeline(pipeline)
        decisions = []
        rows = replay_video_clip(pipeline, video, fps=15.0, period=1 / 6,
                                 on_frame=lambda t, r: {**r, "gt": "x"},
                                 on_decision=decisions.append)
        assert len(rows) == pytest.approx(9 * 6, abs=2)
        assert all(r["gt"] == "x" for r in rows)
        assert [round(r["t"], 3) for r in rows[:3]] == [0.0, 0.167, 0.333]
        first_clip = [r for r in rows if r["kind"] == "clip"][0]
        assert first_clip["t"] >= 2.0 and first_clip["decision_t"] == 2.0
        assert all(r["decision_t"] is None or r["decision_t"] <= r["t"] + 1e-9 for r in rows)
        threat = [r for r in rows if r["level"] >= 3]
        assert threat and threat[0]["pred"] == "weapon" and threat[0]["t"] >= 4.0
        assert "scene_weapon" in threat[0] and "detector_weapon" in threat[0]
        assert {d["kind"] for d in decisions} == {"clip"} and "clip" not in decisions[0]

    def test_replays_are_independent(self, tmp_path):
        video = tmp_path / "armed.avi"
        write_video(video, 6.0)
        scene = FakeScene(lambda t: {"weapon": 0.8})
        pipeline = make_pipeline(tmp_path, FakeDetector(armed_weapon(0.0)), scene)
        first = replay_video_clip(pipeline, video, fps=15.0)
        second = replay_video_clip(pipeline, video, fps=15.0)
        assert [r["level"] for r in first] == [r["level"] for r in second]


class TestReplayTool:
    def test_comparison_pairs_baseline_and_current(self):
        from tools.replay_custom_videos import compare_committed, format_comparison

        def report(caught, false, mode):
            return {"mode": mode, "commit": "abc", "inference_hz": 6.0,
                    "splits": {"all": {"committed": {
                        "normal_hours": 0.06,
                        "L3": {"onsets": 9, "false_onsets": false,
                               "normal_time_at_level": 0.1,
                               "episodes": {"weapon": {"episodes": 12, "caught": caught,
                                                       "median_latency": 0.2,
                                                       "p95_latency": 4.0,
                                                       "within_2s": 8}}}}}}}

        comparison = compare_committed(report(9, 0, "tick"), report(10, 1, "clip"), "m5")
        weapon = comparison["levels"]["L3"]["classes"]["weapon"]
        assert weapon["caught"] == [9, 10] and weapon["episodes"] == 12
        assert comparison["levels"]["L3"]["false_onsets"] == [0, 1]
        assert "weapon 9->10/12" in format_comparison(comparison)

    def test_clip_stats(self):
        from tools.replay_custom_videos import ClipStats
        stats = ClipStats()
        scores = SimpleNamespace(gate={"open": True}, keyframes=[1, 2, 3], compute_ms=120.0,
                                 stages={"S1_objects": {"ran": True, "ms": 80.0, "calls": 3},
                                         "S4_action": {"ran": False, "ms": 0.0, "calls": 0}})
        stats({"kind": "clip", "clip": scores})
        stats({"kind": "fast_lane"})
        report = stats.report()
        assert report["clips"] == 1 and report["fast_lane_decisions"] == 1
        assert report["stages"]["S1_objects"] == {"p50_ms": 80.0, "p95_ms": 80.0, "ran": 1,
                                                  "calls": 3}
        assert "S4_action" not in report["stages"]

    def test_bench_picks_one_cpu_per_core(self, monkeypatch):
        psutil = pytest.importorskip("psutil")
        from tools.bench_clip import pick_cpus
        monkeypatch.setattr(psutil, "cpu_count", lambda logical=True: 24 if logical else 16)
        assert pick_cpus(4) == [0, 2, 4, 6]
        monkeypatch.setattr(psutil, "cpu_count", lambda logical=True: 4)
        assert pick_cpus(4) == [0, 1, 2, 3]
        assert pick_cpus(8) is None
