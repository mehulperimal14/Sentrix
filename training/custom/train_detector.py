"""Train or fine-tune a YOLO weapon / fire detector on the SENTRIX custom dataset.

Boxes come from ``training/datasets/sentrix_custom/boxes.csv`` (see
``annotate_boxes.py``: open-vocabulary proposals gated by the reviewed frame
label, a held-object check, and a visual QC pass). The YOLO dataset is built
from it as follows:

* positives: frames of the task's class that have an accepted box;
* negatives: frames the reviewed labels say contain none of the task's objects
  (``normal``, plus ``fire`` frames for the weapon task and ``weapon`` frames
  for the fire task) — these teach the detector what this environment looks
  like *without* a threat, which is precisely what the v3 models get wrong;
* excluded: positives without an accepted box (a missing box must never become
  a false "nothing here" label), and ``assault``/``robbery`` frames (several
  show a weapon in hand; one label per image cannot say where).

``--rehearsal N`` mixes N images of the detector's original training corpus
back in, with their own labels, so fine-tuning does not erase knives and long
guns; ``--retention N`` measures that afterwards on the original validation
split, for the fine-tuned model *and* the starting weights.

Examples::

    # fine-tune the notebook-trained v3 detector (branch sentrix/finetuned-v3)
    python training/custom/train_detector.py --task weapon \\
        --init models/v3_real/weapon_detector_real_v3.pt \\
        --classes knife,long_gun,pistol,stick --rehearsal 1500 --retention 400 \\
        --tag ft_weapon --out models/v4_finetuned/weapon_detector_ft_v4.pt

    # new detector from generic COCO weights (branch sentrix/custom-v4)
    python training/custom/train_detector.py --task fire --init yolo11n.pt \\
        --classes fire --tag new_fire --out models/v4_custom/fire_detector_custom_v4.pt
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sentrix.evaluation.custom_dataset import dataset_dir, load_frames  # noqa: E402

BOXES_CSV = ROOT / "training" / "datasets" / "sentrix_custom" / "boxes.csv"
BUILD_ROOT = ROOT / "data" / "custom_yolo"            # gitignored
RUNS = ROOT / "training" / "runs" / "custom" / "detector"
ORIGINAL = {
    "weapon": Path(r"G:\Capstone\data\weapon_data"),
    "fire": Path(r"G:\Capstone\data\fire_smoke_data"),
}
#: Frame labels that are guaranteed free of the task's objects.
NEGATIVE_LABELS = {"weapon": {"normal", "fire"}, "fire": {"normal", "weapon"}}


def yolo_line(cls: int, x1, y1, x2, y2, w, h) -> str:
    cx, cy = (x1 + x2) / 2 / w, (y1 + y2) / 2 / h
    bw, bh = (x2 - x1) / w, (y2 - y1) / h
    return f"{cls} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}"


def original_names(task: str) -> list[str]:
    cfg = yaml.safe_load((ORIGINAL[task] / "data.yaml").read_text(encoding="utf-8"))
    names = cfg["names"]
    return [names[k] for k in sorted(names)] if isinstance(names, dict) else list(names)


def sample_original(task: str, split: str, n: int, classes: list[str],
                    seed: int) -> list[tuple[Path, list[str]]]:
    """``n`` images of the original corpus with labels remapped to ``classes``."""
    if n <= 0:
        return []
    src_names = original_names(task)
    remap = {i: classes.index(name) for i, name in enumerate(src_names) if name in classes}
    images = sorted((ORIGINAL[task] / split / "images").glob("*"))
    random.Random(seed).shuffle(images)
    out = []
    for img in images[:n]:
        label = ORIGINAL[task] / split / "labels" / (img.stem + ".txt")
        lines = []
        if label.exists():
            for line in label.read_text().splitlines():
                parts = line.split()
                if parts and int(parts[0]) in remap:
                    lines.append(" ".join([str(remap[int(parts[0])]), *parts[1:5]]))
        out.append((img, lines))
    return out


def build_dataset(args, classes: list[str]) -> tuple[Path, dict]:
    task = args.task
    root = BUILD_ROOT / args.tag
    if root.exists():
        shutil.rmtree(root)
    rows = {r["image"]: r for r in csv.DictReader(open(BOXES_CSV, encoding="utf-8"))}
    frames = load_frames(root=args.dataset)
    stats: dict = {}

    def put(split: str, src: Path, name: str, lines: list[str]) -> None:
        (root / "images" / split).mkdir(parents=True, exist_ok=True)
        (root / "labels" / split).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, root / "images" / split / name)
        (root / "labels" / split / (Path(name).stem + ".txt")).write_text(
            "\n".join(lines) + ("\n" if lines else ""))

    for f in frames:
        r = rows[f.path.name]
        split = f.split
        if f.label == task and r["status"] == "box" and r["box_class"] in classes:
            vals = [float(r[k]) for k in ("x1", "y1", "x2", "y2", "width", "height")]
            line = yolo_line(classes.index(r["box_class"]), *vals)
            repeat = args.pos_repeat if split == "train" else 1
            for k in range(repeat):
                name = f.path.name if k == 0 else f"{f.path.stem}_rep{k}{f.path.suffix}"
                put(split, f.path, name, [line])
            key = f"{split}_positive"
        elif f.label in NEGATIVE_LABELS[task]:
            put(split, f.path, f.path.name, [])
            key = f"{split}_negative"
        else:
            key = f"{split}_excluded"
        stats[key] = stats.get(key, 0) + 1

    for img, lines in sample_original(task, "train", args.rehearsal, classes, args.seed):
        put("train", img, f"orig_{img.name}", lines)
    stats["train_rehearsal"] = args.rehearsal

    data = {"path": str(root), "train": "images/train", "val": "images/val",
            "test": "images/test", "names": {i: c for i, c in enumerate(classes)}}
    cfg = root / "data.yaml"
    cfg.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    if args.retention:
        ret_root = root / "retention"
        for img, lines in sample_original(task, "valid", args.retention, classes, args.seed + 1):
            (ret_root / "images").mkdir(parents=True, exist_ok=True)
            (ret_root / "labels").mkdir(parents=True, exist_ok=True)
            shutil.copyfile(img, ret_root / "images" / img.name)
            (ret_root / "labels" / (img.stem + ".txt")).write_text("\n".join(lines) + "\n")
        (root / "retention.yaml").write_text(yaml.safe_dump(
            {"path": str(ret_root), "train": "images", "val": "images",
             "names": {i: c for i, c in enumerate(classes)}}, sort_keys=False),
            encoding="utf-8")
    return cfg, stats


def box_metrics(model, data: Path, split: str, device, project: Path, name: str) -> dict:
    res = model.val(data=str(data), split=split, imgsz=640, device=device, conf=0.001,
                    iou=0.6, project=str(project), name=name, exist_ok=True,
                    plots=False, verbose=False)
    out = {"mAP50": round(float(res.box.map50), 4), "mAP50_95": round(float(res.box.map), 4),
           "precision": round(float(res.box.mp), 4), "recall": round(float(res.box.mr), 4)}
    try:
        out["per_class_mAP50"] = {model.names[int(c)]: round(float(v), 4)
                                  for c, v in zip(res.box.ap_class_index, res.box.ap50)}
    except Exception:
        pass
    return out


def frame_metrics(model, task: str, splits: tuple[str, ...], dataset, device) -> dict:
    """Image-level: does the detector's max confidence separate class vs normal?"""
    from sklearn.metrics import roc_auc_score
    out: dict = {}
    for split in splits:
        frames = [f for f in load_frames((split,), dataset) if f.label in (task, "normal")]
        if not any(f.label == task for f in frames):
            continue
        scores = []
        for f in frames:
            r = model(str(f.path), conf=0.01, imgsz=640, device=device, verbose=False)[0]
            scores.append(float(r.boxes.conf.max()) if len(r.boxes) else 0.0)
        y = np.array([f.label == task for f in frames])
        s = np.array(scores)
        entry = {"n_pos": int(y.sum()), "n_neg": int((~y).sum()),
                 "auroc": round(float(roc_auc_score(y, s)), 4)}
        for thr in (0.25, 0.40, 0.50, 0.55, 0.70):
            entry[f"recall@{thr}"] = round(float((s[y] >= thr).mean()), 4)
            entry[f"fpr@{thr}"] = round(float((s[~y] >= thr).mean()), 4)
        out[split] = entry
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--task", required=True, choices=["weapon", "fire"])
    parser.add_argument("--init", required=True, help="starting weights")
    parser.add_argument("--classes", required=True, help="comma-separated output classes")
    parser.add_argument("--rehearsal", type=int, default=0)
    parser.add_argument("--retention", type=int, default=0)
    parser.add_argument("--pos-repeat", type=int, default=3,
                        help="copies of each training positive (class balance)")
    parser.add_argument("--epochs", type=int, default=40)
    parser.add_argument("--patience", type=int, default=12)
    parser.add_argument("--batch", type=int, default=16)
    parser.add_argument("--lr0", type=float, default=None)
    parser.add_argument("--freeze", type=int, default=None)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--dataset", type=Path, default=None)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    import torch
    from ultralytics import YOLO

    device = 0 if torch.cuda.is_available() else "cpu"
    classes = [c.strip() for c in args.classes.split(",") if c.strip()]
    init = Path(args.init)
    init_ref = str(init if init.exists() else ROOT / "training" / "notebooks" /
                   "_downloaded_weights" / args.init)
    if not Path(init_ref).exists():
        init_ref = args.init                         # let ultralytics resolve it
    data, stats = build_dataset(args, classes)
    print(f"dataset {data}: {stats}")

    run_dir = RUNS / args.tag
    started = time.time()
    model = YOLO(init_ref)
    train_kwargs = dict(data=str(data), epochs=args.epochs, imgsz=640, batch=args.batch,
                        patience=args.patience, project=str(RUNS), name=args.tag,
                        exist_ok=True, device=device, seed=args.seed, deterministic=True,
                        workers=4, plots=True, pretrained=True, verbose=False)
    if args.lr0 is not None:
        train_kwargs.update(lr0=args.lr0, optimizer="AdamW")
    if args.freeze is not None:
        train_kwargs["freeze"] = args.freeze
    model.train(**train_kwargs)
    minutes = round((time.time() - started) / 60, 1)

    best = run_dir / "weights" / "best.pt"
    tuned = YOLO(str(best))
    metrics = {
        "custom_val": box_metrics(tuned, data, "val", device, run_dir, "eval_val"),
        "frame_level": frame_metrics(tuned, args.task, ("val", "test"), args.dataset, device),
        "training_minutes": minutes,
        "dataset": stats,
    }
    if any(p.read_text().strip() for p in (data.parent / "labels" / "test").glob("*.txt")):
        metrics["custom_test"] = box_metrics(tuned, data, "test", device, run_dir, "eval_test")
    if args.retention:
        ret = data.parent / "retention.yaml"
        metrics["retention"] = {
            "finetuned": box_metrics(tuned, ret, "val", device, run_dir, "eval_retention"),
            "initial": box_metrics(YOLO(init_ref), ret, "val", device, run_dir,
                                   "eval_retention_init"),
        }
    print(json.dumps(metrics, indent=2))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(best, out)
    meta = {
        "model_name": out.stem,
        "architecture": f"YOLO ({Path(init_ref).name})",
        "init": Path(init_ref).name,
        "task": args.task,
        "classes": classes,
        "class_id_map": {str(i): c for i, c in enumerate(classes)},
        "image_size": 640,
        "dataset": "SENTRIX custom frame dataset + training/datasets/sentrix_custom/boxes.csv",
        "rehearsal_images": args.rehearsal,
        "positive_repeat": args.pos_repeat,
        "epochs_configured": args.epochs,
        "patience": args.patience,
        "seed": args.seed,
        "metrics": metrics,
        "run_id": datetime.now().strftime("%Y%m%d_%H%M%S"),
        "training_type": "real_data",
        "synthetic_data": False,
        "box_source": "GroundingDINO proposals gated by reviewed frame labels, "
                      "person-overlap check and visual QC (annotate_boxes.py)",
    }
    out.with_name(out.stem + "_metadata.json").write_text(json.dumps(meta, indent=2),
                                                          encoding="utf-8")
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(f"model written: {out} ({out.stat().st_size / 1e6:.1f} MB) in {minutes} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
