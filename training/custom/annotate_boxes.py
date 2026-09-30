"""Derive bounding boxes for the custom dataset's weapon and fire frames.

The custom dataset is labelled per *image*; YOLO needs *boxes*. The deployed
detectors cannot supply them — on this footage the v3 weapon detector scores
normal and weapon frames alike (AUROC 0.54) and the fire detector finds none of
the aerosol flame jets — so boxes come from an open-vocabulary detector
(GroundingDINO), **gated by the reviewed frame label**:

* a box is only ever proposed on a frame whose reviewed class already says the
  object is there (``weapon`` or ``fire``), and only for that class;
* the prompt follows the scene: the corridor videos (``wa*``) contain a long
  stick, the bedroom videos a handgun (``segments.csv`` notes);
* a weapon box must overlap a detected person (box grown by 25 %): weapons
  here are *held*. Without this the corridor's door edges and gate bars are the
  best "stick" in the frame. Persons are the union of COCO ``yolov8n`` and a
  GroundingDINO "a person." query, because ``yolov8n`` misses the masked,
  top-down subject in the corridor in most frames;
* only the single best box is kept, and only above ``--min-score``. A positive
  frame with no confident box is *excluded* from detector training — it is
  never turned into a false "no weapon here" negative;
* ``normal`` frames become background images (true negatives). ``assault`` and
  ``robbery`` frames are excluded: several show a weapon in hand and single
  labels cannot say which.

Every box is drawn onto contact sheets under ``--qc-dir`` for visual review
before training, and rejected boxes can be listed in ``--reject`` (one image
name per line). The result is written to ``boxes.csv`` — the annotation of
record, committed alongside the dataset metadata.

Usage::

    python training/custom/annotate_boxes.py \
        --out training/datasets/sentrix_custom/boxes.csv \
        --qc-dir training/runs/custom/annotation_qc
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sentrix.evaluation.custom_dataset import load_frames  # noqa: E402

MODEL_ID = "IDEA-Research/grounding-dino-base"

#: (prompt, box class) per scene. Corridor = the WhatsApp CCTV videos.
PROMPTS = {
    ("weapon", "corridor"): ("a long stick. a wooden rod. a bat.", "stick"),
    ("weapon", "room"): ("a handgun. a pistol. a gun.", "pistol"),
    ("fire", "corridor"): ("fire. a flame.", "fire"),
    ("fire", "room"): ("fire. a flame. a jet of flame.", "fire"),
}
#: A weapon box larger than this fraction of the frame is a person, not a gun.
MAX_AREA = {"stick": 0.20, "pistol": 0.08, "fire": 0.35}


#: Fraction of a weapon box that must fall inside a (grown) person box.
MIN_PERSON_OVERLAP = 0.20


def person_boxes(detector, image, grounding=None) -> list[list[float]]:
    """Person boxes grown by 25 % per side, to cover an extended arm."""
    res = detector(image, conf=0.25, classes=[0], verbose=False)[0]
    boxes = res.boxes.xyxy.cpu().numpy().tolist()
    if grounding is not None:
        processor, model, device = grounding
        boxes += [b for score, b in detect(processor, model, image, "a person.",
                                           device, box_thr=0.25)]
    grown = []
    for x1, y1, x2, y2 in boxes:
        dx, dy = 0.25 * (x2 - x1), 0.25 * (y2 - y1)
        grown.append([x1 - dx, y1 - dy, x2 + dx, y2 + dy])
    return grown


def held(box, persons) -> bool:
    x1, y1, x2, y2 = box
    area = max((x2 - x1) * (y2 - y1), 1e-6)
    for px1, py1, px2, py2 in persons:
        iw = max(0.0, min(x2, px2) - max(x1, px1))
        ih = max(0.0, min(y2, py2) - max(y1, py1))
        if iw * ih / area >= MIN_PERSON_OVERLAP:
            return True
    return False


def scene_of(video: str) -> str:
    return "corridor" if video.startswith("wa") else "room"


def load_model(device: str):
    from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor
    processor = AutoProcessor.from_pretrained(MODEL_ID)
    model = AutoModelForZeroShotObjectDetection.from_pretrained(MODEL_ID).to(device).eval()
    return processor, model


def detect(processor, model, image, prompt: str, device: str, box_thr: float):
    import torch
    inputs = processor(images=image, text=prompt, return_tensors="pt").to(device)
    with torch.no_grad():
        outputs = model(**inputs)
    kwargs = dict(text_threshold=0.20, target_sizes=[image.size[::-1]])
    try:
        res = processor.post_process_grounded_object_detection(
            outputs, inputs.input_ids, threshold=box_thr, **kwargs)[0]
    except TypeError:                                   # older transformers
        res = processor.post_process_grounded_object_detection(
            outputs, inputs.input_ids, box_threshold=box_thr, **kwargs)[0]
    boxes = res["boxes"].cpu().numpy().tolist()
    scores = res["scores"].cpu().numpy().tolist()
    return list(zip(scores, boxes))


def contact_sheets(rows, qc_dir: Path, per_sheet: int = 48, cols: int = 8,
                   cell: int = 240) -> list[Path]:
    """Crops around each box plus a thumbnail of the whole frame, for review."""
    from PIL import Image, ImageDraw
    qc_dir.mkdir(parents=True, exist_ok=True)
    written = []
    groups: dict[str, list[dict]] = {}
    for r in rows:
        if r["status"] == "box":
            groups.setdefault(r["box_class"], []).append(r)
    for cls, items in groups.items():
        for s in range(0, len(items), per_sheet):
            chunk = items[s: s + per_sheet]
            nrows = (len(chunk) + cols - 1) // cols
            sheet = Image.new("RGB", (cols * cell, nrows * (cell + 18)), (25, 25, 25))
            draw = ImageDraw.Draw(sheet)
            for k, r in enumerate(chunk):
                img = Image.open(r["path"]).convert("RGB")
                x1, y1, x2, y2 = r["xyxy"]
                d = ImageDraw.Draw(img)
                d.rectangle([x1, y1, x2, y2], outline=(255, 40, 40), width=4)
                # square crop centred on the box, 3x its size, so context shows
                cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
                half = max(x2 - x1, y2 - y1) * 1.5 + 20
                crop = img.crop((cx - half, cy - half, cx + half, cy + half))
                crop.thumbnail((cell, cell))
                ox, oy = (k % cols) * cell, (k // cols) * (cell + 18)
                sheet.paste(crop, (ox, oy + 18))
                draw.text((ox + 3, oy + 2), f"{k + s:03d} {r['image'][:24]} {r['score']:.2f}",
                          fill=(255, 255, 0))
            path = qc_dir / f"{cls}_{s // per_sheet:02d}.jpg"
            sheet.save(path, quality=88)
            written.append(path)
    return written


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dataset", type=Path, default=None)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--qc-dir", type=Path, default=None)
    parser.add_argument("--min-score", type=float, default=0.30)
    parser.add_argument("--reject", type=Path, default=None,
                        help="text file of image names whose box was rejected in QC")
    args = parser.parse_args(argv)

    import torch
    from PIL import Image

    device = "cuda" if torch.cuda.is_available() else "cpu"
    rejected = set()
    if args.reject and args.reject.exists():
        rejected = {line.strip() for line in args.reject.read_text().splitlines()
                    if line.strip() and not line.startswith("#")}

    from ultralytics import YOLO

    frames = load_frames(root=args.dataset)
    processor, model = load_model(device)
    person_detector = YOLO(str(ROOT / "models" / "yolov8n.pt"))
    rows: list[dict] = []
    for i, f in enumerate(frames, 1):
        base = {"image": f.path.name, "path": str(f.path), "split": f.split,
                "video": f.video, "label": f.label}
        if f.label == "normal":
            rows.append({**base, "status": "negative"})
            continue
        if f.label not in ("weapon", "fire"):
            rows.append({**base, "status": "excluded"})
            continue
        prompt, box_class = PROMPTS[(f.label, scene_of(f.video))]
        image = Image.open(f.path).convert("RGB")
        w, h = image.size
        persons = (person_boxes(person_detector, image, (processor, model, device))
                   if f.label == "weapon" else None)
        found = []
        for score, (x1, y1, x2, y2) in detect(processor, model, image, prompt,
                                              device, box_thr=0.15):
            area = (x2 - x1) * (y2 - y1) / float(w * h)
            if area > MAX_AREA[box_class]:
                continue
            if persons is not None and not held((x1, y1, x2, y2), persons):
                continue
            found.append((score, [x1, y1, x2, y2]))
        found.sort(key=lambda s: -s[0])
        if found and found[0][0] >= args.min_score and f.path.name not in rejected:
            score, xyxy = found[0]
            rows.append({**base, "status": "box", "box_class": box_class,
                         "score": round(float(score), 4),
                         "xyxy": [round(float(v), 1) for v in xyxy],
                         "width": w, "height": h})
        else:
            best = found[0][0] if found else 0.0
            reason = "rejected_in_qc" if f.path.name in rejected else "no_confident_box"
            rows.append({**base, "status": reason, "score": round(float(best), 4)})
        if i % 100 == 0:
            print(f"  {i}/{len(frames)}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["image", "split", "video", "label", "status", "box_class",
                         "score", "x1", "y1", "x2", "y2", "width", "height"])
        for r in rows:
            xyxy = r.get("xyxy", ["", "", "", ""])
            writer.writerow([r["image"], r["split"], r["video"], r["label"], r["status"],
                             r.get("box_class", ""), r.get("score", ""), *xyxy,
                             r.get("width", ""), r.get("height", "")])

    from collections import Counter
    counts = Counter((r["label"], r["split"], r["status"]) for r in rows
                     if r["label"] in ("weapon", "fire"))
    for key in sorted(counts):
        print(f"  {key[0]:7} {key[1]:5} {key[2]:18} {counts[key]}")
    print(f"written: {args.out}")
    if args.qc_dir:
        for p in contact_sheets(rows, args.qc_dir):
            print(f"  QC sheet: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
