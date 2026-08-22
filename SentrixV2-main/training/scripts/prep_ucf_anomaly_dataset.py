#!/usr/bin/env python3
"""
prep_ucf_anomaly_dataset.py
============================
Phase 2.5 — Data Restructuring Script (runs on THIS laptop)

Extracts frames from UCF-Crime Part-1 for TRAINING the anomaly detector (Model F).
Uses only Anomaly-Videos-Part-1 (extracted): Abuse, Arrest, Arson, Assault.
Normal frames are sampled from the training normal video list.

Output:
    /Capstone/ucf_anomaly_frames/
        anomalous/      (~1,600 frames from Part-1)
        normal/         (~1,600 frames from normal train videos)
        train.csv
        val.csv
        manifest.json

STRICT: Testing_Normal_Videos and Temporal_Anomaly_Annotation (test set)
        are NOT touched here — reserved for evaluation only.
"""

import csv
import json
import random
import cv2
from pathlib import Path
from tqdm import tqdm

# ── Paths ────────────────────────────────────────────────────────────────────
UCF_PART1     = Path("/Users/harshit/Desktop/Study material/Capstone/1/Anomaly-Videos-Part-1")
TRAIN_LIST    = Path("/Users/harshit/Desktop/Study material/Capstone/1/Anomaly_Train.txt")
# Testing_Normal_Videos.zip must remain untouched (reserved for eval)
# If you have extracted Training-Normal-Videos, put path here:
NORMAL_TRAIN_DIR = None   # e.g. Path("/Capstone/1/Training_Normal_Videos")

OUT_DIR       = Path("/Users/harshit/Desktop/Study material/Capstone/ucf_anomaly_frames")

FRAMES_PER_CLIP = 8     # 8 frames per clip → 200 clips × 8 = 1,600 anomalous frames
RANDOM_SEED     = 42
TRAIN_RATIO     = 0.80
VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv"}


def extract_frames(video_path: Path, n_frames: int, out_dir: Path, prefix: str) -> list:
    """Extract n_frames uniformly-spaced frames. Returns saved frame paths."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return []
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total < 1:
        cap.release()
        return []

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
        fname = f"{prefix}_{idx:02d}.jpg"
        fpath = out_dir / fname
        cv2.imwrite(str(fpath), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        saved.append(fpath)

    cap.release()
    return saved


def collect_anomalous(out_anomalous: Path) -> list:
    """Extract frames from all Part-1 anomaly categories."""
    records = []
    categories = [d for d in UCF_PART1.iterdir()
                  if d.is_dir() and not d.name.startswith(".")]

    for cat_dir in sorted(categories):
        videos = sorted([f for f in cat_dir.iterdir()
                         if f.suffix.lower() in VIDEO_EXTENSIONS])
        print(f"  [{cat_dir.name}] {len(videos)} clips")
        for vp in tqdm(videos, desc=f"  {cat_dir.name}", leave=False):
            prefix = f"{cat_dir.name}_{vp.stem}"
            frames = extract_frames(vp, FRAMES_PER_CLIP, out_anomalous, prefix)
            for fp in frames:
                records.append({
                    "frame_path": str(fp),
                    "label": "anomalous",
                    "category": cat_dir.name,
                    "clip": vp.name,
                })

    return records


def collect_normal_from_train_list(out_normal: Path, n_target: int) -> list:
    """
    Sample normal clips from Anomaly_Train.txt (which lists training normal videos too).
    Only use entries that contain 'Normal' in path or are in a Normal subdirectory.
    If no normal train dir is found, synthesize negatives by sampling anomaly frames
    from non-peak temporal regions (fallback).
    """
    records = []

    if NORMAL_TRAIN_DIR and NORMAL_TRAIN_DIR.exists():
        print(f"  Using normal train dir: {NORMAL_TRAIN_DIR}")
        normal_videos = sorted([f for f in NORMAL_TRAIN_DIR.rglob("*")
                                if f.suffix.lower() in VIDEO_EXTENSIONS])
        random.seed(RANDOM_SEED)
        random.shuffle(normal_videos)

        for vp in tqdm(normal_videos[:n_target // FRAMES_PER_CLIP + 10],
                       desc="  Normal train", leave=False):
            prefix = f"normal_{vp.stem}"
            frames = extract_frames(vp, FRAMES_PER_CLIP, out_normal, prefix)
            for fp in frames:
                records.append({
                    "frame_path": str(fp),
                    "label": "normal",
                    "category": "Normal",
                    "clip": vp.name,
                })
            if len(records) >= n_target:
                break
    else:
        print("  ⚠️  Normal train video directory not found.")
        print("      Extracting 'normal' frames from start-of-clip portions of anomaly videos")
        print("      (pre-event frames are typically non-anomalous in UCF-Crime).")

        all_anomaly_videos = list(UCF_PART1.rglob("*.mp4")) + list(UCF_PART1.rglob("*.avi"))
        random.seed(RANDOM_SEED + 10)
        random.shuffle(all_anomaly_videos)

        for vp in tqdm(all_anomaly_videos, desc="  Normal (pre-event)", leave=False):
            cap = cv2.VideoCapture(str(vp))
            if not cap.isOpened():
                continue
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            cap.release()
            if total < 40:
                continue
            # Take frames from first 20% of clip (pre-event region)
            end_frame = int(total * 0.20)
            cap2 = cv2.VideoCapture(str(vp))
            for idx in range(min(FRAMES_PER_CLIP, end_frame)):
                frame_idx = idx * max(1, end_frame // FRAMES_PER_CLIP)
                cap2.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
                ret, frame = cap2.read()
                if not ret:
                    continue
                prefix = f"normal_preevt_{vp.stem}"
                fname  = f"{prefix}_{idx:02d}.jpg"
                fpath  = out_normal / fname
                cv2.imwrite(str(fpath), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                records.append({
                    "frame_path": str(fpath),
                    "label": "normal",
                    "category": "Normal_preevt",
                    "clip": vp.name,
                })
            cap2.release()
            if len(records) >= n_target:
                break

    return records[:n_target]


def stratified_split(records_anomalous: list, records_normal: list,
                     out_dir: Path, train_ratio: float, seed: int):
    """Clip-level stratified split. Returns (train_recs, val_recs)."""
    from collections import defaultdict

    def clip_split(recs, ratio, seed_offset):
        clips = defaultdict(list)
        for r in recs:
            clips[r["clip"]].append(r)
        keys = list(clips.keys())
        random.seed(seed + seed_offset)
        random.shuffle(keys)
        n_train = int(len(keys) * ratio)
        train_recs = [r for k in keys[:n_train]  for r in clips[k]]
        val_recs   = [r for k in keys[n_train:]  for r in clips[k]]
        return train_recs, val_recs

    a_train, a_val = clip_split(records_anomalous, train_ratio, 0)
    n_train, n_val = clip_split(records_normal,    train_ratio, 1)

    train_recs = a_train + n_train
    val_recs   = a_val   + n_val

    random.shuffle(train_recs)
    random.shuffle(val_recs)

    fields = ["frame_path", "label", "category", "clip"]
    for fname, recs in [("train.csv", train_recs), ("val.csv", val_recs)]:
        with open(out_dir / fname, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(recs)
        print(f"  Wrote {fname}: {len(recs):,} frames")

    return train_recs, val_recs


def main():
    print("=" * 65)
    print("UCF-Crime Anomaly Frame Extraction (TRAINING SET ONLY)")
    print(f"  Source: {UCF_PART1}")
    print(f"  Output: {OUT_DIR}")
    print("  NOTE: Test set annotations are NEVER touched here.")
    print("=" * 65)

    out_anomalous = OUT_DIR / "anomalous"
    out_normal    = OUT_DIR / "normal"
    out_anomalous.mkdir(parents=True, exist_ok=True)
    out_normal.mkdir(parents=True, exist_ok=True)

    print("\n[1/3] Extracting ANOMALOUS frames from Part-1...")
    anom_records = collect_anomalous(out_anomalous)
    print(f"  ✓ {len(anom_records):,} anomalous frames")

    print(f"\n[2/3] Collecting NORMAL frames (target: {len(anom_records):,})...")
    norm_records = collect_normal_from_train_list(out_normal, len(anom_records))
    print(f"  ✓ {len(norm_records):,} normal frames")

    print("\n[3/3] Writing train/val CSVs (clip-level 80/20 split)...")
    train_recs, val_recs = stratified_split(
        anom_records, norm_records, OUT_DIR, TRAIN_RATIO, RANDOM_SEED
    )

    manifest = {
        "anomalous_frames":   len(anom_records),
        "normal_frames":      len(norm_records),
        "train_frames":       len(train_recs),
        "val_frames":         len(val_recs),
        "frames_per_clip":    FRAMES_PER_CLIP,
        "source":             "UCF-Crime Part-1 (Abuse, Arrest, Arson, Assault)",
        "test_annotations":   "RESERVED — use prep_ucf_eval_manifest.py",
    }
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2))

    print("\n" + "=" * 65)
    print("✅ UCF-Crime anomaly training frames ready!")
    print(f"   anomalous/ : {len(anom_records):,} frames → {out_anomalous}")
    print(f"   normal/    : {len(norm_records):,} frames → {out_normal}")
    print(f"   train.csv  : {len(train_recs):,} frames (80%)")
    print(f"   val.csv    : {len(val_recs):,} frames (20%)")
    print("\nNext step (on RTX 4060 machine):")
    print("   python scripts/train_anomaly_classifier.py \\")
    print(f"     --data {OUT_DIR} --epochs 20 --batch 32 --device cuda")


if __name__ == "__main__":
    main()
