"""Regression tests for the custom-dataset evaluation harness.

These pin the measurement itself: if the ground truth, the class mapping or a
metric definition drifts, every number quoted from ``training/runs/custom_eval``
silently changes meaning.
"""

from __future__ import annotations

import pytest

from sentrix.evaluation.custom_dataset import (CLASSES, THREAT_CLASSES, Timeline,
                                               incident_to_class, load_frames,
                                               load_videos, videos_for)
from sentrix.evaluation.metrics import classification_report, event_report


class TestGroundTruth:
    def test_class_order_matches_dataset_indices(self):
        """normal = 0 is the negative class; the folder prefixes depend on it."""
        assert CLASSES == ("normal", "fire", "weapon", "assault", "robbery")
        assert "normal" not in THREAT_CLASSES

    def test_vendored_labels_are_complete(self):
        frames = load_frames()
        assert len(frames) == 765
        by_split = {s: sum(f.split == s for f in frames) for s in ("train", "val", "test")}
        assert by_split == {"train": 588, "val": 84, "test": 93}

    def test_split_is_by_source_video(self):
        """No video may contribute frames to two splits (leakage)."""
        splits_per_video: dict[str, set] = {}
        for f in load_frames():
            splits_per_video.setdefault(f.video, set()).add(f.split)
        assert all(len(s) == 1 for s in splits_per_video.values())

    def test_every_video_is_mapped_to_a_file(self):
        videos = load_videos()
        assert set(videos) == {f.video for f in load_frames()}
        assert [v.video_id for v in videos_for("test")] == ["wa02"]
        assert {v.video_id for v in videos_for("heldout")} == {"gun", "vid04", "wa02"}

    def test_unknown_video_id_is_rejected(self):
        with pytest.raises(KeyError):
            videos_for("vid99")


class TestIncidentMapping:
    def test_threat_incidents_map_to_their_class(self):
        for cls in THREAT_CLASSES:
            assert incident_to_class(cls) == cls

    def test_intrusion_is_not_a_dataset_threat(self):
        """Nobody in the footage is enrolled, so intrusion fires on every
        person; it must not be scored as a recognised threat."""
        assert incident_to_class("intrusion") == "normal"
        assert incident_to_class(None) == "normal"


class TestTimeline:
    timeline = Timeline()

    def test_reviewed_label_overrides_coarse_segment(self):
        """wa02 41.3-47.6 s sits inside a 'normal (0.45)' segment but the frame
        review found a stick being swung. The timeline must follow the review."""
        assert self.timeline.label_at("wa02", 44.161) == "weapon"

    def test_dropped_duplicate_inherits_an_adjacent_twin(self):
        # 45.161 s was dropped as a near-duplicate and carries the segment
        # label 'normal (0.45)'; its twin 0.68 s earlier is a reviewed weapon.
        assert self.timeline.label_at("wa02", 45.161, tolerance=0.01) == "weapon"

    def test_distant_twin_is_not_inherited(self):
        """pHash barely registers a flame jet: vid04 10.917 s is a 'duplicate'
        of a pre-ignition frame 5 s earlier. It keeps its confident segment
        label (fire, 0.92) instead of inheriting 'normal'."""
        assert self.timeline.label_at("vid04", 10.917, tolerance=0.01) == "fire"

    def test_ambiguous_dropped_frame_is_unscored(self):
        # 14.118 s: twin 1.5 s away, own segment 'normal (0.5)' -> no label.
        assert self.timeline.label_at("vid04", 14.118, tolerance=0.01) is None

    def test_far_from_any_sample_is_unscored(self):
        assert self.timeline.label_at("wa02", 500.0) is None
        assert self.timeline.label_at("no_such_video", 1.0) is None

    def test_episodes_are_contiguous_runs(self):
        eps = self.timeline.episodes("wa03")
        assert ("robbery" in [lab for lab, _, _ in eps])
        for (_, s, e) in eps:
            assert e >= s


class TestMetrics:
    def test_macro_f1_averages_only_present_classes(self):
        """A perfect model on a normal/weapon split must score 1.0, not 0.4."""
        y = ["normal", "weapon", "weapon", "normal"]
        report = classification_report(y, y)
        assert report["macro_f1"] == 1.0
        assert report["classes_scored"] == ["normal", "weapon"]

    def test_absent_class_prediction_still_costs_recall(self):
        report = classification_report(["normal", "normal"], ["assault", "normal"])
        assert report["per_class"]["normal"]["recall"] == 0.5
        assert report["threat_detection"]["fp"] == 1
        assert report["threat_detection"]["false_positive_rate"] == 0.5

    def test_mistyped_threat_is_detected_but_wrong(self):
        report = classification_report(["weapon"], ["assault"])
        assert report["threat_detection"]["recall"] == 1.0
        assert report["accuracy"] == 0.0

    def test_confusion_rows_are_ground_truth(self):
        report = classification_report(["fire"], ["normal"])
        cm = report["confusion"]["matrix"]
        assert cm[CLASSES.index("fire")][CLASSES.index("normal")] == 1


class TestEventMetrics:
    @staticmethod
    def rows(spec):
        return [{"t": t, "gt": gt, "pred": pred} for t, gt, pred in spec]

    def test_detection_within_grace_counts(self):
        rows = self.rows([(0.0, "normal", "normal"), (1.0, "weapon", "normal"),
                          (2.0, "weapon", "normal"), (2.5, "normal", "weapon")])
        rep = event_report({"v": rows}, {"v": [("weapon", 1.0, 2.0)]}, grace=1.0)
        assert rep["detected"] == 1
        assert rep["episode_detail"][0]["latency"] == 1.5

    def test_one_stuck_alarm_cannot_hide_false_positives(self):
        """A threat prediction that never switches off overlaps the real
        episode, but its ticks on normal footage are still false alarms."""
        spec = [(float(t), "normal", "assault") for t in range(0, 10)]
        spec += [(10.0, "weapon", "assault"), (11.0, "weapon", "assault")]
        spec += [(float(t), "normal", "assault") for t in range(12, 20)]
        rep = event_report({"v": self.rows(spec)}, {"v": [("weapon", 10.0, 11.0)]},
                           grace=1.0)
        assert rep["false_alarms"] == 2          # before and after the episode
        assert rep["episode_detail"][0]["detected"] is False
        assert rep["episode_detail"][0]["detected_any_threat"] is True
