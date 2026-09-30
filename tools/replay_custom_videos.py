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

Usage::

    python tools/replay_custom_videos.py
    python tools/replay_custom_videos.py --videos all --tag baseline
    python tools/replay_custom_videos.py --videos wa02 --max-seconds 20
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
    args = parser.parse_args(argv)

    from sentrix.config import settings
    from sentrix.evaluation.custom_dataset import Timeline, videos_for
    from sentrix.evaluation.metrics import (classification_report, event_report,
                                            format_report)

    commit, branch = git_describe()
    tag = args.tag or branch.replace("/", "_")
    videos = videos_for(args.videos)
    missing = [v for v in videos if not v.path(args.videos_dir).exists()]
    if missing:
        raise SystemExit(f"video not found: {missing[0].path(args.videos_dir)}")

    timeline = Timeline()
    out = OUT_ROOT / tag
    suffix = "" if args.videos == "heldout" else f"_{args.videos.replace(',', '-')}"
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
        "inference_hz": settings.pipeline.inference_fps,
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
        report["splits"][name] = {"frames": frame_rep, "events": event_rep,
                                  "levels": levels}
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
                         "weapon", "fire", "behaviour", "latency_ms"])
        for vid, rows in rows_by_video.items():
            for r in rows:
                writer.writerow([vid, r["t"], r["gt"] or "", r["pred"],
                                 r["incident_type"], r["level"], r["tci"],
                                 r["weapon"], r["fire"], r["behaviour"],
                                 r["latency_ms"]])
    print(f"written: {out / f'replay{suffix}.json'}  (commit {commit})")
    return 0


def replay_all(videos, args, timeline, rows_by_video) -> dict:
    """Replay every video through one eval pipeline; returns the model inventory."""
    from sentrix.evaluation.harness import build_eval_pipeline, replay_video

    pipeline = build_eval_pipeline()
    try:
        for v in videos:
            t0 = time.time()

            def label(t, row, vid=v.video_id):
                row["gt"] = timeline.label_at(vid, t, args.tolerance)
                return row

            rows = replay_video(pipeline, v.path(args.videos_dir), v.fps,
                                on_frame=label, max_seconds=args.max_seconds)
            rows_by_video[v.video_id] = rows
            flagged = sum(r["pred"] != "normal" for r in rows)
            print(f"  {v.video_id:6} [{v.split:5}] {len(rows):4} ticks  "
                  f"{flagged:4} threat  peak L{max((r['level'] for r in rows), default=1)}"
                  f"  ({time.time() - t0:.0f}s)")
    finally:
        pipeline.shutdown()
    return model_inventory(pipeline)


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
                "latency_ms": float(r["latency_ms"])})
    return rows_by_video


if __name__ == "__main__":
    sys.exit(main())
