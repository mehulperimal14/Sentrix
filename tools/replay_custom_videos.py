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

``--mode clip`` replays through the 4-second clip engine (docs/8_RASPBERRY_PI5_INTEGRATION.md T1,
``build_eval_pipeline(mode="clip")``): every frame into the clip buffer, one
decision per 2 s clip (plus fast-lane raises with ``--fast-lane``), rows on
the same tick grid as the per-frame replay showing the committed state at each
instant. ``--threads`` holds torch to a CPU budget (the Pi 5 has 4 cores);
``--compare`` puts a previous report's committed-level results next to these
(e.g. the M5 per-frame run).

Usage::

    python tools/replay_custom_videos.py
    python tools/replay_custom_videos.py --videos all --tag baseline
    python tools/replay_custom_videos.py --videos wa02 --max-seconds 20
    python tools/replay_custom_videos.py --videos all --hz 2 --tag m5
    python tools/replay_custom_videos.py --negatives D:/footage/normal --tag m5
    python tools/replay_custom_videos.py --videos all --mode clip --tag clip \\
        --threads 4 --compare training/runs/custom_eval/m5/replay_all.json
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sentrix.evaluation.harness import SOURCE_COLUMNS  # noqa: E402
from tools.eval_custom_frames import OUT_ROOT, git_describe, model_inventory  # noqa: E402

#: Extra replay CSV columns in clip mode: the decision a row shows, and its kind.
CLIP_COLUMNS = ("decision_t", "kind")
#: video id -> the soundtrack heard during its replay (None: no audio), D15.
AUDIO_USED: dict[str, dict | None] = {}


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
                        help="inference rate for this replay (default: configured; in "
                             "clip mode the rate rows are sampled at)")
    parser.add_argument("--negatives", type=Path, default=None,
                        help="folder of normal-only videos: measure false alarms/hour")
    parser.add_argument("--mode", choices=("tick", "clip"), default="tick",
                        help="per-frame pipeline (tick) or the 4-second clip engine")
    parser.add_argument("--fast-lane", action="store_true",
                        help="clip mode: run the fast lane (YOLO @320 between clips)")
    parser.add_argument("--threads", type=int, default=None,
                        help="CPU threads for torch/OpenCV (clip mode default: "
                             "SENTRIX_CLIP_THREADS)")
    parser.add_argument("--compare", type=Path, default=None,
                        help="a previous replay JSON to compare committed levels with")
    parser.add_argument("--audio", choices=("auto", "off"), default="auto",
                        help="auto: hear each video's own soundtrack, time-aligned "
                             "(ffmpeg; D15); off: no audio, as before D15")
    args = parser.parse_args(argv)

    from sentrix.config import settings
    from sentrix.evaluation.custom_dataset import Timeline, videos_for
    from sentrix.evaluation.metrics import (classification_report, event_report,
                                            format_levels, format_report,
                                            level_report)

    if args.mode == "clip" or args.threads:
        from sentrix.runtime import configure_threads
        args.thread_report = configure_threads(args.threads or settings.clip.threads)
    else:
        args.thread_report = None
    commit, branch = git_describe()
    tag = args.tag or ("clip" if args.mode == "clip" else branch.replace("/", "_"))
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
    if args.mode == "clip" and args.fast_lane:
        suffix += "_fastlane"
    if args.audio == "off":
        suffix += "_noaudio"
    rows_by_video: dict[str, list[dict]] = {}
    clip_stats: dict | None = None
    started = time.time()
    if args.rescore:
        rows_by_video = load_rows(out / f"replay{suffix}.csv", timeline,
                                  args.tolerance, [v.video_id for v in videos])
        previous = json.loads((out / f"replay{suffix}.json").read_text(encoding="utf-8"))
        commit, branch = previous["commit"], previous["branch"]
        inventory = previous.get("models", {})
        clip_stats = previous.get("clip")
    else:
        inventory, clip_stats = replay_all(videos, args, timeline, rows_by_video)

    report: dict = {
        "commit": commit, "branch": branch, "videos": [v.video_id for v in videos],
        "mode": args.mode,
        "inference_hz": args.hz or settings.pipeline.inference_fps,
        "tolerance_s": args.tolerance, "grace_s": args.grace,
        "models": inventory, "splits": {},
        "audio": {"mode": args.audio, "per_video": dict(AUDIO_USED),
                  "align_tolerance_s": settings.audio.align_tolerance_seconds,
                  "cue_thresholds": dict(settings.audio.cue_thresholds),
                  "audio_votes": settings.threat.audio_votes},
        "identity": {"unknown_alert_home": settings.threat.unknown_alert_home,
                     "note": "the harness scores everyone as an unrecognised person"},
    }
    if args.mode == "clip":
        report["clip"] = clip_stats
        report["threads"] = args.thread_report
        report["clip_config"] = {k: getattr(settings.clip, k) for k in (
            "window_seconds", "hop_seconds", "keyframes", "uniform_keyframes",
            "motion_keyframes", "confirm_keyframes", "min_escalate_observations",
            "fast_lane_imgsz", "fast_lane_every_n", "fast_lane_hits", "runtime", "imgsz",
            "scene_keyframes")}
        report["clip_config"]["fast_lane"] = bool(args.fast_lane)
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
    if clip_stats:
        print(format_clip_stats(clip_stats))
    if args.compare is not None:
        baseline = json.loads(args.compare.read_text(encoding="utf-8"))
        report["comparison"] = compare_committed(baseline, report, str(args.compare))
        print(format_comparison(report["comparison"]))

    out.mkdir(parents=True, exist_ok=True)
    (out / f"replay{suffix}.json").write_text(json.dumps(report, indent=2),
                                              encoding="utf-8")
    extra = CLIP_COLUMNS if args.mode == "clip" else ()
    with open(out / f"replay{suffix}.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["video", "t", "gt", "pred", "incident_type", "level", "tci",
                         "weapon", "fire", "behaviour", "latency_ms", *SOURCE_COLUMNS,
                         *extra])
        for vid, rows in rows_by_video.items():
            for r in rows:
                writer.writerow([vid, r["t"], r["gt"] or "", r["pred"],
                                 r["incident_type"], r["level"], r["tci"],
                                 r["weapon"], r["fire"], r["behaviour"],
                                 r["latency_ms"], *(r.get(c, "") for c in SOURCE_COLUMNS),
                                 *(r.get(c, "") for c in extra)])
    print(f"written: {out / f'replay{suffix}.json'}  (commit {commit})")
    return 0


class ClipStats:
    """Per-clip compute and stage timings over a replay (clip mode)."""

    def __init__(self) -> None:
        self.compute: list[float] = []
        self.stages: dict[str, list[float]] = {}
        self.calls: dict[str, int] = {}
        self.clips = self.gate_open = self.keyframes = self.fast_lane = 0

    def __call__(self, decision: dict) -> None:
        if decision.get("kind") == "fast_lane":
            self.fast_lane += 1
            return
        scores = decision.get("clip")
        if scores is None:
            return
        self.clips += 1
        self.gate_open += bool(scores.gate.get("open"))
        self.keyframes += len(scores.keyframes)
        self.compute.append(scores.compute_ms)
        for name, stage in scores.stages.items():
            self.calls[name] = self.calls.get(name, 0) + int(stage.get("calls", 0))
            if stage.get("ran"):
                self.stages.setdefault(name, []).append(float(stage["ms"]))

    @staticmethod
    def _pct(values: list[float], q: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        return round(ordered[min(len(ordered) - 1, int(len(ordered) * q))], 1)

    def report(self, fast_lane=None) -> dict:
        return {
            "clips": self.clips, "gate_open_fraction": round(self.gate_open / max(self.clips, 1), 4),
            "keyframes_per_clip": round(self.keyframes / max(self.clips, 1), 2),
            "compute_ms": {"p50": self._pct(self.compute, 0.5),
                           "p95": self._pct(self.compute, 0.95),
                           "mean": round(statistics.mean(self.compute), 1) if self.compute else None},
            "stages": {name: {"p50_ms": self._pct(v, 0.5), "p95_ms": self._pct(v, 0.95),
                              "ran": len(v), "calls": self.calls.get(name, 0)}
                       for name, v in self.stages.items()},
            "fast_lane_decisions": self.fast_lane,
            "fast_lane": fast_lane,
            "note": "replay is synchronous: compute is per clip on this machine; "
                    "decisions are stamped at the clip end (compute not added)",
        }


def replay_all(videos, args, timeline, rows_by_video) -> tuple[dict, dict | None]:
    """Replay every video through one eval pipeline; returns the model
    inventory and, in clip mode, the clip timings."""
    from sentrix.evaluation.harness import build_eval_pipeline, replay_video, soundtrack

    pipeline = build_eval_pipeline(mode=args.mode)
    period = 1.0 / args.hz if args.hz else None
    stats = ClipStats() if args.mode == "clip" else None
    lane = None
    try:
        if args.mode == "clip":
            from sentrix.pipeline.clip import replay_video_clip
            if not pipeline.clip_runner.enable_fast_lane(args.fast_lane) and args.fast_lane:
                raise SystemExit("--fast-lane: no fast-lane head could be loaded")
        for v in videos:
            t0 = time.time()

            def label(t, row, vid=v.video_id):
                row["gt"] = timeline.label_at(vid, t, args.tolerance)
                return row

            track = (soundtrack(v.path(args.videos_dir)) if args.audio == "auto" else None)
            AUDIO_USED[v.video_id] = (None if track is None else
                                      {"seconds": round(track.duration, 2),
                                       "model": track.model_name})
            if args.mode == "clip":
                rows = replay_video_clip(pipeline, v.path(args.videos_dir), v.fps,
                                         on_frame=label, max_seconds=args.max_seconds,
                                         period=period, on_decision=stats, audio=track)
            else:
                rows = replay_video(pipeline, v.path(args.videos_dir), v.fps,
                                    on_frame=label, max_seconds=args.max_seconds,
                                    period=period, audio=track)
            rows_by_video[v.video_id] = rows
            flagged = sum(r["pred"] != "normal" for r in rows)
            print(f"  {v.video_id:6} [{v.split:5}] {len(rows):4} ticks  "
                  f"{flagged:4} threat  peak L{max((r['level'] for r in rows), default=1)}"
                  f"  ({time.time() - t0:.0f}s)")
        if args.mode == "clip" and pipeline.clip_runner.fast_lane is not None:
            lane = pipeline.clip_runner.fast_lane.stats()
    finally:
        pipeline.shutdown()
    return model_inventory(pipeline), (stats.report(lane) if stats is not None else None)


def format_clip_stats(stats: dict) -> str:
    lines = [f"clip engine: {stats['clips']} clips, gate open {stats['gate_open_fraction']:.2f}, "
             f"{stats['keyframes_per_clip']:.1f} keyframes/clip, compute p50 "
             f"{stats['compute_ms']['p50']} ms / p95 {stats['compute_ms']['p95']} ms; "
             f"fast-lane decisions {stats['fast_lane_decisions']}"]
    for name, s in sorted(stats["stages"].items()):
        lines.append(f"    {name:12} p50 {s['p50_ms']:>7} ms  p95 {s['p95_ms']:>7} ms  "
                     f"ran {s['ran']:4}  calls {s['calls']}")
    return "\n".join(lines)


def compare_committed(baseline: dict, current: dict, source: str,
                      split: str = "all") -> dict:
    """Committed-level results of two replays side by side, per level and class."""
    def committed(report):
        return ((report.get("splits") or {}).get(split) or {}).get("committed") or {}

    old, new = committed(baseline), committed(current)
    levels = {}
    for key in sorted(k for k in set(old) | set(new) if k.startswith("L")):
        a, b = old.get(key, {}), new.get(key, {})
        classes = {}
        for cls in sorted(set(a.get("episodes", {})) | set(b.get("episodes", {}))):
            ea, eb = a.get("episodes", {}).get(cls, {}), b.get("episodes", {}).get(cls, {})
            classes[cls] = {
                "episodes": eb.get("episodes", ea.get("episodes")),
                "caught": [ea.get("caught"), eb.get("caught")],
                "median_latency_s": [ea.get("median_latency"), eb.get("median_latency")],
                "p95_latency_s": [ea.get("p95_latency"), eb.get("p95_latency")],
                "within_2s": [ea.get("within_2s"), eb.get("within_2s")],
            }
        levels[key] = {"false_onsets": [a.get("false_onsets"), b.get("false_onsets")],
                       "onsets": [a.get("onsets"), b.get("onsets")],
                       "normal_time_at_level": [a.get("normal_time_at_level"),
                                                b.get("normal_time_at_level")],
                       "classes": classes}
    return {"baseline": source, "baseline_commit": baseline.get("commit"),
            "baseline_mode": baseline.get("mode", "tick"),
            "baseline_hz": baseline.get("inference_hz"),
            "current_mode": current.get("mode"), "split": split,
            "normal_hours": [old.get("normal_hours"), new.get("normal_hours")],
            "levels": levels,
            "note": "pairs are [baseline, current]"}


def format_comparison(comparison: dict) -> str:
    lines = [f"committed levels vs {comparison['baseline']} "
             f"({comparison['baseline_mode']} @ {comparison['baseline_hz']} Hz, commit "
             f"{comparison['baseline_commit']}) -> {comparison['current_mode']}:"]
    for key, level in comparison["levels"].items():
        caught = ", ".join(f"{cls} {c['caught'][0]}->{c['caught'][1]}/{c['episodes']}"
                           for cls, c in level["classes"].items())
        lines.append(f"    {key}+ false onsets {level['false_onsets'][0]}->"
                     f"{level['false_onsets'][1]}; caught {caught}")
    return "\n".join(lines)


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
    pipeline = build_eval_pipeline(mode=args.mode)
    try:
        if args.mode == "clip":
            from sentrix.pipeline.clip import replay_video_clip
            pipeline.clip_runner.enable_fast_lane(args.fast_lane)
        for path in paths:
            t0 = time.time()
            if args.mode == "clip":
                rows = replay_video_clip(pipeline, path,
                                         on_frame=lambda t, r: {**r, "gt": "normal"},
                                         max_seconds=args.max_seconds, period=period)
            else:
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
              "mode": args.mode, "videos": [p.name for p in paths],
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
                **{c: float(r[c]) for c in SOURCE_COLUMNS if r.get(c) not in (None, "")},
                **{c: r[c] for c in CLIP_COLUMNS if r.get(c) not in (None, "")}})
    return rows_by_video


if __name__ == "__main__":
    sys.exit(main())
