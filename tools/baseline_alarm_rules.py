"""Published alarm rules against SENTRIX's, on the same replayed ticks.

The capstone report compares SENTRIX with existing practice. Model scores
from public benchmarks cannot be compared with two rooms, but an *alarm rule*
can: this tool takes the per-tick source values a replay already logged
(``replay_custom_videos.py`` writes the scene classifier's and the detector
heads' raw values next to the committed level) and asks what two common rules
would have done with exactly those values:

* **single frame** - the alarm is raised on any tick where one source crosses
  its threshold (detection = alarm; the pre-repair SENTRIX and most
  detection papers);
* **five successive** - the alarm is raised once one source has crossed its
  threshold on five consecutive ticks, the rule of Olmos et al. (2018) for a
  handgun detector, applied here to each source;
* **SENTRIX** - the committed level the replay logged (Level 3 and above),
  i.e. 2-of-3 confirmation, the person gate and the modality rules.

Thresholds are the product's own (scene: the checkpoint raised to the floors;
detector heads: ``DetectionConfig``), so only the rule differs. Episodes and
false onsets use ``metrics.level_report`` with the replay's ground truth, so
the SENTRIX row must reproduce the replay's own Level-3 numbers; the tool
checks that and refuses to write otherwise. Weapon and fire only (the alarm
classes the rules apply to). Read-only: no model runs, nothing is sent.

    python tools/baseline_alarm_rules.py                     # M11b tick replay
    python tools/baseline_alarm_rules.py --tag m11b --scene-weights models/v4_custom/scene_classifier_custom_v4.pt
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sentrix.evaluation.custom_dataset import Timeline, videos_for  # noqa: E402
from sentrix.evaluation.metrics import level_report  # noqa: E402

RUNS = ROOT / "training" / "runs" / "custom_eval"
CLASSES = ("weapon", "fire")
SOURCES = {"weapon": ("scene_weapon", "detector_weapon"),
           "fire": ("scene_fire", "detector_fire")}


def thresholds(scene_weights: Path) -> dict[str, float]:
    """The per-source bars the replayed pipeline voted with."""
    import torch

    from sentrix.config import settings
    ck = torch.load(scene_weights, map_location="cpu", weights_only=False)
    floors = settings.threat.scene_threshold_floor
    scene = {c: max(float(ck["thresholds"][c]), float(floors.get(c, 0.0))) for c in CLASSES}
    det = settings.detection
    return {"scene_weapon": scene["weapon"], "scene_fire": scene["fire"],
            "detector_weapon": det.weapon_conf, "detector_fire": det.fire_conf}


def rule_rows(rows: list[dict], bars: dict[str, float], run: int) -> list[dict]:
    """Rows whose level is 3 while ``run`` consecutive ticks of one source are over its bar."""
    streak = {c: 0 for cols in SOURCES.values() for c in cols}
    out = []
    for r in rows:
        firing = []
        for cls in CLASSES:
            for col in SOURCES[cls]:
                v = r.get(col)
                over = v is not None and v >= bars[col]
                streak[col] = streak[col] + 1 if over else 0
                if streak[col] >= run:
                    firing.append((v - bars[col], cls))
        alarm = max(firing)[1] if firing else None
        out.append({"t": r["t"], "gt": r["gt"], "pred": alarm or "normal",
                    "level": 3 if alarm else 1})
    return out


def summarise(report: dict) -> dict:
    l3 = report["L3"]
    eps = l3["episodes"]
    return {cls: {"caught": eps[cls]["caught"], "episodes": eps[cls]["episodes"],
                  "median_latency_s": eps[cls]["median_latency"]}
            for cls in CLASSES if cls in eps} | {
        "false_onsets": l3["false_onsets"],
        "false_onsets_per_hour": l3["false_onsets_per_hour"],
        "false_onsets_per_hour_ci95": l3["false_onsets_per_hour_ci95"],
        "normal_minutes": round(report["normal_hours"] * 60, 2)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tag", default="m11b", help="replay folder under training/runs/custom_eval")
    ap.add_argument("--csv", default="replay_all.csv")
    ap.add_argument("--scene-weights", type=Path,
                    default=ROOT / "models" / "v4_custom" / "scene_classifier_custom_v4.pt")
    ap.add_argument("--out", type=Path, default=RUNS / "baseline_rules")
    args = ap.parse_args()

    run_dir = RUNS / args.tag
    replay = json.loads((run_dir / args.csv.replace(".csv", ".json")).read_text(encoding="utf-8"))
    sys.path.insert(0, str(ROOT / "tools"))
    from replay_custom_videos import load_rows  # same parsing and ground truth as the replay

    timeline = Timeline()
    video_ids = list(replay["videos"])
    rows_by_video = load_rows(run_dir / args.csv, timeline, replay["tolerance_s"], video_ids)
    episodes = {v: timeline.episodes(v) for v in video_ids}
    grace = replay["grace_s"]
    bars = thresholds(args.scene_weights)

    results = {
        "single_frame": summarise(level_report(
            {v: rule_rows(r, bars, 1) for v, r in rows_by_video.items()}, episodes, grace=grace)),
        "five_successive": summarise(level_report(
            {v: rule_rows(r, bars, 5) for v, r in rows_by_video.items()}, episodes, grace=grace)),
        "sentrix": summarise(level_report(rows_by_video, episodes, grace=grace)),
    }
    stored = replay["splits"]["all"]["committed"]["L3"]
    mine = results["sentrix"]
    same = (mine["false_onsets"] == stored["false_onsets"] and all(
        mine[c]["caught"] == stored["episodes"][c]["caught"] for c in CLASSES))
    if not same:
        raise SystemExit(f"SENTRIX row {mine} does not reproduce the replay's own L3 {stored}")

    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                            text=True, cwd=ROOT).stdout.strip()
    report = {"tool_commit": commit, "replay": f"{args.tag}/{args.csv}",
              "replay_commit": replay["commit"], "mode": replay["mode"],
              "inference_hz": replay["inference_hz"], "videos": video_ids,
              "thresholds": bars, "grace_s": grace,
              "rules": {"single_frame": "alarm on any tick with one source over its bar",
                        "five_successive": "alarm once one source is over its bar on 5 "
                                           "consecutive ticks (Olmos et al., 2018)",
                        "sentrix": "the replay's committed Level 3+ (logged)"},
              "results": results, "sentrix_reproduces_replay": same}
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{args.tag}_{args.csv.replace('.csv', '')}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    for name, r in results.items():
        print(f"{name:16} weapon {r['weapon']['caught']}/{r['weapon']['episodes']}  "
              f"fire {r['fire']['caught']}/{r['fire']['episodes']}  false onsets "
              f"{r['false_onsets']} in {r['normal_minutes']} normal min "
              f"({r['false_onsets_per_hour']}/h)")
    print(f"written: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
