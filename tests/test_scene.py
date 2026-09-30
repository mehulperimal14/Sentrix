"""Regression tests for the scene classifier and its route into the TCI."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from sentrix.config import settings
from sentrix.fusion.features import FeatureVector
from sentrix.fusion.tci import TCIEngine
from sentrix.perception.behaviour import SUSPICIOUS_LABELS, BehaviourEngine
from sentrix.perception.scene import (PRIORITY, SCENE_CLASSES, SUB_THRESHOLD_CEILING,
                                      SceneEngine, SceneResult, align, decide_label)
from sentrix.pipeline.engine import Pipeline


class TestAlignment:
    """A probability at the class threshold must land exactly on the fusion
    override, so the classifier's decision point and the pipeline's action
    point are the same."""

    def test_threshold_maps_to_the_override(self):
        anchor = settings.fusion.weapon_override_high
        assert align(0.62, 0.62, anchor) == pytest.approx(anchor)

    def test_certainty_maps_to_one(self):
        assert align(1.0, 0.4, 0.7) == pytest.approx(1.0)

    def test_below_threshold_never_types_an_incident(self):
        """TCIEngine types an incident above 0.3; sub-threshold must stay under."""
        for p in np.linspace(0.0, 0.599, 50):
            assert align(p, 0.6, 0.55) < SUB_THRESHOLD_CEILING

    def test_monotonic(self):
        values = [align(p, 0.45, 0.55) for p in np.linspace(0, 1, 101)]
        assert all(b >= a for a, b in zip(values, values[1:]))


class TestDecision:
    def test_priority_follows_override_order(self):
        thr = {c: 0.3 for c in PRIORITY}
        probs = {"normal": 0.0, "fire": 0.35, "weapon": 0.35, "assault": 0.3}
        assert decide_label(probs, thr) == "fire"

    def test_nothing_above_threshold_is_normal(self):
        thr = {c: 0.5 for c in PRIORITY}
        assert decide_label({"weapon": 0.49, "assault": 0.49}, thr) == "normal"

    def test_class_order_matches_the_dataset(self):
        from sentrix.evaluation.custom_dataset import CLASSES
        assert SCENE_CLASSES == CLASSES


@pytest.fixture(scope="module")
def tiny_checkpoint(tmp_path_factory):
    """A real, self-describing checkpoint with random weights."""
    torch = pytest.importorskip("torch")
    from sentrix.perception.scene import build_scene_model
    net = build_scene_model("resnet18", len(SCENE_CLASSES))
    path = tmp_path_factory.mktemp("scene") / "scene.pt"
    torch.save({"model_state_dict": {k: v.half() for k, v in net.state_dict().items()},
                "architecture": "resnet18", "classes": list(SCENE_CLASSES),
                "image_size": 64, "temperature": 1.5,
                "thresholds": {"fire": 0.4, "weapon": 0.45, "assault": 0.5,
                               "robbery": 0.6}}, path)
    return path


class TestSceneEngine:
    def test_checkpoint_contract(self, tiny_checkpoint):
        """fp16 weights, temperature and thresholds all come from the file."""
        engine = SceneEngine(tiny_checkpoint)
        assert engine.available and engine.weights_name == "scene.pt"
        assert engine.temperature == 1.5
        assert engine.thresholds["weapon"] == 0.45

    def test_probabilities_are_a_distribution(self, tiny_checkpoint):
        engine = SceneEngine(tiny_checkpoint)
        frame = np.random.default_rng(0).integers(0, 255, (48, 64, 3), dtype=np.uint8)
        probs = engine.score_frames([frame])
        assert probs.shape == (1, 5) and probs.sum() == pytest.approx(1.0, abs=1e-4)

    def test_window_forgets_old_frames(self, tiny_checkpoint):
        engine = SceneEngine(tiny_checkpoint)
        frame = np.zeros((48, 64, 3), dtype=np.uint8)
        for t in (0.0, 0.1, 0.2, 5.0):
            engine.observe(frame, t)
        assert len(engine._buffer) == 1
        engine.reset()
        assert not engine._buffer

    def test_decide_maps_onto_fusion_scales(self, tiny_checkpoint):
        engine = SceneEngine(tiny_checkpoint)
        probs = np.array([0.05, 0.05, 0.80, 0.05, 0.05])
        result = engine.decide(probs)
        assert result.label == "weapon"
        assert result.signals["weapon"] >= settings.fusion.weapon_override_high
        assert result.signals["fire"] < SUB_THRESHOLD_CEILING
        assert result.violence[1] == "normal" and result.robbery[1] == "normal"

    def test_missing_weights_disable_the_engine(self, tmp_path):
        assert not SceneEngine(tmp_path / "absent.pt").available


class TestFusionRoute:
    @staticmethod
    def fuse(mode, det_weapon, scene_weapon):
        fake = SimpleNamespace(cfg=SimpleNamespace(scene=SimpleNamespace(fusion=mode)))
        detections = SimpleNamespace(weapon_score=det_weapon, fire_score=0.0)
        scene = SceneResult(signals={"weapon": scene_weapon, "fire": 0.0})
        return Pipeline._fuse(fake, detections, scene)[0]

    def test_max_takes_the_stronger_signal(self):
        assert self.fuse("max", 0.0, 0.8) == 0.8
        assert self.fuse("max", 0.6, 0.1) == 0.6

    def test_scene_mode_ignores_detector(self):
        assert self.fuse("scene", 0.9, 0.1) == 0.1

    def test_detector_mode_ignores_scene(self):
        assert self.fuse("detector", 0.0, 0.9) == 0.0

    def test_no_scene_passes_detector_through(self):
        fake = SimpleNamespace(cfg=SimpleNamespace(scene=SimpleNamespace(fusion="max")))
        det = SimpleNamespace(weapon_score=0.7, fire_score=0.2)
        assert Pipeline._fuse(fake, det, None) == (0.7, 0.2)


class TestRobberyRoute:
    def test_behaviour_reports_robbery(self):
        score, label = BehaviourEngine().classify([], (480, 640, 3), None, 0.0,
                                                  robbery=(0.7, "robbery"))
        assert label == "robbery"
        assert score >= settings.behaviour.scores["robbery"]
        assert "robbery" in SUSPICIOUS_LABELS

    def test_fight_outranks_robbery(self):
        _, label = BehaviourEngine().classify([], (480, 640, 3), (0.95, "fighting"),
                                              0.0, robbery=(0.7, "robbery"))
        assert label == "fighting"

    def test_tci_types_a_robbery_incident(self):
        features = FeatureVector(behaviour=0.85, behaviour_label="robbery",
                                 person_present=True)
        result = TCIEngine().compute(features, now=0.0)
        assert result.incident_type == "robbery"
        assert "theft" in result.reason.lower()

    def test_weapon_outranks_robbery(self):
        features = FeatureVector(weapon=0.6, behaviour=0.85, behaviour_label="robbery")
        assert TCIEngine().compute(features, now=0.0).incident_type == "weapon"


class TestDeltaCheckpoint:
    """A delta checkpoint must reproduce the trained model exactly, and must
    refuse to load onto public weights other than the ones it was trained on."""

    @staticmethod
    def models():
        torch = pytest.importorskip("torch")
        import copy
        torch.manual_seed(0)
        base = torch.nn.Sequential(torch.nn.Linear(4, 4), torch.nn.Linear(4, 2))
        trained = copy.deepcopy(base)
        with torch.no_grad():
            trained[1].weight += 1.0            # only the "head" was trained
        delta = {k: v for k, v in trained.state_dict().items() if k.startswith("1.")}
        return torch, base, trained, delta, copy

    def test_merge_reproduces_the_trained_model(self):
        from sentrix.perception.scene import base_fingerprint, merge_delta
        torch, base, trained, delta, copy = self.models()
        fingerprint = base_fingerprint(base.state_dict(), frozenset(delta))
        fresh = copy.deepcopy(base)             # stands in for the public download
        merge_delta(fresh, delta, fingerprint)
        for key, value in trained.state_dict().items():
            assert torch.equal(fresh.state_dict()[key], value)

    def test_a_different_base_is_refused(self):
        from sentrix.perception.scene import base_fingerprint, merge_delta
        torch, base, _, delta, copy = self.models()
        fingerprint = base_fingerprint(base.state_dict(), frozenset(delta))
        drifted = copy.deepcopy(base)
        with torch.no_grad():
            drifted[0].bias += 1e-3
        with pytest.raises(ValueError, match="fingerprint"):
            merge_delta(drifted, delta, fingerprint)

    def test_fingerprint_ignores_the_delta_tensors(self):
        from sentrix.perception.scene import base_fingerprint
        torch, base, trained, delta, _ = self.models()
        assert (base_fingerprint(base.state_dict(), frozenset(delta))
                == base_fingerprint(trained.state_dict(), frozenset(delta)))
