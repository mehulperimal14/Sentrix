"""Propose per-room scene-classifier thresholds from the device's own event log.

Why this exists
---------------
The scene classifier is the input that fails in a room it has never seen
(CLAUDE.md section 5): in the demo room it raised 16 false weapon Level 3s and
2 false fire Level 3s, every one from the scene model alone, a little over its
floor. Both were fixed by hand: someone read the Pi's event log, found the
highest score a quiet room produced, and wrote a bar above it into
``var/calibration/scene.json`` (weapon 0.75, fire 0.70). This tool does that
reading the same way every time, so a device can be re-calibrated after it
moves to a new room, without anyone guessing.

What it does
------------
It reads the event logs of a period the team asserts was **quiet** (no staged
threat in view) and, for each class, replays the device's own confirmation
rule at every possible threshold - read-only, nothing is re-run through a
model:

* **clip mode** (the Pi): a class is confirmed in a clip when the scene score
  crosses the bar on ``ClipConfig.confirm_keyframes`` (2) distinct keyframes,
  a weapon keyframe counting only with a person seen in the last
  ``person_memory_seconds`` (the engine's own gate, rebuilt from the logged
  person boxes);
* **tick mode**: ``confirm_k`` of the last ``confirm_n`` fresh evaluations
  (2 of 3), a gap over ``confirm_max_gap_seconds`` resetting the window, a
  weapon crossing counting only when ``person_recent`` was not False.

For each clip (or fresh tick) that gives the **confirming value**: the bar is
crossed by a confirmation exactly when it is at or under that value. The
largest confirming value of the quiet log is the highest bar that would still
have raised a false Level 3 there.

The rule (fixed before it was run on the demo room's logs)::

    proposed = max(floor, confirming_max + 0.10, single_max + 0.01)

rounded up to 0.01 and capped at 0.95: no confirmation in the quiet period
comes within 0.10 of the bar, and not one keyframe crosses it. The two hand
calibrations of the demo room follow almost the same rule (weapon: single
max 0.742 -> 0.75; fire: confirming max 0.587, single max 0.646 -> 0.70).

The cost of a higher bar is a real threat the scene model no longer
confirms on its own (D3: the detector and the other modalities are
unchanged). ``--check LOG`` replays a session in which the team staged a
threat (the replica gun, a flame) and says whether it still confirms at the
proposed bar. A bar over 0.90 is a warning, not a fix: the classifier cannot
tell this room's normal from a threat, and the room's own recordings (M6,
ROADMAP section 15) are the cure.

Safety
------
Read-only by default. ``--write`` merges the proposal into
``var/calibration/scene.json`` after copying the old file to
``scene.json.bak-<time>``, naming the scene weights it was made for (the
engine flags the bars once other weights load: a room bar belongs to one
model, and the room's own fine-tune, M6, changes the model); it never lowers a bar already in that file unless
``--allow-lower`` is given, and it skips a class with less than
``--min-hours`` of scored quiet time. It never opens a camera or a
microphone, never touches ``var/sentrix.db``, and the device reads the file
only at start-up (restart it to apply).

Usage::

    python tools/calibrate_room.py                         # var/events, report only
    python tools/calibrate_room.py --since 2026-10-23T18:00 --until 2026-10-24T06:00
    python tools/calibrate_room.py --exclude 2026-11-02T01:00,2026-11-02T01:30
    python tools/calibrate_room.py --check var/events/events_20261120_*.jsonl
    python tools/calibrate_room.py --write                 # then restart the device
    python tools/calibrate_room.py --json report.json

On the Pi, without copying anything there (the script arrives on stdin)::

    ssh sentrix@<pi> 'cd ~/Sentrix && .venv/bin/python - --events var/events' < tools/calibrate_room.py
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import shutil
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

ROOT = Path(__file__).resolve().parents[1] if "__file__" in globals() else Path.cwd()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: Classes calibrated by default: the alarm classes (D1). Assault and robbery
#: are experimental (L2 on their own) and can be named with ``--classes``.
DEFAULT_CLASSES = ("weapon", "fire")
CONFIRM_MARGIN = 0.10
SINGLE_MARGIN = 0.01
CAP = 0.95
WARN_ABOVE = 0.90
#: Two confirmed observations further apart than this are separate episodes.
EPISODE_GAP_SECONDS = 10.0
CLIP_MARKER = '"kind": "clip"'


# ── rules (from the running configuration) ───────────────────────────────────
@dataclass(frozen=True)
class Rules:
    confirm_k: int = 2
    confirm_n: int = 3
    confirm_max_gap: float = 3.0
    person_memory: float = 2.0
    confirm_keyframes: int = 2
    hop_seconds: float = 2.0
    floors: dict = field(default_factory=lambda: {"weapon": 0.50, "fire": 0.45,
                                                  "assault": 0.60, "robbery": 0.60})

    @classmethod
    def from_settings(cls) -> "Rules":
        from sentrix.config import settings
        threat, clip = settings.threat, settings.clip
        return cls(int(threat.confirm_k), int(threat.confirm_n),
                   float(threat.confirm_max_gap_seconds),
                   float(threat.person_memory_seconds),
                   int(clip.confirm_keyframes), float(clip.hop_seconds),
                   dict(threat.scene_threshold_floor))


@dataclass
class Moment:
    """One clip, or one fresh tick, as the scene model saw it for one class."""

    session: str
    ts: float              # wall clock (row ``ts``)
    clock: float           # the session's own clock (clip ``end``, tick ``ts``)
    confirm: float         # the bar is crossed by a confirmation iff bar <= confirm
    single: float          # highest score of one evaluation (ungated)
    mode: str              # "clip" | "tick"


@dataclass
class ClassLog:
    """Everything the quiet (or checked) period says about one class."""

    moments: list[Moment] = field(default_factory=list)
    #: Every scored value, one per keyframe (clip) or fresh tick.
    values: list[float] = field(default_factory=list)
    seconds: float = 0.0
    #: The bar the device applied, from the newest row that logged one.
    in_force: float | None = None
    in_force_ts: float = float("-inf")

    #: Clips where the device logged its own scene verdict at its own bar:
    #: how often the replay reaches the same verdict (a check of the replay).
    agree: int = 0
    disagree: list = field(default_factory=list)

    def saw_bar(self, ts: float, bar: float) -> None:
        if ts >= self.in_force_ts:
            self.in_force, self.in_force_ts = bar, ts


# ── reading ──────────────────────────────────────────────────────────────────
def log_files(paths: Iterable[str | Path]) -> list[Path]:
    """Event logs named by files, directories or glob patterns, in name order."""
    out: list[Path] = []
    for item in paths:
        text = str(item)
        path = Path(text)
        if path.is_dir():
            out.extend(sorted(path.glob("events_*.jsonl")))
        elif path.exists():
            out.append(path)
        else:
            out.extend(Path(p) for p in sorted(glob.glob(text)))
    seen: set[Path] = set()
    unique = []
    for path in out:
        key = path.resolve()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return sorted(unique, key=lambda p: p.name)


def read_rows(paths: Iterable[Path]) -> Iterator[dict[str, Any]]:
    """Clip rows of files that have them, tick rows with votes of the rest.

    In clip mode every clip also writes a decision tick row whose ``votes``
    are the clip's aggregate; those are skipped, the clip row is the record.
    """
    for path in paths:
        try:
            with open(path, encoding="utf-8") as handle:
                clip_file = any(CLIP_MARKER in line for line in handle)
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    if clip_file:
                        if CLIP_MARKER not in line:
                            continue
                    elif '"votes"' not in line or '"scene"' not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict):
                        yield row
        except OSError as exc:
            print(f"warning: cannot read {path}: {exc}", file=sys.stderr)


def parse_when(text: str) -> float:
    """Local ISO date or date-time (``2026-10-23`` or ``2026-10-23T18:00``)."""
    return datetime.fromisoformat(text.strip()).timestamp()


def keep_row(ts: float, since: float | None, until: float | None,
             exclude: list[tuple[float, float]]) -> bool:
    if since is not None and ts < since:
        return False
    if until is not None and ts > until:
        return False
    return not any(lo <= ts <= hi for lo, hi in exclude)


# ── replaying the confirmation rule ──────────────────────────────────────────
def kth_largest(values: list[float], k: int) -> float:
    if len(values) < k:
        return 0.0
    return sorted(values, reverse=True)[k - 1]


def compact(row: dict[str, Any], classes: tuple[str, ...],
            sources: tuple[str, ...] | None = ("live",)) -> tuple | None:
    """The few fields of a row the replay needs (a Pi's month of logs is
    hundreds of MB of JSON; whole rows would not fit beside SENTRIX). Rows
    of other ``source`` kinds (``eval``: replays of labelled threat videos)
    are dropped unless ``sources`` is None."""
    if not isinstance(row.get("ts"), (int, float)):
        return None
    if sources is not None and str(row.get("source", "live")) not in sources:
        return None
    session, ts = str(row.get("session")), float(row["ts"])
    if row.get("kind") == "clip":
        end = float(row.get("end", 0.0))
        frames = []
        for kf in row.get("keyframes") or []:
            if not isinstance(kf, dict):
                continue
            probs = kf.get("scene") if isinstance(kf.get("scene"), dict) else {}
            t = end + float(kf.get("t", 0.0))
            frames.append((kf.get("seq", t), t, bool(kf.get("persons")),
                           {c: float(probs[c]) for c in classes if c in probs}))
        thresholds = (row.get("scene") or {}).get("thresholds") or {}
        logged = {}
        for cls in classes:
            for vote in ((row.get("evidence") or {}).get(cls) or {}).get("votes") or []:
                if isinstance(vote, dict) and vote.get("source") == "scene":
                    logged[cls] = (float(vote.get("threshold", 0.0)),
                                   bool(vote.get("confirmed")))
        return ("clip", session, end, ts, frames,
                {c: float(thresholds[c]) for c in classes if c in thresholds}, logged)
    votes = (row.get("votes") or {}).get("scene") or {}
    picked = {}
    for cls in classes:
        entry = votes.get(cls)
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            picked[cls] = (float(entry[0]), float(entry[1]),
                           bool(entry[2]) if len(entry) > 2 else True)
    if not picked:
        return None
    return ("tick", session, ts, ts, (row.get("context") or {}).get("person_recent"), picked)


def collect(rows: Iterable[dict[str, Any]], classes: Iterable[str], rules: Rules,
            since: float | None = None, until: float | None = None,
            exclude: list[tuple[float, float]] | None = None,
            sources: tuple[str, ...] | None = ("live",)) -> dict[str, ClassLog]:
    """Per class: the moments, every scored value and the scored time.

    Rows outside the period still advance the replay's state (a person seen
    just before ``--since`` still opens the weapon gate), they are only not
    counted."""
    classes = tuple(classes)
    exclude = exclude or []
    out = {c: ClassLog() for c in classes}
    records = [r for r in (compact(row, classes, sources) for row in rows) if r is not None]
    records.sort(key=lambda r: (r[1], r[2]))
    last_person: dict[str, float] = {}
    windows: dict[tuple[str, str], deque] = {}
    last_tick: dict[tuple[str, str], float] = {}
    seen_keyframes: set[tuple[str, str, Any]] = set()
    for record in records:
        kind, session, clock, ts = record[:4]
        counted = keep_row(ts, since, until, exclude)
        if kind == "clip":
            frames, thresholds, logged = record[4], record[5], record[6]
            person_times = sorted(t for _, t, has_person, _ in frames if has_person)
            earlier = last_person.get(session)

            def person_recent(t: float, earlier=earlier, person_times=person_times) -> bool:
                if earlier is not None and 0.0 <= t - earlier <= rules.person_memory:
                    return True
                return any(0.0 <= t - p <= rules.person_memory for p in person_times)

            if person_times:
                last_person[session] = max(person_times[-1], earlier or person_times[-1])
            if not counted:
                continue
            for cls in classes:
                scored = {seq: (t, probs[cls]) for seq, t, _, probs in frames if cls in probs}
                if not scored:
                    continue
                log = out[cls]
                if cls in thresholds:
                    log.saw_bar(ts, thresholds[cls])
                gated = [p if (cls != "weapon" or person_recent(t)) else 0.0
                         for t, p in scored.values()]
                for seq, (_t, p) in scored.items():
                    if (cls, session, seq) not in seen_keyframes:
                        seen_keyframes.add((cls, session, seq))
                        log.values.append(p)
                log.seconds += rules.hop_seconds
                confirm = kth_largest(gated, rules.confirm_keyframes)
                log.moments.append(Moment(session, ts, clock, confirm,
                                          max(p for _, p in scored.values()), "clip"))
                if cls in logged and 0.0 < logged[cls][0] < 1.0:
                    bar, device = logged[cls]
                    if (confirm >= bar) == device:
                        log.agree += 1
                    elif len(log.disagree) < 1000:
                        log.disagree.append((ts, session, bar, device, round(confirm, 4)))
            continue
        person, picked = record[4], record[5]
        for cls, (value, threshold, fresh) in picked.items():
            if not fresh or not math.isfinite(value):
                continue
            key = (session, cls)
            window = windows.setdefault(key, deque(maxlen=max(1, rules.confirm_n)))
            previous = last_tick.get(key)
            if previous is not None and ts - previous > rules.confirm_max_gap:
                window.clear()
            last_tick[key] = ts
            gate_shut = cls == "weapon" and person is False
            window.append(0.0 if gate_shut else value)
            if not counted:
                continue
            log = out[cls]
            if 0.0 < threshold < 1.0:
                log.saw_bar(ts, threshold)
            if previous is not None:
                log.seconds += min(max(ts - previous, 0.0), rules.confirm_max_gap)
            log.values.append(value)
            confirm = 0.0 if gate_shut else kth_largest(list(window), rules.confirm_k)
            log.moments.append(Moment(session, ts, ts, confirm, value, "tick"))
    return out


def episodes(moments: list[Moment], bar: float) -> tuple[int, int]:
    """``(onsets, confirmed observations)`` at ``bar``. Confirmations less
    than ``EPISODE_GAP_SECONDS`` apart are one episode (the level policy holds
    a level for 5 s, so a one-clip dip does not open a second incident)."""
    onsets = confirmed = 0
    last: dict[str, float] = {}
    for m in sorted(moments, key=lambda m: (m.session, m.clock)):
        if m.confirm < bar:
            continue
        confirmed += 1
        previous = last.get(m.session)
        if previous is None or m.clock - previous > EPISODE_GAP_SECONDS:
            onsets += 1
        last[m.session] = m.clock
    return onsets, confirmed


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def ceil_step(value: float, step: float = 0.01) -> float:
    return round(math.ceil(round(value / step, 6)) * step, 4)


def propose(log: ClassLog, floor: float, confirm_margin: float = CONFIRM_MARGIN,
            single_margin: float = SINGLE_MARGIN, cap: float = CAP) -> dict[str, Any]:
    confirm_max = max((m.confirm for m in log.moments), default=0.0)
    single_max = max((m.single for m in log.moments), default=0.0)
    raw = max(floor, confirm_max + confirm_margin, single_max + single_margin)
    bar = min(ceil_step(raw), cap)
    return {"proposed": bar, "floor": floor, "confirm_max": round(confirm_max, 4),
            "single_max": round(single_max, 4),
            "set_by": ("floor" if raw == floor else
                       "confirming max + %.2f" % confirm_margin
                       if confirm_max + confirm_margin >= single_max + single_margin
                       else "single max + %.2f" % single_margin),
            "capped": raw > cap}


def summarise(cls: str, log: ClassLog, rules: Rules, args) -> dict[str, Any]:
    floor = float(rules.floors.get(cls, 0.5))
    hours = log.seconds / 3600.0
    report: dict[str, Any] = {
        "class": cls, "observations": len(log.moments), "scored_values": len(log.values),
        "scored_hours": round(hours, 3), "in_force": log.in_force,
        "modes": sorted({m.mode for m in log.moments}),
    }
    if not log.moments:
        report["note"] = "the scene model scored nothing for this class in the period"
        return report
    report.update(propose(log, floor, args.confirm_margin, args.single_margin, args.cap))
    report["percentiles"] = {name: round(percentile(log.values, q), 4)
                             for name, q in (("median", 0.5), ("p99", 0.99),
                                             ("p99.9", 0.999), ("max", 1.0))}
    bars = {"floor": floor, "proposed": report["proposed"]}
    if log.in_force is not None:
        bars["in_force"] = log.in_force
    report["at"] = {}
    for name, bar in bars.items():
        onsets, confirmed = episodes(log.moments, bar)
        report["at"][name] = {"bar": bar, "onsets": onsets, "confirmed": confirmed,
                              "per_hour": round(onsets / hours, 3) if hours else None}
    # Zero onsets observed at the proposed bar: an exact Poisson 95 % upper
    # bound on the rate is 3.0 / hours (the "rule of three").
    if hours:
        report["upper_bound_per_hour"] = round(3.0 / hours, 3)
    top = sorted(log.moments, key=lambda m: m.confirm, reverse=True)[:args.top]
    report["top"] = [{"time": datetime.fromtimestamp(m.ts).isoformat(timespec="seconds"),
                      "session": m.session, "confirm": round(m.confirm, 4),
                      "single": round(m.single, 4), "mode": m.mode} for m in top]
    warnings = []
    if hours < args.min_hours:
        warnings.append(f"only {hours:.2f} h scored (< {args.min_hours} h): too little "
                        "quiet time to set a bar; --write skips this class")
    if report["proposed"] > WARN_ABOVE:
        warnings.append("a bar over %.2f leaves the scene model nearly mute here: it "
                        "cannot tell this room's normal from a threat; record the room "
                        "and fine-tune (M6, ROADMAP section 15)" % WARN_ABOVE)
    if report.get("capped"):
        warnings.append(f"capped at {args.cap}")
    checked = log.agree + len(log.disagree)
    if checked:
        report["replay_check"] = {
            "clips": checked, "agree": log.agree,
            "disagree": [{"time": datetime.fromtimestamp(t).isoformat(timespec="seconds"),
                          "session": s, "bar": b, "device_confirmed": d, "replay": c}
                         for t, s, b, d, c in log.disagree[:args.top]]}
        if log.disagree:
            warnings.append(f"the replay disagrees with the device's own verdict on "
                            f"{len(log.disagree)} of {checked} clips")
    report["warnings"] = warnings
    return report


def check_session(cls: str, log: ClassLog, bars: dict[str, float]) -> dict[str, Any]:
    """Does a staged threat still confirm from the scene model alone?"""
    out: dict[str, Any] = {"class": cls, "observations": len(log.moments),
                           "confirm_max": round(max((m.confirm for m in log.moments),
                                                    default=0.0), 4)}
    for name, bar in bars.items():
        onsets, confirmed = episodes(log.moments, bar)
        out[name] = {"bar": bar, "onsets": onsets, "confirmed": confirmed}
    return out


# ── writing ──────────────────────────────────────────────────────────────────
def current_weights() -> dict[str, Any] | None:
    """The scene checkpoint this device loads (name and size), recorded in the
    calibration so the engine can flag the bars once the model changes."""
    try:
        from sentrix.perception.scene import _resolve
        path = _resolve()
        if path is None:
            return None
        return {"name": path.name, "bytes": path.stat().st_size}
    except Exception:
        return None


def merged_calibration(existing: dict[str, Any] | None, reports: list[dict[str, Any]],
                       allow_lower: bool, min_hours: float,
                       sources: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The new ``scene.json`` content and a line per class saying what happened."""
    existing = existing or {}
    old = existing.get("thresholds", existing) if isinstance(existing, dict) else {}
    thresholds = {k: float(v) for k, v in old.items()
                  if k in ("fire", "weapon", "assault", "robbery")
                  and isinstance(v, (int, float))}
    notes: list[str] = []
    why = []
    for rep in reports:
        cls = rep["class"]
        if "proposed" not in rep:
            notes.append(f"{cls}: nothing scored, left as it was")
            continue
        if rep["scored_hours"] < min_hours:
            notes.append(f"{cls}: {rep['scored_hours']:.2f} h scored < {min_hours} h, "
                         "left as it was")
            continue
        new, current = rep["proposed"], thresholds.get(cls)
        if current is not None and new < current and not allow_lower:
            notes.append(f"{cls}: proposal {new:.2f} is under the file's {current:.2f}; "
                         "kept (use --allow-lower to lower it)")
            continue
        thresholds[cls] = new
        notes.append(f"{cls}: {current if current is not None else 'floor'} -> {new:.2f}")
        why.append(f"{cls} {new:.2f}: {rep['scored_hours']:.1f} h of this room scored with "
                   f"no threat asserted; per scored value median "
                   f"{rep['percentiles']['median']}, p99 {rep['percentiles']['p99']}, max "
                   f"{rep['percentiles']['max']}; highest confirming value "
                   f"{rep['confirm_max']} ({rep['set_by']})")
    data = {"thresholds": thresholds,
            "why": ("Deployment calibration from this device's own event log "
                    "(tools/calibrate_room.py, rule: max(floor, confirming max + "
                    f"{CONFIRM_MARGIN:.2f}, single max + {SINGLE_MARGIN:.2f})). "
                    + " ".join(why) + " Delete this file and restart to return to "
                    "the floors.") if why else existing.get("why", ""),
            "calibrated": sources}
    return data, notes


def write_calibration(path: Path, data: dict[str, Any]) -> Path | None:
    """Write ``data`` to ``path`` atomically; returns the backup made, if any."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return backup


# ── command line ─────────────────────────────────────────────────────────────
def print_report(reports: list[dict[str, Any]], checks: list[dict[str, Any]]) -> None:
    for rep in reports:
        cls = rep["class"]
        print(f"\n== {cls} ==")
        print(f"  scored: {rep['observations']} {'/'.join(rep['modes']) or '-'} observations, "
              f"{rep['scored_values']} values, {rep['scored_hours']:.2f} h")
        if "proposed" not in rep:
            print(f"  {rep.get('note')}")
            continue
        pct = rep["percentiles"]
        print(f"  per value: median {pct['median']}, p99 {pct['p99']}, p99.9 {pct['p99.9']}, "
              f"max {pct['max']}")
        print(f"  highest confirming value {rep['confirm_max']}, highest single value "
              f"{rep['single_max']}")
        for name, at in rep["at"].items():
            print(f"  at {name:9s} {at['bar']:.2f}: {at['onsets']} onset(s), "
                  f"{at['confirmed']} confirmed observation(s)")
        print(f"  PROPOSED {rep['proposed']:.2f} (set by {rep['set_by']}); 0 onsets in "
              f"{rep['scored_hours']:.2f} h -> 95 % upper bound "
              f"{rep.get('upper_bound_per_hour')} per hour")
        chk = rep.get("replay_check")
        if chk:
            print(f"  replay check: same verdict as the device on {chk['agree']} of "
                  f"{chk['clips']} clips (at the bar it applied)")
        for item in rep["top"]:
            print(f"    {item['time']}  confirm {item['confirm']:.3f}  single "
                  f"{item['single']:.3f}  {item['mode']} {item['session']}")
        for warning in rep["warnings"]:
            print(f"  WARNING: {warning}")
    for chk in checks:
        print(f"\n== check {chk['class']} ({chk['observations']} observations, highest "
              f"confirming value {chk['confirm_max']}) ==")
        for name in ("in_force", "proposed", "floor"):
            if name in chk:
                at = chk[name]
                print(f"  at {name:9s} {at['bar']:.2f}: {at['onsets']} onset(s), "
                      f"{at['confirmed']} confirmed - "
                      + ("the scene model alone still raises Level 3" if at["onsets"]
                         else "the scene model alone no longer raises Level 3"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--events", nargs="+", default=None,
                        help="event logs: files, directories or globs (default var/events)")
    parser.add_argument("--classes", nargs="+", default=list(DEFAULT_CLASSES))
    parser.add_argument("--since", help="local ISO date/time; earlier rows are ignored")
    parser.add_argument("--until", help="local ISO date/time; later rows are ignored")
    parser.add_argument("--exclude", action="append", default=[],
                        help="START,END (local ISO): a staged test or any non-quiet period")
    parser.add_argument("--check", nargs="+", default=None,
                        help="logs of a session with a staged threat to replay at the bars")
    parser.add_argument("--source", nargs="+", default=["live"],
                        help="row sources to read (default live; 'any' for all, e.g. "
                             "an eval replay of a quiet video)")
    parser.add_argument("--confirm-margin", type=float, default=CONFIRM_MARGIN)
    parser.add_argument("--single-margin", type=float, default=SINGLE_MARGIN)
    parser.add_argument("--cap", type=float, default=CAP)
    parser.add_argument("--min-hours", type=float, default=2.0)
    parser.add_argument("--top", type=int, default=5,
                        help="list this many highest moments (check none was a staged test)")
    parser.add_argument("--json", type=Path, help="write the full report here")
    parser.add_argument("--write", action="store_true",
                        help="merge the proposal into the calibration file (backup first)")
    parser.add_argument("--allow-lower", action="store_true",
                        help="with --write: allow a bar lower than the file's")
    parser.add_argument("--calibration", type=Path, default=None,
                        help="calibration file (default var/calibration/scene.json)")
    args = parser.parse_args(argv)

    from sentrix.paths import CALIBRATION_DIR, EVENTLOG_DIR
    rules = Rules.from_settings()
    files = log_files(args.events or [EVENTLOG_DIR])
    if not files:
        print("no event logs found", file=sys.stderr)
        return 2
    since = parse_when(args.since) if args.since else None
    until = parse_when(args.until) if args.until else None
    exclude = []
    for item in args.exclude:
        lo, _, hi = item.partition(",")
        exclude.append((parse_when(lo), parse_when(hi)))
    sources = None if "any" in args.source else tuple(args.source)
    logs = collect(read_rows(files), args.classes, rules, since, until, exclude, sources)
    reports = [summarise(cls, logs[cls], rules, args) for cls in args.classes]
    if not any(rep["observations"] for rep in reports):
        print(f"the scene model scored nothing in {len(files)} log(s): nothing to calibrate",
              file=sys.stderr)
        return 2

    checks: list[dict[str, Any]] = []
    if args.check:
        staged = collect(read_rows(log_files(args.check)), args.classes, rules,
                         sources=sources)
        for rep in reports:
            if "proposed" not in rep:
                continue
            bars = {"floor": rep["floor"], "proposed": rep["proposed"]}
            if rep.get("in_force") is not None:
                bars["in_force"] = rep["in_force"]
            checks.append(check_session(rep["class"], staged[rep["class"]], bars))

    print(f"{len(files)} event log(s), {files[0].name} .. {files[-1].name}")
    print_report(reports, checks)
    provenance = {"tool": "tools/calibrate_room.py",
               "at": datetime.now().isoformat(timespec="seconds"),
               "logs": len(files), "first": files[0].name, "last": files[-1].name,
               "since": args.since, "until": args.until, "exclude": args.exclude,
               "source": args.source, "weights": current_weights(),
               "rule": {"confirm_margin": args.confirm_margin,
                        "single_margin": args.single_margin, "cap": args.cap}}
    full = {"sources": provenance, "classes": reports, "checks": checks}
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(full, indent=2) + "\n", encoding="utf-8")
        print(f"\nreport: {args.json}")
    if args.write:
        path = args.calibration or (CALIBRATION_DIR / "scene.json")
        existing = None
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"cannot read {path} ({exc}); not writing", file=sys.stderr)
                return 1
        data, notes = merged_calibration(existing, reports, args.allow_lower,
                                         args.min_hours, provenance)
        print()
        for note in notes:
            print(f"  {note}")
        if data["thresholds"] == ((existing or {}).get("thresholds", existing or {})):
            print("nothing changed; file not written")
            return 0
        backup = write_calibration(path, data)
        print(f"written {path}" + (f" (old file kept as {backup.name})" if backup else ""))
        print("restart the device to apply it (it reads the file at start-up)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
