"""Screen frozen backbones by how well their features generalise across videos.

Fine-tuning a candidate under leave-one-video-out costs ~10 GPU-minutes. This
costs seconds: extract pooled features once per backbone (train + val frames
only — the test video is never loaded), then fit a class-balanced logistic
regression per fold. It answers the question fine-tuning results kept raising:
*which representation already separates these classes in a video it has never
seen?* With eight training videos, a backbone that has to learn "handgun" from
scratch learns "which room is this" instead.

Usage::

    python training/custom/screen_backbones.py \\
        --models resnet18_violence convnext_tiny.fb_in22k_ft_in1k@288 \\
                 vit_base_patch16_clip_224.openai

``name@size`` overrides the input resolution. Results are appended to
``training/runs/custom/backbone_screen.json``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sentrix.evaluation.custom_dataset import CLASSES, load_frames  # noqa: E402
from sentrix.evaluation.metrics import classification_report  # noqa: E402

OUT = ROOT / "training" / "runs" / "custom" / "backbone_screen.json"
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def backbone(spec: str):
    """(feature extractor, image size, mean, std) for a spec."""
    name, _, size = spec.partition("@")
    if name == "resnet18_violence":
        import torch.nn as nn
        from torchvision import models
        net = models.resnet18(weights=None)
        net.fc = nn.Sequential(nn.Dropout(0.3), nn.Linear(512, 2))
        ck = torch.load(ROOT / "models/v3_real/violence_classifier_real_v3.pt",
                        map_location="cpu", weights_only=False)
        net.load_state_dict(ck["model_state_dict"])
        net.fc = nn.Identity()
        return net, int(size or 224), [0.485, 0.456, 0.406], [0.229, 0.224, 0.225]
    import timm
    kwargs = {"num_classes": 0}
    if size and ("vit" in name or "eva" in name):
        kwargs["img_size"] = int(size)
    net = timm.create_model(name, pretrained=True, **kwargs)
    cfg = timm.data.resolve_data_config({}, model=net)
    return net, int(size or cfg["input_size"][-1]), list(cfg["mean"]), list(cfg["std"])


@torch.no_grad()
def features(spec: str, frames, flip: bool = True) -> np.ndarray:
    from PIL import Image
    net, size, mean, std = backbone(spec)
    net = net.to(DEVICE).eval()
    m = torch.tensor(mean).view(1, 3, 1, 1).to(DEVICE)
    s = torch.tensor(std).view(1, 3, 1, 1).to(DEVICE)
    out = []
    for i in range(0, len(frames), 32):
        batch = [torch.from_numpy(np.asarray(Image.open(f.path).convert("RGB")
                                             .resize((size, size), Image.BILINEAR)).copy())
                 for f in frames[i:i + 32]]
        x = torch.stack(batch).permute(0, 3, 1, 2).float().div(255).to(DEVICE)
        x = (x - m) / s
        with torch.autocast(DEVICE.type, enabled=DEVICE.type == "cuda"):
            f = net(x).float()
            if flip:
                f = (f + net(torch.flip(x, dims=[3])).float()) / 2
        out.append(f.cpu())
    del net
    torch.cuda.empty_cache()
    return torch.cat(out).numpy()


def lovo(x: np.ndarray, frames, c: float) -> dict:
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    y = np.array([f.class_id for f in frames])
    videos = sorted({f.video for f in frames})
    probs = np.zeros((len(frames), len(CLASSES)))
    for v in videos:
        tr = np.array([f.video != v for f in frames])
        scaler = StandardScaler().fit(x[tr])
        clf = LogisticRegression(C=c, class_weight="balanced", max_iter=3000)
        clf.fit(scaler.transform(x[tr]), y[tr])
        p = clf.predict_proba(scaler.transform(x[~tr]))
        probs[np.ix_(~tr, clf.classes_)] = p
    pred = [CLASSES[i] for i in probs.argmax(1)]
    return classification_report([f.label for f in frames], pred)


def official_val(x: np.ndarray, frames, c: float) -> dict:
    """Fit on the train split, score the val split (test is never loaded)."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    y = np.array([f.class_id for f in frames])
    tr = np.array([f.split == "train" for f in frames])
    scaler = StandardScaler().fit(x[tr])
    clf = LogisticRegression(C=c, class_weight="balanced", max_iter=3000)
    clf.fit(scaler.transform(x[tr]), y[tr])
    pred = [CLASSES[int(i)] for i in clf.predict(scaler.transform(x[~tr]))]
    return classification_report([f.label for f in frames if f.split == "val"], pred)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--models", nargs="+", required=True)
    parser.add_argument("--C", type=float, nargs="+", default=[0.01, 0.1, 1.0])
    args = parser.parse_args(argv)

    frames = load_frames(("train", "val"))
    results = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    for spec in args.models:
        t0 = time.time()
        try:
            x = features(spec, frames)
        except Exception as exc:
            print(f"{spec}: FAILED {type(exc).__name__}: {exc}")
            continue
        best = None
        for c in args.C:
            rep = lovo(x, frames, c)
            if best is None or rep["macro_f1"] > best[1]["macro_f1"]:
                best = (c, rep)
        c, rep = best
        val = official_val(x, frames, c)
        pc = rep["per_class"]
        results[spec] = {"C": c, "dim": int(x.shape[1]), "accuracy": rep["accuracy"],
                         "macro_f1": rep["macro_f1"],
                         "f1": {k: pc[k]["f1"] for k in CLASSES},
                         "threat_fpr": rep["threat_detection"]["false_positive_rate"],
                         "val_accuracy": val["accuracy"], "val_macro_f1": val["macro_f1"],
                         "seconds": round(time.time() - t0, 1)}
        print(f"{spec:45} C={c:<5} acc={rep['accuracy']:.3f} macroF1={rep['macro_f1']:.3f} "
              + " ".join(f"{k}={pc[k]['f1']:.2f}" for k in CLASSES)
              + f" FPR={rep['threat_detection']['false_positive_rate']:.2f}"
              + f" | val acc={val['accuracy']:.3f} mF1={val['macro_f1']:.3f}"
              + f" ({time.time() - t0:.0f}s)", flush=True)
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
