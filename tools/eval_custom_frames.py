"""Score the custom dataset's labelled images through the product pipeline.

Each image is treated as a short static clip and pushed through
``Pipeline.observe`` and ``TCIEngine.compute`` — the same code the live camera
runs — with side effects disabled and identity neutral (see
``sentrix/evaluation/harness.py``). The predicted class is the incident type
the TCI engine assigns, mapped onto the five dataset classes.

Usage::

    python tools/eval_custom_frames.py                       # val + test
    python tools/eval_custom_frames.py --splits train,val,test --tag baseline
    python tools/eval_custom_frames.py --dataset D:/sentrix_dataset --limit 20

Writes ``training/runs/custom_eval/<tag>/frames.json`` (metrics) and
``frames.csv`` (one row per image).
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUT_ROOT = Path(__file__).resolve().parent.parent / "training" / "runs" / "custom_eval"


def git_describe() -> tuple[str, str]:
    def run(*args):
        try:
            return subprocess.run(["git", *args], capture_output=True, text=True,
                                  timeout=5).stdout.strip()
        except Exception:
            return ""
    return (run("rev-parse", "--short", "HEAD") or "unknown",
            run("rev-parse", "--abbrev-ref", "HEAD") or "unknown")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dataset", type=Path, default=None,
                        help="dataset root (default: $SENTRIX_CUSTOM_DATASET)")
    parser.add_argument("--splits", default="val,test")
    parser.add_argument("--tag", default=None,
                        help="output folder name (default: current branch)")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args(argv)

    import cv2

    from sentrix.evaluation.custom_dataset import dataset_dir, load_frames
    from sentrix.evaluation.harness import build_eval_pipeline, score_still, still_record
    from sentrix.evaluation.metrics import classification_report, format_report

    commit, branch = git_describe()
    tag = args.tag or branch.replace("/", "_")
    splits = [s.strip() for s in args.splits.split(",") if s.strip()]
    frames = load_frames(splits, args.dataset)
    if args.limit:
        frames = frames[: args.limit]
    missing = [f for f in frames if not f.path.exists()]
    if missing:
        raise SystemExit(f"{len(missing)} image(s) missing under "
                         f"{dataset_dir(args.dataset)}, e.g. {missing[0].path}")

    pipeline = build_eval_pipeline()
    records: list[dict] = []
    started = time.time()
    try:
        for i, frame in enumerate(frames, 1):
            image = cv2.imread(str(frame.path))
            still = score_still(pipeline, image)
            records.append({"image": frame.path.name, "split": frame.split,
                            "video": frame.video, "gt": frame.label,
                            **still_record(still)})
            if i % 50 == 0 or i == len(frames):
                print(f"  {i}/{len(frames)} images  ({time.time() - started:.0f}s)")
    finally:
        pipeline.shutdown()

    report = {"commit": commit, "branch": branch, "splits": {},
              "models": model_inventory(pipeline)}
    print()
    for split in splits:
        rows = [r for r in records if r["split"] == split]
        if not rows:
            continue
        rep = classification_report([r["gt"] for r in rows], [r["pred"] for r in rows])
        report["splits"][split] = rep
        print(format_report(split, rep))
        print()

    out = OUT_ROOT / tag
    out.mkdir(parents=True, exist_ok=True)
    (out / "frames.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    with open(out / "frames.csv", "w", newline="", encoding="utf-8") as fh:
        cols = ["image", "split", "video", "gt", "pred", "incident_type", "raw_tci",
                "weapon", "fire", "behaviour", "behaviour_label", "violence_score",
                "persons", "scene"]
        writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for r in records:
            writer.writerow({**r, "scene": json.dumps(r["scene"]) if "scene" in r else ""})
    print(f"written: {out / 'frames.json'}  (commit {commit})")
    return 0


def model_inventory(pipeline) -> dict:
    """Which weight files actually served this run."""
    inv: dict = {}
    det = pipeline.detector
    for role, attr in (("person", "_person"), ("weapon", "_weapon"), ("fire", "_fire")):
        model = getattr(det, attr, None)
        inv[role] = Path(getattr(model, "ckpt_path", "") or "").name if model else None
    for name in ("violence", "scene"):
        engine = getattr(pipeline, name, None)
        if engine is None or not getattr(engine, "available", False):
            inv[name] = None
            continue
        weights = getattr(engine, "weights_name", None)
        if weights is None and name == "violence":
            from sentrix.perception.violence import _resolve
            resolved = _resolve()
            weights = resolved.name if resolved else None
        inv[name] = weights
    inv["fusion"] = pipeline.tci.model_source
    return inv


if __name__ == "__main__":
    sys.exit(main())
