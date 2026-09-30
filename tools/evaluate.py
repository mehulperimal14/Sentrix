"""Produce the evaluation table from event logs. Measurement only — no training.

Every number in the final report should come out of this command, alongside the
commit hash that produced it. Nothing here fits a model; it reads the JSONL that
``tools/replay.py`` wrote and computes metrics over it.

The headline output is **false alarms per hour**, counted per *incident* rather
than per frame. A frame-level count inflates the figure by roughly the inference
rate and does not describe what a user experiences: one intruder standing in a
doorway for thirty seconds is one alarm, not one hundred and eighty.

Usage::

    python tools/evaluate.py
    python tools/evaluate.py --events var/events --json report.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

THREAT_WORDS = {"threat", "fight", "weapon", "fire", "intrusion", "violence",
                "anomaly", "true", "1"}


def git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def count_alarms(scores, hz: float, min_level: int):
    """Committed transitions into >= min_level, with the real hysteresis policy."""
    from sentrix.fusion.policy import LevelPolicy

    policy, alarms, previous = LevelPolicy(), 0, 1
    for i, score in enumerate(scores):
        state = policy.update(float(score), now=i / hz)
        if state.level >= min_level and previous < min_level:
            alarms += 1
        previous = state.level
    return alarms


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--events", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None,
                        help="also write the report as JSON")
    args = parser.parse_args(argv)

    from sentrix.config import settings
    from sentrix.paths import EVENTLOG_DIR
    from sentrix.pipeline.events import load_dataset

    directory = args.events or EVENTLOG_DIR
    df = load_dataset(directory)
    if df.empty:
        print(f"no event logs in {directory}")
        print("\nGenerate them first:")
        print("  python tools/replay.py --clips data/clips --no-audio")
        return 1

    hz = settings.pipeline.inference_fps
    commit = git_commit()
    labelled = "label" in df.columns and df["label"].notna().any()

    print("=" * 66)
    print("SENTRIX evaluation")
    print("=" * 66)
    print(f"  commit          : {commit}")
    print(f"  event logs      : {df['log_file'].nunique()} file(s), {len(df):,} frames")
    print(f"  inference rate  : {hz} Hz")
    print(f"  level bands     : {settings.policy.thresholds}")
    print(f"  dwell up/down   : {settings.policy.escalate_dwell_seconds} s per threshold / "
          f"{settings.policy.deescalate_dwell_seconds}s")
    print(f"  labelled        : {labelled}")

    report: dict = {"commit": commit, "frames": int(len(df)),
                    "inference_hz": hz,
                    "thresholds": list(settings.policy.thresholds),
                    "labelled": bool(labelled)}

    # ── latency ───────────────────────────────────────────────────────────────
    if "latency_ms" in df.columns:
        lat = df["latency_ms"].dropna()
        if len(lat):
            print("\nlatency (per frame, full pipeline)")
            print("-" * 66)
            stats = {"mean_ms": round(float(lat.mean()), 2),
                     "p50_ms": round(float(lat.quantile(.50)), 2),
                     "p95_ms": round(float(lat.quantile(.95)), 2),
                     "p99_ms": round(float(lat.quantile(.99)), 2)}
            for k, v in stats.items():
                print(f"  {k:<10} {v:>8.2f} ms")
            report["latency"] = stats

    # ── TCI distribution ──────────────────────────────────────────────────────
    print("\nTCI distribution")
    print("-" * 66)
    tci = df["tci"].astype(float)
    print(f"  mean {tci.mean():.4f} | median {tci.median():.4f} | "
          f"min {tci.min():.4f} | max {tci.max():.4f}")
    counts = df["level"].value_counts().sort_index()
    for level, n in counts.items():
        print(f"  level {level}: {n:>7,} frames ({100 * n / len(df):5.1f}%)")
    report["tci"] = {"mean": round(float(tci.mean()), 4),
                     "max": round(float(tci.max()), 4),
                     "level_counts": {int(k): int(v) for k, v in counts.items()}}

    # ── false alarms per hour ─────────────────────────────────────────────────
    if labelled:
        is_threat = df["label"].astype(str).str.lower().isin(THREAT_WORDS)
        normal = df[~is_threat]
        threat = df[is_threat]
        print(f"\nfalse alarms per hour  ({len(normal):,} normal frames)")
        print("-" * 66)
        fa: dict = {}
        if len(normal):
            hours = len(normal) / hz / 3600.0
            for level in (3, 4, 5):
                # The policy decides on raw_tci (identical to tci since the
                # display EMA was removed; older logs smoothed tci).
                column = "raw_tci" if "raw_tci" in normal else "tci"
                alarms = count_alarms(normal[column].to_numpy(), hz, level)
                rate = alarms / hours if hours else float("nan")
                print(f"  level>={level}: {alarms:>4} alarms over {hours:6.2f} h "
                      f"-> {rate:6.2f}/hour")
                fa[f"level_{level}"] = {"alarms": alarms,
                                        "hours": round(hours, 3),
                                        "per_hour": round(rate, 3)}
        report["false_alarms"] = fa

        if len(threat):
            print(f"\ndetection on threat footage  ({len(threat):,} frames)")
            print("-" * 66)
            det: dict = {}
            for level in (3, 4, 5):
                recall = float((threat["level"] >= level).mean())
                print(f"  level>={level}: {100 * recall:5.1f}% of threat frames")
                det[f"level_{level}"] = round(recall, 4)
            report["detection"] = det

        print("\nCompare against the motion-only baseline on the SAME footage:")
        print("  python tools/baseline.py --clips <same clips>")
    else:
        print("\nNo ground-truth labels in these logs.")
        print("False-alarm rate and detection rate need labelled footage:")
        print("  python tools/replay.py --clips data/clips --no-audio")

    # ── feature activity ──────────────────────────────────────────────────────
    from sentrix.fusion.features import CONTRACT_V2
    present = [f for f in CONTRACT_V2 if f in df.columns]
    if present:
        print("\nfeature activity (mean, max) — a flat zero means a dead modality")
        print("-" * 66)
        feats: dict = {}
        for name in present:
            col = df[name].astype(float)
            flag = "   <-- always zero" if col.max() == 0 else ""
            print(f"  {name:<11} mean={col.mean():.4f}  max={col.max():.4f}{flag}")
            feats[name] = {"mean": round(float(col.mean()), 5),
                           "max": round(float(col.max()), 5)}
        report["features"] = feats

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nJSON report written: {args.json}")

    print("\n" + "=" * 66)
    print(f"Quote these numbers with commit {commit}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
