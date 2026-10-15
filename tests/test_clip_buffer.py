"""The clip engine's ring buffer, keyframe selection and scheduler (docs/8_RASPBERRY_PI5_INTEGRATION.md T1).

Synthetic frames and audio only: nothing here loads a model or opens a device.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from sentrix.config import settings
from sentrix.pipeline.clip import (BufferedFrame, ClipBuffer, ClipScheduler, flame_fraction,
                                   motion_energy, option, select_keyframes)


def frame(value: int = 0, size=(48, 64), square_at: int | None = None) -> np.ndarray:
    image = np.full((*size, 3), value, np.uint8)
    if square_at is not None:
        image[10:30, square_at:square_at + 16] = 255
    return image


def fill(buffer: ClipBuffer, seconds: float, fps: float, moving: bool = False,
         start: float = 0.0) -> None:
    n = int(seconds * fps)
    for i in range(n):
        buffer.add_frame(frame(square_at=(i * 3) % 40 if moving else None), start + i / fps)


class TestRate:
    @pytest.mark.parametrize("source_fps", [25.0, 30.0, 60.0])
    def test_keeps_at_most_the_buffer_rate(self, source_fps):
        buffer = ClipBuffer(max_fps=15.0, keep_seconds=100.0)
        fill(buffer, 10.0, source_fps)
        rate = buffer.kept / 10.0
        assert 13.5 <= rate <= 15.5, f"{rate:.1f} fps kept from {source_fps} fps"

    def test_slower_source_keeps_every_frame(self):
        buffer = ClipBuffer(max_fps=15.0, keep_seconds=100.0)
        fill(buffer, 4.0, 10.0)
        assert buffer.kept == buffer.received == 40

    def test_restarts_pacing_after_a_stall(self):
        buffer = ClipBuffer(max_fps=15.0)
        buffer.add_frame(frame(), 0.0)
        assert buffer.add_frame(frame(), 5.0) is not None
        assert buffer.add_frame(frame(), 5.01) is None
        assert buffer.add_frame(frame(), 5.07) is not None

    def test_out_of_order_frames_are_dropped(self):
        buffer = ClipBuffer(max_fps=0.0)
        assert buffer.add_frame(frame(), 1.0) is not None
        assert buffer.add_frame(frame(), 0.5) is None
        assert buffer.add_frame(frame(), 1.0) is None

    def test_empty_frames_are_ignored(self):
        buffer = ClipBuffer()
        assert buffer.add_frame(None, 0.0) is None
        assert buffer.add_frame(np.zeros((0, 0, 3), np.uint8), 0.1) is None


class TestFrames:
    def test_wide_frames_are_downscaled_on_capture(self):
        buffer = ClipBuffer(analysis_width=640)
        kept = buffer.add_frame(np.zeros((1080, 1920, 3), np.uint8), 0.0)
        assert kept.image.shape[:2] == (360, 640)
        small = buffer.add_frame(np.zeros((240, 320, 3), np.uint8), 1.0)
        assert small.image.shape[:2] == (240, 320)

    def test_motion_energy(self):
        buffer = ClipBuffer(max_fps=0.0)
        first = buffer.add_frame(frame(), 0.0)
        still = buffer.add_frame(frame(), 0.1)
        moved = buffer.add_frame(frame(square_at=20), 0.2)
        assert first.motion == 0.0 and still.motion == 0.0
        assert moved.motion > 0.02

    def test_motion_is_on_the_same_scale_as_the_motion_engine(self):
        """Mean blurred grey difference x 10, so the gate floor means the same."""
        import cv2
        a, b = frame(0, (120, 160)), frame(255, (120, 160))
        thumbs = [cv2.GaussianBlur(cv2.cvtColor(x, cv2.COLOR_BGR2GRAY), (5, 5), 0)
                  for x in (a, b)]
        assert motion_energy(*thumbs) == 1.0
        assert motion_energy(None, thumbs[0]) == 0.0

    def test_buffer_keeps_window_plus_hop(self):
        buffer = ClipBuffer(max_fps=10.0)
        fill(buffer, 20.0, 10.0)
        keep = settings.clip.window_seconds + settings.clip.hop_seconds
        span = buffer.latest_at - buffer.frames_between(-1, 1e9)[0].at
        assert span <= keep + 0.11

    def test_clip_window_excludes_its_start_and_includes_its_end(self):
        buffer = ClipBuffer(max_fps=0.0, keep_seconds=100.0)
        for t in (0.0, 1.0, 2.0, 3.0, 4.0, 5.0):
            buffer.add_frame(frame(), t)
        clip = buffer.clip(5.0, seq=3)
        assert [f.at for f in clip.frames] == [2.0, 3.0, 4.0, 5.0]
        assert clip.start == 1.0 and clip.end == 5.0 and clip.seq == 3

    def test_flame_fraction(self):
        image = np.zeros((120, 160, 3), np.uint8)
        assert flame_fraction(image) == 0.0
        image[:20, :20] = (0, 160, 255)            # bright saturated orange (BGR)
        assert flame_fraction(image) == pytest.approx(400 / (120 * 160), rel=0.05)
        assert flame_fraction(np.full((120, 160, 3), 255, np.uint8)) == 0.0, "white is not flame"


class TestAudioAndEvents:
    def test_audio_window_is_exact(self):
        rate = 16000
        buffer = ClipBuffer(max_fps=0.0, audio_rate=rate, keep_seconds=100.0)
        for second in range(8):
            buffer.add_audio(np.full(rate, second, np.float32), end_at=second + 1.0)
        buffer.add_frame(frame(), 8.0)
        clip = buffer.clip(8.0)
        assert clip.audio.size == 4 * rate
        assert set(np.unique(clip.audio)) == {4.0, 5.0, 6.0, 7.0}

    def test_rms_and_missing_audio(self):
        buffer = ClipBuffer(max_fps=0.0)
        buffer.add_frame(frame(), 1.0)
        assert buffer.clip(1.0).audio is None and buffer.clip(1.0).audio_rms is None
        buffer.add_audio(np.full(16000, 0.5, np.float32), end_at=1.0)
        assert buffer.clip(1.0).audio_rms == pytest.approx(0.5)

    def test_other_sample_rates_are_refused(self):
        buffer = ClipBuffer(audio_rate=16000)
        buffer.add_audio(np.ones(48000, np.float32), end_at=1.0, rate=48000)
        buffer.add_frame(frame(), 1.0)
        assert buffer.clip(1.0).audio is None

    def test_old_audio_is_trimmed(self):
        buffer = ClipBuffer(max_fps=0.0, keep_seconds=6.0)
        for second in range(20):
            buffer.add_audio(np.ones(16000, np.float32), end_at=second + 1.0)
        assert len(buffer._audio) <= 7

    def test_sensor_events_in_the_window(self):
        buffer = ClipBuffer(max_fps=0.0, keep_seconds=100.0)
        buffer.add_event("pir", 0.5, state=True)
        buffer.add_event("door", 3.0, state="open")
        buffer.add_frame(frame(), 5.0)
        events = buffer.clip(5.0).events
        assert [e.kind for e in events] == ["door"] and events[0].detail == {"state": "open"}


def frames_at(times, motion=None) -> list[BufferedFrame]:
    motion = motion or {}
    return [BufferedFrame(i, t, frame(), motion.get(i, 0.0)) for i, t in enumerate(times)]


class TestKeyframes:
    def test_four_uniform_and_two_motion(self):
        times = [i / 15 for i in range(60)]                  # 4 s at 15 fps
        motion = {7: 0.9, 8: 0.8, 33: 0.5}
        frames = frames_at(times, motion)
        keys = select_keyframes(frames, end=times[-1], window=4.0, uniform=4, motion=2)
        roles = [k.role for k in keys]
        assert roles.count("uniform") == 4 and roles.count("motion") == 2
        assert {k.seq for k in keys if k.role == "motion"} == {7, 8}
        assert [k.at for k in keys] == sorted(k.at for k in keys)

    def test_uniform_keyframes_sit_on_the_grid(self):
        """One per second back from the clip end: the newest frame - the one the
        decision is about - is always analysed."""
        frames = frames_at([i / 15 for i in range(61)])
        keys = select_keyframes(frames, end=4.0, window=4.0, uniform=4, motion=0)
        assert [round(k.at, 3) for k in keys] == [1.0, 2.0, 3.0, 4.0]

    def test_overlapping_clips_share_their_common_half(self):
        """Hop 2 s, window 4 s: the older half of clip n+1 is the newer half of
        clip n, so its uniform keyframes are the same frames (analysed once)."""
        times = [i / 15 for i in range(91)]                  # 6 s
        frames = frames_at(times)
        first = select_keyframes([f for f in frames if 0 < f.at <= 4.0], 4.0, 4.0, 4, 0)
        second = select_keyframes([f for f in frames if 2.0 < f.at <= 6.0], 6.0, 4.0, 4, 0)
        newer_half = {k.seq for k in first if k.at > 2.0}
        older_half = {k.seq for k in second if k.at <= 4.0}
        assert newer_half == older_half and len(newer_half) == 2

    def test_partial_window(self):
        """The first clip after start holds 2 s: grid points with no frame
        near them are left out, not filled with a far frame."""
        frames = frames_at([i / 15 for i in range(30)])      # 0 .. 1.93 s
        keys = select_keyframes(frames, end=2.0, window=4.0, uniform=4, motion=2)
        assert [round(k.at, 3) for k in keys if k.role == "uniform"] == [0.0, 1.0, 1.933]

    def test_eligibility_is_respected(self):
        frames = frames_at([i / 15 for i in range(60)], {5: 0.9, 50: 0.4})
        keys = select_keyframes(frames, end=frames[-1].at, window=4.0, uniform=4, motion=1,
                                eligible=lambda f: f.seq >= 30)
        assert all(k.seq >= 30 for k in keys)
        assert [k.seq for k in keys if k.role == "motion"] == [50]

    def test_still_frames_are_not_motion_keyframes(self):
        frames = frames_at([i / 15 for i in range(60)])
        keys = select_keyframes(frames, end=frames[-1].at, window=4.0, uniform=4, motion=2)
        assert all(k.role == "uniform" for k in keys)

    def test_no_frames(self):
        assert select_keyframes([], 4.0, 4.0, 4, 2) == []


class TestScheduler:
    def test_one_clip_per_hop_on_a_fixed_grid(self):
        scheduler = ClipScheduler(hop_seconds=2.0)
        ends = [end for t in np.arange(10.0, 20.01, 1 / 15)
                if (end := scheduler.due(float(t))) is not None]
        assert ends == pytest.approx([12.0, 14.0, 16.0, 18.0, 20.0], abs=1e-6)
        assert scheduler.dropped == 0 and scheduler.clips == 5

    def test_newest_clip_wins_and_late_clips_are_counted(self):
        scheduler = ClipScheduler(hop_seconds=2.0)
        scheduler.due(0.0)
        assert scheduler.due(2.1) == pytest.approx(2.0)
        # The analysis took 5 s: clips ending at 4 and 6 are skipped.
        assert scheduler.due(7.3) == pytest.approx(6.0)
        assert scheduler.dropped == 1
        assert scheduler.due(7.9) is None
        assert scheduler.next_end() == pytest.approx(8.0)

    def test_budget(self):
        scheduler = ClipScheduler(hop_seconds=2.0, budget_fraction=0.8)
        for seconds in (0.5, 0.6, 1.0, 1.2):
            scheduler.record(seconds)
        stats = scheduler.stats()
        assert stats["budget_ms"] == 1600.0 and stats["within_budget"] is True
        scheduler.record(3.0)
        for _ in range(30):
            scheduler.record(2.0)
        assert scheduler.stats()["within_budget"] is False

    def test_nothing_before_frames(self):
        scheduler = ClipScheduler()
        assert scheduler.due(None) is None
        assert scheduler.stats()["within_budget"] is None


class TestOptions:
    def test_missing_config_fields_use_the_documented_defaults(self):
        assert option("scene_keyframes") == 2
        assert option("buffer_fps") == 15.0

    def test_a_config_field_takes_over(self):
        @dataclasses.dataclass(frozen=True)
        class Extended:
            scene_keyframes: int = 1
        assert option("scene_keyframes", Extended()) == 1
