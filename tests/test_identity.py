"""Identity engine: who counts as authorised, and how the backend is built."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from sentrix.config import settings
from sentrix.perception.identity import IdentityEngine, IdentityResult, IdentityWorker


class TestFaceCounts:
    """Faces found and matched are logged for per-track identity (M4)."""

    @staticmethod
    def engine(embeddings, gallery):
        engine = IdentityEngine.__new__(IdentityEngine)
        engine.cfg = settings.identity
        engine._lock = threading.Lock()
        engine._gallery = gallery
        engine._last_match, engine._last_name = 0.0, None
        engine._embed_all = lambda frame: embeddings
        return engine

    def test_resident_and_stranger(self, monkeypatch):
        monkeypatch.setattr(IdentityEngine, "available", property(lambda self: True))
        resident = np.array([1.0, 0.0, 0.0], np.float32)
        stranger = np.array([0.0, 1.0, 0.0], np.float32)
        result = self.engine([resident, stranger], [("res", resident)]).check(
            np.zeros((4, 4, 3), np.uint8), now=0.0)
        assert result.authorized and result.name == "res"
        assert (result.faces, result.matched, result.held) == (2, 1, False)

    def test_hold_is_marked(self, monkeypatch):
        monkeypatch.setattr(IdentityEngine, "available", property(lambda self: True))
        resident = np.array([1.0, 0.0, 0.0], np.float32)
        engine = self.engine([resident], [("res", resident)])
        engine.check(np.zeros((4, 4, 3), np.uint8), now=0.0)
        engine._embed_all = lambda frame: []
        result = engine.check(np.zeros((4, 4, 3), np.uint8), now=1.0)
        assert result.authorized and result.held and result.faces == 0


class TestQuietSessions:
    """InsightFace sessions are rebuilt without ORT spin-waiting (M2)."""

    def test_every_model_gets_a_non_spinning_session(self, monkeypatch):
        ort = pytest.importorskip("onnxruntime")
        built = []

        def fake_session(path, sess_options=None, providers=None):
            built.append((path, sess_options, providers))
            return SimpleNamespace(path=path)

        monkeypatch.setattr(ort, "InferenceSession", fake_session)
        models = {"detection": SimpleNamespace(model_file="det.onnx", session=None),
                  "recognition": SimpleNamespace(model_file="rec.onnx", session=None)}
        engine = IdentityEngine.__new__(IdentityEngine)
        engine.cfg = settings.identity
        engine._quiet_sessions(SimpleNamespace(models=models))

        assert [b[0] for b in built] == ["det.onnx", "rec.onnx"]
        for _, options, providers in built:
            assert options.get_session_config_entry("session.intra_op.allow_spinning") == "0"
            assert options.get_session_config_entry("session.inter_op.allow_spinning") == "0"
            assert options.intra_op_num_threads == settings.identity.ort_threads
            assert providers == ["CPUExecutionProvider"]
        assert models["detection"].session.path == "det.onnx"

    def test_only_detection_and_recognition_are_loaded(self):
        assert tuple(settings.identity.face_modules) == ("detection", "recognition")


class FakeEngine:
    """Counts checks; optionally slow; answers with a fixed verdict."""

    available = True

    def __init__(self, result=None, delay=0.0):
        self.result = result or IdentityResult(authorized=False, faces=1, matched=0)
        self.delay = delay
        self.calls = 0

    def check(self, frame, now=None):
        self.calls += 1
        time.sleep(self.delay)
        return self.result


def drive(worker, seconds, hz=10.0, persons=lambda t: 1, start=0.0):
    """Observe at ``hz`` in (scaled) real time; return the last verdict."""
    frame = np.zeros((4, 4, 3), np.uint8)
    result, t = None, start
    while t < start + seconds:
        result = worker.observe(frame, persons(t), now=t)
        time.sleep(1.0 / hz)
        t += 1.0 / hz
    return result


class TestIdentityWorker:
    """Recognition off the inference thread (CLAUDE.md M2)."""

    def test_observe_never_waits_for_recognition(self):
        worker = IdentityWorker(FakeEngine(delay=0.5))
        started = time.perf_counter()
        worker.observe(np.zeros((4, 4, 3), np.uint8), 1, now=0.0)
        assert time.perf_counter() - started < 0.05
        worker.stop()

    def test_fail_closed_before_the_first_verdict(self):
        resident = IdentityResult(authorized=True, name="res", faces=1, matched=1)
        worker = IdentityWorker(FakeEngine(resident, delay=0.5))
        first = worker.observe(np.zeros((4, 4, 3), np.uint8), 1, now=0.0)
        assert not first.authorized
        worker.stop()

    def test_rechecks_about_once_a_second_not_every_tick(self):
        engine = FakeEngine()
        worker = IdentityWorker(engine)
        drive(worker, seconds=2.5)
        worker.stop()
        assert 2 <= engine.calls <= 4, f"{engine.calls} checks in 2.5 s at 10 Hz"

    def test_someone_new_is_checked_at_once(self):
        engine = FakeEngine()
        worker = IdentityWorker(engine)
        drive(worker, seconds=0.3)                           # first check at t=0
        before = engine.calls
        drive(worker, seconds=0.3, persons=lambda t: 2, start=0.3)
        worker.stop()
        assert engine.calls == before + 1, "an arrival waited for the 1 s cadence"

    def test_everyone_recognised_is_rechecked_less_often(self):
        resident = IdentityResult(authorized=True, name="res", faces=1, matched=1)
        engine = FakeEngine(resident)
        worker = IdentityWorker(engine)
        verdict = drive(worker, seconds=2.5)
        worker.stop()
        assert verdict.authorized and engine.calls == 1

    def test_a_stalled_worker_stops_authorising(self):
        resident = IdentityResult(authorized=True, name="res", faces=1, matched=1)
        worker = IdentityWorker(FakeEngine(resident))
        drive(worker, seconds=0.2)
        worker._busy = True                                  # stalled: no new checks
        late = worker.observe(np.zeros((4, 4, 3), np.uint8), 1,
                              now=settings.identity.verdict_max_age_seconds + 1.0)
        worker.stop()
        assert not late.authorized
