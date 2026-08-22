#!/usr/bin/env python3
"""
prep_audioset_manifest.py
==========================
Phase 2.5 — Data Restructuring Script (runs on THIS laptop)

Parses AudioSet balanced_train_segments.csv to find clips matching
SENTRIX-relevant threat sound classes, then outputs a download manifest.

The training machine runs yt-dlp + ffmpeg using this manifest.

Target AudioSet class IDs:
    /m/032s_3   → Gunshot / Gunfire
    /m/03qc9zr  → Scream
    /m/0639ss   → Glass shatter
    /m/012f08   → Explosion
    /m/03kmc9   → Siren
    /m/09x0r    → Speech (negative/background class)
    /t/dd00066  → Fire crackling (bonus)

Output:
    /Capstone/4/audioset/download_manifest.csv
    /Capstone/4/audioset/class_stats.json
    /Capstone/4/audioset/download_audio.sh  (shell script for training machine)
"""

import csv
import json
from pathlib import Path
from collections import defaultdict

# ── Paths ─────────────────────────────────────────────────────────────────
BASE       = Path("/Users/harshit/Desktop/Study material/Capstone/4/audioset")
TRAIN_CSV  = BASE / "balanced_train_segments.csv"
EVAL_CSV   = BASE / "eval_segments.csv"
OUT_DIR    = BASE

MAX_PER_CLASS = 200     # Target up to 200 clips per class

# ── Target class mapping ───────────────────────────────────────────────────
# AudioSet ontology IDs → friendly label name for SENTRIX
TARGET_CLASSES = {
    "/m/032s_3":   "gunshot",
    "/m/03qc9zr":  "scream",
    "/m/0639ss":   "glass",
    "/m/012f08":   "explosion",
    "/m/03kmc9":   "siren",
    "/m/09x0r":    "speech_normal",   # Background negative
    "/t/dd00066":  "fire",
}


def parse_audioset_csv(csv_path: Path) -> list:
    """
    Parse AudioSet CSV (skipping comment lines) and return list of dicts.
    AudioSet CSV format:
      # YTID, start_seconds, end_seconds, positive_labels
      --PJHxphWEs, 30.000, 40.000, "/m/09x0r,/m/05zppz,..."
    """
    rows = []
    with open(csv_path, "r") as f:
        for line in f:
            line = line.strip()
            if line.startswith("#") or not line:
                continue
            parts = line.split(", ", 3)  # Max 4 splits
            if len(parts) < 4:
                continue
            ytid     = parts[0].strip()
            start_s  = float(parts[1].strip())
            end_s    = float(parts[2].strip())
            labels   = parts[3].strip().strip('"').split(",")
            labels   = [l.strip().strip('"') for l in labels]
            rows.append({
                "ytid":   ytid,
                "start":  start_s,
                "end":    end_s,
                "labels": labels,
            })
    return rows


def find_target_clips(rows: list, targets: dict, max_per: int) -> dict:
    """
    For each target class, find up to max_per matching clips.
    Returns dict: {label: [{"ytid", "start", "end", "label"}]}
    """
    buckets = defaultdict(list)
    for row in rows:
        for audioset_id, label in targets.items():
            if audioset_id in row["labels"]:
                if len(buckets[label]) < max_per:
                    buckets[label].append({
                        "ytid":  row["ytid"],
                        "start": row["start"],
                        "end":   row["end"],
                        "label": label,
                    })
    return dict(buckets)


def write_manifest(buckets: dict, out_path: Path):
    """Write unified download manifest CSV."""
    all_rows = []
    for label, clips in buckets.items():
        for clip in clips:
            all_rows.append(clip)

    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["ytid", "start", "end", "label"])
        writer.writeheader()
        writer.writerows(all_rows)

    return all_rows


def write_download_script(all_rows: list, script_path: Path):
    """
    Write a shell script that uses yt-dlp + ffmpeg to download audio clips.
    Designed to run on the training machine (which has YouTube access).
    """
    lines = [
        "#!/bin/bash",
        "# SENTRIX AudioSet Download Script",
        "# Run on training machine with YouTube internet access",
        "# Requirements: yt-dlp, ffmpeg",
        "#   pip install yt-dlp && apt install ffmpeg (or brew install ffmpeg)",
        "",
        "set -e",
        'OUTDIR="./audioset_clips"',
        "mkdir -p $OUTDIR",
        "",
        "download_clip() {",
        "  local YTID=$1",
        "  local START=$2",
        "  local END=$3",
        "  local LABEL=$4",
        "  local OUTFILE=\"$OUTDIR/${LABEL}/${YTID}_${START}.wav\"",
        "  mkdir -p \"$OUTDIR/$LABEL\"",
        "  if [ -f \"$OUTFILE\" ]; then",
        "    echo \"  Skip (exists): $OUTFILE\"",
        "    return 0",
        "  fi",
        "  yt-dlp \\",
        "    --no-playlist \\",
        "    --quiet \\",
        "    --extract-audio \\",
        "    --audio-format wav \\",
        "    --postprocessor-args \"-ss $START -to $END -ar 22050 -ac 1\" \\",
        "    -o \"$OUTDIR/$LABEL/${YTID}_${START}.%(ext)s\" \\",
        "    \"https://www.youtube.com/watch?v=$YTID\" 2>/dev/null \\",
        "  && echo \"  OK: $OUTFILE\" \\",
        "  || echo \"  FAIL: $YTID\"",
        "}",
        "",
        "echo 'Starting AudioSet download...'",
        "",
    ]

    for row in all_rows:
        lines.append(
            f'download_clip "{row["ytid"]}" {row["start"]} {row["end"]} "{row["label"]}"'
        )

    lines += [
        "",
        "echo ''",
        "echo 'Download complete. Audio clips saved in: $OUTDIR'",
        "echo 'Run scripts/train_audio_classifier.py next.'",
    ]

    script_path.write_text("\n".join(lines))
    script_path.chmod(0o755)
    print(f"  Wrote shell script: {script_path}")


def main():
    print("=" * 65)
    print("AudioSet Manifest Generator")
    print(f"  Input CSV : {TRAIN_CSV}")
    print(f"  Max/class : {MAX_PER_CLASS}")
    print(f"  Output    : {OUT_DIR}")
    print("=" * 65)

    print("\n[1/3] Parsing balanced_train_segments.csv...")
    train_rows = parse_audioset_csv(TRAIN_CSV)
    print(f"  Parsed {len(train_rows):,} entries")

    print("\n[2/3] Finding target class clips...")
    buckets = find_target_clips(train_rows, TARGET_CLASSES, MAX_PER_CLASS)

    # Also check eval CSV
    if EVAL_CSV.exists():
        print("  Also scanning eval_segments.csv for extra clips...")
        eval_rows = parse_audioset_csv(EVAL_CSV)
        extra = find_target_clips(eval_rows, TARGET_CLASSES, MAX_PER_CLASS)
        for label, clips in extra.items():
            remaining = MAX_PER_CLASS - len(buckets.get(label, []))
            if remaining > 0:
                buckets.setdefault(label, []).extend(clips[:remaining])

    print("\n  Class availability:")
    stats = {}
    for label, clips in sorted(buckets.items()):
        print(f"    {label:20s}: {len(clips):4d} clips")
        stats[label] = len(clips)

    # Check minimum coverage
    low_classes = [l for l, n in stats.items() if n < 50]
    if low_classes:
        print(f"\n  ⚠️  Low coverage classes (< 50 clips): {low_classes}")
        print("     Consider supplementing with UrbanSound8k or ESC-50")

    print("\n[3/3] Writing outputs...")
    manifest_path = OUT_DIR / "download_manifest.csv"
    all_rows = write_manifest(buckets, manifest_path)
    print(f"  Wrote manifest: {manifest_path}  ({len(all_rows):,} total clips)")

    stats_path = OUT_DIR / "class_stats.json"
    stats_path.write_text(json.dumps({"classes": stats, "total_clips": len(all_rows)}, indent=2))
    print(f"  Wrote stats: {stats_path}")

    script_path = OUT_DIR / "download_audio.sh"
    write_download_script(all_rows, script_path)

    print("\n" + "=" * 65)
    print("✅ AudioSet manifest ready!")
    print(f"   Total clips to download: {len(all_rows):,}")
    print(f"   Download manifest: {manifest_path}")
    print(f"   Download script  : {script_path}")
    print("\nOn training machine, run:")
    print(f"   bash {script_path}")
    print("   # Then train:")
    print("   python scripts/train_audio_classifier.py --data ./audioset_clips")


if __name__ == "__main__":
    main()
