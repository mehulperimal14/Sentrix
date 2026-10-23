"""Runtime backends (torch / onnxruntime / ncnn), box association, the
runtime detector and the identity model pack.

The parity tests export a randomly initialised YOLO11n and a tiny timm scene
model (no download: both are built from their architecture definitions) and
skip when the export toolchain is not installed.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from sentrix.config import settings
from sentrix.runtime import (build_head, configure_threads, export_stem, exported_path,
                             resolve_backend)
from sentrix.runtime.classifier import NcnnClassifier, preprocess, softmax
from sentrix.runtime.detector import RuntimeDetector, plausible
from sentrix.runtime.tracking import (IouTracker, PersonTracker, RegionGrouper,
                                      containing_track, iou, same_region)
from sentrix.runtime.yolo import Box, NcnnHead, PAD_VALUE, decode, letterbox, nms, parse_names


# ── pre/post-processing ───────────────────────────────────────────────────────
class TestLetterbox:
    def test_square_letterbox_keeps_the_aspect(self):
        frame = np.zeros((100, 200, 3), np.uint8)
        blob, ratio, pad = letterbox(frame, 64)
        assert blob.shape == (1, 3, 64, 64) and blob.dtype == np.float32
        assert ratio == pytest.approx(0.32) and pad == (0.0, 16.0)
        assert blob[0, :, 0, 0] == pytest.approx(PAD_VALUE / 255.0)
        assert blob[0, :, 32, 32] == pytest.approx(0.0)

    def test_channels_are_rgb(self):
        frame = np.zeros((64, 64, 3), np.uint8)
        frame[..., 2] = 255                                  # red in BGR
        blob, _, _ = letterbox(frame, 64)
        assert blob[0, 0].mean() == pytest.approx(1.0) and blob[0, 2].mean() == 0.0


class TestDecode:
    @staticmethod
    def output(rows):
        """rows: (cx, cy, w, h, score per class...) in input pixels."""
        return np.asarray(rows, np.float32).T[None]

    def test_boxes_map_back_to_frame_pixels(self):
        frame_shape = (100, 200, 3)
        _, ratio, pad = letterbox(np.zeros(frame_shape, np.uint8), 64)
        out = self.output([[32, 32, 16, 8, 0.1, 0.9]])
        boxes = decode(out, conf=0.25, iou=0.45, ratio=ratio, pad=pad, shape=frame_shape,
                       names={0: "pistol", 1: "knife"})
        assert len(boxes) == 1
        box = boxes[0]
        assert box.label == "knife" and box.cls == 1 and box.conf == pytest.approx(0.9)
        assert box.bbox == (75, 37, 125, 62)

    def test_nms_is_per_class(self):
        out = self.output([[32, 32, 20, 20, 0.9, 0.0], [33, 32, 20, 20, 0.8, 0.0],
                           [32, 32, 20, 20, 0.0, 0.7]])
        boxes = decode(out, conf=0.25, iou=0.45, ratio=1.0, pad=(0.0, 0.0),
                       shape=(64, 64, 3), names={0: "a", 1: "b"})
        assert sorted((b.label, round(b.conf, 2)) for b in boxes) == [("a", 0.9), ("b", 0.7)]

    def test_confidence_is_strictly_above_the_floor(self):
        out = self.output([[32, 32, 20, 20, 0.5]])
        assert decode(out, conf=0.5, iou=0.45, ratio=1.0, pad=(0, 0), shape=(64, 64, 3),
                      names={}) == []

    def test_class_filter_and_max_det(self):
        out = self.output([[8 + 10 * i, 32, 6, 6, 0.9 - 0.01 * i, 0.0] for i in range(5)]
                          + [[32, 50, 6, 6, 0.0, 0.95]])
        kept = decode(out, conf=0.25, iou=0.45, ratio=1.0, pad=(0, 0), shape=(64, 64, 3),
                      names={}, classes=[0], max_det=3)
        assert len(kept) == 3 and all(b.cls == 0 for b in kept)

    def test_nms_greedy(self):
        boxes = np.array([[0, 0, 10, 10], [1, 1, 11, 11], [20, 20, 30, 30]], np.float32)
        assert nms(boxes, np.array([0.9, 0.8, 0.7]), 0.5) == [0, 2]
        assert nms(np.zeros((0, 4)), np.zeros(0), 0.5) == []

    @pytest.mark.parametrize("raw, expected", [
        ("{0: 'person', 1: 'bicycle'}", {0: "person", 1: "bicycle"}),
        (["pistol", "stick"], {0: "pistol", 1: "stick"}),
        ('{"0": "fire"}', {0: "fire"}),
        (None, {}), ("not names", {}),
    ])
    def test_names(self, raw, expected):
        assert parse_names(raw) == expected


# ── backend selection ─────────────────────────────────────────────────────────
class TestSelection:
    def test_export_names(self, tmp_path):
        weights = Path("models/v4_custom/weapon_detector_custom_v4.pt")
        assert export_stem(weights, 320) == "weapon_detector_custom_v4_320"
        assert exported_path(weights, 320, "onnx", tmp_path).name == \
            "weapon_detector_custom_v4_320.onnx"
        assert exported_path(weights, 640, "ncnn", tmp_path).name == \
            "weapon_detector_custom_v4_640_ncnn_model"
        assert exported_path(weights, None, "onnx", tmp_path).name == \
            "weapon_detector_custom_v4.onnx"

    def test_unknown_backends_fall_back_to_torch(self):
        assert resolve_backend("tensorrt") == "torch"
        assert resolve_backend("ONNX") == "onnx"
        assert resolve_backend() == settings.clip.runtime

    def test_no_weights_no_head(self):
        assert build_head(None, 320) is None

    def test_a_missing_export_falls_back_to_torch(self, tmp_path, monkeypatch):
        from sentrix.runtime import yolo
        made = []
        monkeypatch.setattr(yolo, "TorchHead", lambda w, s: made.append((w, s)) or "torch-head")
        head = build_head(tmp_path / "w.pt", 320, backend="onnx", directory=tmp_path)
        assert head == "torch-head" and made == [(tmp_path / "w.pt", 320)]

    def test_an_export_is_preferred(self, tmp_path, monkeypatch):
        from sentrix.runtime import yolo
        (tmp_path / "w_320.onnx").write_bytes(b"x")
        monkeypatch.setattr(yolo, "OnnxHead", lambda path, threads: SimpleNamespace(
            path=path, threads=threads, imgsz=320))
        head = build_head(tmp_path / "w.pt", 320, backend="onnx", threads=3,
                          directory=tmp_path)
        assert head.path == tmp_path / "w_320.onnx" and head.threads == 3

    def test_thread_budget(self):
        import torch
        before = torch.get_num_threads()
        try:
            report = configure_threads(3)
            assert report["threads"] == 3 and torch.get_num_threads() == 3
        finally:
            torch.set_num_threads(before)


# ── association ───────────────────────────────────────────────────────────────
class TestAssociation:
    def test_iou(self):
        assert iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0
        assert iou((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0
        assert iou((0, 0, 10, 10), (5, 0, 15, 10)) == pytest.approx(1 / 3)

    def test_same_region_tolerates_a_flickering_flame(self):
        assert same_region((100, 100, 140, 160), (110, 80, 135, 130))
        small, large = (150, 150, 170, 170), (100, 100, 160, 160)
        assert iou(small, large) < 0.1 and same_region(small, large), "shape changes a lot"
        assert not same_region((100, 100, 120, 120), (130, 100, 150, 120))
        assert not same_region((0, 0, 20, 20), (100, 100, 120, 120))

    def test_containing_track(self):
        persons = [(1, (0, 0, 60, 120)), (2, (100, 0, 160, 120)), (None, (60, 0, 100, 120))]
        assert containing_track((40, 40, 56, 56), persons) == 1
        assert containing_track((55, 40, 70, 56), persons) == 1, "a hand poking out"
        assert containing_track((70, 40, 90, 56), persons) is None, "untracked person"
        assert containing_track((300, 300, 310, 310), persons) is None

    def test_regions_are_persistent_and_forgotten(self):
        grouper = RegionGrouper(forget_seconds=2.0)
        a, b = grouper.assign([(0, 0, 20, 20), (100, 100, 120, 120)], 0.0)
        assert a != b
        assert grouper.assign([(2, 2, 22, 22)], 1.0) == [a]
        assert grouper.assign([(100, 100, 120, 120)], 5.0) != [b], "forgotten after 2 s"

    def test_iou_tracker(self):
        tracker = IouTracker()
        first = tracker.update([(0, 0, 50, 100), (100, 0, 150, 100)], 0.0)
        moved = tracker.update([(104, 0, 154, 100), (4, 0, 54, 100)], 0.5)
        assert moved == [first[1], first[0]]

    def test_bytetrack_ids_follow_a_moving_person(self):
        tracker = PersonTracker()
        ids = [tracker.update([(10 + 3 * i, 10, 60 + 3 * i, 110)], [0.9],
                              np.zeros((120, 200, 3), np.uint8))[0] for i in range(6)]
        assert ids[-1] is not None and len(set(ids[1:])) == 1
        tracker.reset()


# ── runtime detector ──────────────────────────────────────────────────────────
class ScriptHead:
    backend = "fake"
    imgsz = 640

    def __init__(self, boxes) -> None:
        self.boxes = boxes
        self.kwargs = []

    def predict(self, frame, conf, iou=0.45, classes=None, max_det=50):
        self.kwargs.append({"conf": conf, "classes": classes, "max_det": max_det})
        return list(self.boxes)


class TestRuntimeDetector:
    def test_detection_result_like_the_torch_detector(self):
        frame = np.zeros((480, 640, 3), np.uint8)
        person = ScriptHead([Box("person", 0.9, (100, 50, 300, 450), 0),
                             Box("person", 0.9, (0, 0, 640, 480), 0)])   # fills the frame
        weapon = ScriptHead([Box("pistol", 0.8, (200, 200, 240, 230), 0),
                             Box("pistol", 0.9, (1, 1, 3, 3), 0)])        # a speck
        fire = ScriptHead([Box("fire", 0.7, (400, 300, 480, 400), 0)])
        detector = RuntimeDetector(person, weapon, fire)
        result = detector.detect(frame)
        assert [d.bbox for d in result.persons] == [(100, 50, 300, 450)]
        assert result.persons[0].track_id is not None and result.person_ok
        assert [d.label for d in result.weapons] == ["pistol"] and len(result.fires) == 1
        assert person.kwargs[0]["classes"] == [0]
        assert person.kwargs[0]["conf"] == settings.detection.person_conf
        assert weapon.kwargs[0]["conf"] == settings.detection.weapon_conf

    def test_no_person_head_means_person_unknown(self):
        result = RuntimeDetector(None, None, None).detect(np.zeros((48, 64, 3), np.uint8))
        assert result.person_ok is False and not RuntimeDetector().available

    def test_plausibility(self):
        assert plausible((0, 0, 100, 100), (480, 640, 3))
        assert not plausible((0, 0, 640, 480), (480, 640, 3))

    @pytest.mark.parametrize("size", [640, 320])
    def test_clip_object_stage_uses_the_clip_size(self, monkeypatch, size):
        """SENTRIX_CLIP_IMGSZ picks the exported heads of the clip's S1."""
        from dataclasses import replace

        import sentrix.runtime as runtime
        from sentrix.pipeline import clip

        built = []

        def fake_build(cls, imgsz=None, **kwargs):
            built.append(imgsz)
            return RuntimeDetector(ScriptHead([]), None, None)

        monkeypatch.setattr(runtime, "resolve_backend", lambda *a: "onnx")
        monkeypatch.setattr(RuntimeDetector, "build", classmethod(fake_build))
        monkeypatch.setattr(clip, "settings",
                            replace(settings, clip=replace(settings.clip, imgsz=size)))
        assert clip._runtime_detector() is not None and built == [size]


# ── ncnn (the package is optional: a fake module stands in) ──────────────────
class FakeMat:
    def __init__(self, array) -> None:
        self.array = np.asarray(array)


class FakeNet:
    def __init__(self, out) -> None:
        self.opt = SimpleNamespace(num_threads=0, use_vulkan_compute=True)
        self.loaded, self.inputs, self._out = [], [], out

    def load_param(self, path):
        self.loaded.append(path)

    def load_model(self, path):
        self.loaded.append(path)

    def create_extractor(self):
        net = self

        class Extractor:
            def input(self, name, mat):
                net.inputs.append((name, mat.array.shape))

            def extract(self, name):
                return 0, net._out
        return Extractor()


class TestNcnn:
    def test_head(self, tmp_path):
        folder = tmp_path / "w_64_ncnn_model"
        folder.mkdir()
        (folder / "metadata.yaml").write_text("imgsz: [64, 64]\nnames:\n  0: pistol\n")
        out = np.asarray([[32, 32, 16, 16, 0.9]], np.float32).T
        net = FakeNet(out)
        module = SimpleNamespace(Net=lambda: net, Mat=FakeMat)
        head = NcnnHead(folder, threads=3, ncnn_module=module)
        assert head.imgsz == 64 and head.names == {0: "pistol"}
        assert net.opt.num_threads == 3 and net.opt.use_vulkan_compute is False
        boxes = head.predict(np.zeros((64, 64, 3), np.uint8), conf=0.25)
        assert net.inputs == [("in0", (3, 64, 64))]
        assert [b.label for b in boxes] == ["pistol"] and boxes[0].bbox == (24, 24, 40, 40)

    def test_classifier(self, tmp_path):
        net = FakeNet(np.asarray([1.0, 2.0, 3.0], np.float32))
        clf = NcnnClassifier(tmp_path, threads=2,
                             ncnn_module=SimpleNamespace(Net=lambda: net, Mat=FakeMat))
        logits = clf.logits(np.zeros((2, 3, 8, 8), np.float32))
        assert logits.shape == (2, 3) and len(net.inputs) == 2


class TestClassifierHelpers:
    def test_softmax_with_temperature(self):
        probs = softmax(np.array([[0.0, np.log(3.0)]]), temperature=1.0)
        assert probs[0] == pytest.approx([0.25, 0.75])
        flat = softmax(np.array([[0.0, 10.0]]), temperature=1e6)
        assert flat[0] == pytest.approx([0.5, 0.5], abs=1e-4)

    def test_preprocess_matches_torchvision(self):
        torchvision = pytest.importorskip("torchvision")
        import cv2
        from torchvision import transforms
        rng = np.random.default_rng(0)
        frame = rng.integers(0, 255, (120, 160, 3), dtype=np.uint8)
        ref = transforms.Compose([transforms.ToPILImage(), transforms.Resize((32, 32)),
                                  transforms.ToTensor(),
                                  transforms.Normalize((0.485, 0.456, 0.406),
                                                       (0.229, 0.224, 0.225))])(
            cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)).numpy()
        assert np.abs(preprocess([frame], 32)[0] - ref).max() < 1e-5
        assert torchvision


# ── export parity (skips without the toolchain) ───────────────────────────────
@pytest.fixture(scope="module")
def tiny_yolo(tmp_path_factory):
    pytest.importorskip("onnx")
    pytest.importorskip("onnxruntime")
    ultralytics = pytest.importorskip("ultralytics")
    import ultralytics.utils.checks as checks
    checks.AUTOINSTALL = False                       # never pip-install in a test
    folder = tmp_path_factory.mktemp("yolo")
    try:
        model = ultralytics.YOLO("yolo11n.yaml")
        weights = folder / "tiny.pt"
        model.save(str(weights))
        onnx_path = Path(ultralytics.YOLO(str(weights)).export(
            format="onnx", imgsz=64, simplify=False, dynamic=False, verbose=False))
    except Exception as exc:
        pytest.skip(f"ONNX export unavailable: {exc}")
    return weights, onnx_path


class TestExportParity:
    def frames(self):
        rng = np.random.default_rng(1)
        return [rng.integers(0, 255, (48, 80, 3), dtype=np.uint8) for _ in range(3)]

    def test_raw_output_matches_pytorch(self, tiny_yolo):
        import torch
        from ultralytics import YOLO

        from sentrix.runtime.yolo import OnnxHead
        weights, onnx_path = tiny_yolo
        head = OnnxHead(onnx_path, threads=2)
        net = YOLO(str(weights)).model.float().eval()
        for frame in self.frames():
            blob, _, _ = letterbox(frame, head.imgsz)
            with torch.no_grad():
                ref = net(torch.from_numpy(blob))
            ref = (ref[0] if isinstance(ref, (list, tuple)) else ref).numpy()
            assert np.abs(ref - head.raw(blob)).max() < 1e-3

    def test_decoded_boxes_match_ultralytics_on_the_same_export(self, tiny_yolo):
        from ultralytics import YOLO

        from sentrix.runtime.yolo import OnnxHead
        _, onnx_path = tiny_yolo
        head = OnnxHead(onnx_path, threads=2)
        reference = YOLO(str(onnx_path), task="detect")
        assert head.imgsz == 64 and len(head.names) == 80
        for frame in self.frames():
            ours = sorted((b.cls, round(b.conf, 3), b.bbox) for b in
                          head.predict(frame, conf=0.3, max_det=20))
            res = reference.predict(frame, imgsz=64, conf=0.3, max_det=20, verbose=False)[0]
            theirs = sorted((int(b.cls[0]), round(float(b.conf[0]), 3),
                             tuple(int(v) for v in b.xyxy[0])) for b in res.boxes)
            assert len(ours) == len(theirs)
            for a, b in zip(ours, theirs):
                assert a[0] == b[0] and abs(a[1] - b[1]) <= 2e-3
                assert max(abs(x - y) for x, y in zip(a[2], b[2])) <= 1


@pytest.fixture(scope="module")
def tiny_scene(tmp_path_factory):
    torch = pytest.importorskip("torch")
    pytest.importorskip("timm")
    pytest.importorskip("onnxruntime")
    from sentrix.perception.scene import SCENE_CLASSES, build_scene_model
    folder = tmp_path_factory.mktemp("scene")
    torch.manual_seed(0)
    net = build_scene_model("timm:mobilenetv3_small_050", len(SCENE_CLASSES)).eval()
    meta = {"architecture": "timm:mobilenetv3_small_050", "classes": list(SCENE_CLASSES),
            "image_size": 64, "temperature": 1.3, "storage": "full",
            "normalization": {"mean": [0.485, 0.456, 0.406], "std": [0.229, 0.224, 0.225]},
            "thresholds": {"fire": 0.5, "weapon": 0.55, "assault": 0.7, "robbery": 0.7}}
    path = folder / "slim_scene.pt"
    torch.save({**meta, "model_state_dict": net.state_dict()}, path)
    try:
        torch.onnx.export(net, (torch.zeros(1, 3, 64, 64),), str(folder / "slim_scene.onnx"),
                          input_names=["images"], output_names=["logits"], opset_version=17,
                          dynamo=False, dynamic_axes={"images": {0: "batch"},
                                                      "logits": {0: "batch"}})
    except Exception as exc:
        pytest.skip(f"scene ONNX export unavailable: {exc}")
    (folder / "slim_scene_metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    return path


class TestSceneRuntime:
    """A slim scene model (T-MODEL's format: timm checkpoint + ONNX twin +
    metadata) served on onnxruntime gives the probabilities PyTorch gives."""

    def test_onnx_matches_torch(self, tiny_scene, monkeypatch):
        monkeypatch.delenv("SENTRIX_SCENE_THRESHOLDS", raising=False)
        from sentrix.perception.scene import SceneEngine
        onnx_engine = SceneEngine(tiny_scene, runtime="onnx")
        torch_engine = SceneEngine(tiny_scene, runtime="torch")
        assert onnx_engine.available and onnx_engine.runtime == "onnx"
        assert str(onnx_engine.device) == "onnx:cpu" and torch_engine.runtime == "torch"
        assert onnx_engine.thresholds == torch_engine.thresholds
        assert onnx_engine.temperature == pytest.approx(1.3)
        rng = np.random.default_rng(2)
        frames = [rng.integers(0, 255, (90, 120, 3), dtype=np.uint8) for _ in range(3)]
        a, b = onnx_engine.score_frames(frames), torch_engine.score_frames(frames)
        assert a.shape == (3, 5) and np.abs(a - b).max() < 2e-3
        assert onnx_engine.observe(frames[0], 0.0).fresh

    def test_missing_export_falls_back_to_torch(self, tiny_scene, tmp_path):
        import shutil

        from sentrix.perception.scene import SceneEngine
        lone = tmp_path / tiny_scene.name
        shutil.copy(tiny_scene, lone)
        engine = SceneEngine(lone, runtime="onnx")
        assert engine.available and engine.runtime == "torch"

    def test_runtime_env_override(self, monkeypatch):
        from sentrix.perception.scene import scene_runtime
        monkeypatch.setenv("SENTRIX_SCENE_RUNTIME", "onnx")
        assert scene_runtime() == "onnx"
        assert scene_runtime("torch") == "torch"


# ── identity model pack ───────────────────────────────────────────────────────
class TestFaceModel:
    def test_supported_packs(self):
        from sentrix.perception.identity import FACE_MODELS, face_model_name
        assert set(FACE_MODELS) == {"buffalo_l", "buffalo_s"}
        assert face_model_name("buffalo_s") == "buffalo_s"
        assert face_model_name(" Buffalo_S ") == "buffalo_s"
        assert face_model_name("antelope") == "buffalo_l"
        assert face_model_name() == settings.identity.face_model

    @pytest.mark.parametrize("pack", ["buffalo_s", "buffalo_l"])
    def test_the_configured_pack_is_loaded(self, pack, monkeypatch):
        insightface_app = pytest.importorskip("insightface.app")
        import dataclasses

        from sentrix.perception.identity import IdentityEngine
        made = []

        class FakeAnalysis:
            def __init__(self, name, allowed_modules=None, providers=None):
                made.append((name, tuple(allowed_modules)))
                self.models = {}

            def prepare(self, ctx_id, det_size):
                self.det_size = det_size

        monkeypatch.setattr(insightface_app, "FaceAnalysis", FakeAnalysis)
        engine = IdentityEngine.__new__(IdentityEngine)
        engine.cfg = dataclasses.replace(settings.identity, face_model=pack)
        engine.backend, engine._app, engine.model_name = "insightface", None, None
        engine._init_backend()
        assert made == [(pack, tuple(settings.identity.face_modules))]
        assert engine.model_name == pack and engine._app is not None
