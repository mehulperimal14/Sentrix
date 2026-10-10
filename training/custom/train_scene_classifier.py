"""Train the five-class scene classifier on the SENTRIX custom dataset.

One recipe, two starting points:

``--init violence_v3``
    ResNet-18 initialised from ``models/v3_real/violence_classifier_real_v3.pt``
    — the model the V3 retraining notebook produced. The backbone is kept, and
    the new head's ``normal``/``assault`` rows start from the old
    ``nofight``/``fight`` rows, so the fight direction the notebook learned is
    carried over. ``--rehearsal`` mixes RWF-2000 frames back in (fight ->
    assault, nofight -> normal) so fine-tuning does not erase what the model
    knew about fights outside these two rooms. (branch ``sentrix/finetuned-v3``)

``--init pretrained``
    Any architecture from its generic public weights (torchvision ImageNet, or
    ``timm:<name>`` — e.g. self-supervised DINOv2), trained on this dataset
    only. (branch ``sentrix/custom-v4``)

Subcommands
-----------
``cv``   leave-one-video-out over the nine train + val videos. **The test video
         (wa02) is never loaded.** Writes pooled out-of-fold (OOF) predictions,
         fits a temperature and per-class thresholds on them, and reports OOF
         metrics. This is what model selection uses: with one test video and
         val missing two classes, a single split cannot rank candidates.
``fit``  trains on the train split, applies the temperature/thresholds from a
         ``cv`` run, reports held-out val and test, and writes the checkpoint.

Examples::

    python training/custom/train_scene_classifier.py cv  --init violence_v3 \\
        --arch resnet18 --rehearsal 600 --tag ft_r18_reh
    python training/custom/train_scene_classifier.py fit --init violence_v3 \\
        --arch resnet18 --rehearsal 600 --cv-tag ft_r18_reh \\
        --out models/v4_finetuned/scene_classifier_ft_v4.pt
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sentrix.evaluation.custom_dataset import CLASSES, Frame, load_frames  # noqa: E402
from sentrix.evaluation.metrics import classification_report  # noqa: E402
from sentrix.perception.scene import (PRIORITY, base_fingerprint,  # noqa: E402
                                      build_scene_model, decide_label)

VIOLENCE_V3 = ROOT / "models" / "v3_real" / "violence_classifier_real_v3.pt"
RWF_DIR = Path(os.getenv("SENTRIX_VIOLENCE_DATA", ROOT / "data" / "violence_data"))
RUNS = ROOT / "training" / "runs" / "custom"
MEAN, STD = [0.485, 0.456, 0.406], [0.229, 0.224, 0.225]
NORMAL, ASSAULT = CLASSES.index("normal"), CLASSES.index("assault")
#: Robbery is one event in one video: out-of-fold it is never seen, so its
#: threshold cannot be tuned. Fixed, and deliberately above an argmax tie.
ROBBERY_THRESHOLD = 0.60
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def seed_all(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


# ── data ──────────────────────────────────────────────────────────────────────
_CACHE: dict[tuple[str, int], torch.Tensor] = {}


def load_image(path: Path, size: int) -> torch.Tensor:
    """uint8 CHW, squashed to ``size`` x ``size`` with PIL bilinear.

    Identical to the runtime's ``transforms.Resize((s, s))`` on a PIL image, so
    what the model is evaluated on here is what it sees in production.
    """
    key = (str(path), size)
    if key not in _CACHE:
        img = Image.open(path).convert("RGB").resize((size, size), Image.BILINEAR)
        _CACHE[key] = torch.from_numpy(np.asarray(img).copy()).permute(2, 0, 1)
    return _CACHE[key]


def rwf_items(csv_name: str, per_class: int, seed: int) -> list[tuple[Path, int]]:
    """One frame per RWF clip, ``per_class`` clips per class. fight -> assault."""
    rng = random.Random(seed)
    clips: dict[tuple[str, str], list[str]] = {}
    with open(RWF_DIR / csv_name, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            clips.setdefault((row["label"], row["clip"]), []).append(
                Path(row["frame_path"]).name)
    items: list[tuple[Path, int]] = []
    for label, target in (("fight", ASSAULT), ("nofight", NORMAL)):
        keys = sorted(k for k in clips if k[0] == label)
        rng.shuffle(keys)
        for key in keys[:per_class]:
            path = RWF_DIR / label / rng.choice(sorted(clips[key]))
            if path.exists():
                items.append((path, target))
    return items


class ImageSet(torch.utils.data.Dataset):
    def __init__(self, items: list[tuple[Path, int]], size: int, train: bool) -> None:
        from torchvision.transforms import v2 as T
        self.items, self.train = items, train
        self.load_size = int(round(size * 1.15)) if train else size
        norm = [T.ToDtype(torch.float32, scale=True), T.Normalize(MEAN, STD)]
        if train:
            self.tf = T.Compose([
                T.RandomResizedCrop(size, scale=(0.55, 1.0), ratio=(0.75, 1.33),
                                    antialias=True),
                T.RandomHorizontalFlip(),
                T.RandomApply([T.ColorJitter(0.3, 0.3, 0.25, 0.03)], p=0.8),
                # The corridor is grey CCTV, the bedroom colour handheld video.
                T.RandomGrayscale(p=0.15),
                T.RandomApply([T.GaussianBlur(5, sigma=(0.1, 1.5))], p=0.2),
                *norm,
                T.RandomErasing(p=0.2, scale=(0.02, 0.12)),
            ])
        else:
            self.tf = T.Compose(norm)

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, i):
        path, y = self.items[i]
        return self.tf(load_image(path, self.load_size)), y


def frame_items(frames: list[Frame]) -> list[tuple[Path, int]]:
    return [(f.path, f.class_id) for f in frames]


# ── model ─────────────────────────────────────────────────────────────────────
def build(arch: str, init: str, size: int = 224) -> nn.Module:
    if init == "violence_v3":
        if arch != "resnet18":
            raise SystemExit("--init violence_v3 requires --arch resnet18")
        net = build_scene_model("resnet18", len(CLASSES), pretrained=False)
        ckpt = torch.load(VIOLENCE_V3, map_location="cpu", weights_only=False)
        old = ckpt["model_state_dict"]
        body = {k: v for k, v in old.items() if not k.startswith("fc.")}
        missing, unexpected = net.load_state_dict(body, strict=False)
        assert not unexpected and set(missing) == {"fc.1.weight", "fc.1.bias"}, missing
        # carry the learned fight direction into the new head
        old_classes = list(ckpt.get("classes", ["nofight", "fight"]))
        with torch.no_grad():
            for new_idx, old_name in ((NORMAL, "nofight"), (ASSAULT, "fight")):
                j = old_classes.index(old_name)
                net.fc[1].weight[new_idx] = old["fc.1.weight"][j]
                net.fc[1].bias[new_idx] = old["fc.1.bias"][j]
        return net
    if init in ("pretrained", "imagenet"):
        return build_scene_model(arch, len(CLASSES), pretrained=True, image_size=size)
    raise SystemExit(f"unknown --init {init!r}")


def freeze(net: nn.Module, train_last_blocks: int | None) -> None:
    """Train only the head (``0``) or the head plus the last N ViT blocks.

    Screening showed frozen DINOv2 features generalise across videos better
    than any fine-tuned CNN; updating the whole backbone on eight videos
    mostly teaches it which room it is in.
    """
    if train_last_blocks is None:
        return
    head = {id(p) for p in head_params(net)}
    for p in net.parameters():
        p.requires_grad = id(p) in head
    if train_last_blocks > 0:
        blocks = getattr(net, "blocks", None)
        if blocks is None:
            raise SystemExit("--train-last-blocks needs a transformer with .blocks")
        for block in list(blocks)[-train_last_blocks:]:
            for p in block.parameters():
                p.requires_grad = True
        for p in getattr(net, "norm", nn.Identity()).parameters():
            p.requires_grad = True


def head_params(net: nn.Module) -> list[nn.Parameter]:
    head = net.fc if hasattr(net, "fc") and isinstance(net.fc, nn.Sequential) \
        else net.get_classifier()
    return list(head.parameters())


def train_model(args, items: list[tuple[Path, int]], rehearsal: list[tuple[Path, int]],
                log_prefix: str = "") -> nn.Module:
    seed_all(args.seed)
    net = build(args.arch, args.init, args.size)
    freeze(net, args.train_last_blocks)
    net = net.to(DEVICE)
    all_items = items + rehearsal
    dataset = ImageSet(all_items, args.size, train=True)

    counts = Counter(y for _, y in items)
    weights = [1.0 / math.sqrt(counts[y]) for _, y in items]
    if rehearsal:
        # rehearsal frames take ``rehearsal_share`` of the draws, split evenly
        custom_mass = sum(weights)
        each = custom_mass * args.rehearsal_share / (1 - args.rehearsal_share) / len(rehearsal)
        weights += [each] * len(rehearsal)
    epoch_size = int(len(items) / (1 - args.rehearsal_share)) if rehearsal else len(items)
    sampler = torch.utils.data.WeightedRandomSampler(weights, epoch_size, replacement=True)
    loader = torch.utils.data.DataLoader(dataset, batch_size=args.batch, sampler=sampler,
                                         num_workers=0, drop_last=True)

    head = head_params(net)
    head_ids = {id(p) for p in head}
    body = [p for p in net.parameters() if id(p) not in head_ids and p.requires_grad]
    groups = [{"params": head, "lr": args.lr * args.head_lr_mult}]
    if body:
        groups.append({"params": body, "lr": args.lr})
    opt = torch.optim.AdamW(groups, weight_decay=args.weight_decay)
    steps = args.epochs * len(loader)
    warm = len(loader)
    sched = torch.optim.lr_scheduler.LambdaLR(
        opt, lambda s: min(1.0, (s + 1) / warm) * 0.5 * (1 + math.cos(math.pi * min(s / steps, 1.0))))
    scaler = torch.amp.GradScaler(enabled=DEVICE.type == "cuda")
    loss_fn = nn.CrossEntropyLoss(label_smoothing=args.label_smoothing)

    for epoch in range(1, args.epochs + 1):
        net.train()
        t0, total, seen = time.time(), 0.0, 0
        for x, y in loader:
            x, y = x.to(DEVICE, non_blocking=True), y.to(DEVICE, non_blocking=True)
            with torch.autocast(DEVICE.type, enabled=DEVICE.type == "cuda"):
                loss = loss_fn(net(x), y)
            opt.zero_grad(set_to_none=True)
            scaler.scale(loss).backward()
            scaler.unscale_(opt)
            nn.utils.clip_grad_norm_([p for p in net.parameters() if p.requires_grad], 5.0)
            scaler.step(opt)
            scaler.update()
            sched.step()
            total += loss.item() * len(y)
            seen += len(y)
        if args.verbose or epoch == args.epochs:
            print(f"{log_prefix}epoch {epoch:2}/{args.epochs} loss {total / max(seen, 1):.4f} "
                  f"({time.time() - t0:.1f}s)")
    return net


@torch.no_grad()
def predict_logits(net: nn.Module, items: list[tuple[Path, int]], size: int) -> np.ndarray:
    net.eval()
    loader = torch.utils.data.DataLoader(ImageSet(items, size, train=False),
                                         batch_size=64, shuffle=False)
    out = []
    for x, _ in loader:
        with torch.autocast(DEVICE.type, enabled=DEVICE.type == "cuda"):
            out.append(net(x.to(DEVICE)).float().cpu())
    return torch.cat(out).numpy()


# ── calibration and thresholds ────────────────────────────────────────────────
def softmax(logits: np.ndarray, t: float = 1.0) -> np.ndarray:
    z = logits / t
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def fit_temperature(logits: np.ndarray, y: np.ndarray) -> float:
    """Temperature minimising NLL (grid, then refine). Classic Guo et al. 2017."""
    def nll(t):
        p = softmax(logits, t)
        return float(-np.mean(np.log(p[np.arange(len(y)), y] + 1e-12)))
    grid = np.exp(np.linspace(np.log(0.3), np.log(5.0), 60))
    best = min(grid, key=nll)
    fine = np.linspace(best * 0.9, best * 1.1, 21)
    return float(min(fine, key=nll))


def labels_from(probs: np.ndarray, thresholds: dict[str, float]) -> list[str]:
    return [decide_label({c: float(p[i]) for i, c in enumerate(CLASSES)}, thresholds)
            for p in probs]


def tune_thresholds(probs: np.ndarray, y_true: list[str]) -> dict[str, float]:
    """Coordinate ascent on OOF macro-F1 over fire, weapon, assault."""
    thr = {c: 0.5 for c in PRIORITY}
    thr["robbery"] = ROBBERY_THRESHOLD
    grid = [round(v, 2) for v in np.arange(0.20, 0.86, 0.05)]

    def score(t):
        return classification_report(y_true, labels_from(probs, t))["macro_f1"]

    best = score(thr)
    for _ in range(3):
        improved = False
        for cls in ("fire", "weapon", "assault"):
            for v in grid:
                trial = {**thr, cls: v}
                s = score(trial)
                # prefer the higher threshold on ties: fewer false alarms
                if s > best + 1e-9 or (abs(s - best) <= 1e-9 and v > thr[cls]):
                    best, thr, improved = s, trial, True
        if not improved:
            break
    return thr


# ── retention on RWF-2000 ─────────────────────────────────────────────────────
def retention(net: nn.Module, size: int, seed: int, per_class: int = 200) -> dict:
    """Does P(assault) still separate RWF fight from nofight frames?"""
    from sklearn.metrics import roc_auc_score
    items = rwf_items("val.csv", per_class, seed + 1)
    logits = predict_logits(net, items, size)
    probs = softmax(logits)
    y = np.array([1 if t == ASSAULT else 0 for _, t in items])
    fight_score = probs[:, ASSAULT] / (probs[:, ASSAULT] + probs[:, NORMAL] + 1e-9)
    return {"n": int(len(y)), "auroc": round(float(roc_auc_score(y, fight_score)), 4),
            "accuracy": round(float(((fight_score >= 0.5) == y).mean()), 4)}


# ── commands ──────────────────────────────────────────────────────────────────
def cmd_cv(args) -> int:
    frames = [f for f in load_frames(("train", "val"), args.dataset)]
    videos = sorted({f.video for f in frames})
    rehearsal = rwf_items("train.csv", args.rehearsal, args.seed) if args.rehearsal else []
    out = RUNS / "scene_cv" / args.tag
    out.mkdir(parents=True, exist_ok=True)
    print(f"CV {args.tag}: {len(videos)} folds, {len(frames)} frames, "
          f"{len(rehearsal)} rehearsal frames, device {DEVICE}")

    oof_logits = np.zeros((len(frames), len(CLASSES)), dtype=np.float32)
    started = time.time()
    for k, video in enumerate(videos, 1):
        train_items = frame_items([f for f in frames if f.video != video])
        held = [i for i, f in enumerate(frames) if f.video == video]
        net = train_model(args, train_items, rehearsal, log_prefix=f"  [{k}/{len(videos)} {video}] ")
        oof_logits[held] = predict_logits(net, frame_items([frames[i] for i in held]), args.size)
        del net
        torch.cuda.empty_cache()

    y_idx = np.array([f.class_id for f in frames])
    y_true = [f.label for f in frames]
    temperature = fit_temperature(oof_logits, y_idx)
    probs = softmax(oof_logits, temperature)
    argmax = [CLASSES[i] for i in probs.argmax(1)]
    default_thr = {**{c: 0.5 for c in PRIORITY}, "robbery": ROBBERY_THRESHOLD}
    thresholds = tune_thresholds(probs, y_true)
    reports = {
        "argmax": classification_report(y_true, argmax),
        "thr_0.5": classification_report(y_true, labels_from(probs, default_thr)),
        "tuned": classification_report(y_true, labels_from(probs, thresholds)),
    }
    per_video = {v: classification_report(
        [y_true[i] for i, f in enumerate(frames) if f.video == v],
        [labels_from(probs[[i]], thresholds)[0] for i, f in enumerate(frames) if f.video == v])
        ["accuracy"] for v in videos}
    summary = {"tag": args.tag, "config": vars_json(args), "folds": videos,
               "temperature": temperature, "thresholds": thresholds,
               "minutes": round((time.time() - started) / 60, 1),
               "reports": reports, "per_video_accuracy": per_video}
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    with open(out / "oof.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["image", "video", "gt", *[f"logit_{c}" for c in CLASSES]])
        for f, lg in zip(frames, oof_logits):
            w.writerow([f.path.name, f.video, f.label, *[round(float(v), 5) for v in lg]])

    tuned = reports["tuned"]
    print(f"\nOOF {args.tag}: T={temperature:.2f} thresholds={thresholds}")
    for name, rep in reports.items():
        pc = rep["per_class"]
        print(f"  {name:7} acc={rep['accuracy']:.3f} macroF1={rep['macro_f1']:.3f} "
              + " ".join(f"{c}={pc[c]['f1']:.2f}" for c in CLASSES)
              + f" threatFPR={rep['threat_detection']['false_positive_rate']:.3f}")
    print("  per-video acc: " + " ".join(f"{v}={a:.2f}" for v, a in per_video.items()))
    print(f"  macro-F1 excl. robbery: "
          f"{np.mean([tuned['per_class'][c]['f1'] for c in CLASSES[:4]]):.3f}")
    print(f"written {out}")
    return 0


def cmd_fit(args) -> int:
    if not args.cv_tag:
        raise SystemExit("fit needs --cv-tag: temperature and thresholds come from CV")
    cv = json.loads((RUNS / "scene_cv" / args.cv_tag / "summary.json").read_text(encoding="utf-8"))
    for key in ("arch", "init", "size", "epochs", "lr", "rehearsal", "train_last_blocks"):
        if cv["config"].get(key) != getattr(args, key):
            raise SystemExit(f"--{key} differs from cv run {args.cv_tag}: "
                             f"{getattr(args, key)} vs {cv['config'].get(key)}")
    temperature, thresholds = cv["temperature"], cv["thresholds"]

    train = load_frames(("train",), args.dataset)
    rehearsal = rwf_items("train.csv", args.rehearsal, args.seed) if args.rehearsal else []
    started = time.time()
    net = train_model(args, frame_items(train), rehearsal, log_prefix="  ")
    minutes = round((time.time() - started) / 60, 1)

    metrics: dict = {"cv_oof": cv["reports"]["tuned"], "cv_tag": args.cv_tag}
    rows = []
    for split in ("val", "test"):
        frames = load_frames((split,), args.dataset)
        probs = softmax(predict_logits(net, frame_items(frames), args.size), temperature)
        pred = labels_from(probs, thresholds)
        metrics[split] = classification_report([f.label for f in frames], pred)
        rows += [[f.path.name, split, f.video, f.label, p,
                  *[round(float(v), 4) for v in pr]] for f, p, pr in zip(frames, pred, probs)]
        m = metrics[split]
        print(f"  {split}: acc={m['accuracy']:.3f} macroF1={m['macro_f1']:.3f} "
              f"threatR={m['threat_detection']['recall']:.3f} "
              f"FPR={m['threat_detection']['false_positive_rate']:.3f}")
    if args.init == "violence_v3" or args.rehearsal:
        metrics["rwf_retention"] = retention(net, args.size, args.seed)
        print(f"  RWF-2000 retention: {metrics['rwf_retention']}")

    run_dir = RUNS / "scene" / args.tag
    run_dir.mkdir(parents=True, exist_ok=True)
    with open(run_dir / "predictions.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["image", "split", "video", "gt", "pred", *[f"p_{c}" for c in CLASSES]])
        w.writerows(rows)

    full = net.state_dict()
    storage = "full"
    if args.delta:
        # Only what training changed, plus the position embedding (timm
        # resamples it to the input size, and that resampling may differ
        # between timm versions). Everything else is the public checkpoint.
        keep = {n for n, p in net.named_parameters() if p.requires_grad}
        keep |= {n for n in full if n == "pos_embed"}
        fingerprint = base_fingerprint(full, frozenset(keep))
        full = {k: v for k, v in full.items() if k in keep}
        storage = "delta"
    state = {k: (v.half() if args.fp16 and v.is_floating_point() else v)
             for k, v in full.items()}
    init_from = VIOLENCE_V3.name if args.init == "violence_v3" else f"{args.arch} pretrained"
    ckpt = {
        "model_state_dict": state,
        "state_dtype": "float16" if args.fp16 else "float32",
        "storage": storage,
        "architecture": args.arch,
        "classes": list(CLASSES),
        "image_size": args.size,
        "normalization": {"mean": MEAN, "std": STD},
        "input_format": "RGB, float32, NCHW, ImageNet-normalised, squashed resize",
        "temperature": temperature,
        "thresholds": thresholds,
        "init": init_from,
        "training": {**vars_json(args), "minutes": minutes, "train_frames": len(train),
                     "rehearsal_frames": len(rehearsal)},
        "metrics": {k: v for k, v in metrics.items() if k != "cv_oof"},
        "run_id": datetime.now().strftime("%Y%m%d_%H%M%S"),
        **({"base_model": args.arch, "base_fingerprint": fingerprint} if args.delta else {}),
        "training_type": "real_data",
        "synthetic_data": False,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    torch.save(ckpt, out)
    meta = {k: v for k, v in ckpt.items() if k != "model_state_dict"}
    meta.update(model_name=out.stem, dataset="SENTRIX custom frame dataset "
                "(training/datasets/sentrix_custom)", metrics=metrics)
    out.with_name(out.stem + "_metadata.json").write_text(json.dumps(meta, indent=2),
                                                          encoding="utf-8")
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(f"  model written: {out} ({out.stat().st_size / 1e6:.1f} MB) in {minutes} min")
    return 0


def vars_json(args) -> dict:
    return {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()
            if k not in ("func",)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, func in (("cv", cmd_cv), ("fit", cmd_fit)):
        p = sub.add_parser(name)
        p.set_defaults(func=func)
        p.add_argument("--arch", default="resnet18")
        p.add_argument("--init", default="pretrained",
                       choices=["pretrained", "imagenet", "violence_v3"],
                       help="violence_v3: the notebook's ResNet-18; pretrained (alias "
                            "imagenet): the architecture's generic public weights")
        p.add_argument("--size", type=int, default=224)
        p.add_argument("--epochs", type=int, default=15)
        p.add_argument("--batch", type=int, default=32)
        p.add_argument("--lr", type=float, default=2e-4)
        p.add_argument("--head-lr-mult", type=float, default=10.0)
        p.add_argument("--weight-decay", type=float, default=0.05)
        p.add_argument("--label-smoothing", type=float, default=0.1)
        p.add_argument("--rehearsal", type=int, default=0,
                       help="RWF-2000 clips per class mixed into training")
        p.add_argument("--rehearsal-share", type=float, default=0.3)
        p.add_argument("--train-last-blocks", type=int, default=None,
                       help="freeze the backbone except the head (0) or the head "
                            "plus the last N transformer blocks")
        p.add_argument("--seed", type=int, default=42)
        p.add_argument("--dataset", type=Path, default=None)
        p.add_argument("--tag", required=True)
        p.add_argument("--verbose", action="store_true")
        if name == "fit":
            p.add_argument("--cv-tag", required=True)
            p.add_argument("--out", required=True)
            p.add_argument("--fp16", action="store_true",
                           help="store weights as float16 (halves the file)")
            p.add_argument("--delta", action="store_true",
                           help="store only the trained tensors plus a fingerprint "
                                "of the frozen public weights (needs --init "
                                "pretrained and --train-last-blocks)")
    args = parser.parse_args(argv)
    if getattr(args, "delta", False) and (args.init == "violence_v3"
                                          or args.train_last_blocks is None):
        raise SystemExit("--delta needs --init pretrained and --train-last-blocks")
    if not args.rehearsal:
        args.rehearsal_share = 0.0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
