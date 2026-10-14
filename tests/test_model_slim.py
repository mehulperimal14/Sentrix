"""T-MODEL: the slim scene classifiers and the training tooling behind them.

The trainer (``training/custom/train_scene_classifier.py``) gained what the
Raspberry Pi heads needed: stage freezing for timm CNNs with their BatchNorm
statistics frozen, a delta checkpoint that is still exact for a CNN, and
distillation from a teacher retrained on the student's training frames. These
tests pin that, and that the committed ``models/v5_slim`` files are what
``SceneEngine`` and an ONNX runtime expect (docs/7 §2.4).

Every network here is built without pretrained weights: the test session has
no network access.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("timm")
import torch.nn as nn  # noqa: E402

from sentrix.perception.scene import (PRIORITY, SCENE_CLASSES,  # noqa: E402
                                      SceneEngine, build_scene_model, merge_delta)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "training" / "custom"))

import train_scene_classifier as tsc  # noqa: E402

SLIM_DIR = ROOT / "models" / "v5_slim"
V4_METADATA = ROOT / "models" / "v4_custom" / "scene_classifier_custom_v4_metadata.json"
SLIM_ARCHITECTURES = ("timm:efficientnet_b0", "timm:mobilenetv3_large_100")


def seeded(architecture: str, size: int = 32, seed: int = 0) -> nn.Module:
    """A randomly initialised network that is the same every time (a stand-in
    for the public pretrained weights, which the tests cannot download)."""
    torch.manual_seed(seed)
    return build_scene_model(architecture, len(SCENE_CLASSES), pretrained=False,
                             image_size=size)


def trainable(net: nn.Module) -> set[str]:
    return {n for n, p in net.named_parameters() if p.requires_grad}


def under(net: nn.Module, prefixes: tuple[str, ...]) -> set[str]:
    return {n for n, _ in net.named_parameters() if n.startswith(prefixes)}


# ── freezing ──────────────────────────────────────────────────────────────────
class TestFreeze:
    @pytest.mark.parametrize("architecture", SLIM_ARCHITECTURES)
    def test_cnn_trains_its_last_stages_with_the_head_conv(self, architecture):
        """The 1x1 head convolution reads the last stage; freezing it with the
        stem would leave the classifier on features nothing can adapt."""
        net = seeded(architecture)
        tsc.freeze(net, 3)
        stages = len(net.blocks)
        expected = under(net, tuple(f"blocks.{i}." for i in range(stages - 3, stages))
                         + ("conv_head.", "bn2.", "norm_head.", "classifier."))
        assert trainable(net) == expected
        assert not trainable(net) & under(net, ("conv_stem.", "bn1.", "blocks.0.",
                                                f"blocks.{stages - 4}."))

    def test_head_only(self):
        net = seeded("timm:efficientnet_b0")
        tsc.freeze(net, 0)
        assert trainable(net) == under(net, ("classifier.",))

    def test_vit_selection_is_unchanged(self):
        """The rule DINOv2 ``dino_b_last4`` was trained with: last N blocks,
        the final norm and the head - and nothing else."""
        net = seeded("timm:vit_tiny_patch16_224")
        tsc.freeze(net, 2)
        n = len(net.blocks)
        assert trainable(net) == under(net, (f"blocks.{n - 2}.", f"blocks.{n - 1}.",
                                             "norm.", "head."))

    def test_no_freezing_by_default(self):
        net = seeded("timm:mobilenetv3_large_100")
        tsc.freeze(net, None)
        assert trainable(net) == {n for n, _ in net.named_parameters()}

    def test_a_model_without_blocks_is_refused(self):
        net = build_scene_model("resnet18", len(SCENE_CLASSES))
        with pytest.raises(SystemExit):
            tsc.freeze(net, 2)

    def test_post_block_modules_follow_forward_order(self):
        net = seeded("timm:efficientnet_b0")
        assert tsc.post_block_modules(net) == [net.conv_head, net.bn2, net.global_pool,
                                               net.classifier]
        assert tsc.post_block_modules(build_scene_model("resnet18", 5)) == []


class TestFrozenBatchNorm:
    @staticmethod
    def batchnorms(net):
        return {n: m for n, m in net.named_modules()
                if isinstance(m, nn.modules.batchnorm._BatchNorm)}

    def test_frozen_stages_keep_their_statistics(self):
        net = seeded("timm:efficientnet_b0")
        tsc.freeze(net, 2)
        before = {n: m.running_mean.clone() for n, m in self.batchnorms(net).items()}
        tsc.train_mode(net)
        net(torch.randn(4, 3, 32, 32))
        for name, module in self.batchnorms(net).items():
            moved = not torch.equal(before[name], module.running_mean)
            frozen = not any(p.requires_grad for p in module.parameters(recurse=False))
            assert module.training is not frozen, name
            assert moved is not frozen, name

    def test_full_training_keeps_every_batchnorm_training(self):
        net = seeded("timm:mobilenetv3_large_100")
        tsc.freeze(net, None)
        tsc.train_mode(net)
        assert all(m.training for m in self.batchnorms(net).values())


# ── delta checkpoints ─────────────────────────────────────────────────────────
class TestDelta:
    @staticmethod
    def trained(architecture: str, last_blocks: int):
        base = seeded(architecture)
        net = copy.deepcopy(base)
        tsc.freeze(net, last_blocks)
        opt = torch.optim.SGD([p for p in net.parameters() if p.requires_grad], lr=0.1)
        tsc.train_mode(net)
        for _ in range(3):
            loss = net(torch.randn(4, 3, 32, 32)).logsumexp(1).mean()
            opt.zero_grad()
            loss.backward()
            opt.step()
        return base, net.eval()

    def test_cnn_delta_reproduces_the_trained_network(self):
        base, net = self.trained("timm:efficientnet_b0", 2)
        state, fingerprint = tsc.delta_state(net)
        # the BatchNorm statistics of the stages that trained are part of it
        assert "bn2.running_mean" in state and "bn2.running_var" in state
        assert not any(k.startswith(("conv_stem.", "bn1.", "blocks.0.")) for k in state)
        rebuilt = copy.deepcopy(base)
        merge_delta(rebuilt, state, fingerprint)
        x = torch.randn(2, 3, 32, 32)
        with torch.no_grad():
            assert torch.allclose(rebuilt.eval()(x), net(x), atol=1e-6)

    def test_delta_refuses_a_different_base(self):
        base, net = self.trained("timm:mobilenetv3_large_100", 2)
        state, fingerprint = tsc.delta_state(net)
        other = copy.deepcopy(base)
        with torch.no_grad():
            other.conv_stem.weight.add_(1e-3)
        with pytest.raises(ValueError, match="fingerprint"):
            merge_delta(other, state, fingerprint)

    def test_vit_delta_keys_are_the_shipped_ones(self):
        """The rule must not change the DINOv2 delta format (docs/6 §6.6)."""
        net = seeded("timm:vit_tiny_patch16_224")
        tsc.freeze(net, 2)
        state, _ = tsc.delta_state(net)
        assert set(state) == trainable(net) | {"pos_embed"}


# ── distillation ──────────────────────────────────────────────────────────────
class TestDistillation:
    def test_loss_is_zero_only_when_the_student_matches(self):
        t = torch.randn(6, 5)
        assert float(tsc.distillation_loss(t.clone(), t, 2.0)) == pytest.approx(0.0, abs=1e-6)
        assert float(tsc.distillation_loss(torch.randn(6, 5), t, 2.0)) > 0

    def test_loss_is_temperature_squared_kl(self):
        s, t, temp = torch.randn(4, 5), torch.randn(4, 5), 3.0
        p_t = torch.softmax(t / temp, 1)
        kl = (p_t * (p_t.log() - torch.log_softmax(s / temp, 1))).sum(1).mean()
        assert float(tsc.distillation_loss(s, t, temp)) == pytest.approx(
            float(kl) * temp ** 2, rel=1e-5)

    def test_no_mixup_draws_no_random_numbers(self):
        """``--mixup 0`` must reproduce the runs made before mixup existed."""
        x, y = torch.randn(4, 3, 8, 8), torch.arange(4)
        state = np.random.get_state()[1].copy()
        out, partner, lam = tsc.mixup_batch(x, y, 0.0)
        assert out is x and partner is y and lam == 1.0
        assert np.array_equal(np.random.get_state()[1], state)

    def test_mixup_mixes_each_image_with_its_partner(self):
        np.random.seed(0)
        torch.manual_seed(0)
        x, y = torch.randn(8, 3, 4, 4), torch.arange(8)
        out, partner, lam = tsc.mixup_batch(x, y, 1.0)
        assert 0.0 <= lam <= 1.0
        assert torch.allclose(out, lam * x + (1 - lam) * x[partner])

    def test_resize_batch_matches_the_teacher_input(self):
        x = torch.randn(2, 3, 32, 32)
        assert tsc.resize_batch(x, 32) is x
        assert tsc.resize_batch(x, 48).shape == (2, 3, 48, 48)

    def test_teacher_key_covers_recipe_and_frames(self):
        recipe = argparse.Namespace(**{k: None for k in tsc.RECIPE_KEYS})
        fold_a = [(Path("frames/train/a.jpg"), 0), (Path("frames/train/b.jpg"), 2)]
        fold_b = fold_a[:1]
        assert tsc.teacher_key(recipe, fold_a) == tsc.teacher_key(recipe, fold_a[::-1])
        assert tsc.teacher_key(recipe, fold_a) != tsc.teacher_key(recipe, fold_b)
        changed = argparse.Namespace(**{**vars(recipe), "epochs": 3})
        assert tsc.teacher_key(recipe, fold_a) != tsc.teacher_key(changed, fold_a)

    def test_teacher_recipe_refuses_a_distilled_teacher(self, tmp_path, monkeypatch):
        run = tmp_path / "scene_cv" / "kd_run"
        run.mkdir(parents=True)
        (run / "summary.json").write_text(json.dumps({"config": {"teacher": "x"}}))
        monkeypatch.setattr(tsc, "RUNS", tmp_path)
        with pytest.raises(SystemExit):
            tsc.teacher_recipe("kd_run")
        with pytest.raises(SystemExit):
            tsc.teacher_recipe("absent")

    def test_old_cv_summaries_describe_their_recipe(self):
        """``dino_b_last4`` predates distillation; its recipe still loads."""
        recipe = tsc.teacher_recipe("dino_b_last4")
        assert recipe.arch == "timm:vit_base_patch14_dinov2.lvd142m"
        assert recipe.train_last_blocks == 4 and recipe.mixup == 0.0
        assert recipe.teacher is None


@pytest.fixture()
def tiny_images(tmp_path):
    """Eight labelled 40x40 images on disk, as ``ImageSet`` reads them."""
    from PIL import Image
    rng = np.random.default_rng(0)
    items = []
    for i in range(8):
        path = tmp_path / "frames" / f"img_{i}.png"
        path.parent.mkdir(exist_ok=True)
        Image.fromarray(rng.integers(0, 255, (40, 40, 3), dtype=np.uint8)).save(path)
        items.append((path, i % len(SCENE_CLASSES)))
    return items


def recipe(arch: str, **overrides) -> argparse.Namespace:
    values = {"arch": arch, "init": "pretrained", "size": 32, "epochs": 1, "batch": 4,
              "lr": 1e-3, "head_lr_mult": 10.0, "weight_decay": 0.05,
              "label_smoothing": 0.1, "rehearsal": 0, "rehearsal_share": 0.0,
              "train_last_blocks": None, "seed": 1, "verbose": False, "tag": "tiny",
              **tsc.RECIPE_DEFAULTS}
    values.update(overrides)
    return argparse.Namespace(**values)


class TestTraining:
    @pytest.fixture(autouse=True)
    def offline_build(self, monkeypatch, tmp_path):
        """``--init pretrained`` without the download, and a private cache."""
        monkeypatch.setattr(tsc, "build", lambda arch, init, size=224: seeded(arch, size))
        monkeypatch.setattr(tsc, "TEACHER_CACHE", tmp_path / "teachers")

    def test_teacher_is_trained_once_then_loaded(self, tiny_images):
        teacher_recipe = recipe("timm:efficientnet_b0", train_last_blocks=2, tag="t")
        first = tsc.train_teacher(teacher_recipe, tiny_images)
        cached = list((tsc.TEACHER_CACHE).glob("t_*.pt"))
        assert len(cached) == 1
        assert torch.load(cached[0], weights_only=False)["storage"] == "delta"
        second = tsc.train_teacher(teacher_recipe, tiny_images)
        x = torch.randn(2, 3, 32, 32)
        with torch.no_grad():
            assert torch.allclose(first(x), second(x), atol=1e-6)
        # a different training set is a different teacher
        tsc.train_teacher(teacher_recipe, tiny_images[:6])
        assert len(list(tsc.TEACHER_CACHE.glob("t_*.pt"))) == 2

    def test_student_distils_with_mixup(self, tiny_images):
        teacher = tsc.train_teacher(recipe("timm:efficientnet_b0", tag="t"), tiny_images)
        args = recipe("timm:mobilenetv3_large_100", teacher="t", mixup=1.0, kd_weight=0.7)
        start = seeded(args.arch)
        student = tsc.train_model(args, tiny_images, [], teacher=teacher, teacher_size=48)
        moved = [not torch.equal(a, b) for a, b in
                 zip(start.state_dict().values(), student.state_dict().values())
                 if a.is_floating_point()]
        assert any(moved)
        logits = tsc.predict_logits(student, tiny_images, args.size)
        assert logits.shape == (len(tiny_images), len(SCENE_CLASSES))
        assert np.isfinite(logits).all()

    def test_plain_training_is_reproducible(self, tiny_images):
        args = recipe("timm:mobilenetv3_large_100", train_last_blocks=3)
        a = tsc.predict_logits(tsc.train_model(args, tiny_images, []), tiny_images, 32)
        b = tsc.predict_logits(tsc.train_model(args, tiny_images, []), tiny_images, 32)
        assert np.allclose(a, b, atol=1e-5)


class TestCommandLine:
    def test_distillation_options_are_validated(self):
        with pytest.raises(SystemExit):
            tsc.main(["cv", "--tag", "x", "--kd-weight", "1.5"])
        with pytest.raises(SystemExit):
            tsc.main(["cv", "--tag", "x", "--teacher", "x"])

    def test_fit_refuses_a_recipe_that_differs_from_its_cv_run(self):
        """Temperature and thresholds only mean something for the recipe
        they were fitted on (here: ``dino_b_last4`` had no teacher)."""
        with pytest.raises(SystemExit, match="teacher"):
            tsc.main(["fit", "--arch", "timm:vit_base_patch14_dinov2.lvd142m",
                      "--lr", "2e-05", "--train-last-blocks", "4", "--cv-tag",
                      "dino_b_last4", "--teacher", "ft_r18", "--tag", "x",
                      "--out", "unused.pt"])


# ── the committed slim models ─────────────────────────────────────────────────
SLIM_CHECKPOINTS = sorted(SLIM_DIR.glob("*.pt"))
SLIM_ONNX = sorted(SLIM_DIR.glob("*.onnx"))


def metadata_of(path: Path) -> dict:
    return json.loads(path.with_name(path.stem + "_metadata.json").read_text(encoding="utf-8"))


@pytest.mark.skipif(not SLIM_CHECKPOINTS, reason="no slim checkpoints committed")
class TestSlimCheckpoints:
    @pytest.mark.parametrize("path", SLIM_CHECKPOINTS, ids=lambda p: p.name)
    def test_checkpoint_is_self_describing(self, path):
        ckpt = torch.load(path, map_location="cpu", weights_only=False)
        assert ckpt["architecture"] in SLIM_ARCHITECTURES
        assert ckpt["storage"] == "full", "a Pi must not need the public base weights"
        assert tuple(ckpt["classes"]) == SCENE_CLASSES
        assert set(ckpt["thresholds"]) == set(PRIORITY)
        assert all(0.0 < v < 1.0 for v in ckpt["thresholds"].values())
        assert ckpt["temperature"] > 0
        assert ckpt["normalization"] == {"mean": tsc.MEAN, "std": tsc.STD}
        assert {"val", "test"} <= set(ckpt["metrics"])

    @pytest.mark.parametrize("path", SLIM_CHECKPOINTS, ids=lambda p: p.name)
    def test_metadata_has_the_v4_format(self, path):
        meta = metadata_of(path)
        v4 = json.loads(V4_METADATA.read_text(encoding="utf-8"))
        delta_only = {"base_model", "base_fingerprint"}
        assert set(v4) - delta_only <= set(meta)
        assert meta["model_name"] == path.stem
        assert {"cv_oof", "val", "test"} <= set(meta["metrics"])
        ckpt = torch.load(path, map_location="cpu", weights_only=False)
        for key in ("architecture", "classes", "image_size", "temperature", "thresholds"):
            assert meta[key] == ckpt[key], key

    @pytest.mark.parametrize("path", SLIM_CHECKPOINTS, ids=lambda p: p.name)
    def test_scene_engine_serves_it(self, path, monkeypatch):
        monkeypatch.delenv("SENTRIX_SCENE_THRESHOLDS", raising=False)
        engine = SceneEngine(path)
        assert engine.available and engine.weights_name == path.name
        frame = np.random.default_rng(1).integers(0, 255, (480, 640, 3), dtype=np.uint8)
        probs = engine.score_frames([frame])
        assert probs.shape == (1, len(SCENE_CLASSES))
        assert probs.sum() == pytest.approx(1.0, abs=1e-5)


@pytest.mark.skipif(not SLIM_ONNX, reason="no ONNX export committed")
class TestSlimOnnx:
    @pytest.mark.parametrize("path", SLIM_ONNX, ids=lambda p: p.name)
    def test_onnx_reproduces_the_checkpoint(self, path):
        ort = pytest.importorskip("onnxruntime")
        import export_scene_onnx as exporter
        net, ckpt = exporter.load_checkpoint(path.with_suffix(".pt"))
        size = int(ckpt["image_size"])
        x = torch.from_numpy(np.random.default_rng(2).standard_normal(
            (3, 3, size, size)).astype(np.float32))
        with torch.no_grad():
            expected = torch.softmax(net(x) / ckpt["temperature"], 1).numpy()
        session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        logits = session.run(["logits"], {"input": x.numpy()})[0]
        actual = exporter.softmax(logits, ckpt["temperature"])
        assert np.abs(actual - expected).max() < 1e-4

    @pytest.mark.parametrize("path", SLIM_ONNX, ids=lambda p: p.name)
    def test_onnx_carries_the_checkpoint_description(self, path):
        onnx = pytest.importorskip("onnx")
        props = {p.key: p.value for p in onnx.load(str(path)).metadata_props}
        ckpt = torch.load(path.with_suffix(".pt"), map_location="cpu", weights_only=False)
        assert json.loads(props["classes"]) == list(ckpt["classes"])
        assert float(props["temperature"]) == pytest.approx(ckpt["temperature"])
        assert json.loads(props["thresholds"]) == ckpt["thresholds"]
        assert int(props["image_size"]) == ckpt["image_size"]
        source = hashlib.sha256(path.with_suffix(".pt").read_bytes()).hexdigest()
        assert props["source_sha256"] == source

    @pytest.mark.parametrize("path", SLIM_ONNX, ids=lambda p: p.name)
    def test_metadata_records_the_export(self, path):
        record = metadata_of(path)["onnx"]
        assert record["file"] == path.name
        assert record["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert record["parity"]["max_abs_prob_diff"] < 1e-3
        assert record["parity"]["label_agreement"] == 1.0


# ── export, latency and report tooling ────────────────────────────────────────
@pytest.fixture()
def tiny_scene_checkpoint(tmp_path):
    """A full slim-format checkpoint with random weights, next to its metadata."""
    net = seeded("timm:mobilenetv3_large_100", 32)
    path = tmp_path / "scene_tiny.pt"
    ckpt = {"model_state_dict": net.state_dict(), "storage": "full",
            "architecture": "timm:mobilenetv3_large_100", "classes": list(SCENE_CLASSES),
            "image_size": 32, "normalization": {"mean": tsc.MEAN, "std": tsc.STD},
            "input_format": "RGB", "temperature": 1.7,
            "thresholds": {"fire": 0.4, "weapon": 0.45, "assault": 0.85, "robbery": 0.6}}
    torch.save(ckpt, path)
    path.with_name("scene_tiny_metadata.json").write_text(
        json.dumps({k: v for k, v in ckpt.items() if k != "model_state_dict"}))
    return path


class TestOnnxExport:
    def test_export_reproduces_the_checkpoint_on_real_frames(self, tiny_scene_checkpoint):
        pytest.importorskip("onnx")
        pytest.importorskip("onnxruntime")
        import export_scene_onnx as exporter
        assert exporter.main([str(tiny_scene_checkpoint), "--frames", "6"]) == 0
        onnx_path = tiny_scene_checkpoint.with_suffix(".onnx")
        record = metadata_of(tiny_scene_checkpoint)["onnx"]
        assert record["parity"]["frames"] == 6 and record["parity"]["split"] == "train"
        assert record["parity"]["max_abs_prob_diff"] < 1e-4
        assert record["sha256"] == hashlib.sha256(onnx_path.read_bytes()).hexdigest()

    def test_preprocess_is_the_engine_transform(self, tiny_scene_checkpoint):
        """The ONNX input must be what ``SceneEngine`` feeds PyTorch."""
        import cv2
        import export_scene_onnx as exporter
        engine = SceneEngine(tiny_scene_checkpoint)
        bgr = np.random.default_rng(3).integers(0, 255, (48, 64, 3), dtype=np.uint8)
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        ckpt = torch.load(tiny_scene_checkpoint, weights_only=False)
        ours = exporter.preprocess([rgb], ckpt, "pil")[0]
        theirs = engine._transform(rgb).numpy()
        assert np.abs(ours - theirs).max() < 1e-5


class TestLatencyBench:
    def test_summary_statistics(self):
        import bench_scene_latency as bench
        stats = bench.summarise([1.0, 2.0, 3.0, 4.0, 100.0])
        assert stats["n"] == 5 and stats["median_ms"] == 3.0 and stats["min_ms"] == 1.0
        assert 4.0 < stats["p95_ms"] <= 100.0

    def test_rounds_interleave_the_models(self):
        import bench_scene_latency as bench
        order = []
        timers = {"a": lambda: order.append("a"), "b": lambda: order.append("b")}
        samples = bench.interleaved(timers, rounds=2, per_round=2, warmup=1)
        assert order == ["a", "b"] + ["a", "a", "b", "b"] * 2
        assert {k: len(v) for k, v in samples.items()} == {"a": 4, "b": 4}

    def test_graph_and_flops_from_a_checkpoint(self, tiny_scene_checkpoint):
        import bench_scene_latency as bench
        net, info = bench.load_graph(tiny_scene_checkpoint)
        assert info["image_size"] == 32 and info["storage"] == "full"
        assert info["params_m"] > 0 and bench.gflops(net, 32) > 0


class TestSlimReport:
    def test_blocks_are_replaced_and_prose_kept(self):
        import slim_report
        text = ("intro\n<!-- BEGIN lovo -->\nold\n<!-- END lovo -->\nmiddle\n"
                "<!-- BEGIN latency -->\n<!-- END latency -->\nend")
        out = slim_report.fill(text, {"lovo": "| new |", "latency": "fast"})
        assert out == ("intro\n<!-- BEGIN lovo -->\n| new |\n<!-- END lovo -->\nmiddle\n"
                       "<!-- BEGIN latency -->\nfast\n<!-- END latency -->\nend")

    def test_recipe_description(self):
        import slim_report
        assert slim_report.describe({"arch": "timm:efficientnet_b0", "epochs": 15}) == (
            "EfficientNet-B0", "all", "CE, 15 ep")
        kd = {"arch": "timm:mobilenetv3_large_100", "epochs": 40, "train_last_blocks": 3,
              "teacher": "dino_b_last4", "kd_weight": 0.5, "kd_temperature": 2.0,
              "mixup": 1.0}
        assert slim_report.describe(kd) == (
            "MobileNetV3-L", "last 3 stages + head",
            "KD from dino_b_last4 (w 0.5, T 2.0), mixup 1.0, 40 ep")

    def test_auroc_is_class_against_normal(self):
        import slim_report
        labels = ["normal", "weapon", "fire", "normal", "weapon"]
        probs = np.zeros((5, 5))
        probs[:, 2] = [0.1, 0.9, 0.95, 0.2, 0.8]          # p(weapon)
        assert slim_report.auroc(labels, probs, "weapon") == pytest.approx(1.0)
        assert slim_report.auroc(["normal", "normal"], probs[:2], "fire") is None

    def test_served_thresholds_are_raised_to_the_floors(self):
        import slim_report
        from sentrix.config import settings
        floors = settings.threat.scene_threshold_floor
        served = slim_report.served_thresholds({"weapon": 0.30, "fire": 0.95})
        assert served["weapon"] == floors["weapon"] and served["fire"] == 0.95

    def test_the_shipped_reference_reproduces_docs6(self):
        """The generated reference row is the number docs/6 publishes."""
        import slim_report
        table = slim_report.lovo_table(["dino_b_last4"])
        assert "| `dino_b_last4` | DINOv2 ViT-B/14 |" in table and "**0.440**" in table
