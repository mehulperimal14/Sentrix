"""Replay driver: run tools/replay_custom_videos.py from the robbery-l3 worktree in
clip mode and log the robbery evidence of every clip decision.

Usage: python drive.py OUTDIR VIDEO_IDS [extra replay args...]
The environment (SENTRIX_*) is set by the caller before this process starts.
"""
import json
import os
import sys
from pathlib import Path

WT = Path(r"G:\Sentrix\.claude\worktrees\robbery-l3")
sys.path.insert(0, str(WT))
os.chdir(WT)

out_dir = Path(sys.argv[1])
video_ids = sys.argv[2]
extra = sys.argv[3:]
assert "wa02" not in video_ids.split(","), "wa02 is never replayed here"

import sentrix.pipeline.clip as clipmod  # noqa: E402
from sentrix.config import settings  # noqa: E402

LOG: list[dict] = []
_orig = clipmod.replay_video_clip


def wrapped(pipeline, path, fps=None, on_frame=None, max_seconds=None, period=None,
            fast_lane=None, on_decision=None, audio=None):
    vid = Path(path).name

    def hook(decision):
        if on_decision is not None:
            on_decision(decision)
        scores = decision.get("clip")
        entry = {"video": vid, "t": round(float(decision.get("t", 0.0)), 3),
                 "kind": decision.get("kind", "clip"), "level": decision.get("level"),
                 "tci": round(float(decision.get("tci", 0.0)), 4),
                 "incident_type": decision.get("incident_type")}
        if scores is not None:
            feats = scores.features
            ev = (feats.extras or {}).get("evidence", {}).get("robbery") if feats else None
            if ev:
                entry["robbery_state"] = ev.get("state")
                entry["robbery_confirmed"] = ev.get("confirmed")
                entry["robbery_experimental"] = ev.get("experimental")
                entry["robbery_score"] = ev.get("score")
                entry["robbery_votes"] = ev.get("votes")
            probs = []
            for k in scores.keyframes:
                a = k.analysis
                if a is not None and a.scene is not None:
                    probs.append(round(float(a.scene.probs.get("robbery", 0.0)), 4))
            entry["scene_robbery_keyframes"] = probs
            entry["persons_recent"] = getattr(scores, "persons", None)
            sc = (feats.extras or {}).get("scene") if feats else None
            if sc:
                entry["scene_thresholds"] = sc.get("thresholds")
        LOG.append(entry)

    return _orig(pipeline, path, fps=fps, on_frame=on_frame, max_seconds=max_seconds,
                 period=period, fast_lane=fast_lane, on_decision=hook, audio=audio)


clipmod.replay_video_clip = wrapped

from tools import replay_custom_videos as rcv  # noqa: E402

print("robbery_alarm", settings.threat.robbery_alarm,
      "experimental", settings.threat.experimental_classes,
      "person_gated", settings.threat.person_gated_classes,
      "sos alarm_classes", settings.sos.alarm_classes)
code = rcv.main(["--mode", "clip", "--fast-lane", "--threads", "4",
                 "--videos", video_ids, "--tag", str(out_dir), *extra])
out_dir.mkdir(parents=True, exist_ok=True)
(out_dir / "robbery_decisions.json").write_text(json.dumps({
    "settings": {"robbery_alarm": settings.threat.robbery_alarm,
                 "experimental_classes": list(settings.threat.experimental_classes),
                 "person_gated_classes": list(settings.threat.person_gated_classes),
                 "sos_alarm_classes": list(settings.sos.alarm_classes),
                 "scene_thresholds_env": os.getenv("SENTRIX_SCENE_THRESHOLDS"),
                 "scene_weights_env": os.getenv("SENTRIX_SCENE_WEIGHTS"),
                 "runtime": settings.clip.runtime, "imgsz": settings.clip.imgsz},
    "decisions": LOG}, indent=1), encoding="utf-8")
sys.exit(code)
