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
    only. (branch ``sentrix/custom-v4``; the slim Raspberry Pi heads
    ``timm:efficientnet_b0`` and ``timm:mobilenetv3_large_100`` of
    ``models/v5_slim/``)

``--train-last-blocks N`` freezes everything but the head and the last N
``blocks``: a ViT's transformer blocks, or a timm CNN's stages (EfficientNet-B0
and MobileNetV3-Large have seven). Frozen BatchNorm layers keep their
pretrained statistics.

``--teacher TAG`` distils from the recipe of an earlier ``cv`` run (e.g. the
DINOv2 ``dino_b_last4``). The teacher is retrained on exactly the student's
training frames - per fold in ``cv``, on the train split in ``fit`` - so it has
never seen the frames the student is scored on; trained teachers are cached
(``SENTRIX_TEACHER_CACHE``). Teacher and student see the same augmented batch
(function matching, Beyer et al. 2022), mixed up with ``--mixup``.

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
    python training/custom/train_scene_classifier.py cv --arch timm:efficientnet_b0 \\
        --teacher dino_b_last4 --mixup 1.0 --epochs 40 --tag effb0_kd
"""

from __future__ import annotations

import argparse
import csv
import hashlib
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
                                      build_scene_model, decide_label, merge_delta)

VIOLENCE_V3 = ROOT / "models" / "v3_real" / "violence_classifier_real_v3.pt"
RWF_DIR = Path(os.getenv("SENTRIX_VIOLENCE_DATA", ROOT / "data" / "violence_data"))
RUNS = ROOT / "training" / "runs" / "custom"
#: Teachers trained for distillation, one per training set. Two students share
#: them, and a DINOv2 teacher takes minutes to train, so they are cached -
#: outside the repository (a DINOv2 delta is ~110 MB).
TEACHER_CACHE = Path(os.getenv("SENTRIX_TEACHER_CACHE",
                               Path.home() / ".cache" / "sentrix" / "scene_teachers"))
MEAN, STD = [0.485, 0.456, 0.406], [0.229, 0.224, 0.225]
NORMAL, ASSAULT = CLASSES.index("normal"), CLASSES.index("assault")
#: Robbery is one event in one video: out-of-fold it is never seen, so its
#: threshold cannot be tuned. Fixed, and deliberately above an argmax tie.
ROBBERY_THRESHOLD = 0.60
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
#: Options added after the first ``cv`` runs were written, with the value that
#: reproduces those runs - so an old summary still describes its recipe.
RECIPE_DEFAULTS = {"teacher": None, "kd_weight": 0.5, "kd_temperature": 2.0, "mixup": 0.0}
#: Everything that changes a trained model. A teacher cache entry is keyed by
#: these and by the exact training frames.
RECIPE_KEYS = ("arch", "init", "size", "epochs", "batch", "lr", "head_lr_mult",
               "weight_decay", "label_smoothing", "rehearsal", "rehearsal_share",
               "train_last_blocks", "seed", *RECIPE_DEFAULTS)


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
    """Train only the head (``0``) or the head plus the last N ``blocks``.

    Screening showed frozen DINOv2 features generalise across videos better
    than any fine-tuned CNN; updating the whole backbone on eight videos
    mostly teaches it which room it is in.

    ``blocks`` are a ViT's transformer blocks or a timm CNN's stages. What runs
    after the last block trains with it, because the head reads its output: a
    ViT's final norm, a CNN's 1x1 head convolution and its BatchNorm
    (EfficientNet ``conv_head``/``bn2``, MobileNetV3 ``conv_head``/``norm_head``).
    """
    if train_last_blocks is None:
        return
    head = {id(p) for p in head_params(net)}
    for p in net.parameters():
        p.requires_grad = id(p) in head
    if train_last_blocks > 0:
        blocks = getattr(net, "blocks", None)
        if blocks is None:
            raise SystemExit("--train-last-blocks needs a model with .blocks "
                             "(ViT blocks or timm CNN stages)")
        for module in [*list(blocks)[-train_last_blocks:], *post_block_modules(net)]:
            for p in module.parameters():
                p.requires_grad = True


def post_block_modules(net: nn.Module) -> list[nn.Module]:
    """The children of ``net`` that run after ``net.blocks``, in order.

    timm declares a model's children in forward order, so this is a ViT's
    ``norm``/``fc_norm``/``head`` and a CNN's ``conv_head``, its norm, pooling
    and ``classifier``.
    """
    children = list(net.named_children())
    names = [name for name, _ in children]
    if "blocks" not in names:
        return []
    return [module for _, module in children[names.index("blocks") + 1:]]


def train_mode(net: nn.Module) -> None:
    """``net.train()``, except BatchNorm layers whose weights are frozen.

    In train mode BatchNorm keeps re-estimating its running statistics from
    the batches, weights frozen or not, so a "frozen" stage would drift
    towards these eight videos - and a delta checkpoint, which stores only
    what training changed, would silently miss the drift. A ViT has no
    BatchNorm and a fully trained network has none frozen: both unchanged.
    """
    net.train()
    for module in net.modules():
        if isinstance(module, nn.modules.batchnorm._BatchNorm):
            params = list(module.parameters(recurse=False))
            if params and not any(p.requires_grad for p in params):
                module.eval()


def delta_state(net: nn.Module) -> tuple[dict[str, torch.Tensor], str]:
    """The tensors training changed, and a fingerprint of everything else.

    Kept: the trainable parameters, the buffers (BatchNorm running statistics)
    of every module that trained, and the position embedding (timm resamples
    it to the input size, and that resampling may differ between timm
    versions). Everything else is the public checkpoint, which
    :func:`sentrix.perception.scene.merge_delta` verifies against the
    fingerprint before overlaying the delta.
    """
    full = net.state_dict()
    keep = {n for n, p in net.named_parameters() if p.requires_grad}
    keep |= {n for n in full if n == "pos_embed"}
    for name, module in net.named_modules():
        if any(p.requires_grad for p in module.parameters(recurse=False)):
            keep |= {f"{name}.{b}" if name else b
                     for b, _ in module.named_buffers(recurse=False)}
    fingerprint = base_fingerprint(full, frozenset(keep))
    return {k: v for k, v in full.items() if k in keep}, fingerprint


def head_params(net: nn.Module) -> list[nn.Parameter]:
    head = net.fc if hasattr(net, "fc") and isinstance(net.fc, nn.Sequential) \
        else net.get_classifier()
    return list(head.parameters())


def train_model(args, items: list[tuple[Path, int]], rehearsal: list[tuple[Path, int]],
                log_prefix: str = "", teacher: nn.Module | None = None,
                teacher_size: int | None = None) -> nn.Module:
    """Train one model. With ``teacher``, distil from it (``args.kd_weight``)."""
    seed_all(args.seed)
    net = build(args.arch, args.init, args.size)
    freeze(net, args.train_last_blocks)
    net = net.to(DEVICE)
    mixup = float(getattr(args, "mixup", 0.0) or 0.0)
    kd_weight = float(getattr(args, "kd_weight", RECIPE_DEFAULTS["kd_weight"]))
    kd_temperature = float(getattr(args, "kd_temperature",
                                   RECIPE_DEFAULTS["kd_temperature"]))
    if teacher is not None:
        teacher.eval()
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
        train_mode(net)
        t0, total, seen = time.time(), 0.0, 0
        for x, y in loader:
            x, y = x.to(DEVICE, non_blocking=True), y.to(DEVICE, non_blocking=True)
            x, partner, lam = mixup_batch(x, y, mixup)
            with torch.autocast(DEVICE.type, enabled=DEVICE.type == "cuda"):
                logits = net(x)
                loss = loss_fn(logits, y)
                if lam < 1.0:
                    loss = lam * loss + (1 - lam) * loss_fn(logits, partner)
                if teacher is not None:
                    with torch.no_grad():
                        target = teacher(resize_batch(x, teacher_size or args.size))
                    loss = ((1 - kd_weight) * loss
                            + kd_weight * distillation_loss(logits, target, kd_temperature))
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


# ── distillation ──────────────────────────────────────────────────────────────
def mixup_batch(x: torch.Tensor, y: torch.Tensor,
                alpha: float) -> tuple[torch.Tensor, torch.Tensor, float]:
    """Mix a batch with a shuffled copy of itself (Zhang et al. 2018).

    Returns the mixed images, each image's partner label and the weight
    ``lam ~ Beta(alpha, alpha)`` of the original. With ``alpha <= 0`` the batch
    passes through (``lam = 1``) and no random number is drawn, so a run
    without mixup is the run it was before mixup existed.
    """
    if alpha <= 0:
        return x, y, 1.0
    lam = float(np.random.beta(alpha, alpha))
    perm = torch.randperm(len(y), device=x.device)
    return lam * x + (1 - lam) * x[perm], y[perm], lam


def resize_batch(x: torch.Tensor, size: int) -> torch.Tensor:
    """A normalised batch at ``size`` x ``size`` (the teacher's input size)."""
    if x.shape[-1] == size and x.shape[-2] == size:
        return x
    return F.interpolate(x, size=(size, size), mode="bilinear", align_corners=False,
                         antialias=True)


def distillation_loss(student_logits: torch.Tensor, teacher_logits: torch.Tensor,
                      temperature: float) -> torch.Tensor:
    """KL(teacher || student) at ``temperature``, times T² (Hinton et al. 2015).

    The T² keeps the gradient on the same scale as the label loss whatever
    the temperature, so ``--kd-weight`` means the same at every T.
    """
    t = float(temperature)
    return F.kl_div(F.log_softmax(student_logits.float() / t, dim=1),
                    F.log_softmax(teacher_logits.float() / t, dim=1),
                    reduction="batchmean", log_target=True) * t * t


def teacher_recipe(tag: str) -> argparse.Namespace:
    """The configuration of the ``cv`` run ``tag``, to retrain it as a teacher."""
    path = RUNS / "scene_cv" / tag / "summary.json"
    if not path.exists():
        raise SystemExit(f"--teacher {tag}: no cv run at {path}")
    config = json.loads(path.read_text(encoding="utf-8"))["config"]
    if config.get("teacher"):
        raise SystemExit(f"--teacher {tag} is itself distilled; name its teacher instead")
    return argparse.Namespace(**{**RECIPE_DEFAULTS, **config, "verbose": False})


def teacher_key(recipe: argparse.Namespace, items: list[tuple[Path, int]]) -> str:
    """Cache key: the recipe and the exact training frames.

    A fold's cached teacher can therefore only be one trained without that
    fold's held-out video.
    """
    source = json.dumps({"recipe": {k: getattr(recipe, k, None) for k in RECIPE_KEYS},
                         "items": sorted(("/".join(p.parts[-3:]), int(y)) for p, y in items)},
                        sort_keys=True)
    return hashlib.sha256(source.encode()).hexdigest()[:16]


def train_teacher(recipe: argparse.Namespace, items: list[tuple[Path, int]],
                  log_prefix: str = "") -> nn.Module:
    """The teacher for one training set: from the cache, or trained and cached.

    Stored as a float32 delta when the recipe freezes most of the backbone
    (exact, and a third of the size), else as a full state dict.
    """
    path = TEACHER_CACHE / f"{recipe.tag}_{teacher_key(recipe, items)}.pt"
    if path.exists():
        cached = torch.load(path, map_location="cpu", weights_only=False)
        net = build(recipe.arch, recipe.init, recipe.size)
        if cached["storage"] == "delta":
            merge_delta(net, cached["state"], cached["base_fingerprint"])
        else:
            net.load_state_dict(cached["state"])
        print(f"{log_prefix}teacher {recipe.tag}: cached {path.name}")
        return net.to(DEVICE).eval()

    rehearsal = rwf_items("train.csv", recipe.rehearsal, recipe.seed) if recipe.rehearsal else []
    net = train_model(recipe, items, rehearsal, log_prefix=f"{log_prefix}teacher ")
    if recipe.train_last_blocks is not None and recipe.init != "violence_v3":
        state, fingerprint = delta_state(net)
        record = {"storage": "delta", "state": state, "base_fingerprint": fingerprint}
    else:
        record = {"storage": "full", "state": net.state_dict()}
    record["state"] = {k: v.detach().cpu() for k, v in record["state"].items()}
    record["recipe"] = {k: getattr(recipe, k, None) for k in RECIPE_KEYS}
    TEACHER_CACHE.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".part")
    torch.save(record, partial)
    os.replace(partial, path)
    return net.eval()


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
    recipe = teacher_recipe(args.teacher) if args.teacher else None
    out = RUNS / "scene_cv" / args.tag
    out.mkdir(parents=True, exist_ok=True)
    print(f"CV {args.tag}: {len(videos)} folds, {len(frames)} frames, "
          f"{len(rehearsal)} rehearsal frames, device {DEVICE}"
          + (f", teacher {recipe.tag}" if recipe else ""))

    oof_logits = np.zeros((len(frames), len(CLASSES)), dtype=np.float32)
    teacher_oof = np.zeros_like(oof_logits)
    started = time.time()
    for k, video in enumerate(videos, 1):
        prefix = f"  [{k}/{len(videos)} {video}] "
        train_items = frame_items([f for f in frames if f.video != video])
        held = [i for i, f in enumerate(frames) if f.video == video]
        held_items = frame_items([frames[i] for i in held])
        teacher = None
        if recipe is not None:
            teacher = train_teacher(recipe, train_items, log_prefix=prefix)
            teacher_oof[held] = predict_logits(teacher, held_items, recipe.size)
        net = train_model(args, train_items, rehearsal, log_prefix=prefix, teacher=teacher,
                          teacher_size=recipe.size if recipe else None)
        oof_logits[held] = predict_logits(net, held_items, args.size)
        del net, teacher
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
    if recipe is not None:
        # The teachers' own out-of-fold result, tuned the same way: shows
        # what the student is distilled from (and that the retrained teachers
        # reproduce the teacher's cv run).
        t_temperature = fit_temperature(teacher_oof, y_idx)
        t_probs = softmax(teacher_oof, t_temperature)
        t_thresholds = tune_thresholds(t_probs, y_true)
        summary["teacher"] = {"tag": recipe.tag, "temperature": t_temperature,
                              "thresholds": t_thresholds,
                              "report": classification_report(
                                  y_true, labels_from(t_probs, t_thresholds))}
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
    if recipe is not None:
        t_rep = summary["teacher"]["report"]
        print(f"  teacher {recipe.tag} OOF: acc={t_rep['accuracy']:.3f} "
              f"macroF1={t_rep['macro_f1']:.3f}")
    print(f"written {out}")
    return 0


def cmd_fit(args) -> int:
    if not args.cv_tag:
        raise SystemExit("fit needs --cv-tag: temperature and thresholds come from CV")
    cv = json.loads((RUNS / "scene_cv" / args.cv_tag / "summary.json").read_text(encoding="utf-8"))
    for key in ("arch", "init", "size", "epochs", "lr", "rehearsal", "train_last_blocks",
                *RECIPE_DEFAULTS):
        if cv["config"].get(key, RECIPE_DEFAULTS.get(key)) != getattr(args, key):
            raise SystemExit(f"--{key} differs from cv run {args.cv_tag}: "
                             f"{getattr(args, key)} vs {cv['config'].get(key)}")
    temperature, thresholds = cv["temperature"], cv["thresholds"]

    train = load_frames(("train",), args.dataset)
    rehearsal = rwf_items("train.csv", args.rehearsal, args.seed) if args.rehearsal else []
    recipe = teacher_recipe(args.teacher) if args.teacher else None
    # The teacher is trained on the train split too: val and test stay unseen
    # by both models, as they were by every fold's teacher in cv.
    teacher = train_teacher(recipe, frame_items(train), log_prefix="  ") if recipe else None
    started = time.time()
    net = train_model(args, frame_items(train), rehearsal, log_prefix="  ", teacher=teacher,
                      teacher_size=recipe.size if recipe else None)
    minutes = round((time.time() - started) / 60, 1)

    metrics: dict = {"cv_oof": cv["reports"]["tuned"], "cv_tag": args.cv_tag}
    if recipe is not None:
        # The teacher on val, with its own cv run's temperature and thresholds.
        t_cv = json.loads((RUNS / "scene_cv" / recipe.tag / "summary.json")
                          .read_text(encoding="utf-8"))
        val = load_frames(("val",), args.dataset)
        t_probs = softmax(predict_logits(teacher, frame_items(val), recipe.size),
                          t_cv["temperature"])
        metrics["teacher_val"] = classification_report(
            [f.label for f in val], labels_from(t_probs, t_cv["thresholds"]))
        print(f"  teacher {recipe.tag} val: acc={metrics['teacher_val']['accuracy']:.3f} "
              f"macroF1={metrics['teacher_val']['macro_f1']:.3f}")
        del teacher
        torch.cuda.empty_cache()
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
        full, fingerprint = delta_state(net)
        storage = "delta"
    state = {k: (v.half() if args.fp16 and v.is_floating_point() else v)
             for k, v in full.items()}
    init_from = VIOLENCE_V3.name if args.init == "violence_v3" else f"{args.arch} pretrained"
    if recipe is not None:
        init_from += f", distilled from {recipe.tag} ({recipe.arch})"
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
                            "plus the last N transformer blocks / CNN stages")
        p.add_argument("--teacher", default=RECIPE_DEFAULTS["teacher"],
                       help="distil from the recipe of this cv run, retrained on "
                            "the student's training frames")
        p.add_argument("--kd-weight", type=float, default=RECIPE_DEFAULTS["kd_weight"],
                       help="weight of the distillation term (the rest is the label loss)")
        p.add_argument("--kd-temperature", type=float,
                       default=RECIPE_DEFAULTS["kd_temperature"])
        p.add_argument("--mixup", type=float, default=RECIPE_DEFAULTS["mixup"],
                       help="Beta(a, a) mixup of each batch; 0 = off")
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
    if not 0.0 <= args.kd_weight <= 1.0 or args.kd_temperature <= 0:
        raise SystemExit("--kd-weight must be in [0, 1] and --kd-temperature > 0")
    if args.teacher == args.tag:
        raise SystemExit("--teacher must name another cv run")
    if not args.rehearsal:
        args.rehearsal_share = 0.0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
