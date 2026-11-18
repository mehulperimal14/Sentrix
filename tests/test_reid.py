"""Body re-identification: IntrusionSentry's OSNet, shipped as OSNet-AIN (D22).

Pins the model port (crop, pre-processing, weights, ONNX parity), the
``ReIDEngine`` (backends, bodies with each face check, global ids) and the
identity rule that carries a face-verified resident's verdict to their own
track by body look - and every way it must not: armed mode, a failed face,
the same resident on another track, an ambiguous match, a colour histogram,
stale looks.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import numpy as np
import pytest

from sentrix.config import settings
from sentrix.paths import MODELS_DIR
from sentrix.perception import osnet
from sentrix.perception.identity import (FaceCheck, FaceMatch, IdentityWorker,
                                         TrackIdentity, identity_label)
from sentrix.perception.reid import ReIDEngine

WEIGHTS = MODELS_DIR / "reid" / osnet.WEIGHTS_NAME
needs_weights = pytest.mark.skipif(not WEIGHTS.exists(),
                                   reason="OSNet weights not present (tools/fetch_models.py)")


def unit(*values) -> np.ndarray:
    v = np.zeros(osnet.FEATURE_DIM, np.float32)
    v[: len(values)] = values
    return v / np.linalg.norm(v)


def rotated(base: np.ndarray, cosine: float, axis: int = 100) -> np.ndarray:
    """A unit vector at exactly ``cosine`` to ``base`` (base must be 0 at ``axis``)."""
    other = np.zeros_like(base)
    other[axis] = 1.0
    v = cosine * base + np.sqrt(1 - cosine ** 2) * other
    return v / np.linalg.norm(v)


A = unit(1.0)                    # alice's body
B = unit(0.0, 1.0)               # bob's body
ALICE_FACE = FaceMatch((10, 10, 30, 30), "alice", 0.8)


def reid_cfg(**changes):
    return dataclasses.replace(settings.reid, **changes)


def check(at, persons, faces=(), bodies=(), model="osnet"):
    return FaceCheck(tuple(faces), tuple(persons), at, bodies=tuple(bodies),
                     body_model=model if bodies else None)


BOX1 = (0, 0, 100, 200)          # contains ALICE_FACE
BOX2 = (300, 0, 400, 200)


def verified_then_new_track(ti: TrackIdentity, body=A, hits=2, *, appearance=True,
                            model="osnet"):
    """alice is face-verified on track 1, leaves; track 2 (no face) shows ``body``."""
    ti.update(check(0.0, [(1, BOX1)], [ALICE_FACE], [(1, A)], model), [(1, BOX1)], 0.0)
    result = None
    for k in range(hits):
        at = 2.0 + k
        result = ti.update(check(at, [(2, BOX2)], (), [(2, body)], model), [(2, BOX2)], at,
                           appearance=appearance)
    return result


# ── the model ────────────────────────────────────────────────────────────────
def test_body_crop_pads_a_narrow_box_like_intrusionsentry():
    frame = np.full((400, 400, 3), 7, np.uint8)
    crop = osnet.body_crop(frame, (100, 0, 160, 400))        # 60 x 400: aspect 0.15
    assert crop.shape[0] == 400
    assert crop.shape[1] == 60 + 2 * ((200 - 60) // 2)        # padded towards 0.5
    wide = osnet.body_crop(frame, (0, 0, 300, 400))           # aspect 0.75: untouched
    assert wide.shape[:2] == (400, 300)


def test_body_crop_clips_to_the_frame_and_rejects_empty_boxes():
    frame = np.zeros((100, 100, 3), np.uint8)
    assert osnet.body_crop(frame, (-20, -20, 50, 90)).shape[0] == 90
    assert osnet.body_crop(frame, (120, 0, 150, 50)) is None
    assert osnet.body_crop(frame, (10, 10, 11, 80)) is None


def test_preprocess_matches_torchreid_transforms():
    T = pytest.importorskip("torchvision.transforms")
    crop = np.random.default_rng(0).integers(0, 255, (180, 70, 3), dtype=np.uint8)
    reference = T.Compose([T.Resize((256, 128)), T.ToTensor(),
                           T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])])
    want = reference(T.ToPILImage()(np.ascontiguousarray(crop[:, :, ::-1]))).numpy()
    assert np.abs(osnet.preprocess([crop])[0] - want).max() < 1e-5


def test_both_networks_carry_torchreids_parameter_names():
    pytest.importorskip("torch")
    plain = osnet._build_osnet("osnet_x1_0").state_dict()
    ain = osnet._build_osnet("osnet_ain_x1_0").state_dict()
    assert {"conv2.0.conv2d.3.conv1.weight", "conv2.2.0.conv.weight",
            "conv1.bn.running_mean"} <= set(plain)
    assert {"conv2.0.IN.weight", "conv2.0.conv2.3.layers.3.conv1.weight",
            "pool2.0.conv.weight", "conv3.0.conv3.bn.weight", "conv1.bn.weight"} <= set(ain)
    assert "conv1.bn.running_mean" not in ain            # instance norm: no running stats
    assert "conv2.0.conv3.bn.weight" not in ain          # IN replaces that block's BN
    assert osnet.architecture(ain) == "osnet_ain_x1_0"
    assert osnet.architecture(plain) == "osnet_x1_0"
    with pytest.raises(ValueError):
        osnet._build_osnet("resnet50")


def test_denormal_weights_are_zeroed_on_load(tmp_path):
    """MSMT17-trained checkpoints carry denormals that slowed CPU inference 3-17x."""
    torch = pytest.importorskip("torch")
    weights = tmp_path / "w.pth"
    torch.save({"module.fc.0.weight": torch.tensor([1e-40, 0.5, -2e-39, 0.0]),
                "classifier.weight": torch.ones(2),
                "fc.1.num_batches_tracked": torch.tensor(7)}, weights)
    state = osnet._state_dict(weights)
    assert state["fc.0.weight"].tolist() == [0.0, 0.5, 0.0, 0.0]
    assert "classifier.weight" not in state                     # training head dropped
    assert state["fc.1.num_batches_tracked"].item() == 7          # integers untouched


def test_an_ain_checkpoint_exports_to_onnx_with_parity(tmp_path):
    torch = pytest.importorskip("torch")
    pytest.importorskip("onnxruntime")
    net = osnet._build_osnet("osnet_ain_x1_0").eval()
    weights = tmp_path / "ain.pth"
    torch.save({"module." + k: v for k, v in net.state_dict().items()}, weights)
    assert osnet.export_onnx(weights, tmp_path / "ain.onnx") < 1e-3
    embedder = osnet.BodyEmbedder(weights, threads=1)
    assert embedder.available and embedder.backend == "onnx"
    assert embedder.name == "osnet_ain_x1_0"


def test_missing_weights_leave_the_embedder_unavailable(tmp_path):
    embedder = osnet.BodyEmbedder(tmp_path / "absent.pth")
    assert not embedder.available and embedder.backend == "none"


@needs_weights
def test_weights_load_strictly_and_match_their_checksum():
    import hashlib
    assert hashlib.sha256(WEIGHTS.read_bytes()).hexdigest() == osnet.WEIGHTS_SHA256
    net = osnet.load_torch_osnet(WEIGHTS)          # strict: every name and shape
    assert sum(p.numel() for p in net.parameters()) == 2_169_508


@needs_weights
def test_onnx_and_torch_give_the_same_unit_features():
    crops = [np.random.default_rng(i).integers(0, 255, (220, 90, 3), dtype=np.uint8)
             for i in range(3)]
    onnx = osnet.BodyEmbedder(WEIGHTS, threads=1)
    torch_only = osnet.BodyEmbedder(WEIGHTS, onnx=Path("missing.onnx"))
    assert (onnx.backend, torch_only.backend) == ("onnx", "torch")
    a, b = onnx.embed(crops), torch_only.embed(crops)
    assert a.shape == (3, osnet.FEATURE_DIM)
    assert np.allclose(np.linalg.norm(a, axis=1), 1.0, atol=1e-5)
    assert np.abs(a - b).max() < 1e-4


# ── ReIDEngine ───────────────────────────────────────────────────────────────
class Enrolled:
    enrolled = ["alice"]


class Nobody:
    enrolled: list = []


class FakeEmbedder:
    available = True

    def __init__(self, vectors):
        self.vectors = list(vectors)

    def embed(self, crops):
        return np.stack([self.vectors.pop(0) for _ in crops])


def test_model_off_computes_nothing():
    engine = ReIDEngine(reid_cfg(model="off"))
    frame = np.zeros((200, 200, 3), np.uint8)
    assert not engine.enabled
    assert engine.bodies_for(frame, ((1, (0, 0, 50, 150)),), Enrolled()) == {}
    assert engine.embed(frame) is None


def test_bodies_come_with_a_face_check_only_when_someone_is_enrolled():
    engine = ReIDEngine(reid_cfg(model="histogram"))
    frame = np.random.default_rng(0).integers(0, 255, (200, 200, 3), dtype=np.uint8)
    persons = ((1, (0, 0, 60, 150)), (None, (100, 0, 160, 150)))
    assert engine.bodies_for(frame, persons, Nobody()) == {}
    assert engine.bodies_for(frame, persons, object()) == {}       # eval identity
    got = engine.bodies_for(frame, persons, Enrolled())
    assert got["body_model"] == "histogram"
    assert [t for t, _ in got["bodies"]] == [1]                     # untracked skipped


def test_osnet_backend_uses_its_own_threshold():
    engine = ReIDEngine(reid_cfg(model="auto"), embedder=FakeEmbedder([]))
    assert engine.deep and engine.match_similarity == settings.reid.osnet_match_similarity
    assert ReIDEngine(reid_cfg(model="histogram")).match_similarity == \
        settings.reid.match_similarity


def test_global_ids_fold_each_check_once_and_stay_with_the_track():
    engine = ReIDEngine(reid_cfg(model="auto"), embedder=FakeEmbedder([]))
    first = check(1.0, [(1, BOX1), (2, BOX2)], bodies=[(1, A), (2, B)])
    ids = engine.global_ids(first, 1.0)
    assert ids[1] != ids[2]
    assert engine.global_ids(first, 1.5) == ids                     # same check: no refold
    assert engine.known_count == 2
    again = check(2.0, [(3, BOX1)], bodies=[(3, rotated(A, 0.99))])
    assert engine.global_ids(again, 2.0)[3] == ids[1]               # same body, same id
    engine.reset()
    assert engine.global_ids(FaceCheck(), 3.0) == {}


def test_a_check_without_bodies_keeps_previous_ids():
    engine = ReIDEngine(reid_cfg(model="histogram"))
    ids = engine.global_ids(check(1.0, [(1, BOX1)], bodies=[(1, A)], model="histogram"), 1.0)
    assert engine.global_ids(check(2.0, [(1, BOX1)]), 2.0) == ids


# ── the identity rule (D22) ─────────────────────────────────────────────────
def test_a_verified_residents_body_carries_their_verdict_to_a_new_track():
    result = verified_then_new_track(TrackIdentity(), body=rotated(A, 0.97))
    assert result.tracks[2] == {"state": "authorised", "name": "alice", "via": "appearance"}
    assert result.authorized and result.name == "alice"


def test_one_matching_check_is_not_enough():
    result = verified_then_new_track(TrackIdentity(), hits=settings.reid.identity_hits - 1)
    assert result.tracks[2]["state"] == "unverified"


def test_armed_mode_lets_only_a_face_authorise():
    result = verified_then_new_track(TrackIdentity(), appearance=False)
    assert result.tracks[2]["state"] == "unverified" and not result.authorized


def test_a_failed_face_wins_over_a_matching_body():
    ti = TrackIdentity()
    verified_then_new_track(ti)
    stranger = FaceMatch((310, 10, 330, 30), None, 0.1)            # inside BOX2
    result = ti.update(check(5.0, [(2, BOX2)], [stranger], [(2, A)]), [(2, BOX2)], 5.0)
    assert result.tracks[2]["state"] == "unknown"


def test_a_different_body_stays_unverified():
    low = settings.reid.identity_similarity - 0.1
    result = verified_then_new_track(TrackIdentity(), body=rotated(A, low))
    assert result.tracks[2]["state"] == "unverified"


def test_the_resident_in_view_by_face_is_not_also_another_tracks_body():
    ti = TrackIdentity()
    both = [(1, BOX1), (2, BOX2)]
    for at in (0.0, 1.0, 2.0):
        result = ti.update(check(at, both, [ALICE_FACE], [(1, A), (2, A)]), both, at)
    assert result.tracks[1]["via"] == "face"
    assert result.tracks[2]["state"] == "unverified"               # a look-alike beside her


def test_an_ambiguous_body_between_two_residents_stays_unverified():
    ti = TrackIdentity()
    bob_face = FaceMatch((10, 10, 30, 30), "bob", 0.8)
    alike_a, alike_b = unit(1.0, 0.1), unit(1.0, -0.1)             # two similar outfits
    ti.update(check(0.0, [(1, BOX1)], [ALICE_FACE], [(1, alike_a)]), [(1, BOX1)], 0.0)
    ti.update(check(1.0, [(3, BOX1)], [bob_face], [(3, alike_b)]), [(3, BOX1)], 1.0)
    between = unit(1.0)                                             # 0.995 to both
    assert float(between @ alike_a) > settings.reid.identity_similarity
    for at in (3.0, 4.0):
        result = ti.update(check(at, [(2, BOX2)], (), [(2, between)]), [(2, BOX2)], at)
    assert result.tracks[2]["state"] == "unverified"


def test_colour_histogram_bodies_never_carry_identity():
    result = verified_then_new_track(TrackIdentity(), model="histogram")
    assert result.tracks[2]["state"] == "unverified"


def test_body_identity_can_be_switched_off():
    ti = TrackIdentity(reid_cfg=reid_cfg(identity=False))
    assert verified_then_new_track(ti).tracks[2]["state"] == "unverified"


def test_looks_expire():
    cfg = reid_cfg(identity_memory_seconds=10.0)
    ti = TrackIdentity(reid_cfg=cfg)
    ti.update(check(0.0, [(1, BOX1)], [ALICE_FACE], [(1, A)]), [(1, BOX1)], 0.0)
    for at in (20.0, 21.0):
        result = ti.update(check(at, [(2, BOX2)], (), [(2, A)]), [(2, BOX2)], at)
    assert result.tracks[2]["state"] == "unverified"


def test_a_body_verdict_lapses_without_new_looks():
    ti = TrackIdentity()
    verified_then_new_track(ti)
    later = 3.0 + settings.identity.track_hold_seconds + 1
    result = ti.update(check(later, [(2, BOX2)]), [(2, BOX2)], later)
    assert result.tracks[2]["state"] == "unverified"


def test_reset_forgets_looks():
    ti = TrackIdentity()
    ti.update(check(0.0, [(1, BOX1)], [ALICE_FACE], [(1, A)]), [(1, BOX1)], 0.0)
    ti.reset()
    for at in (2.0, 3.0):
        result = ti.update(check(at, [(2, BOX2)], (), [(2, A)]), [(2, BOX2)], at)
    assert result.tracks[2]["state"] == "unverified"


def test_the_label_says_when_only_the_body_matched():
    assert identity_label({"state": "authorised", "name": "alice", "via": "appearance"}) \
        == "alice (body)"
    assert identity_label({"state": "authorised", "name": "alice", "via": "face"}) == "alice"
    assert identity_label({"state": "unverified", "name": None, "via": None}) == "unverified"


def test_the_recognition_worker_sends_bodies_with_its_faces():
    class Engine:
        available = True
        enrolled = ["alice"]

        def match_faces(self, frame):
            return [ALICE_FACE]

    reid = ReIDEngine(reid_cfg(model="histogram"))
    worker = IdentityWorker(Engine(), reid=reid)
    try:
        frame = np.random.default_rng(1).integers(0, 255, (240, 320, 3), dtype=np.uint8)
        worker.observe(frame, ((1, BOX1),), 1.0)
        import time
        deadline = time.monotonic() + 5.0
        while worker.checks == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        result = worker.observe(frame, ((1, BOX1),), 1.1)
    finally:
        worker.stop()
    assert result.faces == (ALICE_FACE,)
    assert result.body_model == "histogram" and [t for t, _ in result.bodies] == [1]


def test_the_measurement_tool_refuses_the_test_video():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import reid_eval
    assert reid_eval.main(["--videos", "wa02"]) == 2
