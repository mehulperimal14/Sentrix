"""Identity engine: who counts as authorised, and how the backend is built."""

from __future__ import annotations

import threading

import numpy as np

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
