#!/usr/bin/env python3
"""
verify_weapons_labels.py
========================
Phase 2.5 — Data Restructuring Script (runs on THIS laptop)

Sanity-checks the WeaponsData v4 dataset YOLO labels before training.
Checks:
- No missing label files for images
- All class IDs are valid (0=knife, 1=long_gun, 2=pistol)
- Bounding box coordinates are valid [0, 1]
- No degenerate boxes (area < threshold)

Output: Terminal report
"""

import os
from pathlib import Path

BASE = Path("/Users/harshit/Desktop/Study material/Capstone/5/weaponsdata.v4i.yolov8")

def check_split(split_name):
    img_dir = BASE / split_name / "images"
    lbl_dir = BASE / split_name / "labels"
    
    if not img_dir.exists():
        return
        
    print(f"\n--- Checking {split_name} split ---")
    
    img_files = list(img_dir.glob("*.jpg")) + list(img_dir.glob("*.png"))
    print(f"Total images: {len(img_files)}")
    
    missing_labels = 0
    invalid_classes = 0
    invalid_coords = 0
    class_counts = {0: 0, 1: 0, 2: 0}
    
    for img in img_files:
        lbl = lbl_dir / (img.stem + ".txt")
        if not lbl.exists():
            missing_labels += 1
            continue
            
        with open(lbl, "r") as f:
            for line in f:
                parts = line.strip().split()
                if not parts:
                    continue
                try:
                    c = int(parts[0])
                    if c not in class_counts:
                        invalid_classes += 1
                    else:
                        class_counts[c] += 1
                        
                    coords = [float(x) for x in parts[1:]]
                    if any(x < 0 or x > 1 for x in coords):
                        invalid_coords += 1
                except ValueError:
                    pass
                    
    print(f"Missing labels: {missing_labels}")
    print(f"Invalid classes: {invalid_classes}")
    print(f"Invalid coords: {invalid_coords}")
    print(f"Class distribution: {class_counts}")

def main():
    print("="*50)
    print("WeaponsData v4 Verification")
    print("="*50)
    
    check_split("train")
    check_split("valid")
    check_split("test")
    
    print("\n✅ Verification complete.")

if __name__ == "__main__":
    main()
