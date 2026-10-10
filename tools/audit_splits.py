"""Detect train/validation contamination in image and frame datasets.

Every model in this project was measured against a contaminated split, and the
same mistake in three different shapes:

* **violence** — RWF-2000 was split at *frame* level, so 17.7% of validation
  source videos also appear in training. Clips 0 and 2 of a fight went to train
  while clip 1 of the *same fight* went to validation.
* **weapon** — 12.2% of validation images have a near-duplicate in training
  (cos > 0.99 on 128x128 thumbnails), including one exact pair differing only in
  its Roboflow processing suffix.
* **fire/smoke** — 20.0% of validation images have a near-duplicate in training.

Reported mAP and accuracy are therefore optimistic. This tool re-measures both
kinds of leakage so the numbers can be corrected before they go into a report.

Two modes:

``manifest``
    Group a CSV of frame paths by source video and report split overlap.

``images``
    Perceptual near-duplicate scan between two image directories.

Usage::

    python tools/audit_splits.py manifest \\
        --train training/runs/v3_real/violence/sample_manifest_train.csv \\
        --val   training/runs/v3_real/violence/sample_manifest_val.csv

    python tools/audit_splits.py images \\
        --train data/weapon_data/train/images \\
        --val   data/weapon_data/valid/images
"""

from __future__ import annotations

import argparse
import csv
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

#: Strip a trailing ``_<clip>_<frame>`` so frames collapse onto their source video.
_FRAME_SUFFIX = re.compile(r"_\d+_\d+$")
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def source_id(path: str) -> str:
    """Best-effort source-video identifier for a frame path."""
    name = Path(str(path).replace("\\", "/")).name
    name = re.sub(r"\.(jpe?g|png|bmp|webp)$", "", name, flags=re.I)
    # Roboflow: "<original>_jpg.rf.<hash>"
    name = name.split("_jpg.rf.")[0].split("_png.rf.")[0]
    return _FRAME_SUFFIX.sub("", name)


def load_manifest(path: Path) -> list[str]:
    rows: list[str] = []
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        column = next((c for c in (reader.fieldnames or [])
                       if c and ("path" in c.lower() or "file" in c.lower())), None)
        if column is None:
            raise SystemExit(f"no path-like column in {path}: {reader.fieldnames}")
        for row in reader:
            if row.get(column):
                rows.append(row[column])
    return rows


def audit_manifest(train_csv: Path, val_csv: Path) -> int:
    train_rows, val_rows = load_manifest(train_csv), load_manifest(val_csv)
    train_groups = defaultdict(int)
    val_groups = defaultdict(int)
    for row in train_rows:
        train_groups[source_id(row)] += 1
    for row in val_rows:
        val_groups[source_id(row)] += 1

    shared = set(train_groups) & set(val_groups)
    leaked_rows = sum(val_groups[g] for g in shared)

    print(f"train : {len(train_rows):,} rows from {len(train_groups):,} sources")
    print(f"val   : {len(val_rows):,} rows from {len(val_groups):,} sources")
    print(f"\nsources in BOTH splits : {len(shared):,} "
          f"({100 * len(shared) / max(len(val_groups), 1):.1f}% of val sources)")
    print(f"val rows affected      : {leaked_rows:,} "
          f"({100 * leaked_rows / max(len(val_rows), 1):.1f}% of val rows)")

    if shared:
        print("\nexamples (source: train rows / val rows):")
        for group in sorted(shared, key=lambda g: -val_groups[g])[:8]:
            print(f"  {group[:64]:<66} {train_groups[group]:>4} / {val_groups[group]:>4}")
        print("\nVERDICT: LEAKED. Re-split by source video and re-measure.")
        return 1
    print("\nVERDICT: clean — splits are source-disjoint.")
    return 0


def _thumbnails(directory: Path, sample: int, size: int, seed: int):
    import cv2

    files = [p for p in directory.iterdir() if p.suffix.lower() in _IMAGE_SUFFIXES]
    if not files:
        raise SystemExit(f"no images in {directory}")
    rng = random.Random(seed)
    if sample and len(files) > sample:
        files = rng.sample(files, sample)

    vectors, names = [], []
    for path in files:
        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            continue
        vector = cv2.resize(image, (size, size)).astype(np.float32).ravel()
        vector -= vector.mean()
        norm = float(np.linalg.norm(vector))
        if norm < 1e-6:                     # flat image, no usable signature
            continue
        vectors.append(vector / norm)
        names.append(path.name)
    return np.asarray(vectors), names, len(files)


def audit_images(train_dir: Path, val_dir: Path, sample_train: int,
                 sample_val: int, size: int, seed: int) -> int:
    try:
        import cv2                                          # noqa: F401
    except ImportError:
        raise SystemExit("opencv-python is required for the images mode")

    train_vectors, _, train_total = _thumbnails(train_dir, sample_train, size, seed)
    val_vectors, val_names, val_total = _thumbnails(val_dir, sample_val, size, seed)

    print(f"comparing {len(val_names):,} val images "
          f"against {train_vectors.shape[0]:,} train images "
          f"({size}x{size} mean-centred cosine)")
    if sample_train and train_total > sample_train:
        print(f"NOTE: train subsampled {sample_train:,}/{train_total:,} — "
              "results are a LOWER BOUND on contamination")

    similarity = val_vectors @ train_vectors.T
    best = similarity.max(axis=1)

    print()
    exit_code = 0
    for threshold in (0.99, 0.97, 0.95, 0.90):
        share = float((best > threshold).mean())
        flag = ""
        if threshold == 0.99 and share > 0.01:
            flag, exit_code = "  <-- duplicates", 1
        print(f"  val images with a train match at cos > {threshold:.2f}: "
              f"{share * 100:5.1f}%{flag}")
    print(f"\n  median nearest-neighbour similarity: {np.median(best):.3f}")

    order = np.argsort(-best)[:5]
    print("\nclosest pairs:")
    for i in order:
        print(f"  cos={best[i]:.4f}  val: {val_names[i][:60]}")

    if exit_code:
        print("\nVERDICT: CONTAMINATED. De-duplicate before quoting mAP/accuracy.")
    else:
        print("\nVERDICT: no strong duplicate signal at this sample size.")
    print("\nCaveat: visually homogeneous datasets (fire, night footage) inflate "
          "mid-range similarity. Trust cos > 0.99, treat 0.90-0.95 as a prompt "
          "to inspect rather than a verdict.")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)

    manifest = sub.add_parser("manifest", help="source-video overlap in CSV splits")
    manifest.add_argument("--train", type=Path, required=True)
    manifest.add_argument("--val", type=Path, required=True)

    images = sub.add_parser("images", help="near-duplicate scan between directories")
    images.add_argument("--train", type=Path, required=True)
    images.add_argument("--val", type=Path, required=True)
    images.add_argument("--sample-train", type=int, default=3000)
    images.add_argument("--sample-val", type=int, default=400)
    images.add_argument("--size", type=int, default=128)
    images.add_argument("--seed", type=int, default=0)

    args = parser.parse_args(argv)
    if args.mode == "manifest":
        return audit_manifest(args.train, args.val)
    return audit_images(args.train, args.val, args.sample_train,
                        args.sample_val, args.size, args.seed)


if __name__ == "__main__":
    sys.exit(main())
