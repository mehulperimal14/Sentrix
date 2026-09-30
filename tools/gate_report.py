"""Demo gates G0-G5 (CLAUDE.md M9), with the statistical bounds each needs.

Each gate is PASS, FAIL, INSUFFICIENT (not enough data to decide - e.g. fewer
than 30 episodes, or under 3 hours of normal footage) or NOT RUN (its input was
not given). The gates are measured once, on a held-out gate set; a failing
class is dropped from what the demo promises or re-tested on fresh footage,
never tuned on the gate set.

G0  gate data exists and is untouched (SHA-256 manifest of the folder)
G1  per promised class, frame AUROC >= 0.90 for at least one source
G2  per promised class, >= 27/30 episodes caught at L3+ within 2 s
G3  0 L4/L5 over >= 3 h of normal footage, and <= 1 L3 onset per hour
G4  AI loop >= 5 Hz live, and onset -> L3 p95 + frame age p95 <= 2 s
G5  30-min live dry run: every scripted event reaches L3+, no unscripted L3+

Inputs are the outputs of the other tools::

    python tools/replay_custom_videos.py --videos all --tag gate      # G1 G2 G4
    python tools/replay_custom_videos.py --negatives <normal> --tag gate   # G3
    python tools/bench_live.py --json bench.json                        # G4
    python tools/gate_report.py \\
        --replay training/runs/custom_eval/gate/replay_all.json \\
        --negatives training/runs/custom_eval/gate/negatives.json \\
        --bench bench.json --data <gate footage> --manifest gate_manifest.json \\
        --dry-run-log var/events/events_....jsonl --script script.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS, FAIL, INSUFFICIENT, NOT_RUN, RECORDED = "PASS", "FAIL", "INSUFFICIENT", "NOT RUN", "RECORDED"


def gate(status: str, detail: str, **data) -> dict:
    return {"status": status, "detail": detail, **data}


# ── G0 ────────────────────────────────────────────────────────────────────────
def manifest_of(folder: Path) -> dict:
    files = {}
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files[path.relative_to(folder).as_posix()] = digest
    total = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return {"folder": str(folder), "files": len(files), "sha256": total, "per_file": files}


def g0(data: Path | None, manifest: Path | None) -> dict:
    if data is None:
        return gate(NOT_RUN, "no --data folder")
    if not data.exists():
        return gate(FAIL, f"{data} does not exist")
    current = manifest_of(data)
    if manifest is None:
        return gate(RECORDED, f"{current['files']} files, sha256 {current['sha256'][:16]}; "
                    "pass --manifest to record and later verify it", sha256=current["sha256"])
    if not manifest.exists():
        manifest.write_text(json.dumps(current, indent=2), encoding="utf-8")
        return gate(RECORDED, f"manifest written to {manifest} ({current['files']} files)",
                    sha256=current["sha256"])
    recorded = json.loads(manifest.read_text(encoding="utf-8"))
    if recorded["sha256"] == current["sha256"]:
        return gate(PASS, f"{current['files']} files unchanged since the manifest")
    changed = sorted(set(recorded["per_file"].items()) ^ set(current["per_file"].items()))
    return gate(FAIL, f"gate data changed ({len(changed)} differing entries)",
                changed=[name for name, _ in changed][:20])


# ── G1 ────────────────────────────────────────────────────────────────────────
def g1(replay_csv: Path | None, classes: list[str], split_videos: set[str] | None,
       floor: float = 0.90) -> dict:
    if replay_csv is None or not replay_csv.exists():
        return gate(NOT_RUN, "no replay CSV next to --replay")
    from sklearn.metrics import roc_auc_score

    with open(replay_csv, newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if r.get("gt")
                and (split_videos is None or r["video"] in split_videos)]
    per_class, ok = {}, True
    for cls in classes:
        y = [r["gt"] == cls for r in rows]
        if not any(y) or all(y):
            per_class[cls] = {"auroc": None, "note": "class absent from the labels"}
            ok = False
            continue
        scores = {}
        for column in (f"scene_{cls}", f"detector_{cls}", cls):
            values = [r.get(column) for r in rows]
            if all(v not in (None, "") for v in values):
                scores[column] = round(float(roc_auc_score(y, [float(v) for v in values])), 4)
        best = max(scores.items(), key=lambda kv: kv[1]) if scores else (None, None)
        per_class[cls] = {"auroc": scores, "best": best[0], "best_auroc": best[1],
                          "positives": sum(y), "ticks": len(y)}
        ok = ok and best[1] is not None and best[1] >= floor
    missing = [c for c, m in per_class.items() if m.get("auroc") is None]
    status = PASS if ok else (INSUFFICIENT if missing else FAIL)
    detail = "; ".join(f"{c}: {m['best']} {m['best_auroc']}" if m.get("best")
                       else f"{c}: {m.get('note')}" for c, m in per_class.items())
    return gate(status, detail, per_class=per_class, floor=floor)


# ── G2 / G4 (episodes) ────────────────────────────────────────────────────────
def g2(committed: dict | None, classes: list[str], need: int = 27, of: int = 30,
       within: float = 2.0) -> dict:
    if committed is None:
        return gate(NOT_RUN, "no --replay")
    episodes = committed["L3"]["episodes"]
    per_class, statuses = {}, []
    for cls in classes:
        m = episodes.get(cls)
        if m is None:
            per_class[cls] = {"episodes": 0}
            statuses.append(INSUFFICIENT)
            continue
        fast = m[f"within_{within:g}s"]
        per_class[cls] = {"episodes": m["episodes"], f"within_{within:g}s": fast,
                          "caught": m["caught"], "recall_ci95": m["recall_ci95"]}
        if m["episodes"] < of:
            # Not enough episodes to decide, unless it has already failed:
            # even catching every remaining one could not reach need/of.
            statuses.append(FAIL if fast + (of - m["episodes"]) < need else INSUFFICIENT)
        else:
            statuses.append(PASS if fast / m["episodes"] >= need / of else FAIL)
    status = (FAIL if FAIL in statuses else INSUFFICIENT if INSUFFICIENT in statuses
              else PASS)
    detail = "; ".join(f"{c}: {m.get(f'within_{within:g}s', 0)}/{m['episodes']} within "
                       f"{within:g}s" for c, m in per_class.items())
    return gate(status, detail + f" (needs >= {need}/{of})", per_class=per_class)


def g4(bench: dict | None, committed: dict | None, classes: list[str],
       min_hz: float = 5.0, budget: float = 2.0) -> dict:
    if bench is None or committed is None:
        return gate(NOT_RUN, "needs --bench and --replay")
    p95s = [committed["L3"]["episodes"][c]["p95_latency"] for c in classes
            if committed["L3"]["episodes"].get(c, {}).get("p95_latency") is not None]
    age = (bench.get("frame_age_ms") or {}).get("p95") or 0.0
    if not p95s:
        return gate(INSUFFICIENT, "no caught episodes to time", ai_hz=bench.get("ai_hz"))
    worst = max(p95s) + age / 1000.0
    ok = bench["ai_hz"] >= min_hz and worst <= budget
    return gate(PASS if ok else FAIL,
                f"AI {bench['ai_hz']} Hz (needs {min_hz:g}); onset -> L3 p95 "
                f"{max(p95s):.2f}s + frame age p95 {age / 1000:.2f}s = {worst:.2f}s "
                f"(needs <= {budget:g}s)", ai_hz=bench["ai_hz"], worst_seconds=round(worst, 3))


# ── G3 ────────────────────────────────────────────────────────────────────────
def g3(negatives: dict | None, min_hours: float = 3.0, l3_per_hour: float = 1.0) -> dict:
    if negatives is None:
        return gate(NOT_RUN, "no --negatives")
    c = negatives["committed"]
    hours = c["normal_hours"]
    l4 = c["L4"]["false_onsets"]
    l3 = c["L3"]["false_onsets"]
    rate, (_, hi) = c["L3"]["false_onsets_per_hour"], c["L3"]["false_onsets_per_hour_ci95"]
    detail = (f"{hours:.2f} h normal: L4+ onsets {l4}, L3+ onsets {l3} ({rate:.2f}/h, "
              f"95% <= {hi:.2f}/h)")
    if l4 > 0 or (hours >= min_hours and rate > l3_per_hour):
        return gate(FAIL, detail, hours=hours)
    if hours < min_hours:
        return gate(INSUFFICIENT, detail + f"; needs >= {min_hours:g} h", hours=hours)
    return gate(PASS, detail, hours=hours)


# ── G5 ────────────────────────────────────────────────────────────────────────
def g5(log: Path | None, script: Path | None, grace: float = 2.0,
       min_minutes: float = 30.0) -> dict:
    if log is None or script is None:
        return gate(NOT_RUN, "needs --dry-run-log and --script")
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    rows = [r for r in rows if "features" in r]
    if not rows:
        return gate(FAIL, "no ticks in the log")
    t0 = rows[0]["ts"]
    ticks = [(r["ts"] - t0, int(r["level"])) for r in rows]
    with open(script, newline="", encoding="utf-8") as fh:
        events = [(float(r["start_s"]), float(r["end_s"]), r.get("label", ""))
                  for r in csv.DictReader(fh)]
    caught = [any(s <= t <= e + grace and lvl >= 3 for t, lvl in ticks)
              for s, e, _ in events]
    onsets = [t for i, (t, lvl) in enumerate(ticks)
              if lvl >= 3 and (i == 0 or ticks[i - 1][1] < 3)]
    unscripted = [t for t in onsets if not any(s - grace <= t <= e + grace
                                               for s, e, _ in events)]
    minutes = ticks[-1][0] / 60.0
    detail = (f"{minutes:.1f} min; scripted events caught {sum(caught)}/{len(events)}; "
              f"unscripted L3+ onsets {len(unscripted)}")
    if minutes < min_minutes:
        return gate(INSUFFICIENT, detail + f"; needs >= {min_minutes:g} min")
    ok = all(caught) and not unscripted
    return gate(PASS if ok else FAIL, detail,
                missed=[events[i][2] for i, c in enumerate(caught) if not c],
                unscripted_at_s=[round(t, 1) for t in unscripted])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--replay", type=Path, default=None,
                        help="labelled replay JSON from replay_custom_videos.py")
    parser.add_argument("--split", default="all", help="split of the replay to judge")
    parser.add_argument("--negatives", type=Path, default=None)
    parser.add_argument("--bench", type=Path, default=None)
    parser.add_argument("--data", type=Path, default=None)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--dry-run-log", type=Path, default=None)
    parser.add_argument("--script", type=Path, default=None,
                        help="CSV with start_s,end_s,label for the dry run")
    parser.add_argument("--classes", default="weapon,fire",
                        help="classes the demo promises")
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    classes = [c.strip() for c in args.classes.split(",") if c.strip()]
    replay = json.loads(args.replay.read_text(encoding="utf-8")) if args.replay else None
    committed = split_videos = None
    if replay is not None:
        split = replay["splits"].get(args.split)
        if split is None:
            raise SystemExit(f"split {args.split!r} not in {sorted(replay['splits'])}")
        committed = split.get("committed")
        if committed is None:
            raise SystemExit("replay predates the committed-level report; re-run it")
        if args.split != "all":
            split_videos = {e["video"] for e in split["events"]["episode_detail"]}
    replay_csv = args.replay.with_suffix(".csv") if args.replay else None
    negatives = json.loads(args.negatives.read_text(encoding="utf-8")) if args.negatives else None
    bench = json.loads(args.bench.read_text(encoding="utf-8")) if args.bench else None

    gates = {
        "G0 gate data untouched": g0(args.data, args.manifest),
        "G1 frame AUROC >= 0.90": g1(replay_csv, classes, split_videos),
        "G2 >= 27/30 episodes at L3+ within 2 s": g2(committed, classes),
        "G3 no L4/L5 in >= 3 h normal, <= 1 L3/h": g3(negatives),
        "G4 >= 5 Hz, onset -> L3 p95 <= 2 s": g4(bench, committed, classes),
        "G5 30-min scripted dry run": g5(args.dry_run_log, args.script),
    }
    print(f"\nDemo gates for classes: {', '.join(classes)}\n")
    for name, g in gates.items():
        print(f"  [{g['status']:^12}] {name}\n                 {g['detail']}")
    overall = (PASS if all(g["status"] in (PASS, RECORDED) for g in gates.values())
               else FAIL if any(g["status"] == FAIL for g in gates.values())
               else "INCOMPLETE")
    print(f"\n  overall: {overall}")
    if args.json:
        args.json.write_text(json.dumps({"classes": classes, "overall": overall,
                                         "gates": gates}, indent=2), encoding="utf-8")
    return 0 if overall == PASS else 1


if __name__ == "__main__":
    sys.exit(main())
