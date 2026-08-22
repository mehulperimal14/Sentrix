#!/usr/bin/env python3
"""
prep_ucf_eval_manifest.py
=========================
Phase 2.5 — Data Restructuring Script

Creates evaluation manifest for UCF-Crime testing videos.
Parses Temporal_Anomaly_Annotation_for_Testing_Videos.txt.
Uses 5 frames from the anomaly region and 5 frames from outside (normal).
"""

import csv
import os
from pathlib import Path

BASE = Path("/Users/harshit/Desktop/Study material/Capstone/1")
ANNOTATION_FILE = BASE / "Temporal_Anomaly_Annotation_for_Testing_Videos.txt"
OUT_FILE = Path("/Users/harshit/Desktop/Study material/Capstone/SentrixV2-main/evaluation/data/ucf_crime_eval_manifest.csv")

def main():
    if not ANNOTATION_FILE.exists():
        print(f"Error: {ANNOTATION_FILE} not found.")
        return

    out_dir = OUT_FILE.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    
    rows = []
    with open(ANNOTATION_FILE, "r") as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) >= 6:
                video_name = parts[0]
                anomaly_type = parts[1]
                start1 = int(parts[2])
                end1 = int(parts[3])
                start2 = int(parts[4])
                end2 = int(parts[5])
                
                rows.append({
                    "sample_id": f"ucf_eval_{video_name}",
                    "video_path": video_name,
                    "anomaly_type": anomaly_type,
                    "start1": start1,
                    "end1": end1,
                    "start2": start2,
                    "end2": end2
                })
                
    with open(OUT_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["sample_id", "video_path", "anomaly_type", "start1", "end1", "start2", "end2"])
        writer.writeheader()
        writer.writerows(rows)
        
    print(f"Generated {OUT_FILE} with {len(rows)} video annotations.")

if __name__ == "__main__":
    main()
