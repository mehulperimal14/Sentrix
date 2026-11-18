"""Per-model metrics for every vision model, on every dataset it applies to.

``tools/eval_custom_frames.py`` and ``tools/replay_custom_videos.py`` measure
the *product* (the TCI and the committed level); this measures each *model*
on its own, so a retrained model can be compared with the one it would
replace on the same images:

* **YOLO detectors** (person-free heads: weapon, fire) - box AP@0.5 per class
  the model and the dataset share (matched by name: ``pistol``, ``knife``,
  ``fire``), and image-level AUROC (an image's score is the model's highest
  confidence for those classes), on the public weapon ``test`` split, the
  public fire/smoke ``valid`` split, and the custom frames (``weapon`` or
  ``fire`` frames against ``normal`` frames; box AP on the QC'd custom boxes).
* **Scene classifier** - accuracy and macro-F1 with its deployed thresholds,
  and one-vs-rest AUROC per class, on the custom frames; cross-domain AUROC on
  public stills (p(weapon) on weapon images vs background images, p(fire) on
  fire images vs background images, p(assault) on RWF-2000 fight vs nofight
  frames).
* **Violence classifier** (the fallback) - AUROC fight vs nofight on RWF-2000
  frames (its own training corpus: in-sample) and assault vs normal on the
  custom frames.

Custom frames are reported in groups, because a model's training data
differs: ``old_train`` (the train split of the first ten videos: in-sample for
the v4 models), ``old_val``, ``old_test`` (``wa02``, reported, never used to
choose anything) and ``new`` (the five videos added on 2026-11-18, unseen by
every model trained before that day).

Usage::

    python tools/eval_models.py --tag baseline
    python tools/eval_models.py --tag ft --weapon models/v5_room/weapon.pt --scene ...

Reads images only; runs no pipeline and sends nothing.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sentrix.evaluation.custom_dataset import CLASSES, METADATA_DIR, load_frames  # noqa: E402
from sentrix.paths import MODELS_DIR  # noqa: E402

NEW_VIDEOS = {"mmtheft", "mmassault", "mmgun", "mmknife", "nvroom2"}
IMAGE_EXT = {".jpg", ".jpeg", ".png"}


# ── helpers ───────────────────────────────────────────────────────────────────
def long(path: Path) -> str:
    text = str(Path(path).resolve())
    return "\\\\?\\" + text if os.name == "nt" and not text.startswith("\\\\?\\") else text


def imread(path: Path) -> np.ndarray | None:
    import cv2
    with open(long(path), "rb") as fh:
        data = np.frombuffer(fh.read(), np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None


def auroc(pos, neg) -> float | None:
    pos, neg = np.asarray(pos, float), np.asarray(neg, float)
    if pos.size == 0 or neg.size == 0:
        return None
    scores = np.concatenate([pos, neg])
    order = scores.argsort(kind="mergesort")
    ranks = np.empty(len(scores))
    ranks[order] = np.arange(1, len(scores) + 1)
    for value in np.unique(scores):                      # average ties
        tied = scores == value
        ranks[tied] = ranks[tied].mean()
    return round(float((ranks[:pos.size].sum() - pos.size * (pos.size + 1) / 2)
                       / (pos.size * neg.size)), 4)


def iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def average_precision(preds: list[tuple[float, int, list]], gts: dict[int, list]) -> float | None:
    """VOC all-point AP@0.5 for one class. ``preds``: (conf, image, box);
    ``gts``: image -> list of boxes."""
    total = sum(len(v) for v in gts.values())
    if total == 0:
        return None
    used = {k: [False] * len(v) for k, v in gts.items()}
    tp, fp = [], []
    for conf, image, box in sorted(preds, key=lambda p: -p[0]):
        best, best_j = 0.0, -1
        for j, g in enumerate(gts.get(image, [])):
            o = iou(box, g)
            if o > best:
                best, best_j = o, j
        if best >= 0.5 and not used[image][best_j]:
            used[image][best_j] = True
            tp.append(1)
            fp.append(0)
        else:
            tp.append(0)
            fp.append(1)
    tp, fp = np.cumsum(tp), np.cumsum(fp)
    recall = tp / total
    precision = tp / np.maximum(tp + fp, 1e-9)
    mrec = np.concatenate([[0.0], recall, [1.0]])
    mpre = np.concatenate([[0.0], precision, [0.0]])
    for i in range(len(mpre) - 2, -1, -1):
        mpre[i] = max(mpre[i], mpre[i + 1])
    idx = np.where(mrec[1:] != mrec[:-1])[0]
    return round(float(((mrec[idx + 1] - mrec[idx]) * mpre[idx + 1]).sum()), 4)


def macro_f1(y_true: list[str], y_pred: list[str]) -> tuple[float, float, dict]:
    labels = sorted(set(y_true))
    f1s, per = [], {}
    for c in labels:
        tp = sum(t == c and p == c for t, p in zip(y_true, y_pred))
        fp = sum(t != c and p == c for t, p in zip(y_true, y_pred))
        fn = sum(t == c and p != c for t, p in zip(y_true, y_pred))
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        f1s.append(f1)
        per[c] = {"precision": round(prec, 4), "recall": round(rec, 4), "f1": round(f1, 4),
                  "n": sum(t == c for t in y_true)}
    acc = sum(t == p for t, p in zip(y_true, y_pred)) / max(len(y_true), 1)
    return round(acc, 4), round(float(np.mean(f1s)) if f1s else 0.0, 4), per


def group_of(frame) -> str:
    if frame.video in NEW_VIDEOS:
        return "new"
    return {"train": "old_train", "val": "old_val", "test": "old_test"}[frame.split]


# ── datasets ─────────────────────────────────────────────────────────────────
def yolo_split(root: Path, split: str, names_wanted: set[str]) -> list[dict]:
    """[{path, boxes: {class name: [xyxy]}}] for a YOLO split (pixel boxes)."""
    import yaml
    with open(long(root / "data.yaml"), encoding="utf-8") as fh:
        names = yaml.safe_load(fh)["names"]
    names = list(names.values()) if isinstance(names, dict) else list(names)
    items = []
    for image in sorted((root / split / "images").iterdir()):
        if image.suffix.lower() not in IMAGE_EXT:
            continue
        label = root / split / "labels" / f"{image.stem}.txt"
        boxes = defaultdict(list)
        if os.path.exists(long(label)):
            with open(long(label), encoding="utf-8") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) < 5:
                        continue
                    name = names[int(float(parts[0]))]
                    if len(parts) == 5:
                        cx, cy, w, h = (float(v) for v in parts[1:])
                    else:                                 # polygon -> its bounding box
                        xs, ys = [float(v) for v in parts[1::2]], [float(v) for v in parts[2::2]]
                        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
                        w, h = max(xs) - min(xs), max(ys) - min(ys)
                    boxes[name].append((cx, cy, w, h))
        items.append({"path": image, "norm_boxes": dict(boxes),
                      "any": bool(set(boxes) & names_wanted), "empty": not boxes})
    return items


def custom_boxes() -> dict[str, dict]:
    with open(METADATA_DIR / "boxes.csv", newline="", encoding="utf-8") as fh:
        return {r["image"]: r for r in csv.DictReader(fh)}


# ── detectors ────────────────────────────────────────────────────────────────
def predict(model, images: list[np.ndarray], batch: int = 16) -> list[list[tuple]]:
    """[(class name, conf, xyxy)] per image."""
    out = []
    for k in range(0, len(images), batch):
        results = model.predict(images[k:k + batch], conf=0.001, iou=0.6, imgsz=640,
                                verbose=False, max_det=100)
        for r in results:
            dets = []
            for box, conf, cls in zip(r.boxes.xyxy.cpu().numpy(), r.boxes.conf.cpu().numpy(),
                                      r.boxes.cls.cpu().numpy()):
                dets.append((model.names[int(cls)], float(conf), [float(v) for v in box]))
            out.append(dets)
    return out


def detector_on_public(model, items: list[dict], classes: list[str]) -> dict:
    shared = [c for c in classes if c in set(model.names.values())]
    if not shared:
        return {"skipped": f"model classes {sorted(set(model.names.values()))} share none of {classes}"}
    images = [imread(it["path"]) for it in items]
    preds = predict(model, images)
    out = {"images": len(items), "shared_classes": shared, "ap50": {}}
    for c in shared:
        p, g = [], {}
        for i, (it, dets, img) in enumerate(zip(items, preds, images)):
            h, w = img.shape[:2]
            g[i] = [[(cx - bw / 2) * w, (cy - bh / 2) * h, (cx + bw / 2) * w, (cy + bh / 2) * h]
                    for cx, cy, bw, bh in it["norm_boxes"].get(c, [])]
            p += [(conf, i, box) for name, conf, box in dets if name == c]
        out["ap50"][c] = average_precision(p, g)
    score = [max((conf for name, conf, _ in dets if name in shared), default=0.0) for dets in preds]
    pos = [s for s, it in zip(score, items) if it["any"]]
    neg = [s for s, it in zip(score, items) if it["empty"]]
    out["image_auroc_vs_background"] = auroc(pos, neg)
    out["n_pos_images"], out["n_background_images"] = len(pos), len(neg)
    return out


def detector_on_custom(model, frames, positive: str, box_classes: list[str]) -> dict:
    """Image score = the highest confidence among the classes that *vote* in
    the fusion (``settings.threat.detector_weapon_labels`` / ``_fire_labels``:
    a ``stick`` box is drawn and logged but never an alarm, D1); box AP for
    every class the model and the custom boxes share."""
    from sentrix.config import settings
    boxes = custom_boxes()
    model_classes = set(model.names.values())
    scored = [f for f in frames if f.label in (positive, "normal")]
    images = [imread(f.path) for f in scored]
    preds = predict(model, images)
    voting = set(settings.threat.detector_weapon_labels if positive == "weapon"
                 else settings.threat.detector_fire_labels)
    weapon_like = model_classes & voting
    out = {"voting_classes": sorted(weapon_like)}
    for group in ("old_train", "old_val", "old_test", "new"):
        idx = [i for i, f in enumerate(scored) if group_of(f) == group]
        if not idx:
            continue
        score = {i: max((c for n, c, _ in preds[i] if n in weapon_like), default=0.0) for i in idx}
        pos = [score[i] for i in idx if scored[i].label == positive]
        neg = [score[i] for i in idx if scored[i].label == "normal"]
        entry = {"n_pos": len(pos), "n_neg": len(neg), "image_auroc": auroc(pos, neg)}
        for thr in (0.25, 0.5):
            entry[f"recall@{thr}"] = round(float(np.mean([s >= thr for s in pos])), 4) if pos else None
            entry[f"fpr@{thr}"] = round(float(np.mean([s >= thr for s in neg])), 4) if neg else None
        aps = {}
        for c in sorted(model_classes & set(box_classes)):
            p, g = [], {}
            for i in idx:
                b = boxes.get(scored[i].path.name)
                if b is None or b["status"] not in ("box", "negative"):
                    continue                          # unknown ground truth: skip the image
                g[i] = ([[float(b[k]) for k in ("x1", "y1", "x2", "y2")]]
                        if b["status"] == "box" and b["box_class"] == c else [])
                p += [(conf, i, box) for name, conf, box in preds[i] if name == c]
            ap = average_precision(p, g)
            if ap is not None:
                aps[c] = ap
        entry["box_ap50"] = aps
        out[group] = entry
    return out


def eval_detector(weights: Path, data: Path, frames) -> dict:
    from ultralytics import YOLO
    model = YOLO(str(weights))
    names = set(model.names.values())
    out = {"weights": weights.relative_to(ROOT).as_posix() if weights.is_relative_to(ROOT)
           else str(weights), "classes": sorted(names)}
    if names & {"pistol", "knife"}:
        items = yolo_split(data / "weapon_data", "test", {"pistol", "knife"})
        out["public_weapon_test"] = detector_on_public(model, items, ["pistol", "knife"])
        out["custom_weapon"] = detector_on_custom(model, frames, "weapon",
                                                  ["pistol", "knife", "stick"])
    if "fire" in names:
        items = yolo_split(data / "fire_smoke_data", "valid", {"fire"})
        out["public_fire_valid"] = detector_on_public(model, items, ["fire"])
        out["custom_fire"] = detector_on_custom(model, frames, "fire", ["fire"])
    return out


# ── classifiers ──────────────────────────────────────────────────────────────
def score_images(fn, paths: list[Path], batch: int = 16) -> np.ndarray:
    out = []
    for k in range(0, len(paths), batch):
        out.append(fn([imread(p) for p in paths[k:k + batch]]))
    return np.concatenate(out) if out else np.zeros((0,))


def public_stills(data: Path, per_class: int, seed: int) -> dict[str, tuple[list, list]]:
    rng = random.Random(seed)
    weapon = yolo_split(data / "weapon_data", "test", {"pistol", "knife"})
    fire = yolo_split(data / "fire_smoke_data", "valid", {"fire"})
    rwf = {c: sorted((data / "violence_data" / c).iterdir()) for c in ("fight", "nofight")}
    pick = lambda xs: rng.sample(xs, min(per_class, len(xs)))  # noqa: E731
    return {
        "weapon": (pick([i["path"] for i in weapon if i["any"]]),
                   [i["path"] for i in weapon if i["empty"]]),
        "fire": (pick([i["path"] for i in fire if i["any"]]),
                 pick([i["path"] for i in fire if i["empty"]])),
        "assault": (pick(rwf["fight"]), pick(rwf["nofight"])),
    }


def eval_scene(weights: Path | None, frames, data: Path, per_class: int, seed: int) -> dict:
    from sentrix.perception.scene import SceneEngine, decide_label
    engine = SceneEngine(weights=weights)
    if not engine.available:
        return {"skipped": "scene model unavailable"}
    classes = list(engine.classes)
    probs = score_images(engine.score_frames, [f.path for f in frames])
    out = {"weights": engine.weights_name, "thresholds": dict(engine.thresholds),
           "temperature": engine.temperature, "custom": {}, "public": {}}
    rows = []
    for f, p in zip(frames, probs):
        pd = {c: float(p[i]) for i, c in enumerate(classes)}
        rows.append({"image": f.path.name, "video": f.video, "group": group_of(f),
                     "gt": f.label, "pred": decide_label(pd, engine.thresholds),
                     **{f"p_{c}": round(pd[c], 5) for c in classes}})
    for group in ("old_train", "old_val", "old_test", "new"):
        g = [r for r in rows if r["group"] == group]
        if not g:
            continue
        acc, mf1, per = macro_f1([r["gt"] for r in g], [r["pred"] for r in g])
        aurocs = {c: auroc([r[f"p_{c}"] for r in g if r["gt"] == c],
                           [r[f"p_{c}"] for r in g if r["gt"] != c]) for c in classes
                  if c != "normal"}
        out["custom"][group] = {"n": len(g), "accuracy": acc, "macro_f1": mf1, "per_class": per,
                                "auroc_one_vs_rest": aurocs}
    for cls, (pos, neg) in public_stills(data, per_class, seed).items():
        i = classes.index(cls)
        pp = score_images(engine.score_frames, pos)[:, i]
        pn = score_images(engine.score_frames, neg)[:, i]
        out["public"][cls] = {"n_pos": len(pos), "n_neg": len(neg),
                              "auroc": auroc(pp, pn),
                              "recall_at_threshold": round(float(np.mean(pp >= engine.thresholds[cls])), 4),
                              "fpr_at_threshold": round(float(np.mean(pn >= engine.thresholds[cls])), 4)}
    out["_rows"] = rows
    return out


def eval_violence(frames, data: Path, per_class: int, seed: int) -> dict:
    from sentrix.perception.violence import ViolenceEngine
    engine = ViolenceEngine()
    if not engine.available:
        return {"skipped": "violence model unavailable"}
    fn = lambda imgs: np.array([engine._score_window([im]) for im in imgs])  # noqa: E731
    pos, neg = public_stills(data, per_class, seed)["assault"]
    out = {"rwf2000_in_sample": {"n_pos": len(pos), "n_neg": len(neg),
                                 "auroc": auroc(score_images(fn, pos), score_images(fn, neg))}}
    custom = [f for f in frames if f.label in ("assault", "normal")]
    s = score_images(fn, [f.path for f in custom])
    out["custom_assault_vs_normal"] = auroc([v for v, f in zip(s, custom) if f.label == "assault"],
                                            [v for v, f in zip(s, custom) if f.label == "normal"])
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data", type=Path, default=Path(os.getenv("SENTRIX_DATA_DIR",
                                                                     "G:/Capstone/data")))
    parser.add_argument("--tag", required=True)
    parser.add_argument("--weapon", type=Path, action="append", default=None,
                        help="weapon detector weights (repeatable); default v4_custom and v3_real")
    parser.add_argument("--fire", type=Path, action="append", default=None,
                        help="fire detector weights (repeatable); default v4_custom and v3_real")
    parser.add_argument("--scene", type=Path, default=None, help="scene weights; default shipped")
    parser.add_argument("--skip", default="", help="comma list: detectors,scene,violence")
    parser.add_argument("--per-class", type=int, default=600, help="public stills per class")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    skip = set(filter(None, args.skip.split(",")))
    frames = load_frames()                                # every split, every video
    report = {"tag": args.tag, "data": str(args.data), "frames": len(frames),
              "groups": {g: sum(group_of(f) == g for f in frames)
                         for g in ("old_train", "old_val", "old_test", "new")}}
    started = time.time()
    if "detectors" not in skip:
        weapons = args.weapon or [MODELS_DIR / "v4_custom" / "weapon_detector_custom_v4.pt",
                                  MODELS_DIR / "v3_real" / "weapon_detector_real_v3.pt"]
        fires = args.fire or [MODELS_DIR / "v4_custom" / "fire_detector_custom_v4.pt",
                              MODELS_DIR / "v3_real" / "fire_smoke_detector_real_v3.pt"]
        report["detectors"] = {}
        for w in [*weapons, *fires]:
            print(f"detector {w.name} ...", flush=True)
            report["detectors"][w.stem] = eval_detector(w, args.data, frames)
    rows = []
    if "scene" not in skip:
        print("scene classifier ...", flush=True)
        report["scene"] = eval_scene(args.scene, frames, args.data, args.per_class, args.seed)
        rows = report["scene"].pop("_rows", [])
    if "violence" not in skip:
        print("violence classifier ...", flush=True)
        report["violence"] = eval_violence(frames, args.data, args.per_class, args.seed)
    report["seconds"] = round(time.time() - started, 1)
    out = ROOT / "training" / "runs" / "eval" / args.tag
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if rows:
        with open(out / "scene_predictions.csv", "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    print(json.dumps(report, indent=1)[:6000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
