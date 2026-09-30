"""Regression tests for the scene classifier and its route into the TCI.

Since M1 the classifier only reports probabilities; what they are worth is the
evidence layer's decision (tests/test_evidence.py).
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from sentrix.config import settings
from sentrix.fusion.features import FeatureVector
from sentrix.fusion.tci import TCIEngine
from sentrix.perception.behaviour import SUSPICIOUS_LABELS, BehaviourEngine
from sentrix.perception.detector import Detection, DetectionResult
from sentrix.perception.scene import (PRIORITY, SCENE_CLASSES, SceneEngine, SceneResult,
                                      decide_label, deployment_thresholds)
from sentrix.pipeline.engine import Pipeline


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
    def test_checkpoint_contract(self, tiny_checkpoint, monkeypatch):
        """fp16 weights, temperature and thresholds all come from the file."""
        monkeypatch.delenv("SENTRIX_SCENE_THRESHOLDS", raising=False)
        engine = SceneEngine(tiny_checkpoint)
        assert engine.available and engine.weights_name == "scene.pt"
        assert engine.temperature == 1.5
        assert engine.thresholds["assault"] == 0.60   # 0.5 raised to the floor

    def test_checkpoint_thresholds_are_raised_to_the_floor(self, tiny_checkpoint,
                                                          monkeypatch):
        """Checkpoint thresholds were tuned at 41 % threat prevalence; live
        they sat at the 92nd percentile of normal ticks (CLAUDE.md §5)."""
        monkeypatch.delenv("SENTRIX_SCENE_THRESHOLDS", raising=False)
        engine = SceneEngine(tiny_checkpoint)
        floors = settings.threat.scene_threshold_floor
        for cls, floor in floors.items():
            assert engine.thresholds[cls] >= floor
        assert engine.threshold_source == "checkpoint raised to floor"

    def test_deployment_calibration_overrides(self, tiny_checkpoint, monkeypatch):
        monkeypatch.setenv("SENTRIX_SCENE_THRESHOLDS", '{"weapon": 0.71, "bogus": 1}')
        assert deployment_thresholds() == {"weapon": 0.71}
        engine = SceneEngine(tiny_checkpoint)
        assert engine.thresholds["weapon"] == 0.71
        assert "deployment calibration (weapon)" in engine.threshold_source

    def test_partial_calibration_keeps_the_other_floors(self, tiny_checkpoint, monkeypatch):
        """A weapon-only override used to drop fire back to the checkpoint's
        value, the one behind the live false fire alarms (M1 review F5)."""
        monkeypatch.setenv("SENTRIX_SCENE_THRESHOLDS", '{"weapon": 0.40}')
        engine = SceneEngine(tiny_checkpoint)
        floors = settings.threat.scene_threshold_floor
        assert engine.thresholds["weapon"] == 0.40, "an explicit override may go below"
        for cls in ("fire", "assault", "robbery"):
            assert engine.thresholds[cls] >= floors[cls]

    def test_malformed_calibration_is_ignored(self, monkeypatch):
        monkeypatch.setenv("SENTRIX_SCENE_THRESHOLDS", "{not json")
        assert deployment_thresholds() == {}

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

    def test_decide_reports_probabilities_not_fusion_scores(self, tiny_checkpoint):
        engine = SceneEngine(tiny_checkpoint)
        result = engine.decide(np.array([0.05, 0.05, 0.80, 0.05, 0.05]))
        assert result.label == "weapon"
        assert result.probs["weapon"] == pytest.approx(0.80)
        assert result.thresholds == engine.thresholds
        assert set(result.to_dict()) == {"label", "probs", "thresholds", "fresh"}

    def test_missing_weights_disable_the_engine(self, tmp_path):
        assert not SceneEngine(tmp_path / "absent.pt").available


class TestDeploymentThresholdValidation:
    """A typo in a calibration must not silently disable a class (F6)."""

    @pytest.mark.parametrize("value", [0, 1, 55, -0.2, "x"])
    def test_out_of_range_override_is_rejected(self, value, monkeypatch, tmp_path):
        import json as _json
        import sentrix.perception.scene as scene_mod
        monkeypatch.setattr(scene_mod, "CALIBRATION_DIR", tmp_path)
        monkeypatch.setenv("SENTRIX_SCENE_THRESHOLDS", _json.dumps({"weapon": value,
                                                                     "fire": 0.6}))
        assert scene_mod.deployment_thresholds() == {"fire": 0.6}


class TestSourceAssembly:
    """What the pipeline hands the evidence layer each tick."""

    @staticmethod
    def sources(weapons=(), fires=(), scene=None, violence=None):
        fake = SimpleNamespace(cfg=settings)
        detections = DetectionResult(weapons=list(weapons), fires=list(fires))
        return Pipeline._sources(fake, detections, scene, violence)

    @staticmethod
    def box(label, conf):
        return Detection(label=label, confidence=conf, bbox=(0, 0, 10, 10))

    def test_smoke_is_not_fire(self):
        """Public-data smoke heads read a backlit webcam as smoke (D1)."""
        src = self.sources(fires=[self.box("smoke", 0.9)])
        assert src["detector"]["fire"][0] == 0.0
        src = self.sources(fires=[self.box("smoke", 0.9), self.box("fire", 0.6)])
        assert src["detector"]["fire"][0] == 0.6

    def test_stick_boxes_do_not_vote(self):
        """``stick`` is experimental (D1, test mAP50 0.003): drawn, never a vote."""
        src = self.sources(weapons=[self.box("stick", 0.9)])
        assert src["detector"]["weapon"][0] == 0.0
        src = self.sources(weapons=[self.box("stick", 0.9), self.box("pistol", 0.6)])
        assert src["detector"]["weapon"][0] == 0.6

    def test_detector_thresholds_are_the_detection_floors(self):
        src = self.sources(weapons=[self.box("pistol", 0.7)])
        assert src["detector"]["weapon"] == (0.7, settings.detection.weapon_conf, True)
        assert src["detector"]["fire"][1] == settings.detection.fire_conf

    def test_scene_and_violence_votes(self):
        scene = SceneResult(probs={c: 0.2 for c in SCENE_CLASSES},
                            thresholds={c: 0.5 for c in PRIORITY})
        src = self.sources(scene=scene, violence=(0.7, 0.4, True))
        assert set(src["scene"]) == set(PRIORITY)
        assert src["scene"]["weapon"] == (0.2, 0.5, True)
        assert src["violence"] == {"assault": (0.7, 0.4, True)}

    def test_cached_scene_result_is_marked_stale(self):
        """A failed or skipped inference returns the last verdict, not fresh."""
        engine = SceneEngine.__new__(SceneEngine)
        engine._last = SceneResult(probs={"weapon": 0.7}, thresholds={"weapon": 0.5})
        engine._model = None
        stale = engine.observe(np.zeros((8, 8, 3), np.uint8), 1.0)
        assert stale.fresh is False and stale.probs == {"weapon": 0.7}
        src = self.sources(scene=stale)
        assert src["scene"]["weapon"] == (0.7, 0.5, False)


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

    def test_robbery_is_experimental_and_stays_in_l2(self):
        """Robbery exists as one event in one video (D1): never above L2."""
        features = FeatureVector(behaviour=0.95, behaviour_label="robbery")
        result = TCIEngine().compute(features, now=0.0)
        assert result.raw_tci <= settings.threat.experimental_cap < settings.policy.thresholds[1]

    def test_weapon_outranks_robbery(self):
        features = FeatureVector(weapon=0.6, behaviour=0.85, behaviour_label="robbery")
        assert TCIEngine().compute(features, now=0.0).incident_type == "weapon"


class TestDashboardPayload:
    def test_scene_verdict_reaches_the_websocket(self):
        """The dashboard's Scene Classifier card reads ``scene`` from the
        websocket payload; it must survive serialisation."""
        import json
        from sentrix.pipeline.state import SystemState
        live = SystemState()
        live.update(scene=SceneResult(probs={"normal": 0.2, "weapon": 0.8},
                                      label="weapon").to_dict())
        payload = json.loads(live.ws_payload())
        assert payload["scene"]["label"] == "weapon"
        assert payload["scene"]["probs"]["weapon"] == 0.8


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
