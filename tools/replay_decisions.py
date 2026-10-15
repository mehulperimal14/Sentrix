"""Re-run the current decision layer on a logged live session.

Perception is expensive and the room is not always available, but every live
run leaves an event log. This tool feeds that log's per-tick inputs through the
*current* evidence -> TCI -> level policy -> incident logic and reports what
the system would have done, next to what it did. It is the regression test for
false alarms: the first live run (CLAUDE.md §5) is the fixture.

Inputs per tick:

* schema >= 3 rows carry the raw votes (``votes``) and replay exactly;
* schema 2 rows written by the v4 scene-fusion build carry ``weapon``/``fire``
  as ``align(p, 0.35, anchor)`` of the scene probabilities; the mapping is
  inverted to recover p(weapon) and p(fire). Assault/robbery probabilities and
  detector confidences were not logged and are taken as 0 (the live run drew a
  YOLO weapon box on 3 of 1340 frames and a fire box on none).

Usage::

    python tools/replay_decisions.py var/events/events_20260930_232502_c27258a25457.jsonl
    python tools/replay_decisions.py <log> --armed --json report.json
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: The v4 scene build logged weapon/fire as align(p, threshold, anchor).
SCHEMA2_ALIGN = {"weapon": (0.35, 0.55), "fire": (0.35, 0.70)}
SUB_CEILING = 0.3


def invert_align(value: float, threshold: float, anchor: float) -> float:
    if value >= anchor:
        return threshold + (value - anchor) * (1.0 - threshold) / (1.0 - anchor)
    if value < SUB_CEILING:
        return value * threshold / SUB_CEILING
    return threshold


class ClipSessionError(ValueError):
    """The log is a clip-mode session: its tick rows were decided per clip."""


def is_clip_row(row: dict) -> bool:
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    return row.get("kind") == "clip" or extra.get("mode") in ("clip", "fast_lane")


def load(path: Path) -> list[dict]:
    """The tick rows of a tick-mode session. This tool re-applies the *tick*
    k-of-n rule to the logged votes, so a clip-mode session (any ``"kind":
    "clip"`` row, or tick rows marked ``extra.mode`` clip / fast_lane) is
    refused instead of being replayed under the wrong rule."""
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    if any(is_clip_row(r) for r in rows):
        raise ClipSessionError(f"{path.name} is a clip-mode session; replay_decisions "
                               "only replays tick-mode logs")
    return [r for r in rows if "features" in r]


def tick_inputs(row: dict, floors: dict, detection) -> tuple[float, dict, dict]:
    """(frame start time, sources, context) for one logged row."""
    context = row.get("context", {})
    t = float(row["ts"]) - float(row.get("latency_ms", 0.0)) / 1000.0
    if row.get("votes"):
        sources = {s: {c: tuple(v) for c, v in per.items()}
                   for s, per in row["votes"].items()}
        return t, sources, context
    feats = row["features"]
    probs = {c: invert_align(float(feats.get(c, 0.0)), *SCHEMA2_ALIGN[c])
             for c in SCHEMA2_ALIGN}
    sources = {
        "scene": {c: (probs[c], floors[c]) for c in probs},
        "detector": {"weapon": (0.0, detection.weapon_conf),
                     "fire": (0.0, detection.fire_conf)},
    }
    return t, sources, context


def onsets(levels: list[int], times: list[float], minimum: int) -> dict:
    count, seconds = 0, 0.0
    for i, level in enumerate(levels):
        if level >= minimum and (i == 0 or levels[i - 1] < minimum):
            count += 1
        if level >= minimum and i + 1 < len(times):
            seconds += times[i + 1] - times[i]
    return {"onsets": count, "seconds": round(seconds, 1)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("log", type=Path)
    parser.add_argument("--armed", action="store_true", help="replay in armed mode")
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    from sentrix.config import settings
    from sentrix.fusion.evidence import EvidenceAccumulator
    from sentrix.fusion.features import FeatureVector
    from sentrix.fusion.policy import LevelPolicy
    from sentrix.fusion.tci import TCIEngine
    from sentrix.pipeline.incidents import IncidentManager

    try:
        rows = load(args.log)
    except ClipSessionError as exc:
        raise SystemExit(str(exc))
    if not rows:
        raise SystemExit(f"no feature rows in {args.log}")
    threat = dataclasses.replace(settings.threat, armed=args.armed)
    floors = threat.scene_threshold_floor
    acc = EvidenceAccumulator(threat)
    tci = TCIEngine()
    tci.threat = threat
    policy, incidents = LevelPolicy(), IncidentManager()

    last_person = None
    times, new_levels, old_levels = [], [], []
    types, closed, max_raw = Counter(), [], 0.0
    for row in rows:
        t, sources, context = tick_inputs(row, floors, settings.detection)
        person = bool(context.get("person_present"))
        if person:
            last_person = t
        person_recent = last_person is not None and t - last_person <= threat.person_memory_seconds
        if "person_recent" in context:
            person_recent = context["person_recent"]        # schema 3: as decided live
        # Schema 5 logs the fight rule's context (people recently, motion);
        # older logs fall back to this tick's person count and motion feature.
        persons = context.get("persons_recent", context.get("person_count"))
        motion = context.get("motion", (row.get("features") or {}).get("motion"))
        evidence = acc.update(t, sources, person_recent=person_recent,
                              context={"persons": persons, "motion": motion})
        features = FeatureVector(
            weapon=evidence["weapon"].score, fire=evidence["fire"].score,
            person_present=person, authorized=bool(context.get("authorized")),
            identity_degraded=bool(context.get("identity_degraded")),
            extras={"evidence": {c: e.to_dict() for c, e in evidence.items()},
                    "sensors": context.get("sensors") or {}})
        result = tci.compute(features, t)
        level = policy.update(result.raw_tci, t).level
        _, done = incidents.update(level, result.tci, result.incident_type, t,
                                   features=features.model_features(), reason=result.reason)
        if done is not None:
            closed.append(done)
        times.append(t)
        new_levels.append(level)
        old_levels.append(int(row.get("level", 1)))
        max_raw = max(max_raw, result.raw_tci)
        if result.incident_type != "normal":
            types[result.incident_type] += 1
    if incidents.current is not None:
        closed.append(incidents.current)

    duration = times[-1] - times[0]
    report = {
        "log": str(args.log), "ticks": len(rows), "minutes": round(duration / 60, 2),
        "armed": args.armed,
        "as_logged": {f"L{k}+": onsets(old_levels, times, k) for k in (2, 3, 4, 5)},
        "current_logic": {f"L{k}+": onsets(new_levels, times, k) for k in (2, 3, 4, 5)},
        "typed_ticks": dict(types), "max_raw_tci": round(max_raw, 4),
        "incidents": [{"type": i.incident_type, "peak_level": i.peak_level,
                       "peak_tci": round(i.peak_tci, 4), "reason": i.peak_reason}
                      for i in closed],
    }
    print(f"{args.log.name}: {len(rows)} ticks over {report['minutes']} min "
          f"({'armed' if args.armed else 'home'} mode)")
    print(f"{'':12}{'L2+':>16}{'L3+':>16}{'L4+':>16}{'L5+':>16}")
    for name in ("as_logged", "current_logic"):
        cells = "".join(f"{report[name][f'L{k}+']['onsets']:>6} / {report[name][f'L{k}+']['seconds']:>6}s"
                        for k in (2, 3, 4, 5))
        print(f"{name:12}{cells}")
    print(f"typed ticks: {dict(types) or 'none'}; max raw TCI {report['max_raw_tci']}")
    print(f"incidents: {len(closed)}" + "".join(
        f"\n  L{i['peak_level']} {i['type']:9} {i['peak_tci']:.3f}  {i['reason'][:90]}"
        for i in report["incidents"]))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
