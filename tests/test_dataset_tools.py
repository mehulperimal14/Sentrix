"""The dataset builder, the label auditor and the metric helpers (2026-11-18).

``tools/build_custom_dataset.py`` must reproduce the original extraction
(sampling per class, quality filters, pHash de-duplication, ambiguous segments
dropped) without touching anything already in the dataset;
``tools/audit_labels.py`` must find broken YOLO labels; the AP / AUROC helpers
of ``tools/eval_models.py`` must give textbook values.
"""

from __future__ import annotations

import csv
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

cv2 = pytest.importorskip("cv2")

import audit_labels  # noqa: E402
import build_custom_dataset as builder  # noqa: E402
import eval_models  # noqa: E402


def make_video(path: Path, seconds: float = 4.0, fps: int = 30) -> None:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (160, 120))
    rng = np.random.default_rng(0)
    for i in range(int(seconds * fps)):
        t = i / fps
        if t < 1.0:
            frame = np.full((120, 160, 3), 128, np.uint8)            # flat: dropped
        else:
            frame = rng.integers(0, 255, (120, 160, 3), dtype=np.uint8)  # all distinct
        writer.write(frame)
    writer.release()


@pytest.fixture
def dataset(tmp_path):
    root = tmp_path / "ds"
    (root / "frames").mkdir(parents=True)
    (root / "videos").mkdir()
    shutil.copy(builder.METADATA_DIR / "run_config.json", root / "run_config.json")
    for name, header in (("frames.csv", builder.FRAMES_HEADER),
                         ("labels.csv", builder.LABELS_HEADER),
                         ("segments.csv", builder.SEGMENTS_HEADER),
                         ("video_split.csv", builder.SPLIT_HEADER),
                         ("videos.csv", ["video_id", "file", "split", "fps", "frames",
                                         "duration_s"])):
        with open(root / name, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh, lineterminator="\n").writerow(header)
    (root / "frames" / "labels.csv").write_text((root / "labels.csv").read_text())
    return root


def write_segments(path: Path, rows: list[tuple]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(builder.SEGMENTS_HEADER)
        for start, end, label, conf in rows:
            w.writerow(["vx", "vx.avi", start, end, label, conf, "test", "train", "n"])


def read(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_builder_samples_filters_and_labels(dataset, tmp_path):
    video = tmp_path / "v.avi"
    make_video(video)
    segs = tmp_path / "s.csv"
    write_segments(segs, [(0.0, 2.0, "normal", 0.9), (2.0, 3.0, "weapon", 0.9),
                          (3.0, 4.0, "assault", 0.5)])
    assert builder.main(["--segments", str(segs), "--video", f"vx={video}",
                         "--root", str(dataset)]) == 0
    frames = read(dataset / "frames.csv")
    reasons = {r["reason"] for r in frames}
    # A flat grey frame has zero Laplacian variance: the blur check takes it.
    assert {"blurry", "ok", "ambiguous_low_confidence"} <= reasons
    times = sorted(float(r["timestamp"]) for r in frames if r["class"] == "weapon")
    assert np.allclose(np.diff(times), 5 / 30, atol=1e-3)            # 6 fps in weapon
    normal_t = sorted(float(r["timestamp"]) for r in frames if r["class"] == "normal")
    assert np.allclose(np.diff(normal_t), 10 / 30, atol=1e-3)        # 3 fps elsewhere
    labels = read(dataset / "labels.csv")
    assert labels and all(r["class_name"] in ("normal", "weapon") for r in labels)
    for r in labels:                                                   # images written
        assert (dataset / "frames" / r["filepath"]).exists()
        assert r["filepath"].startswith(f"train/{builder.CLASS_TO_IDX[r['class_name']]}_")
    assert (dataset / "videos" / "vx.avi").exists()
    assert read(dataset / "videos.csv")[0]["video_id"] == "vx"
    split = read(dataset / "video_split.csv")[0]
    assert split["dominant_class"] == "weapon" and split["review_seconds"] == "1.0"
    assert (dataset / "frames" / "labels.csv").read_text() == (dataset / "labels.csv").read_text()


def test_builder_refuses_an_existing_video_and_replaces_on_request(dataset, tmp_path):
    video = tmp_path / "v.avi"
    make_video(video, seconds=2.0)
    segs = tmp_path / "s.csv"
    write_segments(segs, [(0.0, 2.0, "normal", 0.9)])
    args = ["--segments", str(segs), "--video", f"vx={video}", "--root", str(dataset)]
    builder.main(args)
    with pytest.raises(SystemExit):
        builder.main(args)
    builder.main(args + ["--replace"])
    assert len(read(dataset / "videos.csv")) == 1                     # not doubled
    assert len(read(dataset / "labels.csv")) == len(
        list((dataset / "frames").rglob("vx_*.jpg")))


def test_near_duplicates_are_dropped_against_kept_frames(dataset, tmp_path):
    video = tmp_path / "still.avi"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 30, (160, 120))
    still = np.random.default_rng(1).integers(0, 255, (120, 160, 3), dtype=np.uint8)
    for _ in range(60):
        writer.write(still)
    writer.release()
    segs = tmp_path / "s.csv"
    write_segments(segs, [(0.0, 2.0, "normal", 0.9)])
    builder.main(["--segments", str(segs), "--video", f"vx={video}", "--root", str(dataset)])
    rows = read(dataset / "frames.csv")
    assert sum(r["status"] == "kept" for r in rows) == 1
    assert all(r["duplicate_status"].startswith("duplicate_of:vx_t00000.000")
               for r in rows if r["reason"] == "near_duplicate")


def test_builder_keeps_line_endings(tmp_path):
    path = tmp_path / "t.csv"
    path.write_bytes(b"a,b\r\n1,2\r\n")
    builder.write_csv(path, ["a", "b"], [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}])
    assert path.read_bytes() == b"a,b\r\n1,2\r\n3,4\r\n"


def test_label_audit_finds_broken_yolo_labels(tmp_path):
    root = tmp_path / "yolo"
    for split in ("train", "valid"):
        (root / split / "images").mkdir(parents=True)
        (root / split / "labels").mkdir(parents=True)
    (root / "data.yaml").write_text("names: ['knife', 'pistol']\n")
    img = np.zeros((10, 10, 3), np.uint8)
    for name in ("a", "b", "c"):
        cv2.imwrite(str(root / "train" / "images" / f"{name}.jpg"), img)
    (root / "train" / "labels" / "a.txt").write_text("1 0.5 0.5 0.2 0.2\n")
    (root / "train" / "labels" / "b.txt").write_text("7 0.5 0.5 0.2 0.2\n0 1.5 0.5 0 0.2\n")
    (root / "train" / "labels" / "orphan.txt").write_text("")
    cv2.imwrite(str(root / "valid" / "images" / "a2.jpg"), img)       # same bytes as train
    out = audit_labels.audit_yolo(root)
    train = out["splits"]["train"]
    assert train["images_without_label_file"] == 1                    # c.jpg
    assert train["label_files_without_image"] == 1                    # orphan.txt
    assert train["problems"] == {"class_out_of_range": 1, "coord_outside_0_1": 1,
                                 "zero_size_box": 1}
    assert train["boxes_per_class"] == {"knife": 1, "pistol": 1}
    assert out["byte_identical_images"]["across_splits"]["train~valid"] == 1


def test_the_real_custom_dataset_audits_clean():
    report = audit_labels.audit_custom(audit_labels.METADATA_DIR)
    assert report["problems"] == {}
    assert report["labelled_images"] == sum(
        sum(v.values()) for v in report["images_per_class_split"].values())


def test_average_precision_and_auroc_helpers():
    gts = {0: [[0, 0, 10, 10]], 1: [[0, 0, 10, 10]], 2: []}
    perfect = [(0.9, 0, [0, 0, 10, 10]), (0.8, 1, [0, 0, 10, 10])]
    assert eval_models.average_precision(perfect, gts) == 1.0
    half = [(0.9, 2, [0, 0, 10, 10]), (0.8, 0, [0, 0, 10, 10])]       # FP first, one GT missed
    assert eval_models.average_precision(half, gts) == 0.25
    assert eval_models.average_precision([], {0: []}) is None
    assert eval_models.auroc([0.9, 0.8], [0.1, 0.2]) == 1.0
    assert eval_models.auroc([0.5], [0.5]) == 0.5                     # ties count half
    assert eval_models.auroc([], [0.1]) is None


def test_detector_weights_can_be_pinned_per_role(monkeypatch, tmp_path):
    """SENTRIX_WEAPON_WEIGHTS / SENTRIX_FIRE_WEIGHTS, like SENTRIX_SCENE_WEIGHTS:
    used as given, a missing file is never replaced by the next candidate."""
    from sentrix.perception import detector
    weights = tmp_path / "w.pt"
    weights.write_bytes(b"x")
    monkeypatch.setenv("SENTRIX_WEAPON_WEIGHTS", str(weights))
    assert detector._resolve(detector._WEAPON_WEIGHTS) == weights
    monkeypatch.setenv("SENTRIX_FIRE_WEIGHTS", str(tmp_path / "missing.pt"))
    assert detector._resolve(detector._FIRE_WEIGHTS) is None
    assert detector._resolve(detector._PERSON_WEIGHTS).name == "yolov8n.pt"


def test_relative_weights_overrides_are_taken_from_the_repository(monkeypatch, tmp_path):
    """pi.env writes ``models/v4_custom/...``; a tool started from another
    directory must load that file, not look for it under its own cwd."""
    from sentrix.paths import ROOT_DIR
    from sentrix.perception import detector, scene
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SENTRIX_FIRE_WEIGHTS", "models/v4_custom/fire_detector_custom_v4.pt")
    assert detector._resolve(detector._FIRE_WEIGHTS) ==         ROOT_DIR / "models" / "v4_custom" / "fire_detector_custom_v4.pt"
    monkeypatch.setenv("SENTRIX_SCENE_WEIGHTS", "models/v4_custom/scene_classifier_custom_v4.pt")
    assert scene._resolve() == ROOT_DIR / "models" / "v4_custom" / "scene_classifier_custom_v4.pt"


def test_knife_videos_come_from_segment_notes():
    sys.path.insert(0, str(ROOT / "training" / "custom"))
    import annotate_boxes
    assert "mmknife" in annotate_boxes.knife_videos()
    assert "mmgun" not in annotate_boxes.knife_videos()
    json.dumps(annotate_boxes.PROMPTS[("weapon", "knife")])
