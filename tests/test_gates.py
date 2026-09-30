"""Evaluation that matches the product (CLAUDE.md M5): committed-level metrics,
their bounds, and the demo gates built on them."""

from __future__ import annotations

import json

import pytest

from sentrix.evaluation.metrics import clopper_pearson, level_report, poisson_rate_interval
from tools import gate_report as gates


def rows(levels, preds=None, gts=None, hz=2.0):
    preds = preds or ["normal"] * len(levels)
    gts = gts or ["normal"] * len(levels)
    return [{"t": i / hz, "level": lv, "pred": p, "gt": g}
            for i, (lv, p, g) in enumerate(zip(levels, preds, gts))]


class TestBounds:
    def test_clopper_pearson(self):
        assert clopper_pearson(27, 30) == (0.7347, 0.9789)
        assert clopper_pearson(0, 10)[0] == 0.0 and clopper_pearson(10, 10)[1] == 1.0

    def test_zero_events_still_bounds_the_rate(self):
        lo, hi = poisson_rate_interval(0, 1 / 3)            # 20 min, no alarm
        assert lo == 0.0 and hi == pytest.approx(11.07, abs=0.01)


class TestLevelReport:
    def test_false_onsets_are_counted_on_normal_ticks_only(self):
        video = rows([1, 3, 3, 1, 1, 1, 1, 1, 3, 1],
                     preds=["normal", "weapon", "weapon"] + ["normal"] * 5 + ["weapon", "normal"],
                     gts=["normal", "weapon", "weapon"] + ["normal"] * 7)
        report = level_report({"v": video}, {"v": [("weapon", 0.5, 1.0)]}, grace=0.5)
        l3 = report["L3"]
        assert l3["onsets"] == 2 and l3["false_onsets"] == 1
        assert l3["episodes"]["weapon"] == {**l3["episodes"]["weapon"], "episodes": 1,
                                            "caught": 1, "within_2s": 1}

    def test_a_mistyped_alarm_does_not_catch_the_episode(self):
        video = rows([1, 3, 3], preds=["normal", "fire", "fire"],
                     gts=["normal", "weapon", "weapon"])
        report = level_report({"v": video}, {"v": [("weapon", 0.5, 1.0)]})
        assert report["L3"]["episodes"]["weapon"]["caught"] == 0

    def test_negative_session(self):
        video = rows([1] * 7200, hz=2.0)                    # one hour, quiet
        report = level_report({"v": video}, {})
        assert report["normal_hours"] == pytest.approx(1.0, abs=0.01)
        assert report["L3"]["false_onsets"] == 0
        assert report["L3"]["false_onsets_per_hour_ci95"][1] == pytest.approx(3.69, abs=0.02)


def committed(caught_within, episodes, p95=1.0):
    return {"L3": {"episodes": {"weapon": {"episodes": episodes, "caught": caught_within,
                                           "within_2s": caught_within,
                                           "recall_ci95": (0.0, 1.0),
                                           "p95_latency": p95}}}}


class TestGates:
    def test_g2_needs_thirty_episodes(self):
        assert gates.g2(committed(28, 30), ["weapon"])["status"] == gates.PASS
        assert gates.g2(committed(26, 30), ["weapon"])["status"] == gates.FAIL
        assert gates.g2(committed(12, 12), ["weapon"])["status"] == gates.INSUFFICIENT
        # 5 misses already: even 18 more catches cannot reach 27/30.
        assert gates.g2(committed(7, 12), ["weapon"])["status"] == gates.FAIL

    def test_g3(self):
        def neg(hours, l3, l4):
            return {"committed": {"normal_hours": hours,
                                  "L3": {"false_onsets": l3,
                                         "false_onsets_per_hour": l3 / hours,
                                         "false_onsets_per_hour_ci95": (0.0, 9.9)},
                                  "L4": {"false_onsets": l4}}}
        assert gates.g3(neg(3.5, 2, 0))["status"] == gates.PASS
        assert gates.g3(neg(3.5, 5, 0))["status"] == gates.FAIL
        assert gates.g3(neg(1.0, 0, 0))["status"] == gates.INSUFFICIENT
        assert gates.g3(neg(1.0, 0, 1))["status"] == gates.FAIL

    def test_g4(self):
        bench = {"ai_hz": 5.5, "frame_age_ms": {"p95": 270.0}}
        assert gates.g4(bench, committed(28, 30, p95=1.5), ["weapon"])["status"] == gates.PASS
        assert gates.g4(bench, committed(28, 30, p95=1.9), ["weapon"])["status"] == gates.FAIL
        assert gates.g4({**bench, "ai_hz": 3.0}, committed(28, 30), ["weapon"])["status"] == gates.FAIL

    def test_g0_records_then_verifies(self, tmp_path):
        data = tmp_path / "gate"
        data.mkdir()
        (data / "a.mp4").write_bytes(b"clip")
        manifest = tmp_path / "manifest.json"
        assert gates.g0(data, manifest)["status"] == gates.RECORDED
        assert gates.g0(data, manifest)["status"] == gates.PASS
        (data / "a.mp4").write_bytes(b"edited")
        assert gates.g0(data, manifest)["status"] == gates.FAIL

    def test_g5_scripted_dry_run(self, tmp_path):
        log = tmp_path / "events.jsonl"
        ticks = [{"ts": 1000.0 + i, "level": 3 if 600 <= i <= 610 else 1, "features": {}}
                 for i in range(1900)]
        log.write_text("\n".join(json.dumps(t) for t in ticks), encoding="utf-8")
        script = tmp_path / "script.csv"
        script.write_text("start_s,end_s,label\n598,612,knife\n", encoding="utf-8")
        assert gates.g5(log, script)["status"] == gates.PASS
        script.write_text("start_s,end_s,label\n598,612,knife\n1200,1210,fire\n",
                          encoding="utf-8")
        result = gates.g5(log, script)
        assert result["status"] == gates.FAIL and result["missed"] == ["fire"]
