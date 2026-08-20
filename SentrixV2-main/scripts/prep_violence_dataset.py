#!/usr/bin/env python3
"""
prep_violence_dataset.py
========================
Phase 2.5 — Data Restructuring Script (runs on THIS laptop)

Extracts 10 uniformly-spaced frames per clip from:
  - RWF-2000 (Dataset 2) — Fight / NonFight
  - Fight Surveillance Dataset (Dataset 6) — fight / noFight

Merges both, applies 80/20 stratified train/val split.
Outputs a unified frame directory + manifest CSVs.

Output:
    /Capstone/violence_frames/
        fight/      (~9,500 frames)
        nofight/    (~9,500 frames)
        train.csv   (80%)
        val.csv     (20%)
        manifest.json
"""

import csv
import json
import random
import shutil
import cv2
from pathlib import Path
from tqdm import tqdm

# ── Paths ────────────────────────────────────────────────────────────────────
RWF_TRAIN   = Path("/Users/harshit/Desktop/Study material/Capstone/2/SENTRIX/datasets/rwf2000/data/RWF-2000 Sliced/train")
RWF_VAL     = Path("/Users/harshit/Desktop/Study material/Capstone/2/SENTRIX/datasets/rwf2000/data/RWF-2000 Sliced/val")
DS6_FIGHT   = Path("/Users/harshit/Desktop/Study material/Capstone/6/fight-detection-surv-dataset-master/fight")
DS6_NOFIGHT = Path("/Users/harshit/Desktop/Study material/Capstone/6/fight-detection-surv-dataset-master/noFight")
OUT_DIR     = Path("/Users/harshit/Desktop/Study material/Capstone/violence_frames")

FRAMES_PER_CLIP = 10
RANDOM_SEED     = 42
TRAIN_RATIO     = 0.80

VIDEO_EXTENSIONS = {".avi", ".mp4", ".mov", ".mkv", ".AVI", ".MP4"}


def extract_frames(video_path: Path, n_frames: int, out_dir: Path, prefix: str) -> list:
    """
    Extract n_frames uniformly-spaced frames from a video.
    Returns list of saved frame paths.
    """
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        print(f"  ⚠️  Cannot open: {video_path.name}")
        return []

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total < 1:
        cap.release()
        return []

    # Compute uniform frame indices
    if total <= n_frames:
        indices = list(range(total))
    else:
        step = total / n_frames
        indices = [int(i * step) for i in range(n_frames)]

    saved = []
    for idx, frame_idx in enumerate(indices):
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ret, frame = cap.read()
        if not ret:
            continue
        frame_name = f"{prefix}_{idx:02d}.jpg"
        frame_path = out_dir / frame_name
        cv2.imwrite(str(frame_path), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        saved.append(frame_path)

    cap.release()
    return saved


def process_source(src_fight: Path, src_nofight: Path, source_name: str,
                   out_fight: Path, out_nofight: Path) -> list:
    """
    Process one source (RWF or DS6). Returns list of record dicts.
    """
    records = []

    for label, src_dir, out_dir in [("fight", src_fight, out_fight),
                                    ("nofight", src_nofight, out_nofight)]:
        if not src_dir.exists():
            print(f"  ⚠️  Dir not found: {src_dir}")
            continue

        videos = [f for f in src_dir.iterdir() if f.suffix.lower() in {e.lower() for e in VIDEO_EXTENSIONS}]
        print(f"  [{source_name}] {label}: {len(videos)} clips")

        for video_path in tqdm(videos, desc=f"  {source_name}/{label}", leave=False):
            prefix = f"{source_name}_{label}_{video_path.stem}"
            frames = extract_frames(video_path, FRAMES_PER_CLIP, out_dir, prefix)
            for frame_path in frames:
                records.append({
                    "frame_path": str(frame_path),
                    "label": label,
                    "source": source_name,
                    "clip": video_path.name,
                })

    return records


def write_split_csvs(records: list, out_dir: Path, train_ratio: float, seed: int):
    """Stratified 80/20 split at clip level, then write train.csv / val.csv."""
    random.seed(seed)

    # Group by (label, source, clip) to avoid frame leakage across splits
    from collections import defaultdict
    clips = defaultdict(list)
    for rec in records:
        key = (rec["label"], rec["source"], rec["clip"])
        clips[key].append(rec)

    fight_clips   = {k: v for k, v in clips.items() if k[0] == "fight"}
    nofight_clips = {k: v for k, v in clips.items() if k[0] == "nofight"}

    def split_dict(d, ratio):
        keys = list(d.keys())
        random.shuffle(keys)
        n_train = int(len(keys) * ratio)
        train_keys = set(keys[:n_train])
        val_keys   = set(keys[n_train:])
        train_recs = [r for k in train_keys for r in d[k]]
        val_recs   = [r for k in val_keys   for r in d[k]]
        return train_recs, val_recs

    fight_train, fight_val     = split_dict(fight_clips,   train_ratio)
    nofight_train, nofight_val = split_dict(nofight_clips, train_ratio)

    train_records = fight_train + nofight_train
    val_records   = fight_val   + nofight_val

    random.shuffle(train_records)
    random.shuffle(val_records)

    csv_fields = ["frame_path", "label", "source", "clip"]

    for fname, recs in [("train.csv", train_records), ("val.csv", val_records)]:
        csv_path = out_dir / fname
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=csv_fields)
            writer.writeheader()
            writer.writerows(recs)
        print(f"  Wrote {fname}: {len(recs):,} frames")

    return train_records, val_records


def main():
    print("=" * 65)
    print("Violence Dataset Frame Extraction & Merger")
    print(f"  Sources: RWF-2000 (train+val) + Fight Surveillance DS6")
    print(f"  Frames per clip: {FRAMES_PER_CLIP}")
    print(f"  Output: {OUT_DIR}")
    print("=" * 65)

    if OUT_DIR.exists():
        print(f"\n⚠️  Output dir already exists. Delete and re-run for fresh extraction.")

    out_fight   = OUT_DIR / "fight"
    out_nofight = OUT_DIR / "nofight"
    out_fight.mkdir(parents=True, exist_ok=True)
    out_nofight.mkdir(parents=True, exist_ok=True)

    all_records = []

    print("\n[1/3] Processing RWF-2000 train split...")
    all_records += process_source(
        RWF_TRAIN / "Fight", RWF_TRAIN / "NonFight",
        "rwf_train", out_fight, out_nofight
    )

    print("\n[2/3] Processing RWF-2000 val split...")
    all_records += process_source(
        RWF_VAL / "Fight", RWF_VAL / "NonFight",
        "rwf_val", out_fight, out_nofight
    )

    print("\n[3/3] Processing Fight Surveillance Dataset (DS6)...")
    all_records += process_source(
        DS6_FIGHT, DS6_NOFIGHT,
        "surv_ds6", out_fight, out_nofight
    )

    # ── Stats ─────────────────────────────────────────────────────────────
    n_fight   = sum(1 for r in all_records if r["label"] == "fight")
    n_nofight = sum(1 for r in all_records if r["label"] == "nofight")
    print(f"\n  Total frames extracted: {len(all_records):,}")
    print(f"    Fight:   {n_fight:,}")
    print(f"    NonFight:{n_nofight:,}")

    # ── Manifests ─────────────────────────────────────────────────────────
    print("\n[4/4] Writing train/val CSVs (stratified 80/20 by clip)...")
    train_recs, val_recs = write_split_csvs(all_records, OUT_DIR, TRAIN_RATIO, RANDOM_SEED)

    manifest = {
        "total_frames":   len(all_records),
        "fight_frames":   n_fight,
        "nofight_frames": n_nofight,
        "train_frames":   len(train_recs),
        "val_frames":     len(val_recs),
        "frames_per_clip": FRAMES_PER_CLIP,
        "sources":        ["rwf_train", "rwf_val", "surv_ds6"],
    }
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2))

    print("\n" + "=" * 65)
    print("✅ Violence frame dataset ready!")
    print(f"   fight/   : {n_fight:,} frames  → {out_fight}")
    print(f"   nofight/ : {n_nofight:,} frames → {out_nofight}")
    print(f"   train.csv: {len(train_recs):,} frames")
    print(f"   val.csv  : {len(val_recs):,} frames")
    print(f"\nNext step (on RTX 4060 machine):")
    print("   python scripts/train_violence_classifier.py \\")
    print(f"     --data {OUT_DIR} --epochs 20 --batch 8 --device cuda")


if __name__ == "__main__":
    main()
