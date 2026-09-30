"""End-to-end test: replay the custom dataset's source videos through SENTRIX.

Every video is decoded at its native rate and fed to ``Pipeline.process`` at
the configured inference rate with its true timestamps, so windowing, evidence
confirmation, hysteresis and incident typing all behave as they do live. Side
effects are disabled and identity is neutral (``sentrix/evaluation/harness.py``).

Each processed frame is scored against the reviewed label timeline
(``sentrix.evaluation.custom_dataset.Timeline``), which yields:

* frame-level 5-class metrics on every scored tick;
* episode-level recall and detection latency per threat class;
* false alarms: threat runs overlapping no ground-truth threat episode.

By default only the held-out videos are replayed (val: ``gun``, ``vid04``;
test: ``wa02``). ``--videos all`` adds the training videos, whose numbers are
in-sample for any model fitted on this dataset.

The report also measures the product's actual output, the committed level
(``metrics.level_report``): false onsets per hour of normal footage with a
Poisson bound, and episodes caught at each level with Clopper-Pearson
intervals. ``--hz`` replays at another inference rate; ``--negatives DIR``
replays every video in a folder of normal-only footage (no labels needed) to
measure false alarms per hour, which gate G3 asks for over >= 3 hours.

Usage::

    python tools/replay_custom_videos.py
    python tools/replay_custom_videos.py --videos all --tag baseline
    python tools/replay_custom_videos.py --videos wa02 --max-seconds 20
    python tools/replay_custom_videos.py --videos all --hz 2 --tag m5
    python tools/replay_custom_videos.py --negatives D:/footage/normal --tag m5
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sentrix.evaluation.harness import SOURCE_COLUMNS  # noqa: E402
from tools.eval_custom_frames import OUT_ROOT, git_describe, model_inventory  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--videos-dir", type=Path, default=None,
                        help="source video folder (default: $SENTRIX_CUSTOM_VIDEOS)")
    parser.add_argument("--videos", default="heldout",
                        help="heldout | all | train | val | test | id,id,...")
    parser.add_argument("--tag", default=None)
    parser.add_argument("--tolerance", type=float, default=0.25,
                        help="max seconds to the nearest labelled frame")
    parser.add_argument("--grace", type=float, default=1.0,
                        help="seconds after an episode ends that still count")
    parser.add_argument("--max-seconds", type=float, default=None)
    parser.add_argument("--rescore", action="store_true",
                        help="recompute metrics from the saved replay CSV "
                             "instead of replaying")
    parser.add_argument("--hz", type=float, default=None,
                        help="inference rate for this replay (default: configured)")
    parser.add_argument("--negatives", type=Path, default=None,
                        help="folder of normal-only videos: measure false alarms/hour")
    args = parser.parse_args(argv)

    from sentrix.config import settings
    from sentrix.evaluation.custom_dataset import Timeline, videos_for
    from sentrix.evaluation.metrics import (classification_report, event_report,
                                            format_levels, format_report,
                                            level_report)

    commit, branch = git_describe()
    tag = args.tag or branch.replace("/", "_")
    if args.negatives is not None:
        return replay_negatives(args, tag, commit, branch)
    videos = videos_for(args.videos)
    missing = [v for v in videos if not v.path(args.videos_dir).exists()]
    if missing:
        raise SystemExit(f"video not found: {missing[0].path(args.videos_dir)}")

    timeline = Timeline()
    out = OUT_ROOT / tag
    suffix = "" if args.videos == "heldout" else f"_{args.videos.replace(',', '-')}"
    if args.hz:
        suffix += f"_{args.hz:g}hz"
    rows_by_video: dict[str, list[dict]] = {}
    started = time.time()
    if args.rescore:
        rows_by_video = load_rows(out / f"replay{suffix}.csv", timeline,
                                  args.tolerance, [v.video_id for v in videos])
        previous = json.loads((out / f"replay{suffix}.json").read_text(encoding="utf-8"))
        commit, branch = previous["commit"], previous["branch"]
        inventory = previous.get("models", {})
    else:
        inventory = replay_all(videos, args, timeline, rows_by_video)

    report: dict = {
        "commit": commit, "branch": branch, "videos": [v.video_id for v in videos],
        "inference_hz": args.hz or settings.pipeline.inference_fps,
        "tolerance_s": args.tolerance, "grace_s": args.grace,
        "models": inventory, "splits": {},
    }
    groups = {"all": videos}
    for split in ("val", "test", "train"):
        members = [v for v in videos if v.split == split]
        if members and len(members) != len(videos):
            groups[split] = members

    print()
    for name, members in groups.items():
        rows = [r for v in members for r in rows_by_video[v.video_id] if r["gt"]]
        if not rows:
            continue
        frame_rep = classification_report([r["gt"] for r in rows],
                                          [r["pred"] for r in rows])
        episodes = {v.video_id: timeline.episodes(v.video_id) for v in members}
        event_rep = event_report({v.video_id: rows_by_video[v.video_id]
                                  for v in members}, episodes, grace=args.grace)
        threat_rows = [r for r in rows if r["gt"] != "normal"]
        normal_rows = [r for r in rows if r["gt"] == "normal"]
        levels = {
            "threat_ticks_at_L3plus": round(sum(r["level"] >= 3 for r in threat_rows)
                                            / max(len(threat_rows), 1), 4),
            "normal_ticks_at_L3plus": round(sum(r["level"] >= 3 for r in normal_rows)
                                            / max(len(normal_rows), 1), 4),
        }
        committed = level_report({v.video_id: rows_by_video[v.video_id]
                                  for v in members}, episodes, grace=args.grace)
        report["splits"][name] = {"frames": frame_rep, "events": event_rep,
                                  "levels": levels, "committed": committed}
        print(format_report(f"{name} (replayed ticks)", frame_rep))
        print(f"  episodes: {event_rep['detected']}/{event_rep['episodes']} detected "
              f"(recall {event_rep['episode_recall']:.3f}); false alarms "
              f"{event_rep['false_alarms']} over {event_rep['normal_minutes']:.2f} "
              f"normal min; L>=3 on threat {levels['threat_ticks_at_L3plus']:.2f} "
              f"/ normal {levels['normal_ticks_at_L3plus']:.2f}")
        for lab, m in event_rep["by_label"].items():
            lat = m["median_latency"]
            print(f"    {lab:8} {m['detected']}/{m['episodes']} episodes, median "
                  f"latency {'n/a' if lat is None else f'{lat:.2f}s'}")
        print(format_levels(committed))
        print()

    lat = [r["latency_ms"] for rows in rows_by_video.values() for r in rows]
    if lat:
        lat.sort()
        report["latency_ms"] = {"mean": round(sum(lat) / len(lat), 1),
                                "p95": lat[int(0.95 * (len(lat) - 1))]}
        print(f"pipeline latency: mean {report['latency_ms']['mean']} ms, "
              f"p95 {report['latency_ms']['p95']} ms ({time.time() - started:.0f}s total)")

    out.mkdir(parents=True, exist_ok=True)
    (out / f"replay{suffix}.json").write_text(json.dumps(report, indent=2),
                                              encoding="utf-8")
    with open(out / f"replay{suffix}.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["video", "t", "gt", "pred", "incident_type", "level", "tci",
                         "weapon", "fire", "behaviour", "latency_ms", *SOURCE_COLUMNS])
        for vid, rows in rows_by_video.items():
            for r in rows:
                writer.writerow([vid, r["t"], r["gt"] or "", r["pred"],
                                 r["incident_type"], r["level"], r["tci"],
                                 r["weapon"], r["fire"], r["behaviour"],
                                 r["latency_ms"], *(r.get(c, "") for c in SOURCE_COLUMNS)])
    print(f"written: {out / f'replay{suffix}.json'}  (commit {commit})")
    return 0


def replay_all(videos, args, timeline, rows_by_video) -> dict:
    """Replay every video through one eval pipeline; returns the model inventory."""
    from sentrix.evaluation.harness import build_eval_pipeline, replay_video

    pipeline = build_eval_pipeline()
    period = 1.0 / args.hz if args.hz else None
    try:
        for v in videos:
            t0 = time.time()

            def label(t, row, vid=v.video_id):
                row["gt"] = timeline.label_at(vid, t, args.tolerance)
                return row

            rows = replay_video(pipeline, v.path(args.videos_dir), v.fps,
                                on_frame=label, max_seconds=args.max_seconds,
                                period=period)
            rows_by_video[v.video_id] = rows
            flagged = sum(r["pred"] != "normal" for r in rows)
            print(f"  {v.video_id:6} [{v.split:5}] {len(rows):4} ticks  "
                  f"{flagged:4} threat  peak L{max((r['level'] for r in rows), default=1)}"
                  f"  ({time.time() - t0:.0f}s)")
    finally:
        pipeline.shutdown()
    return model_inventory(pipeline)


VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv", ".m4v"}


def replay_negatives(args, tag: str, commit: str, branch: str) -> int:
    """Replay a folder of normal-only footage; every tick is ground-truth normal.

    Reports committed-level onsets per hour with exact Poisson bounds - the
    measurement behind gate G3 (0 L4/L5 over >= 3 h, <= 1 L3/h).
    """
    from sentrix.config import settings
    from sentrix.evaluation.harness import build_eval_pipeline, replay_video
    from sentrix.evaluation.metrics import format_levels, level_report

    paths = sorted(p for p in args.negatives.rglob("*") if p.suffix.lower() in VIDEO_SUFFIXES)
    if not paths:
        raise SystemExit(f"no videos under {args.negatives}")
    period = 1.0 / args.hz if args.hz else None
    rows_by_video: dict[str, list[dict]] = {}
    pipeline = build_eval_pipeline()
    try:
        for path in paths:
            t0 = time.time()
            rows = replay_video(pipeline, path, on_frame=lambda t, r: {**r, "gt": "normal"},
                                max_seconds=args.max_seconds, period=period)
            rows_by_video[path.name] = rows
            print(f"  {path.name:40} {len(rows):6} ticks  peak L"
                  f"{max((r['level'] for r in rows), default=1)}  ({time.time() - t0:.0f}s)")
        inventory = model_inventory(pipeline)
    finally:
        pipeline.shutdown()
    committed = level_report(rows_by_video, {}, grace=args.grace)
    print(format_levels(committed))
    report = {"commit": commit, "branch": branch, "negatives": str(args.negatives),
              "videos": [p.name for p in paths],
              "inference_hz": args.hz or settings.pipeline.inference_fps,
              "models": inventory, "committed": committed}
    out = OUT_ROOT / tag
    out.mkdir(parents=True, exist_ok=True)
    name = f"negatives{f'_{args.hz:g}hz' if args.hz else ''}.json"
    (out / name).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"written: {out / name}  (commit {commit})")
    return 0


def load_rows(path: Path, timeline, tolerance: float, video_ids) -> dict[str, list[dict]]:
    """Rows of a previous replay, re-labelled against the current timeline."""
    rows_by_video: dict[str, list[dict]] = {v: [] for v in video_ids}
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["video"] not in rows_by_video:
                continue
            t = float(r["t"])
            rows_by_video[r["video"]].append({
                "t": t, "gt": timeline.label_at(r["video"], t, tolerance),
                "pred": r["pred"], "incident_type": r["incident_type"],
                "level": int(r["level"]), "tci": float(r["tci"]),
                "weapon": float(r["weapon"]), "fire": float(r["fire"]),
                "behaviour": float(r["behaviour"]),
                "latency_ms": float(r["latency_ms"]),
                **{c: float(r[c]) for c in SOURCE_COLUMNS if r.get(c) not in (None, "")}})
    return rows_by_video


if __name__ == "__main__":
    sys.exit(main())
