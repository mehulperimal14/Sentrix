#!/usr/bin/env python3
"""
prep_dfire_subset.py
====================
Phase 2.5 — Data Restructuring Script (runs on THIS laptop)

Stratified-samples 10,000 images from the D-Fire training set while
preserving the natural class ratio (fire-only / smoke-only / both / background).
Also carves 2,000 images from the test set as validation.

Output:
    /Capstone/3/D-Fire/dfire_subset/
        train/images/   (10,000 images)
        train/labels/   (10,000 YOLO labels)
        valid/images/   (2,000 images)
        valid/labels/   (2,000 labels)
        data.yaml
"""

import os
import random
import shutil
import yaml
from pathlib import Path
from collections import defaultdict

# ── Paths ────────────────────────────────────────────────────────────────────
BASE = Path("/Users/harshit/Desktop/Study material/Capstone/3/D-Fire")
TRAIN_IMG_DIR = BASE / "train" / "images"
TRAIN_LBL_DIR = BASE / "train" / "labels"
TEST_IMG_DIR  = BASE / "test"  / "images"
TEST_LBL_DIR  = BASE / "test"  / "labels"
OUT_DIR       = BASE / "dfire_subset"

TRAIN_SAMPLE_SIZE = 10_000
VALID_SAMPLE_SIZE = 2_000
RANDOM_SEED       = 42

# ── Class IDs (D-Fire YOLO convention) ──────────────────────────────────────
CLASS_FIRE  = 0
CLASS_SMOKE = 1


def categorize_image(label_path: Path) -> str:
    """Return bucket name based on which classes appear in the label file."""
    if not label_path.exists():
        return "background"
    text = label_path.read_text().strip()
    if not text:
        return "background"
    classes_present = set()
    for line in text.splitlines():
        parts = line.strip().split()
        if parts:
            classes_present.add(int(parts[0]))
    if CLASS_FIRE in classes_present and CLASS_SMOKE in classes_present:
        return "both"
    elif CLASS_FIRE in classes_present:
        return "fire_only"
    elif CLASS_SMOKE in classes_present:
        return "smoke_only"
    return "background"


def stratified_sample(img_dir: Path, lbl_dir: Path, n: int, seed: int) -> list:
    """Return a stratified list of n (img_path, lbl_path) tuples."""
    buckets = defaultdict(list)
    img_files = sorted(list(img_dir.glob("*.jpg")) +
                       list(img_dir.glob("*.png")) +
                       list(img_dir.glob("*.JPG")) +
                       list(img_dir.glob("*.jpeg")))
    print(f"  Scanning {len(img_files)} images in {img_dir.name}/...")

    for img_path in img_files:
        lbl_path = lbl_dir / (img_path.stem + ".txt")
        cat = categorize_image(lbl_path)
        buckets[cat].append((img_path, lbl_path))

    print("  Category distribution (before sampling):")
    for cat, items in sorted(buckets.items()):
        pct = 100.0 * len(items) / max(len(img_files), 1)
        print(f"    {cat:15s}: {len(items):6,}  ({pct:.1f}%)")

    total = sum(len(v) for v in buckets.values())
    random.seed(seed)
    selected = []
    for cat, items in buckets.items():
        quota = max(1, round(n * len(items) / total))
        sampled = random.sample(items, min(quota, len(items)))
        selected.extend(sampled)

    # Trim or fill to exact n
    random.shuffle(selected)
    selected = selected[:n]
    print(f"  Sampled {len(selected):,} items (target={n:,})")
    return selected


def copy_pairs(pairs: list, img_out: Path, lbl_out: Path):
    """Copy (img_path, lbl_path) pairs into the output directories."""
    img_out.mkdir(parents=True, exist_ok=True)
    lbl_out.mkdir(parents=True, exist_ok=True)
    for img_path, lbl_path in pairs:
        shutil.copy2(img_path, img_out / img_path.name)
        if lbl_path.exists():
            shutil.copy2(lbl_path, lbl_out / lbl_path.name)
        else:
            # Background image — write empty label file
            (lbl_out / (img_path.stem + ".txt")).write_text("")


def write_yaml(out_dir: Path):
    config = {
        "path": str(out_dir),
        "train": "train/images",
        "val":   "valid/images",
        "nc": 2,
        "names": ["fire", "smoke"],
    }
    yaml_path = out_dir / "data.yaml"
    with open(yaml_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False)
    print(f"  Wrote {yaml_path}")


def main():
    print("=" * 60)
    print("D-Fire Subset Preparation")
    print(f"  Target train : {TRAIN_SAMPLE_SIZE:,} images")
    print(f"  Target valid : {VALID_SAMPLE_SIZE:,} images (from test set)")
    print(f"  Output       : {OUT_DIR}")
    print("=" * 60)

    if OUT_DIR.exists():
        print(f"\n⚠️  Output dir already exists: {OUT_DIR}")
        print("  Delete it manually and re-run if you want a fresh subset.")
        return

    print("\n[1/3] Sampling training set...")
    train_pairs = stratified_sample(TRAIN_IMG_DIR, TRAIN_LBL_DIR, TRAIN_SAMPLE_SIZE, RANDOM_SEED)
    copy_pairs(train_pairs, OUT_DIR / "train" / "images", OUT_DIR / "train" / "labels")
    print(f"  ✓ Copied {len(train_pairs):,} train images")

    print("\n[2/3] Sampling validation set (carved from D-Fire test set)...")
    valid_pairs = stratified_sample(TEST_IMG_DIR, TEST_LBL_DIR, VALID_SAMPLE_SIZE, RANDOM_SEED + 1)
    copy_pairs(valid_pairs, OUT_DIR / "valid" / "images", OUT_DIR / "valid" / "labels")
    print(f"  ✓ Copied {len(valid_pairs):,} valid images")

    print("\n[3/3] Writing data.yaml...")
    write_yaml(OUT_DIR)

    print("\n" + "=" * 60)
    print("✅ D-Fire subset ready!")
    print(f"   Train : {len(train_pairs):,} → {OUT_DIR}/train/images/")
    print(f"   Valid : {len(valid_pairs):,} → {OUT_DIR}/valid/images/")
    print(f"   Config: {OUT_DIR}/data.yaml")
    print("\nTraining command (on RTX 4060 machine):")
    print("  yolo detect train \\")
    print(f"    data=<path_to_dfire_subset>/data.yaml \\")
    print("    model=yolov8n.pt epochs=30 batch=16 imgsz=640 \\")
    print("    device=0 patience=8 project=sentrix_models name=fire_smoke_detector")


if __name__ == "__main__":
    main()
