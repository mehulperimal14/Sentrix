"""Identity engine: who counts as authorised, and how the backend is built."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import numpy as np
import pytest

from sentrix.config import settings
from sentrix.perception.identity import IdentityEngine


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
