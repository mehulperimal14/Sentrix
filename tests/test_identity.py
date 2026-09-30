"""Identity engine: who counts as authorised, and how the backend is built."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from sentrix.config import settings
from sentrix.perception.identity import (FaceCheck, FaceMatch, IdentityEngine,
                                         IdentityResult, IdentityWorker, TrackIdentity)


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
    """Counts face checks; optionally slow; answers with fixed faces."""

    available = True

    def __init__(self, faces=(), delay=0.0):
        self.faces = list(faces)
        self.delay = delay
        self.calls = 0

    def match_faces(self, frame):
        self.calls += 1
        time.sleep(self.delay)
        return self.faces


ONE = ((1, (0, 0, 100, 200)),)
TWO = ((1, (0, 0, 100, 200)), (2, (200, 0, 300, 200)))


def drive(worker, seconds, hz=10.0, persons=lambda t: ONE, settled=False, start=0.0):
    """Observe at ``hz`` in (scaled) real time; return the last face check."""
    frame = np.zeros((4, 4, 3), np.uint8)
    check, t = None, start
    while t < start + seconds:
        check = worker.observe(frame, persons(t), now=t, settled=settled)
        time.sleep(1.0 / hz)
        t += 1.0 / hz
    return check


class TestIdentityWorker:
    """Recognition off the inference thread (CLAUDE.md M2)."""

    def test_observe_never_waits_for_recognition(self):
        worker = IdentityWorker(FakeEngine(delay=0.5))
        started = time.perf_counter()
        worker.observe(np.zeros((4, 4, 3), np.uint8), ONE, now=0.0)
        assert time.perf_counter() - started < 0.05
        worker.stop()

    def test_nothing_before_the_first_check(self):
        worker = IdentityWorker(FakeEngine(delay=0.5))
        first = worker.observe(np.zeros((4, 4, 3), np.uint8), ONE, now=0.0)
        assert first.at is None and first.faces == ()
        worker.stop()

    def test_rechecks_about_once_a_second_not_every_tick(self):
        engine = FakeEngine()
        worker = IdentityWorker(engine)
        drive(worker, seconds=2.5)
        worker.stop()
        assert 2 <= engine.calls <= 4, f"{engine.calls} checks in 2.5 s at 10 Hz"

    def test_a_new_track_is_checked_at_once(self):
        engine = FakeEngine()
        worker = IdentityWorker(engine)
        drive(worker, seconds=0.3)                           # first check at t=0
        before = engine.calls
        drive(worker, seconds=0.3, persons=lambda t: TWO, start=0.3)
        worker.stop()
        assert engine.calls == before + 1, "an arrival waited for the 1 s cadence"

    def test_everyone_recognised_is_rechecked_less_often(self):
        engine = FakeEngine()
        worker = IdentityWorker(engine)
        drive(worker, seconds=2.5, settled=True)
        worker.stop()
        assert engine.calls == 1

    def test_check_carries_the_persons_of_its_frame(self):
        worker = IdentityWorker(FakeEngine())
        check = drive(worker, seconds=0.3)
        worker.stop()
        assert check.persons == ONE and check.at == 0.0


def face(box, name=None, similarity=0.0):
    return FaceMatch(box, name, similarity)


RESIDENT_FACE = (30, 20, 70, 60)                  # inside track 1
STRANGER_FACE = (230, 20, 270, 60)                # inside track 2


class TestTrackIdentity:
    """Identity per tracked person (CLAUDE.md M4)."""

    def test_stranger_beside_a_resident_is_not_authorised(self):
        """Frame-global identity let one match authorise everyone (tailgating)."""
        tracks = TrackIdentity()
        check = FaceCheck((face(RESIDENT_FACE, "alice", 0.8), face(STRANGER_FACE)), TWO, 0.0)
        result = tracks.update(check, TWO, 0.0)
        assert not result.authorized
        assert result.tracks[1] == {"state": "authorised", "name": "alice"}
        assert result.tracks[2]["state"] == "unknown"

    def test_resident_turning_away_stays_authorised_on_their_track(self):
        tracks = TrackIdentity()
        tracks.update(FaceCheck((face(RESIDENT_FACE, "alice", 0.8),), ONE, 0.0), ONE, 0.0)
        later = tracks.update(FaceCheck((), ONE, 20.0), ONE, 20.0)
        assert later.authorized and later.tracks[1]["name"] == "alice"

    def test_the_hold_does_not_pass_to_a_new_track(self):
        tracks = TrackIdentity()
        tracks.update(FaceCheck((face(RESIDENT_FACE, "alice", 0.8),), ONE, 0.0), ONE, 0.0)
        swapped = ((7, (0, 0, 100, 200)),)
        result = tracks.update(FaceCheck((), swapped, 1.0), swapped, 1.0)
        assert not result.authorized and result.tracks[7]["state"] == "unverified"

    def test_one_bad_look_does_not_flip_a_resident(self):
        tracks = TrackIdentity()
        tracks.update(FaceCheck((face(RESIDENT_FACE, "alice", 0.8),), ONE, 0.0), ONE, 0.0)
        once = tracks.update(FaceCheck((face(RESIDENT_FACE),), ONE, 1.0), ONE, 1.0)
        assert once.authorized
        twice = tracks.update(FaceCheck((face(RESIDENT_FACE),), ONE, 2.0), ONE, 2.0)
        assert not twice.authorized and twice.tracks[1]["state"] == "unknown"

    def test_unverified_person_is_not_authorised(self):
        result = TrackIdentity().update(FaceCheck((), ONE, 0.0), ONE, 0.0)
        assert not result.authorized and result.tracks[1]["state"] == "unverified"

    def test_faces_without_person_boxes_must_all_match(self):
        tracks = TrackIdentity()
        both = FaceCheck((face(RESIDENT_FACE, "alice", 0.8), face(STRANGER_FACE)), (), 0.0)
        assert not tracks.update(both, (), 0.0).authorized
        alone = FaceCheck((face(RESIDENT_FACE, "alice", 0.8),), (), 1.0)
        assert tracks.update(alone, (), 1.0).authorized

    def test_stale_check_fails_closed(self):
        tracks = TrackIdentity()
        check = FaceCheck((face(RESIDENT_FACE, "alice", 0.8),), ONE, 0.0)
        tracks.update(check, ONE, 0.0)
        late = settings.identity.verdict_max_age_seconds + 1.0
        assert not tracks.update(check, ONE, late).authorized

    def test_degraded_backend_never_authorises(self):
        result = TrackIdentity().update(FaceCheck(degraded=True), ONE, 0.0)
        assert not result.authorized and result.degraded


def assessing_engine(box, gallery=()):
    """An engine whose face finder returns one face at ``box``."""
    engine = IdentityEngine.__new__(IdentityEngine)
    engine.cfg = settings.identity
    engine._lock = threading.Lock()
    engine._reload_lock = threading.Lock()
    engine._entries = {f"{name}.jpg": (name, emb) for name, emb in gallery}
    engine._gallery = list(engine._entries.values())
    engine._faces = lambda bgr: [] if box is None else [(box, np.array([1.0, 0.0, 0.0], np.float32))]
    return engine


def textured(size=200, mean=120):
    """A sharp, mid-grey image."""
    rng = np.random.default_rng(0)
    image = rng.normal(mean, 40, (size, size, 3)).clip(0, 255).astype(np.uint8)
    return image


class TestEnrolmentGates:
    """Enrolment used to accept any file and report 'not enrolled' for all of
    them (a name-vs-stem bug); it now says what is wrong and how to fix it."""

    @pytest.fixture(autouse=True)
    def _available(self, monkeypatch):
        monkeypatch.setattr(IdentityEngine, "available", property(lambda self: True))

    def test_good_image_passes(self):
        check = assessing_engine((20, 20, 180, 180)).assess(textured(), "alice")
        assert check.ok and check.embedding is not None

    def test_no_face(self):
        check = assessing_engine(None).assess(textured(), "alice")
        assert not check.ok and "no face" in check.reason

    def test_small_face(self):
        check = assessing_engine((10, 10, 50, 50)).assess(textured(), "alice")
        assert not check.ok and "move closer" in check.reason

    def test_blurred(self):
        check = assessing_engine((20, 20, 180, 180)).assess(
            np.full((200, 200, 3), 120, np.uint8), "alice")
        assert not check.ok and "blurred" in check.reason

    def test_dark(self):
        check = assessing_engine((20, 20, 180, 180)).assess(textured(mean=15), "alice")
        assert not check.ok and "too dark" in check.reason

    def test_same_face_under_another_name(self):
        engine = assessing_engine((20, 20, 180, 180),
                                  gallery=[("bob", np.array([1.0, 0.0, 0.0], np.float32))])
        check = engine.assess(textured(), "alice")
        assert not check.ok and "'bob'" in check.reason
        assert engine.assess(textured(), "bob").ok, "another photo of bob is fine"


class TestGalleryUpdates:
    def test_incremental_enrol_and_remove(self):
        engine = assessing_engine(None)
        name = engine.enrol("alice__2.jpg", np.array([0.0, 1.0, 0.0], np.float32))
        assert name == "alice" and engine.enrolled == ["alice"]
        assert engine.enrolled_files == {"alice__2.jpg": "alice"}
        engine.remove("alice__2.jpg")
        assert engine.enrolled == []

    def test_person_names(self):
        from sentrix.perception.identity import person_name
        assert person_name("alice") == person_name("alice__3") == "alice"

    def test_free_file_names(self, tmp_path, monkeypatch):
        from sentrix.web import routes
        monkeypatch.setattr(routes, "FACES_DIR", tmp_path)
        assert routes._enrol_filename("alice") == "alice.jpg"
        (tmp_path / "alice.jpg").write_bytes(b"x")
        assert routes._enrol_filename("alice") == "alice__2.jpg"
        assert routes._enrol_filename("../evil name") == "evil_name.jpg"


class TestAudit:
    def test_gallery_changes_are_logged_and_skipped_by_loaders(self, tmp_path):
        from sentrix.pipeline.events import EventLog, load_dataset
        log = EventLog(directory=tmp_path, source="test")
        log.audit("gallery_enrolled", person="alice", file="alice.jpg", via="camera")
        rows = [json.loads(r) for r in log.path.read_text(encoding="utf-8").splitlines()]
        assert rows[0]["event"] == "gallery_enrolled" and rows[0]["person"] == "alice"
        assert len(load_dataset(tmp_path)) == 0
