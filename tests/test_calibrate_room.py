"""tools/calibrate_room.py: per-room scene thresholds from the device's own log.

The tool replays the device's confirmation rule on logged scene scores; these
tests pin that replay (clip and tick mode, the weapon person gate, cached
keyframes), the proposal rule and the file it writes (never lowered by
default, backed up, other classes kept).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import calibrate_room as cr  # noqa: E402

RULES = cr.Rules()           # the documented defaults, independent of the env


def clip(session: str, end: float, frames: list, *, ts: float | None = None,
         bars: dict | None = None, logged: dict | None = None,
         source: str = "live") -> dict:
    """A clip row: ``frames`` = [(seq, t, has_person, {cls: p})]."""
    keyframes = []
    for seq, t, person, probs in frames:
        kf = {"seq": seq, "t": t, "persons": [{"conf": 0.9}] if person else []}
        if probs:
            kf["scene"] = probs
        keyframes.append(kf)
    row = {"schema": 4, "kind": "clip", "session": session, "source": source,
           "ts": 1_790_000_000.0 + end if ts is None else ts, "end": end,
           "keyframes": keyframes, "evidence": {}}
    if bars:
        row["scene"] = {"thresholds": bars}
    for cls, (bar, confirmed) in (logged or {}).items():
        row["evidence"][cls] = {"votes": [{"source": "scene", "threshold": bar,
                                           "confirmed": confirmed}]}
    return row


def tick(session: str, ts: float, values: dict, *, person=True, fresh=True,
         source: str = "live") -> dict:
    return {"schema": 5, "session": session, "source": source, "ts": ts,
            "context": {"person_recent": person},
            "votes": {"scene": {c: [v, 0.5, fresh] for c, v in values.items()}}}


def args(**kw):
    base = dict(confirm_margin=cr.CONFIRM_MARGIN, single_margin=cr.SINGLE_MARGIN,
                cap=cr.CAP, min_hours=2.0, top=5)
    base.update(kw)
    return SimpleNamespace(**base)


# ── the replay ────────────────────────────────────────────────────────────────
def test_clip_confirming_value_is_the_second_highest_keyframe():
    rows = [clip("s", 10.0, [(1, -3.0, True, {"fire": 0.30}), (2, -2.0, True, {"fire": 0.62}),
                             (3, -1.0, True, {"fire": 0.55})])]
    log = cr.collect(rows, ["fire"], RULES)["fire"]
    [moment] = log.moments
    assert moment.confirm == pytest.approx(0.55)   # two keyframes >= 0.55
    assert moment.single == pytest.approx(0.62)
    assert sorted(log.values) == pytest.approx([0.30, 0.55, 0.62])
    assert cr.episodes(log.moments, 0.55) == (1, 1)
    assert cr.episodes(log.moments, 0.56) == (0, 0)


def test_weapon_keyframes_need_a_person_seen_within_two_seconds():
    # No person anywhere: nothing can confirm, whatever the score.
    alone = [clip("a", 10.0, [(1, -2.0, False, {"weapon": 0.9}),
                              (2, -1.0, False, {"weapon": 0.9})])]
    assert cr.collect(alone, ["weapon"], RULES)["weapon"].moments[0].confirm == 0.0
    # A person in the previous clip 1.5 s before still opens the gate.
    carried = [clip("b", 8.0, [(1, -0.5, True, {})]),             # person at t = 7.5
               clip("b", 10.0, [(2, -1.0, False, {"weapon": 0.8}),   # t = 9.0
                                (3, -0.6, False, {"weapon": 0.7})])]  # t = 9.4
    moments = cr.collect(carried, ["weapon"], RULES)["weapon"].moments
    assert moments[-1].confirm == pytest.approx(0.7)
    # ... but not 3 s before.
    late = [clip("c", 6.0, [(1, -0.5, True, {})]),
            clip("c", 10.0, [(2, -1.0, False, {"weapon": 0.8}),
                             (3, -0.6, False, {"weapon": 0.7})])]
    assert cr.collect(late, ["weapon"], RULES)["weapon"].moments[-1].confirm == 0.0
    # Fire has no person gate.
    fire = [clip("d", 10.0, [(1, -2.0, False, {"fire": 0.8}), (2, -1.0, False, {"fire": 0.7})])]
    assert cr.collect(fire, ["fire"], RULES)["fire"].moments[0].confirm == pytest.approx(0.7)


def test_a_cached_keyframe_is_one_value_but_counts_in_both_clips():
    shared = (5, -1.0, True, {"fire": 0.6})
    rows = [clip("s", 10.0, [(4, -2.0, True, {"fire": 0.5}), shared]),
            clip("s", 12.0, [(5, -3.0, True, {"fire": 0.6}), (6, -1.0, True, {"fire": 0.7})])]
    log = cr.collect(rows, ["fire"], RULES)["fire"]
    assert sorted(log.values) == pytest.approx([0.5, 0.6, 0.7])
    assert [m.confirm for m in log.moments] == pytest.approx([0.5, 0.6])
    assert log.seconds == pytest.approx(2 * RULES.hop_seconds)


def test_tick_mode_two_of_three_fresh_with_gap_reset_and_gate():
    rows = [tick("t", 100.0, {"weapon": 0.8}),
            tick("t", 100.5, {"weapon": 0.9}, fresh=False),   # cached: ignored
            tick("t", 101.0, {"weapon": 0.6}),
            tick("t", 105.0, {"weapon": 0.95}),                # gap > 3 s: reset
            tick("t", 105.5, {"weapon": 0.97}, person=False)]  # gate shut
    moments = cr.collect(rows, ["weapon"], RULES)["weapon"].moments
    assert [round(m.confirm, 3) for m in moments] == [0.0, 0.6, 0.0, 0.0]
    assert [round(m.single, 3) for m in moments] == [0.8, 0.6, 0.95, 0.97]


def test_episodes_merge_confirmations_closer_than_the_gap():
    moments = [cr.Moment("s", 0, t, c, c, "clip") for t, c in
               [(0, 0.8), (2, 0.1), (4, 0.8), (30, 0.8)]]
    assert cr.episodes(moments, 0.7) == (2, 3)


def test_eval_rows_are_ignored_unless_asked_for():
    rows = [clip("e", 10.0, [(1, -2.0, True, {"fire": 0.9}), (2, -1.0, True, {"fire": 0.9})],
                 source="eval")]
    assert not cr.collect(rows, ["fire"], RULES)["fire"].moments
    assert cr.collect(rows, ["fire"], RULES, sources=None)["fire"].moments


def test_period_filters_count_nothing_outside_but_keep_the_person_state():
    rows = [clip("s", 8.0, [(1, -0.5, True, {})], ts=1000.0),
            clip("s", 10.0, [(2, -1.0, False, {"weapon": 0.8}),
                             (3, -0.6, False, {"weapon": 0.7})], ts=1002.0),
            clip("s", 12.0, [(4, -1.0, True, {"weapon": 0.9}),
                             (5, -0.6, True, {"weapon": 0.9})], ts=1004.0)]
    log = cr.collect(rows, ["weapon"], RULES, since=1001.0, exclude=[(1003.0, 1005.0)])
    assert len(log["weapon"].moments) == 1                 # the 1004 clip is excluded
    assert log["weapon"].moments[0].confirm == pytest.approx(0.7)   # gate from ts 1000


def test_replay_check_compares_with_the_devices_own_verdict():
    frames = [(1, -2.0, True, {"fire": 0.72}), (2, -1.0, True, {"fire": 0.71})]
    rows = [clip("s", 10.0, frames, logged={"fire": (0.70, True)}),
            clip("s", 12.0, [(3, -2.0, True, {"fire": 0.72}), (4, -1.0, True, {"fire": 0.5})],
                 logged={"fire": (0.70, True)})]           # device said yes, replay no
    log = cr.collect(rows, ["fire"], RULES)["fire"]
    assert log.agree == 1 and len(log.disagree) == 1
    report = cr.summarise("fire", log, RULES, args(min_hours=0.0))
    assert report["replay_check"]["clips"] == 2
    assert any("disagrees" in w for w in report["warnings"])


def test_in_force_bar_is_the_newest_logged_one():
    rows = [clip("zz", 10.0, [(1, -1.0, True, {"fire": 0.1})], ts=100.0, bars={"fire": 0.45}),
            clip("aa", 10.0, [(1, -1.0, True, {"fire": 0.1})], ts=200.0, bars={"fire": 0.70})]
    assert cr.collect(rows, ["fire"], RULES)["fire"].in_force == pytest.approx(0.70)


# ── the proposal ──────────────────────────────────────────────────────────────
def test_proposal_rule_floor_confirming_and_single_terms():
    def log_of(confirm, single):
        return cr.ClassLog(moments=[cr.Moment("s", 0, 0, confirm, single, "clip")])
    # The demo room's hand calibrations, from the values they were set on.
    assert cr.propose(log_of(0.5903, 0.7416), 0.50)["proposed"] == pytest.approx(0.76)
    assert cr.propose(log_of(0.5865, 0.6459), 0.45)["proposed"] == pytest.approx(0.69)
    quiet = cr.propose(log_of(0.20, 0.30), 0.50)
    assert quiet["proposed"] == pytest.approx(0.50) and quiet["set_by"] == "floor"
    capped = cr.propose(log_of(0.97, 0.99), 0.50)
    assert capped["proposed"] == pytest.approx(cr.CAP) and capped["capped"]


def test_summary_warns_on_little_data_and_a_mute_model():
    log = cr.ClassLog(moments=[cr.Moment("s", 0, 0, 0.85, 0.86, "clip")], values=[0.86],
                      seconds=600.0)
    report = cr.summarise("weapon", log, RULES, args())
    assert report["proposed"] == pytest.approx(0.95)
    assert report["at"]["proposed"]["onsets"] == 0
    assert report["at"]["floor"]["onsets"] == 1
    assert report["upper_bound_per_hour"] == pytest.approx(18.0)
    assert any("too little quiet time" in w for w in report["warnings"])
    assert any("nearly mute" in w for w in report["warnings"])


# ── the file ──────────────────────────────────────────────────────────────────
def _report(cls, proposed, hours=5.0):
    return {"class": cls, "proposed": proposed, "scored_hours": hours, "confirm_max": 0.6,
            "set_by": "confirming max + 0.10",
            "percentiles": {"median": 0.1, "p99": 0.5, "p99.9": 0.6, "max": 0.7}}


def test_merge_never_lowers_skips_thin_data_and_keeps_other_classes():
    existing = {"thresholds": {"weapon": 0.75, "fire": 0.70, "robbery": 0.65}, "why": "hand"}
    data, notes = cr.merged_calibration(
        existing, [_report("weapon", 0.78), _report("fire", 0.60),
                   _report("assault", 0.80, hours=0.5)],
        allow_lower=False, min_hours=2.0, sources={"tool": "t"})
    assert data["thresholds"] == {"weapon": 0.78, "fire": 0.70, "robbery": 0.65}
    assert any("kept" in n for n in notes) and any("left as it was" in n for n in notes)
    lowered, _ = cr.merged_calibration(existing, [_report("fire", 0.60)], allow_lower=True,
                                       min_hours=2.0, sources={})
    assert lowered["thresholds"]["fire"] == pytest.approx(0.60)
    # A flat legacy file ({"weapon": 0.75}) is read the same way.
    flat, _ = cr.merged_calibration({"weapon": 0.75}, [_report("fire", 0.72)],
                                    allow_lower=False, min_hours=2.0, sources={})
    assert flat["thresholds"] == {"weapon": 0.75, "fire": 0.72}


def test_written_file_is_read_by_the_scene_engine(tmp_path, monkeypatch):
    from sentrix.perception import scene
    path = tmp_path / "calibration" / "scene.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"thresholds": {"weapon": 0.75}}), encoding="utf-8")
    data, _ = cr.merged_calibration(json.loads(path.read_text()), [_report("fire", 0.72)],
                                    allow_lower=False, min_hours=2.0, sources={"tool": "t"})
    backup = cr.write_calibration(path, data)
    assert backup is not None and json.loads(backup.read_text()) == {"thresholds": {"weapon": 0.75}}
    monkeypatch.setattr(scene, "CALIBRATION_DIR", path.parent)
    monkeypatch.delenv("SENTRIX_SCENE_THRESHOLDS", raising=False)
    assert scene.deployment_thresholds() == {"weapon": 0.75, "fire": 0.72}


# ── reading and the command line ──────────────────────────────────────────────
def _write_log(path: Path, rows: list[dict], extra_lines: list[str] = ()) -> Path:
    lines = [json.dumps(r) for r in rows] + list(extra_lines)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_read_rows_skips_decision_ticks_in_clip_files_and_bad_lines(tmp_path):
    clip_row = clip("s", 10.0, [(1, -1.0, True, {"fire": 0.2})])
    decision = tick("s", 1.0, {"fire": 0.99})
    _write_log(tmp_path / "events_1_s.jsonl", [clip_row, decision], ["{not json"])
    _write_log(tmp_path / "events_2_t.jsonl", [tick("t", 1.0, {"fire": 0.3}),
                                               {"event": "gallery_enrolled", "ts": 2.0}])
    rows = list(cr.read_rows(cr.log_files([tmp_path])))
    assert [r.get("kind", "tick") for r in rows] == ["clip", "tick"]


def _quiet_room(directory: Path, clips: int = 4000) -> None:
    rows = []
    for i in range(clips):                       # 4000 clips x 2 s = 2.2 h
        end = 10.0 + 2.0 * i
        hi = 0.60 if i == 1234 else 0.20
        rows.append(clip("q", end, [(2 * i, -2.0, True, {"weapon": hi, "fire": 0.10}),
                                    (2 * i + 1, -1.0, True, {"weapon": hi, "fire": 0.12})],
                         bars={"weapon": 0.50, "fire": 0.45},
                         logged={"weapon": (0.50, hi >= 0.50), "fire": (0.45, False)}))
    _write_log(directory / "events_20261023_000000_q.jsonl", rows)


def test_main_reports_writes_with_a_backup_and_checks_a_staged_session(tmp_path, capsys):
    logs = tmp_path / "events"
    logs.mkdir()
    _quiet_room(logs)
    staged = tmp_path / "staged"
    staged.mkdir()
    _write_log(staged / "events_20261124_000000_g.jsonl",
               [clip("g", 10.0 + 2 * i, [(i, -2.0, True, {"weapon": 0.82}),
                                         (i + 1000, -1.0, True, {"weapon": 0.81})])
                for i in range(3)])
    cal = tmp_path / "calibration" / "scene.json"
    cal.parent.mkdir()
    cal.write_text(json.dumps({"thresholds": {"weapon": 0.55}}), encoding="utf-8")
    report_path = tmp_path / "report.json"
    code = cr.main(["--events", str(logs), "--check", str(staged), "--write",
                    "--calibration", str(cal), "--json", str(report_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "PROPOSED 0.70" in out                      # 0.60 + 0.10
    assert "same verdict as the device on 4000 of 4000" in out
    assert "still raises Level 3" in out               # 0.81 >= 0.70
    written = json.loads(cal.read_text(encoding="utf-8"))
    assert written["thresholds"]["weapon"] == pytest.approx(0.70)
    assert written["thresholds"]["fire"] == pytest.approx(0.45)   # floor, 0.12 + 0.10 < 0.45
    assert written["calibrated"]["tool"] == "tools/calibrate_room.py"
    assert list(cal.parent.glob("scene.json.bak-*"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["checks"][0]["proposed"]["onsets"] == 1


def test_main_without_logs_or_scores_fails_cleanly(tmp_path, capsys):
    assert cr.main(["--events", str(tmp_path / "nothing")]) == 2
    _write_log(tmp_path / "events_1_x.jsonl", [{"event": "boot", "ts": 1.0}])
    assert cr.main(["--events", str(tmp_path)]) == 2


def test_a_calibration_for_other_weights_is_applied_but_flagged(tmp_path, monkeypatch):
    from sentrix.perception import scene
    weights = tmp_path / "scene_v5.pt"
    weights.write_bytes(b"x" * 10)
    cal = tmp_path / "scene.json"
    monkeypatch.setattr(scene, "CALIBRATION_DIR", tmp_path)
    monkeypatch.delenv("SENTRIX_SCENE_THRESHOLDS", raising=False)

    def engine():
        e = scene.SceneEngine.__new__(scene.SceneEngine)
        e.thresholds = {"fire": 0.35, "weapon": 0.35, "assault": 0.6, "robbery": 0.6}
        e.calibration_stale = False
        e._apply_threshold_policy(weights)
        return e

    cal.write_text(json.dumps({"thresholds": {"weapon": 0.75},
                               "calibrated": {"weights": {"name": "scene_v4.pt",
                                                          "bytes": 99}}}), encoding="utf-8")
    stale = engine()
    assert stale.thresholds["weapon"] == pytest.approx(0.75)      # still applied
    assert stale.calibration_stale and "other weights (scene_v4.pt)" in stale.threshold_source
    cal.write_text(json.dumps({"thresholds": {"weapon": 0.75},
                               "calibrated": {"weights": scene.weights_identity(weights)}}),
                   encoding="utf-8")
    assert not engine().calibration_stale
    cal.write_text(json.dumps({"thresholds": {"weapon": 0.75}}), encoding="utf-8")
    assert not engine().calibration_stale        # a hand-made file names no weights
