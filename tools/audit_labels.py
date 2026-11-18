"""Audit the labels of every training dataset on this machine.

Checks that each dataset's labels are complete and well-formed before anything
is trained or measured on it (what ``tools/audit_splits.py`` does for
train/val leakage, this does for the labels themselves):

* **YOLO detection sets** (``SENTRIX_WEAPON_DATA``, ``SENTRIX_FIRE_DATA``;
  ``data.yaml`` + ``<split>/images`` + ``<split>/labels``): images with no
  label file (background), label files with no image, malformed lines (field
  count, class id outside ``names``, coordinates outside [0, 1], zero-size
  boxes), boxes and images per class, byte-identical images across splits.
* **Folder classification sets** (RWF-2000 frames ``fight``/``nofight``, the
  UCF-Crime DVS frames ``anomalous``/``normal``): images per class, empty
  files, a decode check on a sample, source videos per class (filename prefix).
* **Audio clips** (``audioset_clips/<class>``): files and seconds per class,
  unreadable WAVs, formats.
* **The SENTRIX custom dataset**: every ``labels.csv`` row has its image and
  the image sits in its class folder; no orphan images; every kept
  ``frames.csv`` row is in ``labels.csv``; each video in one split; segments
  sorted, non-overlapping, known labels; ``boxes.csv`` covers every labelled
  image, boxes lie inside their image, box classes are known and boxes only
  sit on ``weapon``/``fire`` frames.

Usage::

    python tools/audit_labels.py [--data G:/Capstone/data] [--out training/runs/audit/<tag>]

Reads files only. Writes ``report.json`` and ``REPORT.md``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import sys
import wave
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sentrix.evaluation.custom_dataset import CLASSES, METADATA_DIR  # noqa: E402

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def data_root(arg: Path | None) -> Path:
    for candidate in (arg, os.getenv("SENTRIX_DATA_DIR"), ROOT / "data",
                      Path("G:/Capstone/data")):
        if candidate and Path(candidate).exists():
            return Path(candidate)
    raise SystemExit("no data directory found (--data)")


def long(path: Path) -> str:
    """Roboflow file names exceed Windows' 260-character limit: use \\\\?\\."""
    text = str(path.resolve())
    return "\\\\?\\" + text if os.name == "nt" and not text.startswith("\\\\?\\") else text


def read_text(path: Path) -> str:
    with open(long(path), encoding="utf-8") as fh:
        return fh.read()


def md5(path: Path) -> str:
    with open(long(path), "rb") as fh:
        return hashlib.md5(fh.read()).hexdigest()


def yolo_names(yaml_path: Path) -> list[str]:
    text = yaml_path.read_text(encoding="utf-8")
    import yaml  # ultralytics depends on PyYAML
    names = yaml.safe_load(text)["names"]
    return list(names.values()) if isinstance(names, dict) else list(names)


def audit_yolo(root: Path) -> dict:
    names = yolo_names(root / "data.yaml")
    out = {"root": str(root), "classes": names, "splits": {}}
    hashes: dict[str, dict[str, str]] = {}
    for split in ("train", "valid", "test"):
        images_dir, labels_dir = root / split / "images", root / split / "labels"
        if not images_dir.exists():
            continue
        images = {p.stem: p for p in images_dir.iterdir() if p.suffix.lower() in IMAGE_EXT}
        labels = {p.stem: p for p in labels_dir.glob("*.txt")} if labels_dir.exists() else {}
        boxes, with_class = Counter(), Counter()
        problems = Counter()
        examples = defaultdict(list)
        empty = 0
        for stem, label in labels.items():
            lines = [ln.split() for ln in read_text(label).splitlines() if ln.strip()]
            if not lines:
                empty += 1
            seen = set()
            for parts in lines:
                if len(parts) != 5:
                    kind = "segment_polygon" if len(parts) > 5 and len(parts) % 2 == 1 \
                        else "bad_field_count"
                    problems[kind] += 1
                    examples[kind].append(stem)
                    if kind == "segment_polygon":
                        cls = int(float(parts[0]))
                        boxes[cls] += 1
                        seen.add(cls)
                    continue
                cls, coords = int(float(parts[0])), [float(v) for v in parts[1:]]
                if not 0 <= cls < len(names):
                    problems["class_out_of_range"] += 1
                    examples["class_out_of_range"].append(stem)
                    continue
                if any(v < 0 or v > 1 for v in coords):
                    problems["coord_outside_0_1"] += 1
                    examples["coord_outside_0_1"].append(stem)
                if coords[2] <= 0 or coords[3] <= 0:
                    problems["zero_size_box"] += 1
                    examples["zero_size_box"].append(stem)
                boxes[cls] += 1
                seen.add(cls)
            for cls in seen:
                with_class[cls] += 1
        hashes[split] = {md5(p): p.name for p in images.values()}
        out["splits"][split] = {
            "images": len(images), "label_files": len(labels),
            "images_without_label_file": len(set(images) - set(labels)),
            "label_files_without_image": len(set(labels) - set(images)),
            "empty_label_files": empty,
            "boxes_per_class": {names[c]: n for c, n in sorted(boxes.items())},
            "images_per_class": {names[c]: n for c, n in sorted(with_class.items())},
            "problems": dict(problems),
            "problem_examples": {k: v[:5] for k, v in examples.items()},
        }
    cross = {}
    splits = list(hashes)
    for i, a in enumerate(splits):
        for b in splits[i + 1:]:
            cross[f"{a}~{b}"] = len(set(hashes[a]) & set(hashes[b]))
    within = {s: sum(n - 1 for n in Counter(md5(p) for p in
                     (root / s / "images").iterdir() if p.suffix.lower() in IMAGE_EXT).values()
                     if n > 1) for s in splits}
    out["byte_identical_images"] = {"across_splits": cross, "within_split_extra_copies": within}
    return out


def decodes(paths: list[Path], sample: int, seed: int = 0) -> int:
    import cv2
    import numpy as np
    rng = random.Random(seed)
    bad = 0
    for p in rng.sample(paths, min(sample, len(paths))):
        with open(long(p), "rb") as fh:
            data = np.frombuffer(fh.read(), np.uint8)
        if data.size == 0 or cv2.imdecode(data, cv2.IMREAD_COLOR) is None:
            bad += 1
    return bad


def audit_folders(root: Path, sample: int) -> dict:
    out = {"root": str(root), "classes": {}}
    for cls_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        files = [p for p in cls_dir.rglob("*") if p.suffix.lower() in IMAGE_EXT]
        prefixes = {p.stem.rsplit("_", 1)[0] for p in files}
        out["classes"][cls_dir.name] = {
            "images": len(files), "empty_files": sum(p.stat().st_size == 0 for p in files),
            "source_groups_by_prefix": len(prefixes),
            "undecodable_in_sample": decodes(files, sample), "sample": min(sample, len(files))}
    return out


def audit_audio(root: Path) -> dict:
    out = {"root": str(root), "classes": {}}
    for cls_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        files = list(cls_dir.iterdir())
        formats = Counter(p.suffix.lower() for p in files)
        seconds, unreadable, rates = 0.0, 0, Counter()
        for p in files:
            if p.suffix.lower() != ".wav":
                continue
            try:
                with wave.open(str(p)) as w:
                    seconds += w.getnframes() / w.getframerate()
                    rates[w.getframerate()] += 1
            except Exception:
                unreadable += 1
        out["classes"][cls_dir.name] = {"files": len(files), "formats": dict(formats),
                                        "wav_seconds": round(seconds, 1),
                                        "unreadable_wav": unreadable,
                                        "sample_rates": dict(rates)}
    return out


def read(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def audit_custom(root: Path) -> dict:
    labels = read(root / "labels.csv")
    frames = read(root / "frames.csv")
    segments = read(root / "segments.csv")
    boxes = {r["image"]: r for r in read(root / "boxes.csv")}
    on_disk = {p.relative_to(root / "frames").as_posix()
               for p in (root / "frames").rglob("*.jpg")
               if "quality_check" not in p.parts}       # contact sheets, not frames
    listed = {r["filepath"] for r in labels}
    problems = Counter()
    examples = defaultdict(list)

    def flag(kind: str, what: str) -> None:
        problems[kind] += 1
        examples[kind].append(what)

    for r in labels:
        if r["filepath"] not in on_disk:
            flag("label_without_image", r["filepath"])
        folder = r["filepath"].split("/")[1] if "/" in r["filepath"] else ""
        if folder != f"{CLASSES.index(r['class_name'])}_{r['class_name']}":
            flag("image_in_wrong_class_folder", r["filepath"])
        if r["filepath"].split("/")[0] != r["split"]:
            flag("image_in_wrong_split_folder", r["filepath"])
    for path in on_disk - listed:
        flag("orphan_image", path)
    kept = {r["image_path"] for r in frames if r["status"] == "kept"}
    for path in kept ^ listed:
        flag("frames_csv_and_labels_csv_disagree", path)
    splits = defaultdict(set)
    for r in labels:
        splits[Path(r["source_video"]).stem].add(r["split"])
    for video, s in splits.items():
        if len(s) > 1:
            flag("video_in_several_splits", video)
    by_video = defaultdict(list)
    for r in segments:
        by_video[r["video_id"]].append(r)
        if r["label"] not in CLASSES:
            flag("segment_unknown_label", f"{r['video_id']} {r['label']}")
        if not 0 <= float(r["confidence"]) <= 1:
            flag("segment_confidence_out_of_range", r["video_id"])
    for video, rows in by_video.items():
        rows.sort(key=lambda r: float(r["start_time"]))
        for a, b in zip(rows, rows[1:]):
            if float(b["start_time"]) < float(a["end_time"]) - 1e-6:
                flag("overlapping_segments", f"{video} {a['start_time']}-{a['end_time']} / "
                                             f"{b['start_time']}-{b['end_time']}")
    for r in labels:
        name = Path(r["filepath"]).name
        b = boxes.get(name)
        if b is None:
            flag("labelled_image_without_boxes_row", name)
            continue
        if b["label"] != r["class_name"]:
            flag("boxes_label_differs", name)
        if b["status"] == "box":
            if b["box_class"] not in ("pistol", "stick", "knife", "fire"):
                flag("unknown_box_class", name)
            if r["class_name"] not in ("weapon", "fire"):
                flag("box_on_non_weapon_fire_frame", name)
            x1, y1, x2, y2, w, h = (float(b[k]) for k in ("x1", "y1", "x2", "y2",
                                                           "width", "height"))
            if not (0 <= x1 < x2 <= w + 1 and 0 <= y1 < y2 <= h + 1):
                flag("box_outside_image", name)
    box_status = Counter((b["label"], b["status"]) for b in boxes.values())
    box_class = Counter(b["box_class"] for b in boxes.values() if b["status"] == "box")
    per = defaultdict(Counter)
    for r in labels:
        per[r["class_name"]][r["split"]] += 1
    return {"root": str(root), "labelled_images": len(labels), "sampled_frames": len(frames),
            "videos": len(splits), "segments": len(segments),
            "images_per_class_split": {c: dict(per[c]) for c in CLASSES},
            "frames_status": dict(Counter(r["reason"] for r in frames)),
            "boxes_status": {f"{k[0]}:{k[1]}": n for k, n in sorted(box_status.items())},
            "boxes_per_class": dict(box_class),
            "problems": dict(problems), "problem_examples": {k: v[:5] for k, v in examples.items()}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data", type=Path, default=None)
    parser.add_argument("--sample", type=int, default=300, help="decode check per class")
    parser.add_argument("--out", type=Path, default=ROOT / "training" / "runs" / "audit" / "labels")
    args = parser.parse_args(argv)
    data = data_root(args.data)
    report = {"data": str(data)}
    report["weapon_data"] = audit_yolo(Path(os.getenv("SENTRIX_WEAPON_DATA") or data / "weapon_data"))
    report["fire_smoke_data"] = audit_yolo(Path(os.getenv("SENTRIX_FIRE_DATA") or data / "fire_smoke_data"))
    report["violence_data"] = audit_folders(Path(os.getenv("SENTRIX_VIOLENCE_DATA")
                                                 or data / "violence_data"), args.sample)
    report["anomaly_data"] = audit_folders(data / "anomaly_data", args.sample)
    report["audio_data"] = audit_audio(data / "audio_data" / "audioset_clips")
    report["sentrix_custom"] = audit_custom(METADATA_DIR)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    lines = ["# Label audit", "", f"Data: `{data}` and `{METADATA_DIR.relative_to(ROOT)}`.", ""]
    for key in ("weapon_data", "fire_smoke_data"):
        d = report[key]
        lines += [f"## {key} ({', '.join(d['classes'])})", "",
                  "| split | images | label files | no label file | empty | boxes per class "
                  "| problems |", "|---|---|---|---|---|---|---|"]
        for s, v in d["splits"].items():
            lines.append(f"| {s} | {v['images']} | {v['label_files']} | "
                         f"{v['images_without_label_file']} | {v['empty_label_files']} | "
                         f"{v['boxes_per_class']} | {v['problems'] or 'none'} |")
        lines += ["", f"Byte-identical images: {d['byte_identical_images']}", ""]
    for key in ("violence_data", "anomaly_data"):
        lines += [f"## {key}", "", "| class | images | source groups | empty | undecodable "
                  "(sample) |", "|---|---|---|---|---|"]
        for c, v in report[key]["classes"].items():
            lines.append(f"| {c} | {v['images']} | {v['source_groups_by_prefix']} | "
                         f"{v['empty_files']} | {v['undecodable_in_sample']} / {v['sample']} |")
        lines.append("")
    lines += ["## audio_data (AudioSet clips)", "", "| class | files | formats | WAV seconds "
              "| unreadable |", "|---|---|---|---|---|"]
    for c, v in report["audio_data"]["classes"].items():
        lines.append(f"| {c} | {v['files']} | {v['formats']} | {v['wav_seconds']} | "
                     f"{v['unreadable_wav']} |")
    cu = report["sentrix_custom"]
    lines += ["", "## SENTRIX custom dataset", "",
              f"{cu['labelled_images']} labelled images from {cu['videos']} videos "
              f"({cu['sampled_frames']} sampled frames, {cu['segments']} segments).", "",
              f"Per class and split: {cu['images_per_class_split']}", "",
              f"Frames by status: {cu['frames_status']}", "",
              f"Boxes: {cu['boxes_per_class']}; by label and status {cu['boxes_status']}", "",
              f"Problems: {cu['problems'] or 'none'}"]
    (args.out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
