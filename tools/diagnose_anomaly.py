"""Diagnose the UCF-Crime-DVS test-set collapse. Analysis only — no training.

``docs/5`` section 2.3 attributes the test ROC-AUC of 0.194 to "category-level
domain shift" and proposes replacing the SpikingJelly network with a MobileNetV3
trained on optical flow — roughly a month of work.

That diagnosis is wrong, and this script shows why in about a second.

An AUC of 0.194 is not a weak model. Chance is 0.5; a model that scores 0.194 is
ranking anomalies systematically *below* normal videos. Inverting it yields
0.806, which sits right beside the validation AUC of 0.826. The network learned
the task; something downstream flipped the sign.

The score distribution makes it unambiguous — normal footage receives a *higher*
anomaly score than anomalous footage:

    normal     n=147   mean=0.9230   median=0.9574
    anomalous  n=140   mean=0.6853   median=0.8772

Fix the polarity before rewriting the architecture.

Usage::

    python tools/diagnose_anomaly.py
    python tools/diagnose_anomaly.py --predictions path/to/predictions.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_CSV = Path("training/runs/ucf_crime_dvs/sentrix_dvs_predictions.csv")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--predictions", type=Path, default=DEFAULT_CSV)
    args = parser.parse_args(argv)

    if not args.predictions.exists():
        raise SystemExit(f"predictions not found: {args.predictions}")

    import pandas as pd
    from sklearn.metrics import average_precision_score, roc_auc_score

    df = pd.read_csv(args.predictions)
    required = {"video_id", "anomaly_score", "true_binary"}
    missing = required - set(df.columns)
    if missing:
        raise SystemExit(f"missing columns: {sorted(missing)}")

    score_col = ("video_anomaly_score" if "video_anomaly_score" in df.columns
                 else "anomaly_score")
    videos = df.groupby("video_id").agg(
        score=(score_col, "first"),
        clip_mean=("anomaly_score", "mean"),
        clip_max=("anomaly_score", "max"),
        y=("true_binary", "first"),
    ).reset_index()

    print(f"clips: {len(df):,} | videos: {len(videos):,} | "
          f"anomalous: {int(videos.y.sum())} | normal: {int((videos.y == 0).sum())}")

    print("\nAUC by scoring choice (video level)")
    print("-" * 64)
    for name, col in (("video-level score (as reported)", "score"),
                      ("mean of clip scores", "clip_mean"),
                      ("max of clip scores", "clip_max")):
        auc = roc_auc_score(videos.y, videos[col])
        ap = average_precision_score(videos.y, videos[col])
        print(f"  {name:<32} AUC={auc:.4f}  AP={ap:.4f}  "
              f"(inverted AUC={1 - auc:.4f})")

    clip_auc = roc_auc_score(df.true_binary, df.anomaly_score)
    print(f"\n  {'clip level':<32} AUC={clip_auc:.4f}  "
          f"(inverted AUC={1 - clip_auc:.4f})")

    print("\nScore distribution by true class")
    print("-" * 64)
    for label, name in ((0, "normal   "), (1, "anomalous")):
        s = videos[videos.y == label]["score"]
        print(f"  {name} n={len(s):>4}  mean={s.mean():.4f}  "
              f"median={s.median():.4f}  p10={s.quantile(.1):.4f}  "
              f"p90={s.quantile(.9):.4f}")

    auc = roc_auc_score(videos.y, videos.score)
    print("\nVERDICT")
    print("-" * 64)
    if auc >= 0.45:
        print(f"  AUC {auc:.4f} — not an inversion. Investigate domain shift.")
        return 0

    print(f"  AUC {auc:.4f} is BELOW chance; inverted it is {1 - auc:.4f},")
    print("  consistent with the validation AUC. This is a polarity or")
    print("  label-mapping bug, NOT domain shift.")
    print()
    print("  Check in this order:")
    print("    1. how true_binary is built for the TEST split — is 1 = anomaly?")
    print("    2. which logit index is read as the anomaly probability")
    print("    3. whether test_split.txt is parsed with the same convention")
    print("       as train_split.txt (they have different line formats)")
    print()
    print("  Do not replace the architecture until this is ruled out.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
