"""Add reviewed videos to the SENTRIX custom frame dataset.

The dataset (``training/datasets/sentrix_custom/``) was built once by an
external pipeline whose parameters survive in ``run_config.json``; this tool
re-implements that extraction so new footage lands in exactly the same form
(ROADMAP.md §2.4 listed it as missing):

* **Labels come only from reviewed segments** (``segments.csv`` rows for the
  video: start, end, label, confidence, notes). A frame outside every segment
  is not sampled.
* **Sampling**: 6 frames/s inside ``weapon`` and ``assault`` segments (the
  fast classes), 3 frames/s elsewhere (``fps`` / ``fast_class_fps``).
* **Quality**: dropped as ``blurry`` (Laplacian variance < 12), ``too_dark``
  (mean < 6), ``too_bright`` (mean > 249) or ``flat`` (std < 4).
* **Near-duplicates**: a 64-bit DCT perceptual hash within 2 bits of a frame
  already kept from the same video is dropped (``duplicate_of:<kept frame>``),
  as the original build did (``phash_distance``, ``dedup_scope: video``).
* **Ambiguous segments**: a segment under 0.7 confidence
  (``confidence_threshold``) is labelled but its frames are dropped as
  ``ambiguous_low_confidence``: never trained on, and unscored in replays
  (``sentrix.evaluation.custom_dataset.Timeline``).
* Kept frames are written at the video's own resolution (after its rotation
  tag) to ``frames/<split>/<idx>_<class>/<id>_t<seconds>_f<frame>.jpg`` (JPEG
  95), and every sampled frame - kept or dropped, with its reason - goes to
  ``frames.csv``; kept ones to ``labels.csv`` and ``frames/labels.csv``. The
  video is copied to ``videos/<id><ext>`` and listed in ``videos.csv`` and
  ``video_split.csv``. Boxes are added afterwards by
  ``training/custom/annotate_boxes.py``.

Usage::

    python tools/build_custom_dataset.py --segments reviewed.csv \\
        --video mmgun=CustomVideos/masked_man_with_gun_with_audio.mp4 [--video ...]

``reviewed.csv`` has the columns of ``segments.csv``. A video id already in
the dataset is refused (``--replace`` rebuilds it). The test split stays
``wa02``'s alone unless a video's segments say ``test``.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sentrix.evaluation.custom_dataset import CLASS_TO_IDX, CLASSES, METADATA_DIR  # noqa: E402

FAST_CLASSES = ("weapon", "assault")
FRAMES_HEADER = ["image_path", "class", "class_id", "source_video", "source_video_id",
                 "source_frame", "timestamp", "split", "width", "height", "blur_score",
                 "brightness_score", "duplicate_status", "label_confidence",
                 "annotation_method", "status", "reason"]
LABELS_HEADER = ["filepath", "class_name", "class_id", "split", "source_video",
                 "source_frame", "timestamp_sec", "label_confidence", "annotation_method"]
SEGMENTS_HEADER = ["video_id", "video_path", "start_time", "end_time", "label", "confidence",
                   "annotation_method", "split", "notes"]
SPLIT_HEADER = ["video_id", "video_file", "split", "n_segments", "dominant_class",
                "classes_present", "confident_seconds", "review_seconds"]


def config(root: Path) -> dict:
    return json.loads((root / "run_config.json").read_text(encoding="utf-8"))


def phash(gray: np.ndarray) -> int:
    """64-bit DCT perceptual hash (the ``imagehash.phash`` construction)."""
    import cv2
    small = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    low = cv2.dct(small)[:8, :8].flatten()
    bits = low > np.median(low[1:])
    return int("".join("1" if b else "0" for b in bits), 2)


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def quality(gray: np.ndarray, cfg: dict) -> tuple[float, float, str | None]:
    import cv2
    blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    bright = float(gray.mean())
    if blur < cfg["blur_threshold"]:
        return blur, bright, "blurry"
    if bright < cfg["dark_threshold"]:
        return blur, bright, "too_dark"
    if bright > cfg["bright_threshold"]:
        return blur, bright, "too_bright"
    if float(gray.std()) < cfg["min_std"]:
        return blur, bright, "flat"
    return blur, bright, None


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, header: list[str], rows: list[dict]) -> None:
    """Rewrite a table keeping its own line endings (no whole-file git diffs)."""
    ending = "\n"
    if path.exists():
        with path.open("rb") as fh:
            ending = "\r\n" if fh.readline().endswith(b"\r\n") else "\n"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore",
                                lineterminator=ending)
        writer.writeheader()
        writer.writerows(rows)


def segment_at(segments: list[dict], t: float) -> dict | None:
    for seg in segments:
        if float(seg["start_time"]) <= t < float(seg["end_time"]):
            return seg
    return None


def extract(video_id: str, source: Path, segments: list[dict], root: Path, cfg: dict
            ) -> tuple[list[dict], dict]:
    """Sample, filter and write one video's frames; returns (frames rows, video row)."""
    import cv2
    cap = cv2.VideoCapture(str(source))
    fps = cap.get(cv2.CAP_PROP_FPS)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    steps = {True: max(1, round(fps / cfg["fast_class_fps"])),
             False: max(1, round(fps / cfg["fps"]))}
    file_name = f"{video_id}{source.suffix.lower()}"
    kept_hashes: list[tuple[int, str]] = []
    rows, index = [], 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        t = index / fps
        seg = segment_at(segments, t)
        if seg is not None and index % steps[seg["label"] in FAST_CLASSES] == 0:
            label, split = seg["label"], seg["split"]
            confidence = float(seg["confidence"])
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            blur, bright, bad = quality(gray, cfg)
            name = f"{video_id}_t{t:09.3f}_f{index:06d}.jpg"
            row = {"image_path": "", "class": label, "class_id": CLASS_TO_IDX[label],
                   "source_video": file_name, "source_video_id": video_id,
                   "source_frame": index, "timestamp": round(t, 3), "split": split,
                   "width": frame.shape[1], "height": frame.shape[0],
                   "blur_score": round(blur, 2), "brightness_score": round(bright, 2),
                   "duplicate_status": "unique", "label_confidence": confidence,
                   "annotation_method": seg["annotation_method"], "status": "dropped",
                   "reason": bad or "ok"}
            if bad is None:
                h = phash(gray)
                twin = next((n for kh, n in kept_hashes
                             if hamming(h, kh) <= cfg["phash_distance"]), None)
                if twin is not None:
                    row["duplicate_status"], row["reason"] = f"duplicate_of:{twin}", "near_duplicate"
                elif confidence < cfg["confidence_threshold"]:
                    row["reason"] = "ambiguous_low_confidence"
                else:
                    rel = f"{split}/{CLASS_TO_IDX[label]}_{label}/{name}"
                    out = root / "frames" / rel
                    out.parent.mkdir(parents=True, exist_ok=True)
                    cv2.imwrite(str(out), frame, [cv2.IMWRITE_JPEG_QUALITY, cfg["jpeg_quality"]])
                    kept_hashes.append((h, name))
                    row.update(image_path=rel, status="kept")
            rows.append(row)
        index += 1
    cap.release()
    shutil.copy2(source, root / "videos" / file_name)
    video = {"video_id": video_id, "file": file_name, "split": segments[0]["split"],
             "fps": f"{fps:.2f}", "frames": total, "duration_s": f"{total / fps:.1f}"}
    return rows, video


def split_row(video_id: str, file_name: str, segments: list[dict], cfg: dict) -> dict:
    seconds = defaultdict(float)
    confident = review = 0.0
    for seg in segments:
        span = float(seg["end_time"]) - float(seg["start_time"])
        seconds[seg["label"]] += span
        if float(seg["confidence"]) >= cfg["confidence_threshold"]:
            confident += span
        else:
            review += span
    threats = {c: s for c, s in seconds.items() if c != "normal"}
    dominant = max(threats or seconds, key=(threats or seconds).get)
    present = "|".join(sorted(seconds))
    return {"video_id": video_id, "video_file": file_name, "split": segments[0]["split"],
            "n_segments": len(segments), "dominant_class": dominant,
            "classes_present": present, "confident_seconds": round(confident, 1),
            "review_seconds": round(review, 1)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--segments", type=Path, required=True,
                        help="reviewed segments (segments.csv columns) for the new videos")
    parser.add_argument("--video", action="append", required=True,
                        help="ID=PATH of a video whose segments are in --segments")
    parser.add_argument("--root", type=Path, default=METADATA_DIR)
    parser.add_argument("--replace", action="store_true", help="rebuild ids already present")
    args = parser.parse_args(argv)

    root = args.root
    cfg = config(root)
    reviewed = read_csv(args.segments)
    videos = dict(v.split("=", 1) for v in args.video)
    for row in reviewed:
        if row["label"] not in CLASSES:
            raise SystemExit(f"unknown label {row['label']!r} in {args.segments}")
    present = {r["video_id"] for r in read_csv(root / "videos.csv")}
    clash = sorted(set(videos) & present)
    if clash and not args.replace:
        raise SystemExit(f"already in the dataset: {clash} (use --replace)")

    frames = [r for r in read_csv(root / "frames.csv") if r["source_video_id"] not in videos]
    labels = [r for r in read_csv(root / "labels.csv")
              if Path(r["source_video"]).stem not in videos]
    segments = [r for r in read_csv(root / "segments.csv") if r["video_id"] not in videos]
    video_rows = [r for r in read_csv(root / "videos.csv") if r["video_id"] not in videos]
    split_rows = [r for r in read_csv(root / "video_split.csv") if r["video_id"] not in videos]
    if clash:
        for image in (root / "frames").rglob("*.jpg"):
            if image.name.split("_t")[0] in clash:
                image.unlink()

    for video_id, path in videos.items():
        own = sorted((r for r in reviewed if r["video_id"] == video_id),
                     key=lambda r: float(r["start_time"]))
        if not own:
            raise SystemExit(f"no reviewed segments for {video_id}")
        rows, video = extract(video_id, Path(path), own, root, cfg)
        frames += rows
        for r in rows:
            if r["status"] == "kept":
                labels.append({"filepath": r["image_path"], "class_name": r["class"],
                               "class_id": r["class_id"], "split": r["split"],
                               "source_video": r["source_video"],
                               "source_frame": r["source_frame"],
                               "timestamp_sec": r["timestamp"],
                               "label_confidence": r["label_confidence"],
                               "annotation_method": r["annotation_method"]})
        segments += [dict(r, video_path=video["file"]) for r in own]
        video_rows.append(video)
        split_rows.append(split_row(video_id, video["file"], own, cfg))
        kept = sum(r["status"] == "kept" for r in rows)
        reasons = defaultdict(int)
        for r in rows:
            reasons[r["reason"]] += 1
        print(f"{video_id:10s} {len(rows):4d} sampled, {kept:4d} kept  {dict(reasons)}")

    write_csv(root / "frames.csv", FRAMES_HEADER, frames)
    write_csv(root / "labels.csv", LABELS_HEADER, labels)
    write_csv(root / "frames" / "labels.csv", LABELS_HEADER, labels)
    write_csv(root / "segments.csv", SEGMENTS_HEADER, segments)
    write_csv(root / "videos.csv", ["video_id", "file", "split", "fps", "frames", "duration_s"],
              video_rows)
    write_csv(root / "video_split.csv", SPLIT_HEADER, split_rows)
    counts = defaultdict(lambda: defaultdict(int))
    for r in labels:
        counts[r["class_name"]][r["split"]] += 1
    print("kept frames by class and split:")
    for c in CLASSES:
        print(f"  {c:8s} " + "  ".join(f"{s} {counts[c][s]:4d}" for s in ("train", "val", "test")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
