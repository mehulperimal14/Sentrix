"""Per-video summary of the robbery_l3 replays (reads the replay CSV/JSON and
robbery_decisions.json written by drive.py). Prints markdown and writes
summary.json next to the runs."""
import csv
import json
import sys
from pathlib import Path

WT = Path(r"G:\Sentrix\.claude\worktrees\robbery-l3")
sys.path.insert(0, str(WT))
from sentrix.evaluation.custom_dataset import Timeline  # noqa: E402

ROOT = Path(r"G:\Sentrix\training\runs\custom_eval\robbery_l3_2026-11-21")
RUNS = ["v4_off", "v4_on", "v5_on"]
STATE_RANK = {None: -1, "unconfirmed": 0, "confirmed": 1, "corroborated": 2}
FILE_TO_ID = {}
for row in csv.DictReader(open(WT / "training/datasets/sentrix_custom/videos.csv", encoding="utf-8")):
    FILE_TO_ID[row["file"]] = row["video_id"]

timeline = Timeline()
summary = {}
for run in RUNS:
    d = ROOT / run
    csvs = list(d.glob("replay_*_fastlane.csv"))
    if not csvs:
        print(f"{run}: no replay yet")
        continue
    rows = list(csv.DictReader(open(csvs[0], encoding="utf-8")))
    report = json.loads(csvs[0].with_suffix(".json").read_text(encoding="utf-8"))
    dec = json.loads((d / "robbery_decisions.json").read_text(encoding="utf-8"))
    per = {}
    for r in rows:
        v = per.setdefault(r["video"], {"rows": []})
        v["rows"].append(r)
    out = {}
    for vid, v in per.items():
        rs = v["rows"]
        levels = [int(r["level"]) for r in rs]
        peak = max(levels)
        peak_types = sorted({r["incident_type"] for r in rs if int(r["level"]) == peak})
        l3 = [r for r in rs if int(r["level"]) >= 3]
        first_l3 = (float(l3[0]["t"]), l3[0]["incident_type"]) if l3 else None
        rob_l3 = [r for r in rs if int(r["level"]) >= 3 and r["incident_type"] == "robbery"]
        first_rob_l3 = float(rob_l3[0]["t"]) if rob_l3 else None
        rob_rows = [r for r in rs if r["incident_type"] == "robbery"]
        # L3+ onsets (level rising to >= 3) and whether gt was normal at the onset
        onsets = []
        prev = 1
        for r in rs:
            lv = int(r["level"])
            if lv >= 3 and prev < 3:
                onsets.append({"t": float(r["t"]), "type": r["incident_type"], "gt": r["gt"] or ""})
            prev = lv
        decs = [e for e in dec["decisions"] if FILE_TO_ID.get(e["video"]) == vid]
        best_state = max((e.get("robbery_state") for e in decs), key=lambda s: STATE_RANK.get(s, -1),
                         default=None)
        probs = [p for e in decs for p in (e.get("scene_robbery_keyframes") or [])]
        peak_p = max(probs) if probs else None
        above = sum(p >= 0.60 for p in probs)
        confirmed_clips = [e["t"] for e in decs if e.get("robbery_confirmed")]
        gated = sum(1 for e in decs for vt in (e.get("robbery_votes") or []) if vt.get("gated"))
        eps = [e for e in timeline.episodes(vid)]
        threat_eps = [e for e in eps if e[0] != "normal"]
        out[vid] = {
            "peak_level": peak, "peak_types": peak_types,
            "first_L3": first_l3, "first_robbery_L3_t": first_rob_l3,
            "robbery_typed_rows": len(rob_rows),
            "robbery_state_best": best_state,
            "robbery_confirmed_clip_ends": confirmed_clips[:20],
            "robbery_confirmed_clips": len(confirmed_clips),
            "peak_scene_robbery": peak_p, "keyframes_scored": len(probs),
            "keyframes_ge_0_60": above, "robbery_votes_gated_flag": gated,
            "L3_onsets": onsets,
            "threat_episodes": [(e[0], round(e[1], 2), round(e[2], 2)) for e in threat_eps],
        }
    summary[run] = {"videos": out, "commit": report.get("commit"), "models": report.get("models"),
                    "settings": dec["settings"],
                    "committed_all": report["splits"]["all"]["committed"]}

(ROOT / "summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
for run, s in summary.items():
    print(f"\n## {run}  (commit {s['commit']}, scene {s['models'].get('scene')}, "
          f"robbery_alarm {s['settings']['robbery_alarm']})")
    print("| video | peak level (type) | first L3 (t, type) | first robbery L3 | robbery evidence best | "
          "confirmed clips | peak p(robbery) | keyframes >= 0.60 / scored | L3+ onsets on normal gt |")
    print("|---|---|---|---|---|---|---|---|---|")
    for vid, v in s["videos"].items():
        fl = f"{v['first_L3'][0]:.1f} s {v['first_L3'][1]}" if v["first_L3"] else "-"
        fr = f"{v['first_robbery_L3_t']:.1f} s" if v["first_robbery_L3_t"] is not None else "-"
        normal_on = [o for o in v["L3_onsets"] if o["gt"] == "normal"]
        pp = f"{v['peak_scene_robbery']:.3f}" if v["peak_scene_robbery"] is not None else "-"
        print(f"| {vid} | L{v['peak_level']} ({','.join(v['peak_types'])}) | {fl} | {fr} | "
              f"{v['robbery_state_best']} | {v['robbery_confirmed_clips']} | {pp} | "
              f"{v['keyframes_ge_0_60']}/{v['keyframes_scored']} | "
              f"{len(normal_on)} {[ (o['t'], o['type']) for o in normal_on]} |")
