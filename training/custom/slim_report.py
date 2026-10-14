"""Generate the tables of ``training/runs/custom/slim/REPORT.md`` from the runs.

Numbers in the report are generated, never transcribed: this reads the
leave-one-video-out summaries and out-of-fold logits
(``training/runs/custom/scene_cv/<tag>/``), the held-out predictions of the
fitted models (``training/runs/custom/scene/<tag>/``) and the latency report,
and rewrites every ``<!-- BEGIN name -->`` ... ``<!-- END name -->`` block of
the report. Prose outside the blocks is left alone.

Besides the threshold-dependent metrics the trainer reports, it computes
threshold-free ones - the AUROC of each alarm class against ``normal`` - and
the held-out result at the thresholds the runtime actually serves (the
checkpoint's, raised to ``ThreatConfig.scene_threshold_floor``).

Usage::

    python training/custom/slim_report.py            # rewrite REPORT.md
    python training/custom/slim_report.py --print    # print the tables only
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sentrix.evaluation.custom_dataset import CLASSES  # noqa: E402
from sentrix.evaluation.metrics import classification_report  # noqa: E402
from sentrix.perception.scene import PRIORITY, decide_label  # noqa: E402

RUNS = ROOT / "training" / "runs" / "custom"
SLIM = RUNS / "slim"
REPORT = SLIM / "REPORT.md"
LATENCY = SLIM / "latency.json"

#: The shipped DINOv2 and earlier references, then the slim candidates in the
#: order of PROTOCOL.md.
REFERENCE_TAGS = ("dino_b_last4", "dino_b_frozen", "r18_imagenet")
SLIM_TAGS = ("effb0_full", "effb0_last3", "effb0_kd",
             "mnv3l_full", "mnv3l_last3", "mnv3l_kd")
SHORT_ARCH = {"timm:vit_base_patch14_dinov2.lvd142m": "DINOv2 ViT-B/14",
              "timm:efficientnet_b0": "EfficientNet-B0",
              "timm:mobilenetv3_large_100": "MobileNetV3-L",
              "resnet18": "ResNet-18"}


def _f(x, digits: int = 3) -> str:
    return "-" if x is None else f"{x:.{digits}f}"


def softmax(logits: np.ndarray, t: float) -> np.ndarray:
    z = logits / t
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def auroc(labels: list[str], probs: np.ndarray, cls: str) -> float | None:
    """AUROC of ``p(cls)`` separating ``cls`` frames from ``normal`` frames."""
    from sklearn.metrics import roc_auc_score
    idx = [i for i, y in enumerate(labels) if y in ("normal", cls)]
    y = [labels[i] == cls for i in idx]
    if not idx or all(y) or not any(y):
        return None
    return float(roc_auc_score(y, probs[idx, CLASSES.index(cls)]))


def load_cv(tag: str) -> dict | None:
    path = RUNS / "scene_cv" / tag / "summary.json"
    if not path.exists():
        return None
    summary = json.loads(path.read_text(encoding="utf-8"))
    with open(RUNS / "scene_cv" / tag / "oof.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    logits = np.array([[float(r[f"logit_{c}"]) for c in CLASSES] for r in rows])
    summary["oof_labels"] = [r["gt"] for r in rows]
    summary["oof_probs"] = softmax(logits, summary["temperature"])
    return summary


def describe(config: dict) -> tuple[str, str, str]:
    """(backbone, what is trained, loss) of a cv recipe."""
    arch = SHORT_ARCH.get(config["arch"], config["arch"])
    blocks = config.get("train_last_blocks")
    unit = "blocks" if "vit" in config["arch"] else "stages"
    trained = ("all" if blocks is None else "head only" if blocks == 0
               else f"last {blocks} {unit} + head")
    if config.get("init") == "violence_v3":
        trained += " (from violence v3)"
    loss = "CE"
    if config.get("teacher"):
        loss = (f"KD from {config['teacher']} (w {config['kd_weight']}, "
                f"T {config['kd_temperature']})")
    if config.get("mixup"):
        loss += f", mixup {config['mixup']}"
    return arch, trained, f"{loss}, {config['epochs']} ep"


def lovo_table(tags: list[str]) -> str:
    lines = ["| Candidate | Backbone | Trained | Loss | OOF acc | **OOF macro-F1** | "
             "normal F1 | fire F1 | weapon F1 | assault F1 | robbery F1 | Threat FPR | "
             "AUROC weapon | AUROC fire |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for tag in tags:
        s = load_cv(tag)
        if s is None:
            continue
        rep, pc = s["reports"]["tuned"], s["reports"]["tuned"]["per_class"]
        arch, trained, loss = describe(s["config"])
        aucs = [auroc(s["oof_labels"], s["oof_probs"], c) for c in ("weapon", "fire")]
        lines.append(f"| `{tag}` | {arch} | {trained} | {loss} | {_f(rep['accuracy'])} | "
                     f"**{_f(rep['macro_f1'])}** | "
                     + " | ".join(_f(pc[c]["f1"]) for c in CLASSES)
                     + f" | {_f(rep['threat_detection']['false_positive_rate'])} | "
                     + " | ".join(_f(a) for a in aucs) + " |")
    return "\n".join(lines)


def calibration_table(tags: list[str]) -> str:
    lines = ["| Candidate | Temperature | fire thr | weapon thr | assault thr | robbery thr |",
             "|---|---|---|---|---|---|"]
    for tag in tags:
        s = load_cv(tag)
        if s is None:
            continue
        thr = s["thresholds"]
        lines.append(f"| `{tag}` | {s['temperature']:.2f} | "
                     + " | ".join(f"{thr[c]:.2f}" for c in PRIORITY) + " |")
    return "\n".join(lines)


def per_video_table(tags: list[str]) -> str:
    folds = None
    rows = []
    for tag in tags:
        s = load_cv(tag)
        if s is None:
            continue
        folds = folds or s["folds"]
        rows.append(f"| `{tag}` | " + " | ".join(_f(s["per_video_accuracy"][v], 2)
                                                for v in folds) + " |")
    if not folds:
        return ""
    return "\n".join([f"| Candidate | {' | '.join(folds)} |",
                      "|---|" + "---|" * len(folds), *rows])


def teacher_table(tags: list[str]) -> str:
    """Did the per-fold teachers reproduce the teacher's own cv run?"""
    lines = ["| Student | Teacher | Teacher OOF macro-F1, retrained per fold | "
             "Teacher's own cv run | Student OOF macro-F1 |", "|---|---|---|---|---|"]
    for tag in tags:
        s = load_cv(tag)
        if s is None or "teacher" not in s:
            continue
        own = load_cv(s["teacher"]["tag"])
        lines.append(f"| `{tag}` | `{s['teacher']['tag']}` | "
                     f"{_f(s['teacher']['report']['macro_f1'])} | "
                     f"{_f(own['reports']['tuned']['macro_f1']) if own else '-'} | "
                     f"{_f(s['reports']['tuned']['macro_f1'])} |")
    return "\n".join(lines) if len(lines) > 2 else "_(no distilled candidate run)_"


def served_thresholds(thresholds: dict[str, float]) -> dict[str, float]:
    """The checkpoint's thresholds as ``SceneEngine`` serves them (floors)."""
    from sentrix.config import settings
    floors = settings.threat.scene_threshold_floor
    return {c: max(float(t), float(floors.get(c, 0.0))) for c, t in thresholds.items()}


def load_heldout(tag: str) -> dict | None:
    metrics_path = RUNS / "scene" / tag / "metrics.json"
    if not metrics_path.exists():
        return None
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    by_split: dict[str, tuple[list[str], np.ndarray]] = {}
    with open(RUNS / "scene" / tag / "predictions.csv", newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            labels, probs = by_split.setdefault(r["split"], ([], []))
            labels.append(r["gt"])
            probs.append([float(r[f"p_{c}"]) for c in CLASSES])
    metrics["probs"] = {k: (v[0], np.array(v[1])) for k, v in by_split.items()}
    return metrics


def heldout_table(tags: list[str]) -> str:
    lines = ["| Model | Split | n | Accuracy | Macro-F1 | Threat recall | Threat FPR | "
             "weapon F1 | fire F1 | AUROC weapon | AUROC fire | Macro-F1 at served thresholds |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for tag in tags:
        m = load_heldout(tag)
        cv = load_cv(m["cv_tag"]) if m else None
        if m is None or cv is None:
            continue
        served = served_thresholds(cv["thresholds"])
        for split in ("val", "test"):
            rep = m[split]
            labels, probs = m["probs"][split]
            pc, td = rep["per_class"], rep["threat_detection"]
            at_floor = classification_report(labels, [
                decide_label(dict(zip(CLASSES, map(float, p))), served) for p in probs])

            def f1(c):
                return _f(pc[c]["f1"]) if pc[c]["support"] else "-"
            lines.append(f"| `{tag}` | {split} | {rep['n']} | {_f(rep['accuracy'])} | "
                         f"{_f(rep['macro_f1'])} | {_f(td['recall'])} | "
                         f"{_f(td['false_positive_rate'])} | {f1('weapon')} | {f1('fire')} | "
                         + " | ".join(_f(auroc(labels, probs, c)) for c in ("weapon", "fire"))
                         + f" | {_f(at_floor['macro_f1'])} |")
    return "\n".join(lines)


def latency_table() -> str:
    if not LATENCY.exists():
        return "_(latency not measured yet)_"
    rep = json.loads(LATENCY.read_text(encoding="utf-8"))
    lines = [f"Measured {rep['measured']} on {rep['machine']['cpu']}, "
             f"**{rep['threads']} threads**, batch {rep['batch']}, "
             f"{rep['runs_per_model']} timed runs per model in interleaved rounds "
             f"(CPU load before the run: {rep['machine']['cpu_load_before_percent']} %); "
             f"torch {rep['versions']['torch']}, onnxruntime "
             f"{rep['versions']['onnxruntime']}.", "",
             "| Model | Checkpoint | Params | GFLOPs | File | PyTorch median / p95 | "
             "ONNX Runtime median / p95 |", "|---|---|---|---|---|---|---|"]
    for m in rep["models"]:
        lines.append(f"| {SHORT_ARCH.get(m['architecture'], m['architecture'])} | "
                     f"`{m['checkpoint']}` | {m['params_m']:.1f} M | {m['gflops']:.2f} | "
                     f"{m['checkpoint_mb']:.1f} MB ({m['storage']}) | "
                     f"{m['torch']['median_ms']:.1f} / {m['torch']['p95_ms']:.1f} ms | "
                     f"{m['onnxruntime']['median_ms']:.1f} / "
                     f"{m['onnxruntime']['p95_ms']:.1f} ms |")
    for size, stats in rep["preprocess"].items():
        lines.append("")
        lines.append(f"Preprocessing (BGR 640x480 frame -> normalised {size}x{size} "
                     f"tensor, as `SceneEngine` does it): {stats['median_ms']:.2f} ms "
                     f"median, {stats['p95_ms']:.2f} ms p95.")
    return "\n".join(lines)


def blocks(fitted: list[str]) -> dict[str, str]:
    all_tags = [*REFERENCE_TAGS, *SLIM_TAGS]
    return {"lovo": lovo_table(all_tags),
            "calibration": calibration_table([REFERENCE_TAGS[0], *SLIM_TAGS]),
            "per_video": per_video_table([REFERENCE_TAGS[0], *SLIM_TAGS]),
            "teachers": teacher_table(list(SLIM_TAGS)),
            "heldout": heldout_table(["dino_b_last4", *fitted]),
            "latency": latency_table()}


def fill(text: str, generated: dict[str, str]) -> str:
    """Replace the body of each ``<!-- BEGIN name -->`` block."""
    for name, body in generated.items():
        pattern = re.compile(rf"(<!-- BEGIN {name} -->\n).*?(\n<!-- END {name} -->)", re.S)
        text, n = pattern.subn(lambda m: m.group(1) + body + m.group(2), text)
        if n == 0:
            print(f"note: REPORT.md has no '{name}' block", file=sys.stderr)
    return text


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--fitted", nargs="*", default=None,
                        help="fitted slim tags for the held-out table "
                             "(default: every slim tag with a fit run)")
    parser.add_argument("--print", action="store_true", help="print, do not rewrite")
    args = parser.parse_args(argv)
    fitted = args.fitted if args.fitted is not None else [
        t for t in SLIM_TAGS if (RUNS / "scene" / t / "metrics.json").exists()]
    generated = blocks(fitted)
    if args.print or not REPORT.exists():
        for name, body in generated.items():
            print(f"## {name}\n\n{body}\n")
        return 0
    REPORT.write_text(fill(REPORT.read_text(encoding="utf-8"), generated), encoding="utf-8")
    print(f"rewritten {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
