"""The 4-second clip engine (HARDWARE.md T1; docs/7 §2.4, track T-CLIP).

Why clips
---------
SENTRIX's per-frame stack - three YOLO passes, a DINOv2 scene classifier,
face recognition - runs at 5.6 Hz on the laptop CPU and would run at about
0.5-0.7 Hz on a Raspberry Pi 5 (HARDWARE.md §2), the rate at which the first
live test failed. The clip engine keeps the last 4 s of everything in a ring
buffer and, every hop of 2 s, analyses the window ``[t - 4 s, t]`` as one
unit: a handful of well-chosen keyframes for vision, the whole window for
motion and audio, the sensor events in it. "Run every model 30 times a
second" becomes "run each model a few times every 2 s".

Pieces
------
* :class:`ClipBuffer` - frames at the analysis resolution (at most
  ``buffer_fps``), each with its capture time and a motion energy computed on
  capture (a 160 px grey thumbnail difference), audio samples, sensor events.
* :func:`select_keyframes` - 4 uniform keyframes on a grid anchored to the clip
  end (so two overlapping clips share the frames of their common half) plus
  the 2 frames with the highest motion energy (a brief gesture between them).
* :class:`ClipScheduler` - one clip per hop on a fixed grid; the newest clip
  wins, late ones are dropped and counted.
* :class:`ClipEngine` - the stage cascade (cheap stages decide whether
  expensive ones run):

  ====  ==========================================================  ===========
  S0    gate: motion energy, audio RMS, PIR/door; held open after   always
        activity, during an alarm, and at least every 10 s
  S1    objects: the detector on keyframes not analysed by the       gate open
        previous clip (results of the shared half are reused)
  S2    scene classifier on up to 2 new keyframes                    a person or
                                                                     a fire
                                                                     candidate
  S3    identity on the keyframe with people (<= once per hop)       persons
  S4    action (violence) model over the two 2 s halves              >= 2 persons,
                                                                     high motion
  S5    audio over the full window                                   always
  S6    within-clip confirmation, then the unchanged evidence ->     always
        TCI -> level policy -> incident path (``Pipeline.decide``)
  ====  ==========================================================  ===========

* :class:`ClipPolicy` - ``LevelPolicy`` that commits L4/L5 on one clip
  (``ClipConfig.min_escalate_observations``), with the dwell measured inside
  the clip: the policy is told how long the confirming keyframes span.
* :class:`ClipRunner` - buffer + scheduler + engine (+ fast lane) on one
  pipeline. Driven synchronously by replay (:func:`replay_video_clip`) and by
  :class:`ClipStrategy` on the live loop's inference thread.

What stays the same
-------------------
Confirmation moves inside the clip (:class:`~sentrix.fusion.evidence.
ClipConfirmation`: a class is confirmed on 2 keyframes of one track or one
fire region), but the corroboration rules of M1 are unchanged: one source
stays in the L3 band, a classifier and a detector box together reach L4/L5,
experimental classes stop at L2. Capture, preview, the identity worker and
the HUD stay in ``Pipeline.run_live``; side effects stay in
``Pipeline._act``; measurement pipelines keep every no-op of the harness.

The ring buffer is also the pre-roll the evidence vault never had: when an
incident seals evidence, the clip around it is sealed too
(``EvidenceVault.save_clip``), then the seconds after it.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from sentrix.config import settings
from sentrix.fusion.evidence import (CLASSIFIER_SOURCES, DETECTOR_SOURCES, ClassEvidence,
                                     ClipConfirmation, ClipObservation, strongest)
from sentrix.fusion.features import FeatureVector
from sentrix.fusion.policy import LevelPolicy, LevelState, _per_threshold
from sentrix.perception.detector import Detection, DetectionResult
from sentrix.perception.identity import FaceCheck, IdentityResult
from sentrix.perception.scene import PRIORITY
from sentrix.pipeline.fastlane import FastLane, FastLaneFrame, fast_lane_evidence
from sentrix.pipeline.state import state
from sentrix.runtime.tracking import RegionGrouper, containing_track
from sentrix.utils.logging import get_logger, telemetry

log = get_logger("clip")

try:
    import cv2
except ImportError:                                    # pragma: no cover
    cv2 = None                                         # type: ignore[assignment]

#: Clip-mode engine tunables and their reasons. ``ClipConfig`` (docs/7 §3.4)
#: carries the same fields since integration; :func:`option` reads the field
#: of a config object and falls back to these for a partial one (tests).
DEFAULTS: dict[str, float] = {
    #: Frames per second kept in the ring (the Pi's lores analysis stream).
    "buffer_fps": 15.0,
    #: Frames wider than this are downscaled on capture (640x480 lores).
    "analysis_width": 640,
    #: New scene-classifier inferences per clip (S2).
    "scene_keyframes": 2,
    #: S0: clip motion peak (MotionEngine units) that opens the cascade. Low on
    #: purpose: the gate is for a still, empty room, not a filter of activity.
    "gate_motion_floor": 0.015,
    #: S0 stays open this long after a clip saw a person or a fire candidate.
    "gate_hold_seconds": 10.0,
    #: A full cascade runs at least this often, whatever the gate says, so a
    #: still threat is not missed for ever.
    "idle_refresh_seconds": 10.0,
    #: S3 at most this often (a new track is checked at once).
    "identity_interval_seconds": 2.0,
    #: S4 runs the action model only above this clip motion peak.
    "action_motion_floor": 0.10,
    #: Share of bright flame-coloured pixels that makes a fire candidate (S2 gate).
    "flame_fraction": 0.0005,
    #: Frames per second sealed into an evidence clip bundle.
    "evidence_fps": 5.0,
    #: Seconds sealed after an evidence capture (HARDWARE.md T1: 4-8 s).
    "post_roll_seconds": 8.0,
}

STAGES: tuple[str, ...] = ("S0_gate", "S1_objects", "S2_scene", "S3_identity",
                           "S4_action", "S5_audio", "S6_fusion")
#: Sensor events that open the gate (T-HW publishes PIR and door events).
SENSOR_KINDS = frozenset({"pir", "door", "motion"})
MOTION_WIDTH = 160
#: Same scale as ``MotionEngine`` (mean blurred grey difference x 10).
MOTION_GAIN = 10.0


def option(name: str, cfg=None):
    """A clip tunable: the ``ClipConfig`` field if it exists, else :data:`DEFAULTS`."""
    return getattr(cfg or settings.clip, name, DEFAULTS[name])


# ── cheap per-frame measures ──────────────────────────────────────────────────
def _thumb(frame: np.ndarray) -> np.ndarray:
    grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    height, width = grey.shape[:2]
    if width > MOTION_WIDTH:
        grey = cv2.resize(grey, (MOTION_WIDTH, max(1, round(height * MOTION_WIDTH / width))),
                          interpolation=cv2.INTER_AREA)
    return cv2.GaussianBlur(grey, (5, 5), 0)


def motion_energy(previous: np.ndarray | None, current: np.ndarray) -> float:
    """Mean blurred grey difference between two thumbnails, in [0, 1]."""
    if previous is None or previous.shape != current.shape:
        return 0.0
    diff = cv2.absdiff(previous, current)
    return float(min(float(np.mean(diff)) / 255.0 * MOTION_GAIN, 1.0))


def flame_fraction(frame: np.ndarray) -> float:
    """Share of bright, saturated flame-coloured pixels (HSV hue 0-25 deg,
    S >= 180, V >= 200) on a 320 px thumbnail. Only gates the scene
    classifier; never evidence."""
    if cv2 is None or frame is None or frame.ndim != 3:
        return 0.0
    height, width = frame.shape[:2]
    small = frame
    if width > 320:
        small = cv2.resize(frame, (320, max(1, round(height * 320 / width))),
                           interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (0, 180, 200), (25, 255, 255))
    return float(np.count_nonzero(mask)) / float(max(mask.size, 1))


# ── ring buffer ───────────────────────────────────────────────────────────────
@dataclass(eq=False)
class BufferedFrame:
    seq: int
    at: float
    image: np.ndarray
    motion: float = 0.0


@dataclass(frozen=True)
class SensorEvent:
    kind: str
    at: float
    detail: dict = field(default_factory=dict)


@dataclass
class Clip:
    """The window ``(end - window, end]`` cut from the buffer."""

    seq: int
    start: float
    end: float
    frames: list[BufferedFrame]
    audio: np.ndarray | None = None
    sample_rate: int = 16000
    events: list[SensorEvent] = field(default_factory=list)

    @property
    def motion_peak(self) -> float:
        return max((f.motion for f in self.frames), default=0.0)

    @property
    def motion_mean(self) -> float:
        return float(np.mean([f.motion for f in self.frames])) if self.frames else 0.0

    @property
    def audio_rms(self) -> float | None:
        if self.audio is None or self.audio.size == 0:
            return None
        return float(np.sqrt(np.mean(np.square(self.audio, dtype=np.float64))))


class ClipBuffer:
    """Ring buffer of the last ``window + hop`` seconds of frames, audio and
    sensor events. ``add_*`` run on the capture side (O(1) per frame: a
    downscale, a thumbnail and a difference); ``clip`` on the inference side."""

    def __init__(self, cfg=None, *, window_seconds: float | None = None,
                 keep_seconds: float | None = None, max_fps: float | None = None,
                 analysis_width: int | None = None, audio_rate: int | None = None) -> None:
        cfg = cfg or settings.clip
        self.window = float(window_seconds or cfg.window_seconds)
        self.keep = float(keep_seconds or (self.window + float(cfg.hop_seconds)))
        fps = float(max_fps if max_fps is not None else option("buffer_fps", cfg))
        self.period = 1.0 / fps if fps > 0 else 0.0
        self.width = int(analysis_width or option("analysis_width", cfg))
        self.audio_rate = int(audio_rate or settings.windows.audio_sample_rate)
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._frames: deque[BufferedFrame] = deque()
            self._audio: deque[tuple[float, np.ndarray]] = deque()
            self._events: deque[SensorEvent] = deque()
            self._seq = 0
            self._next_due: float | None = None
            self._thumb: np.ndarray | None = None
            self.received = 0
            self.kept = 0

    # ── writers ───────────────────────────────────────────────────────────────
    def add_frame(self, frame: np.ndarray, at: float) -> BufferedFrame | None:
        """Keep ``frame`` if it is due at ``buffer_fps``; None when skipped."""
        if frame is None or getattr(frame, "size", 0) == 0 or cv2 is None:
            return None
        with self._lock:
            self.received += 1
            last = self._frames[-1].at if self._frames else None
            if last is not None and at <= last:
                return None                         # out of order or repeated
            if self._next_due is not None and at < self._next_due - 0.25 * self.period:
                return None
            # Pace from the previous deadline, not from now, so a 25 fps source
            # keeps 15 fps on average; after a stall, restart from now.
            base = (self._next_due if self._next_due is not None
                    and at - self._next_due < self.period else at)
            self._next_due = base + self.period
            image = frame
            height, width = frame.shape[:2]
            if self.width and width > self.width:
                image = cv2.resize(frame, (self.width, max(1, round(height * self.width / width))),
                                   interpolation=cv2.INTER_AREA)
            thumb = _thumb(image)
            kept = BufferedFrame(self._seq, float(at), image, motion_energy(self._thumb, thumb))
            self._thumb = thumb
            self._seq += 1
            self.kept += 1
            self._frames.append(kept)
            self._trim(at)
            return kept

    def add_audio(self, samples: np.ndarray, end_at: float, rate: int | None = None) -> None:
        """Mono samples whose last one was captured at ``end_at``."""
        if samples is None or len(samples) == 0:
            return
        rate = int(rate or self.audio_rate)
        if rate != self.audio_rate:
            log.warning("audio at %d Hz dropped (buffer is %d Hz)", rate, self.audio_rate)
            return
        data = np.asarray(samples, dtype=np.float32).reshape(-1)
        with self._lock:
            self._audio.append((float(end_at) - len(data) / rate, data))
            self._trim(end_at)

    def add_event(self, kind: str, at: float, **detail: Any) -> None:
        with self._lock:
            self._events.append(SensorEvent(kind, float(at), dict(detail)))
            self._trim(at)

    def _trim(self, now: float) -> None:
        horizon = now - self.keep
        while self._frames and self._frames[0].at < horizon:
            self._frames.popleft()
        while self._audio and self._audio[0][0] + len(self._audio[0][1]) / self.audio_rate < horizon:
            self._audio.popleft()
        while self._events and self._events[0].at < horizon:
            self._events.popleft()

    # ── readers ───────────────────────────────────────────────────────────────
    @property
    def latest_at(self) -> float | None:
        with self._lock:
            return self._frames[-1].at if self._frames else None

    def __len__(self) -> int:
        with self._lock:
            return len(self._frames)

    def clip(self, end: float, seq: int = 0) -> Clip:
        start = end - self.window
        with self._lock:
            frames = [f for f in self._frames if start < f.at <= end + 1e-6]
            events = [e for e in self._events if start < e.at <= end + 1e-6]
            audio = self._audio_window(start, end)
        return Clip(seq, start, end, frames, audio, self.audio_rate, events)

    def frames_between(self, start: float, end: float) -> list[BufferedFrame]:
        """Frames with ``start < at <= end`` (evidence pre- and post-roll)."""
        with self._lock:
            return [f for f in self._frames if start < f.at <= end + 1e-6]

    def _audio_window(self, start: float, end: float) -> np.ndarray | None:
        parts = []
        for begin, data in self._audio:
            first = int(max(0, math.ceil((start - begin) * self.audio_rate)))
            last = int(min(len(data), math.floor((end - begin) * self.audio_rate) + 1))
            if last > first:
                parts.append(data[first:last])
        return np.concatenate(parts) if parts else None


# ── keyframes ─────────────────────────────────────────────────────────────────
@dataclass
class FrameAnalysis:
    """What the models said about one buffered frame; shared by every clip
    whose window contains it."""

    seq: int
    at: float
    detections: DetectionResult
    flame: float = 0.0
    #: Alarm-label boxes (stick and smoke never vote) and their groups:
    #: ("track", id) for a weapon held by a tracked person, ("region", id)
    #: for a fire or an unattributed weapon.
    weapons: list = field(default_factory=list)
    weapon_groups: list = field(default_factory=list)
    fires: list = field(default_factory=list)
    fire_groups: list = field(default_factory=list)
    scene: Any = None

    @property
    def persons(self) -> list[Detection]:
        return self.detections.persons

    def fire_candidate(self, floor: float) -> bool:
        return bool(self.fires) or self.flame >= floor


@dataclass(eq=False)
class Keyframe:
    frame: BufferedFrame
    role: str                      # "uniform" | "motion"
    analysis: FrameAnalysis | None = None
    #: True when the analysis came from an earlier, overlapping clip.
    cached: bool = False

    @property
    def seq(self) -> int:
        return self.frame.seq

    @property
    def at(self) -> float:
        return self.frame.at

    @property
    def image(self) -> np.ndarray:
        return self.frame.image

    def to_dict(self, end: float) -> dict:
        data: dict[str, Any] = {"seq": self.seq, "t": round(self.at - end, 3),
                                "role": self.role, "motion": round(self.frame.motion, 4),
                                "cached": self.cached}
        a = self.analysis
        if a is None:
            data["analysed"] = False
            return data
        data.update(
            persons=[{"conf": round(p.confidence, 3), "track_id": p.track_id}
                     for p in a.persons],
            weapons=[{"label": w.label, "conf": round(w.confidence, 3),
                      "group": list(g) if g else None}
                     for w, g in zip(a.weapons, a.weapon_groups)],
            fires=[{"label": f.label, "conf": round(f.confidence, 3),
                    "group": list(g) if g else None}
                   for f, g in zip(a.fires, a.fire_groups)],
            flame=round(a.flame, 5))
        if a.scene is not None:
            data["scene"] = {k: round(v, 4) for k, v in a.scene.probs.items()}
        return data


def select_keyframes(frames: list[BufferedFrame], end: float, window: float,
                     uniform: int, motion: int,
                     eligible: Callable[[BufferedFrame], bool] | None = None) -> list[Keyframe]:
    """``uniform`` frames on a grid anchored to the clip end, then the
    ``motion`` highest-motion frames left; sorted by time.

    Grid points sit at ``end - j * window / uniform``: the newest frame - the
    one the decision is about - is always analysed. Clip ends move by the hop,
    so with a 4 s window, 4 uniform frames and a 2 s hop the grid of one
    clip's older half is the grid of the previous clip's newer half: the same
    frames, analysed once. ``eligible`` restricts the choice (the engine allows
    only frames it already analysed, or frames newer than the last one its
    tracker saw, so the person tracker never runs backwards in time).
    """
    if not frames:
        return []
    ok = eligible or (lambda f: True)
    chosen: dict[int, Keyframe] = {}
    if uniform > 0:
        spacing = window / uniform
        for j in range(uniform):
            target = end - j * spacing
            pool = [f for f in frames if ok(f) and f.seq not in chosen
                    and abs(f.at - target) < spacing / 2]
            if pool:
                best = min(pool, key=lambda f: (abs(f.at - target), -f.at))
                chosen[best.seq] = Keyframe(best, "uniform")
    pool = [f for f in frames if f.seq not in chosen and ok(f) and f.motion > 0]
    for f in sorted(pool, key=lambda f: (-f.motion, -f.at))[:max(0, motion)]:
        chosen[f.seq] = Keyframe(f, "motion")
    return sorted(chosen.values(), key=lambda k: k.at)


# ── scheduling ────────────────────────────────────────────────────────────────
class ClipScheduler:
    """One clip per hop on a fixed grid; the newest clip wins.

    Clip ends sit at ``origin + k * hop`` (origin: the first frame). When the
    analysis of one clip takes longer than a hop, the grid points that passed
    meanwhile are not queued: the next clip is the newest window, and every
    skipped one is counted in ``dropped`` - the "newest frame wins" rule of
    the live loop, applied to clips. Compute time is kept against the budget
    (``budget_fraction`` of the hop at p95, HARDWARE.md T1).
    """

    def __init__(self, cfg=None, *, hop_seconds: float | None = None,
                 budget_fraction: float | None = None) -> None:
        cfg = cfg or settings.clip
        self.hop = float(hop_seconds or cfg.hop_seconds)
        fraction = cfg.budget_fraction if budget_fraction is None else budget_fraction
        self.budget = self.hop * float(fraction)
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._origin: float | None = None
            self._next = 1
            self.clips = 0
            self.dropped = 0
            self._compute: deque[float] = deque(maxlen=1000)

    def due(self, latest_at: float | None) -> float | None:
        """The end of the clip to analyse now, or None (claims it)."""
        if latest_at is None:
            return None
        with self._lock:
            if self._origin is None:
                self._origin = float(latest_at)
                return None
            k = math.floor((latest_at - self._origin) / self.hop + 1e-9)
            if k < self._next:
                return None
            self.dropped += k - self._next
            self._next = k + 1
            self.clips += 1
            return self._origin + k * self.hop

    def next_end(self) -> float | None:
        with self._lock:
            return None if self._origin is None else self._origin + self._next * self.hop

    def record(self, compute_seconds: float) -> None:
        with self._lock:
            self._compute.append(float(compute_seconds))

    def stats(self) -> dict:
        with self._lock:
            values = sorted(self._compute)
            clips, dropped = self.clips, self.dropped
        def pct(q):
            if not values:
                return None
            return round(1000 * values[min(len(values) - 1, int(len(values) * q))], 1)
        p95 = pct(0.95)
        return {"clips": clips, "dropped": dropped,
                "dropped_fraction": round(dropped / max(clips + dropped, 1), 4),
                "compute_ms": {"p50": pct(0.5), "p95": p95, "max": pct(1.0)},
                "budget_ms": round(1000 * self.budget, 1),
                "within_budget": None if p95 is None else p95 <= 1000 * self.budget}


# ── scores ────────────────────────────────────────────────────────────────────
@dataclass
class ClipScores:
    """Everything one clip produced: the record behind its decision."""

    seq: int
    start: float
    end: float
    frames: int = 0
    gate: dict = field(default_factory=dict)
    stages: dict = field(default_factory=dict)
    keyframes: list[Keyframe] = field(default_factory=list)
    evidence: dict[str, ClassEvidence] = field(default_factory=dict)
    #: Seconds the confirming keyframes of each class span (ClipPolicy dwell).
    spans: dict[str, float] = field(default_factory=dict)
    scene: dict | None = None
    audio: dict = field(default_factory=dict)
    identity: dict = field(default_factory=dict)
    persons: int = 0
    features: FeatureVector | None = None
    trigger: Keyframe | None = None
    #: The last buffered frame, used when no keyframe was analysed.
    fallback_image: np.ndarray | None = None
    compute_ms: float = 0.0
    decision: dict | None = None

    @property
    def image(self) -> np.ndarray | None:
        """The frame the decision's snapshot and evidence show."""
        if self.trigger is not None:
            return self.trigger.image
        return self.fallback_image

    @property
    def span(self) -> float:
        """The dwell evidence of the class that sets the TCI."""
        cls, score = strongest(self.evidence)
        return self.spans.get(cls, 0.0) if cls is not None and score > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "mode": "clip", "clip": self.seq, "start": round(self.start, 3),
            "end": round(self.end, 3), "frames": self.frames, "gate": self.gate,
            "stages": self.stages,
            "keyframes": [k.to_dict(self.end) for k in self.keyframes],
            "evidence": {c: e.to_dict() for c, e in self.evidence.items()},
            "spans": {c: round(s, 3) for c, s in self.spans.items()},
            "scene": self.scene, "audio": self.audio, "identity": self.identity,
            "persons": self.persons,
            "trigger": self.trigger.seq if self.trigger is not None else None,
            "compute_ms": round(self.compute_ms, 1), "decision": self.decision,
        }


def evidence_span(evidence: ClassEvidence) -> float:
    """How long the confirming keyframes of one class span.

    Corroborated evidence holds only as long as both sides do, so its span is
    the shorter of the classifier's and the detector's.
    """
    confirmed = [v for v in evidence.votes if v.confirmed]

    def longest(sources):
        return max((float(getattr(v, "span", 0.0)) for v in confirmed if v.source in sources),
                   default=None)

    if evidence.state == "corroborated":
        sides = [s for s in (longest(CLASSIFIER_SOURCES), longest(DETECTOR_SOURCES))
                 if s is not None]
        return min(sides) if sides else 0.0
    return max((float(getattr(v, "span", 0.0)) for v in confirmed), default=0.0)


class _Stages:
    """Per-stage timings of one clip."""

    def __init__(self) -> None:
        self.data: dict[str, dict] = {}

    def start(self, name: str) -> tuple[str, float]:
        return name, time.perf_counter()

    def done(self, token: tuple[str, float], calls: int = 0, reason: str = "") -> None:
        name, began = token
        entry = {"ran": True, "ms": round((time.perf_counter() - began) * 1000.0, 2),
                 "calls": calls}
        if reason:
            entry["reason"] = reason
        self.data[name] = entry
        telemetry.record("clip", name, None, entry["ms"] / 1000.0)

    def skip(self, name: str, reason: str) -> None:
        self.data[name] = {"ran": False, "ms": 0.0, "calls": 0, "reason": reason}


def _with_identity(detections: dict, tracks: dict) -> dict:
    """Label each person box with its track's identity (HUD, logs)."""
    for person in detections.get("persons", []):
        verdict = tracks.get(person.get("track_id"))
        if verdict is not None:
            person["identity"] = verdict["name"] or verdict["state"]
    return detections


# ── the stage cascade ─────────────────────────────────────────────────────────
class ClipEngine:
    """S0-S5 over one clip, then the clip's evidence and feature vector.

    Uses the pipeline's own components: the detector (or a runtime detector on
    exported heads), the scene classifier, the violence model, identity and
    per-track identity, behaviour, ReID and audio. S6 - the decision - is the
    pipeline's unchanged ``decide`` (called by :class:`ClipRunner`).
    """

    def __init__(self, pipeline, detector=None, cfg=None) -> None:
        self.pipeline = pipeline
        self.cfg = cfg or settings.clip
        self.threat = settings.threat
        self.detection = settings.detection
        self.detector = detector if detector is not None else pipeline.detector
        self.confirmation = ClipConfirmation(self.cfg.confirm_keyframes)
        self._weapon_regions = RegionGrouper(forget_seconds=float(self.cfg.window_seconds))
        self._fire_regions = RegionGrouper(forget_seconds=float(self.cfg.window_seconds))
        self.reset()

    def reset(self) -> None:
        self._cache: dict[int, FrameAnalysis] = {}
        self._last_tracked_seq = -1
        self.last_person_at: float | None = None
        self._last_activity_at: float | None = None
        self._last_full_at: float | None = None
        self._identity_at: float | None = None
        self._identity_tracks: set = set()
        identity = getattr(self.pipeline, "identity", None)
        degraded = not getattr(identity, "available", False)
        self._check = FaceCheck(degraded=degraded)
        self._settled = False
        self.last_features: FeatureVector | None = None
        self.last_identity = IdentityResult(authorized=False, degraded=degraded)
        self._weapon_regions.reset()
        self._fire_regions.reset()

    def forget(self) -> None:
        """Drop every cached analysis (an incident was closed on request): the
        next clip analyses only frames newer than the last one tracked, so the
        scene has to be confirmed afresh."""
        self._cache.clear()
        self._weapon_regions.reset()
        self._fire_regions.reset()

    # ── S0 ────────────────────────────────────────────────────────────────────
    def _gate(self, clip: Clip, level: int) -> tuple[bool, list[str]]:
        if not clip.frames:
            return False, ["no frames"]
        reasons = []
        if clip.motion_peak >= float(option("gate_motion_floor", self.cfg)):
            reasons.append("motion")
        rms = clip.audio_rms
        if rms is not None and rms >= settings.windows.audio_rms_floor:
            reasons.append("audio")
        elif rms is None:
            audio = getattr(self.pipeline, "audio", None)
            if audio is not None and audio.result().score > 0:
                reasons.append("audio")
        if any(e.kind in SENSOR_KINDS for e in clip.events):
            reasons.append("sensor")
        if level > 1:
            reasons.append(f"alarm L{level}")
        hold = float(option("gate_hold_seconds", self.cfg))
        if self._last_activity_at is not None and clip.end - self._last_activity_at <= hold:
            reasons.append("recent activity")
        refresh = float(option("idle_refresh_seconds", self.cfg))
        if self._last_full_at is None or clip.end - self._last_full_at >= refresh:
            reasons.append("refresh")
        return bool(reasons), reasons

    # ── S1 ────────────────────────────────────────────────────────────────────
    def _analyse_frame(self, frame: BufferedFrame) -> FrameAnalysis:
        detections = self.detector.detect(frame.image)
        threat = self.threat
        weapons = [w for w in detections.weapons if w.label in threat.detector_weapon_labels]
        persons = [(p.track_id, tuple(p.bbox)) for p in detections.persons]
        groups: list = []
        untracked: list[int] = []
        for i, weapon in enumerate(weapons):
            track_id = containing_track(weapon.bbox, persons)
            groups.append(("track", track_id) if track_id is not None else None)
            if track_id is None:
                untracked.append(i)
        for i, rid in zip(untracked, self._weapon_regions.assign(
                [weapons[i].bbox for i in untracked], frame.at)):
            groups[i] = ("region", rid)
        fires = [f for f in detections.fires if f.label in threat.detector_fire_labels]
        fire_groups = [("region", rid) for rid in
                       self._fire_regions.assign([f.bbox for f in fires], frame.at)]
        return FrameAnalysis(frame.seq, frame.at, detections, flame_fraction(frame.image),
                             weapons, groups, fires, fire_groups)

    def _objects(self, keyframes: list[Keyframe], stages: _Stages) -> None:
        token = stages.start("S1_objects")
        calls = 0
        for kf in keyframes:
            cached = self._cache.get(kf.seq)
            if cached is not None:
                kf.analysis, kf.cached = cached, True
                continue
            if kf.seq <= self._last_tracked_seq:
                continue                            # never run the tracker backwards
            analysis = self._analyse_frame(kf.frame)
            self._last_tracked_seq = kf.seq
            self._cache[kf.seq] = analysis
            kf.analysis = analysis
            calls += 1
        stages.done(token, calls)

    # ── S2 ────────────────────────────────────────────────────────────────────
    def _scene(self, keyframes: list[Keyframe], window: float, stages: _Stages) -> None:
        scene = getattr(self.pipeline, "scene", None)
        if scene is None or not getattr(scene, "available", False):
            stages.skip("S2_scene", "no scene model")
            return
        floor = float(option("flame_fraction", self.cfg))
        analysed = [k for k in keyframes if k.analysis is not None]
        if not any(k.analysis.persons or k.analysis.fire_candidate(floor) for k in analysed):
            stages.skip("S2_scene", "no person or fire candidate")
            return
        pool = [k for k in analysed if k.analysis.scene is None
                and (k.analysis.persons or k.analysis.fire_candidate(floor))]
        if not pool:
            stages.done(stages.start("S2_scene"), 0, "keyframes already scored")
            return

        def signal(k: Keyframe):
            a = k.analysis
            box = max([b.confidence for b in a.weapons + a.fires], default=0.0)
            return (box, a.flame, len(a.persons), k.frame.motion, k.at)

        want = max(1, int(option("scene_keyframes", self.cfg)))
        gap = window / max(2 * int(self.cfg.uniform_keyframes), 1)
        chosen: list[Keyframe] = []
        ranked = sorted(pool, key=signal, reverse=True)
        for k in ranked:                            # strongest first, spread in time
            if len(chosen) < want and all(abs(k.at - c.at) >= gap for c in chosen):
                chosen.append(k)
        for k in ranked:
            if len(chosen) < want and k not in chosen:
                chosen.append(k)
        token = stages.start("S2_scene")
        try:
            probs = scene.score_frames([k.image for k in chosen])
        except Exception as exc:
            log.error("scene inference failed: %s", exc)
            stages.done(token, 0, f"failed: {exc}")
            return
        for k, p in zip(chosen, probs):
            k.analysis.scene = scene.decide(p)
        stages.done(token, len(chosen))

    # ── S3 ────────────────────────────────────────────────────────────────────
    def _identity(self, keyframes: list[Keyframe], end: float,
                  stages: _Stages) -> IdentityResult:
        pipeline = self.pipeline
        newest = next((k for k in reversed(keyframes)
                       if k.analysis is not None and k.analysis.persons), None)
        persons = () if newest is None else tuple(
            (p.track_id, tuple(p.bbox)) for p in newest.analysis.persons)
        token = stages.start("S3_identity")
        calls = 0
        if newest is not None:
            worker = getattr(pipeline, "_identity_worker", None)
            if worker is not None:
                # Live: the recognition thread schedules its own re-checks.
                self._check = worker.observe(newest.image, persons, newest.at,
                                             settled=self._settled)
                calls = 1
            else:
                ids = {t for t, _ in persons if t is not None}
                interval = float(option("identity_interval_seconds", self.cfg))
                due = (self._identity_at is None or end - self._identity_at >= interval
                       or bool(ids - self._identity_tracks))
                if due:
                    identity = pipeline.identity
                    self._check = FaceCheck(tuple(identity.match_faces(newest.image)),
                                            persons, newest.at,
                                            degraded=not getattr(identity, "available", False))
                    self._identity_at, self._identity_tracks = end, ids
                    calls = 1
        result = pipeline.track_identity.update(self._check, persons, end)
        self._settled = result.authorized
        if newest is None:
            stages.skip("S3_identity", "no persons")
        else:
            stages.done(token, calls, "" if calls else "verdict held")
        return result

    # ── S4 ────────────────────────────────────────────────────────────────────
    def _action(self, clip: Clip, keyframes: list[Keyframe], person_recent,
                stages: _Stages) -> tuple[list[ClipObservation], float | None]:
        violence = getattr(self.pipeline, "violence", None)
        if violence is None or not getattr(violence, "available", False):
            stages.skip("S4_action", "no action model (the scene classifier's "
                                     "assault class covers it)")
            return [], None
        most = max((len(k.analysis.persons) for k in keyframes if k.analysis), default=0)
        if most < 2:
            stages.skip("S4_action", "fewer than 2 persons")
            return [], None
        if clip.motion_peak < float(option("action_motion_floor", self.cfg)):
            stages.skip("S4_action", "low motion")
            return [], None
        token = stages.start("S4_action")
        observations, best = [], None
        mid = clip.start + (clip.end - clip.start) / 2.0
        per_half = max(2, int(settings.windows.violence_frames) // 2)
        violence.reset()
        for lo, hi in ((clip.start, mid), (mid, clip.end)):
            half = [f for f in clip.frames if lo < f.at <= hi]
            if len(half) < 2:
                continue
            picks = np.linspace(0, len(half) - 1, min(per_half, len(half))).astype(int)
            for i in picks:
                violence.observe(half[i].image, half[i].at)
            score, _ = violence.result(hi)
            if getattr(violence, "fresh", True):
                observations.append(ClipObservation("violence", "assault", float(score),
                                                    float(violence.threshold), hi, None,
                                                    person_recent(hi)))
                best = score if best is None else max(best, score)
        stages.done(token, len(observations))
        return observations, best

    # ── S5 ────────────────────────────────────────────────────────────────────
    def _audio(self, clip: Clip, stages: _Stages) -> dict:
        token = stages.start("S5_audio")
        engine = getattr(self.pipeline, "audio", None)
        rms = clip.audio_rms
        if rms is not None:
            if rms < settings.windows.audio_rms_floor:
                out = {"score": 0.0, "label": "normal_ambient", "rms": round(rms, 5),
                       "source": "clip (quiet)"}
            else:
                classify = (getattr(engine, "classify_window", None)
                            or getattr(engine, "_classify", None))
                if classify is None:
                    out = {"score": 0.0, "label": "unclassified", "rms": round(rms, 5),
                           "source": "clip"}
                else:
                    result = classify(clip.audio, rms)
                    out = {"score": float(result.score), "label": result.label,
                           "rms": round(rms, 5), "source": "clip"}
        elif engine is not None:
            # The audio engine owns the microphone and classifies its own 4 s
            # window every second: the same window length as the clip.
            result = engine.result()
            out = {"score": float(result.score), "label": result.label, "rms": None,
                   "source": "audio engine"}
        else:
            out = {"score": 0.0, "label": "disabled", "rms": None, "source": None}
        stages.done(token, 1 if out["source"] == "clip" else 0)
        return out

    # ── votes ─────────────────────────────────────────────────────────────────
    def _observations(self, keyframes: list[Keyframe], person_recent) -> list[ClipObservation]:
        weapon_floor, fire_floor = self.detection.weapon_conf, self.detection.fire_conf
        out: list[ClipObservation] = []
        for k in keyframes:
            a = k.analysis
            if a is None:
                continue
            gate = person_recent(k.at)
            # A zero-valued row per keyframe so "seen" counts every keyframe
            # the detector looked at, not only those with a box.
            out.append(ClipObservation("detector", "weapon", 0.0, weapon_floor, k.at, None, gate))
            out.extend(ClipObservation("detector", "weapon", w.confidence, weapon_floor,
                                       k.at, g, gate) for w, g in zip(a.weapons, a.weapon_groups))
            out.append(ClipObservation("detector", "fire", 0.0, fire_floor, k.at, None, gate))
            out.extend(ClipObservation("detector", "fire", f.confidence, fire_floor, k.at, g, gate)
                       for f, g in zip(a.fires, a.fire_groups))
            if a.scene is not None:
                out.extend(ClipObservation("scene", c, float(a.scene.probs[c]),
                                           float(a.scene.thresholds.get(c, 1.0)), k.at, None,
                                           gate)
                           for c in PRIORITY if c in a.scene.probs)
        return out

    def _person_gate(self, keyframes: list[Keyframe]):
        analysed = [k.analysis for k in keyframes if k.analysis is not None]
        known = all(a.detections.person_ok for a in analysed)
        times = sorted(a.at for a in analysed if a.persons)
        earlier = self.last_person_at
        memory = self.threat.person_memory_seconds

        def person_recent(t: float) -> bool | None:
            if not known:
                return None
            if earlier is not None and 0.0 <= t - earlier <= memory:
                return True
            return any(0.0 <= t - p <= memory for p in times)

        return person_recent, (times[-1] if times else None)

    @staticmethod
    def _trigger(evidence: dict, observations: list[ClipObservation],
                 keyframes: list[Keyframe]) -> Keyframe | None:
        """The keyframe the decision is about: the strongest crossing of the
        leading class, else the newest analysed keyframe."""
        by_time = {k.at: k for k in keyframes if k.analysis is not None}
        cls, score = strongest(evidence)
        if cls is not None and score > 0:
            crossing = [o for o in observations if o.cls == cls and o.value >= o.threshold
                        and 0 < o.threshold < 1 and o.at in by_time]
            if crossing:
                best = max(crossing, key=lambda o: ((o.value - o.threshold) / (1 - o.threshold),
                                                    o.at))
                return by_time[best.at]
        return by_time[max(by_time)] if by_time else None

    # ── the clip ──────────────────────────────────────────────────────────────
    def analyse(self, clip: Clip, level: int = 1) -> ClipScores:
        stages = _Stages()
        scores = ClipScores(clip.seq, clip.start, clip.end, frames=len(clip.frames),
                            fallback_image=clip.frames[-1].image if clip.frames else None)
        cfg = self.cfg
        token = stages.start("S0_gate")
        open_, reasons = self._gate(clip, level)
        scores.gate = {"open": open_, "reasons": reasons,
                       "motion_peak": round(clip.motion_peak, 4),
                       "motion_mean": round(clip.motion_mean, 4),
                       "audio_rms": None if clip.audio_rms is None else round(clip.audio_rms, 5),
                       "events": len(clip.events)}
        stages.done(token)

        keyframes: list[Keyframe] = []
        violence_obs: list[ClipObservation] = []
        violence_score = None
        if open_:
            self._last_full_at = clip.end
            keyframes = select_keyframes(
                clip.frames, clip.end, float(cfg.window_seconds), int(cfg.uniform_keyframes),
                int(cfg.motion_keyframes),
                eligible=lambda f: f.seq in self._cache or f.seq > self._last_tracked_seq)
            self._objects(keyframes, stages)
            self._scene(keyframes, float(cfg.window_seconds), stages)
        else:
            for name in ("S1_objects", "S2_scene"):
                stages.skip(name, "gate closed")
        person_recent, newest_person = self._person_gate(keyframes)
        identity = self._identity(keyframes, clip.end, stages)
        if open_:
            violence_obs, violence_score = self._action(clip, keyframes, person_recent, stages)
        else:
            stages.skip("S4_action", "gate closed")
        audio = self._audio(clip, stages)

        token = stages.start("S6_fusion")
        observations = self._observations(keyframes, person_recent) + violence_obs
        evidence = self.confirmation.update(observations)
        scores.evidence = evidence
        scores.spans = {c: evidence_span(e) for c, e in evidence.items()}
        scores.keyframes = keyframes
        scores.trigger = self._trigger(evidence, observations, keyframes)
        floor = float(option("flame_fraction", cfg))
        analysed = [k for k in keyframes if k.analysis is not None]
        if any(k.analysis.persons or k.analysis.fire_candidate(floor) for k in analysed):
            self._last_activity_at = clip.end
        if newest_person is not None:
            self.last_person_at = max(newest_person, self.last_person_at or newest_person)
        scores.features = self._features(clip, keyframes, evidence, identity, audio,
                                         person_recent, violence_score, scores)
        self.last_features, self.last_identity = scores.features, identity
        self._prune(clip.start)
        stages.done(token, 1)
        scores.stages = stages.data
        return scores

    def _features(self, clip: Clip, keyframes: list[Keyframe], evidence: dict,
                  identity: IdentityResult, audio: dict, person_recent,
                  violence_score, scores: ClipScores) -> FeatureVector:
        pipeline = self.pipeline
        analysed = [k for k in keyframes if k.analysis is not None]
        hop = float(self.cfg.hop_seconds)
        newest = analysed[-1] if analysed else None
        recent = [k for k in analysed if k.at > clip.end - hop] or analysed[-1:]
        person_present = any(k.analysis.persons for k in recent)
        persons_now = newest.analysis.persons if newest is not None else []
        scores.persons = max((len(k.analysis.persons) for k in analysed), default=0)

        assault, robbery = evidence["assault"], evidence["robbery"]
        behaviour = pipeline.behaviour
        new = [k for k in analysed if not k.cached]
        shape = (newest.image.shape if newest is not None
                 else (scores.image.shape if scores.image is not None else (480, 640, 3)))
        for k in new[:-1]:
            behaviour.classify(k.analysis.persons, k.image.shape, None, k.at)
        last = new[-1] if new else None
        behaviour_score, behaviour_label = behaviour.classify(
            last.analysis.persons if last is not None else [], shape,
            (assault.score, "fighting" if assault.confirmed else "normal"),
            last.at if last is not None else clip.end,
            robbery=(robbery.score, "robbery" if robbery.confirmed else "normal"))

        for person in persons_now:
            x1, y1, x2, y2 = person.bbox
            crop = newest.image[max(0, y1):y2, max(0, x1):x2]
            person.global_id = pipeline.reid.match_or_register(pipeline.reid.embed(crop),
                                                               clip.end)

        trigger = scores.trigger
        shown = trigger if trigger is not None else newest
        detections = (_with_identity(shown.analysis.detections.to_dict(), identity.tracks)
                      if shown is not None else DetectionResult().to_dict())
        scored = [k for k in analysed if k.analysis.scene is not None]
        scene_dict = None
        if scored:
            pick = trigger if trigger is not None and trigger.analysis.scene is not None \
                else scored[-1]
            scene_dict = dict(pick.analysis.scene.to_dict(), keyframes_scored=len(scored))
        scores.scene = scene_dict
        scores.audio = audio
        scores.identity = {"authorized": identity.authorized, "name": identity.name,
                           "degraded": identity.degraded, "faces": identity.faces,
                           "matched": identity.matched}
        sources: dict[str, dict] = {}
        for cls, ev in evidence.items():
            for vote in ev.votes:
                sources.setdefault(vote.source, {})[cls] = (vote.value, vote.threshold, True)
        identity_score = pipeline.identity.score(identity, person_present)
        night_frame = newest.image if newest is not None else scores.image
        is_night = (1.0 if night_frame is not None and pipeline.motion.is_night(night_frame)
                    else 0.0)
        return FeatureVector(
            vision=max((p.confidence for k in analysed for p in k.analysis.persons),
                       default=0.0),
            audio=audio["score"], motion=clip.motion_peak, behaviour=behaviour_score,
            identity=identity_score, weapon=evidence["weapon"].score,
            fire=evidence["fire"].score, is_night=is_night,
            authorized=identity.authorized, person_present=person_present,
            behaviour_label=behaviour_label, audio_label=audio["label"],
            person_count=len(persons_now), identity_degraded=identity.degraded,
            extras={"detections": detections, "identity_name": identity.name,
                    "violence_score": (round(float(violence_score), 4)
                                       if violence_score is not None else None),
                    "scene": scene_dict,
                    "evidence": {c: e.to_dict() for c, e in evidence.items()},
                    "sources": sources, "person_recent": person_recent(clip.end),
                    "faces": identity.faces, "faces_matched": identity.matched,
                    "clip": {"seq": clip.seq, "start": round(clip.start, 3),
                             "end": round(clip.end, 3), "keyframes": len(keyframes),
                             "spans": {c: round(s, 3) for c, s in scores.spans.items()}}})

    def _prune(self, start: float) -> None:
        """Forget analyses no later window can contain (the next one starts a
        hop later)."""
        horizon = start + float(self.cfg.hop_seconds)
        for seq in [s for s, a in self._cache.items() if a.at <= horizon]:
            del self._cache[seq]


# ── level policy ──────────────────────────────────────────────────────────────
class ClipPolicy(LevelPolicy):
    """``LevelPolicy`` for one decision per clip.

    * L4/L5 commit on ``ClipConfig.min_escalate_observations`` clips (1): one
      clip already needed two confirming keyframes; the tick loop's "2 ticks"
      would make every L4 wait for a second clip, 2 s later.
    * The dwell (0.6 s for L4/L5) is measured inside the clip: before a
      decision the runner sets :attr:`evidence_span`, the seconds the
      confirming keyframes cover, and a threshold first crossed by that
      decision counts as held since ``now - span``. Two confirming keyframes
      1 s apart satisfy the dwell at once; two 0.1 s apart wait for the next
      clip, exactly as two ticks 0.1 s apart would.
    """

    def __init__(self, min_observations=None) -> None:
        super().__init__()
        n = len(self.cfg.thresholds)
        value = (settings.clip.min_escalate_observations if min_observations is None
                 else min_observations)
        self._min_obs = _per_threshold(value, n)
        self.evidence_span = 0.0

    def update(self, tci: float, now: float | None = None) -> LevelState:
        now = time.monotonic() if now is None else now
        span = min(max(float(self.evidence_span), 0.0), float(settings.clip.window_seconds))
        if span > 0:
            for i, threshold in enumerate(self.cfg.thresholds):
                if tci >= threshold and self._above_since[i] is None:
                    self._above_since[i], self._above_count[i] = now - span, 0
        return super().update(tci, now)


# ── the runner ────────────────────────────────────────────────────────────────
@dataclass
class _PostRoll:
    incident: Any
    until: float
    last_at: float
    frames: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)


def _subsample(frames: list[BufferedFrame], fps: float,
               after: float | None = None) -> list[tuple[float, np.ndarray]]:
    """Frames at most ``fps`` per second, oldest first."""
    out: list[tuple[float, np.ndarray]] = []
    period = 1.0 / fps if fps > 0 else 0.0
    last = after
    for f in frames:
        if last is None or f.at - last >= period - 1e-6:
            out.append((f.at, f.image))
            last = f.at
    return out


class ClipRunner:
    """Clip mode on one pipeline: buffer, scheduler, engine, fast lane.

    ``feed`` drives it synchronously (replay, tests); :class:`ClipStrategy`
    drives it from the live loop (frames on the capture thread, clips on the
    inference thread, the fast lane on its own). Every decision goes through
    one lock and pipeline time never runs backwards, whichever path decides.
    """

    def __init__(self, pipeline, *, cfg=None, engine: ClipEngine | None = None,
                 buffer: ClipBuffer | None = None, scheduler: ClipScheduler | None = None,
                 fast_lane: FastLane | bool | None = None, detector=None) -> None:
        self.pipeline = pipeline
        self.cfg = cfg or settings.clip
        self.buffer = buffer or ClipBuffer(self.cfg)
        self.scheduler = scheduler or ClipScheduler(self.cfg)
        self.engine = engine or ClipEngine(pipeline, detector=detector, cfg=self.cfg)
        if fast_lane is None:
            fast_lane = FastLane.build(self.cfg) if self.cfg.fast_lane else None
        self.fast_lane: FastLane | None = fast_lane if isinstance(fast_lane, FastLane) else None
        self._lock = threading.RLock()
        self.recent: deque[dict] = deque(maxlen=256)
        self.fast_decisions = 0
        self.reset_state()

    def reset_state(self) -> None:
        self._last_now = -math.inf
        self.level = 1
        self._captured: dict[str, int] = {}
        self._post_roll: _PostRoll | None = None
        self.last_scores: ClipScores | None = None

    def reset(self) -> None:
        """Clear every temporal state (between replayed videos)."""
        with self._lock:
            self.buffer.reset()
            self.scheduler.reset()
            self.engine.reset()
            if self.fast_lane is not None:
                self.fast_lane.reset()
            self.recent.clear()
            self.fast_decisions = 0
            self.reset_state()

    def enable_fast_lane(self, enabled: bool) -> bool:
        """Switch the fast lane on (building it) or off; returns whether it runs."""
        with self._lock:
            if not enabled:
                self.fast_lane = None
            elif self.fast_lane is None:
                self.fast_lane = FastLane.build(self.cfg)
            return self.fast_lane is not None

    # ── synchronous driving (replay) ──────────────────────────────────────────
    def feed(self, frame: np.ndarray, at: float, label: str | None = None) -> list[dict]:
        """Add one frame; run the clip that is due and the fast lane. Returns
        the decisions made (oldest first)."""
        kept = self.buffer.add_frame(frame, at)
        if kept is None:
            return []
        out = []
        end = self.scheduler.due(kept.at)
        if end is not None:
            out.append(self.run_clip(end, label))
        if self.fast_lane is not None and self.fast_lane.due(kept.seq):
            decision = self.run_fast_lane(kept, label)
            if decision is not None:
                out.append(decision)
        return out

    # ── one clip ──────────────────────────────────────────────────────────────
    def run_clip(self, end: float, label: str | None = None) -> dict:
        started = time.perf_counter()
        clip = self.buffer.clip(end, self.scheduler.clips)
        scores = self.engine.analyse(clip, level=self.level)
        decide_at = time.perf_counter()
        out = self._decide(scores.image, scores.features, clip.end, label, started,
                           span=scores.span, kind="clip", scores=scores)
        scores.stages["S6_fusion"]["ms"] = round(
            scores.stages["S6_fusion"]["ms"] + (time.perf_counter() - decide_at) * 1000.0, 2)
        compute = time.perf_counter() - started
        scores.compute_ms = compute * 1000.0
        scores.decision = {"level": out["level"], "tci": round(out["tci"], 4),
                           "incident_type": out["incident_type"],
                           "incident_id": out["incident_id"], "t": round(out["t"], 3),
                           "tick_seq": out.get("tick_seq")}
        self.scheduler.record(compute)
        self.last_scores = scores
        summary = {"seq": scores.seq, "end": round(clip.end, 3),
                   "gate_open": scores.gate.get("open"), "keyframes": len(scores.keyframes),
                   "compute_ms": round(scores.compute_ms, 1),
                   "stages": {k: v["ms"] for k, v in scores.stages.items()},
                   "level": out["level"], "incident_type": out["incident_type"]}
        self.recent.append(summary)
        events = getattr(self.pipeline, "events", None)
        if events is not None and hasattr(events, "write_clip"):
            try:
                events.write_clip(scores.to_dict(), decision_seq=out.get("tick_seq"),
                                  label=label)
            except Exception as exc:
                log.debug("clip row not written: %s", exc)
        out["clip"] = scores
        return out

    # ── one fast-lane pass ────────────────────────────────────────────────────
    def run_fast_lane(self, kept: BufferedFrame, label: str | None = None) -> dict | None:
        lane = self.fast_lane
        if lane is None:
            return None
        started = time.perf_counter()
        seen = lane.analyse(kept.image, kept.at, person_seen_at=self.engine.last_person_at)
        if not seen.hits:
            return None
        features = self._fast_lane_features(seen, kept)
        span = max((h.at - h.first_at for h in seen.hits), default=0.0)
        out = self._decide(kept.image, features, kept.at, label, started, span=span,
                           kind="fast_lane")
        self.fast_decisions += 1
        return out

    def _fast_lane_features(self, seen: FastLaneFrame, kept: BufferedFrame) -> FeatureVector:
        evidence = fast_lane_evidence(seen.hits)
        last = self.engine.last_features
        identity = self.engine.last_identity
        persons = [Detection("person", b.conf, b.bbox) for b in seen.persons]
        detections = DetectionResult(
            persons=persons,
            weapons=[Detection(b.label, b.conf, b.bbox) for b in seen.boxes.get("weapon", [])],
            fires=[Detection(b.label, b.conf, b.bbox) for b in seen.boxes.get("fire", [])])
        sources = {"fastlane": {c: (v.value, v.threshold, True)
                                for c, e in evidence.items() for v in e.votes}}
        present = bool(persons) or bool(seen.person_recent)
        return FeatureVector(
            vision=max((p.confidence for p in persons), default=0.0),
            audio=last.audio if last is not None else 0.0, motion=kept.motion,
            behaviour=last.behaviour if last is not None else 0.0,
            identity=self.pipeline.identity.score(identity, present),
            weapon=evidence["weapon"].score, fire=evidence["fire"].score,
            is_night=last.is_night if last is not None else 0.0,
            authorized=identity.authorized, person_present=present,
            behaviour_label=last.behaviour_label if last is not None else "normal",
            audio_label=last.audio_label if last is not None else "disabled",
            person_count=len(persons), identity_degraded=identity.degraded,
            extras={"detections": detections.to_dict(), "identity_name": identity.name,
                    "violence_score": None,
                    "scene": (last.extras.get("scene") if last is not None else None),
                    "evidence": {c: e.to_dict() for c, e in evidence.items()},
                    "sources": sources, "person_recent": seen.person_recent,
                    "faces": None, "faces_matched": None,
                    "fast_lane": {"hits": [h.to_dict() for h in seen.hits],
                                  "ms": round(seen.ms, 1)}})

    # ── the decision (S6) ─────────────────────────────────────────────────────
    def _decide(self, frame, features: FeatureVector, now: float, label, started: float,
                span: float = 0.0, kind: str = "clip", scores: ClipScores | None = None) -> dict:
        pipeline = self.pipeline
        if frame is None:
            frame = np.zeros((480, 640, 3), np.uint8)
        with self._lock:
            now = max(float(now), self._last_now)
            self._last_now = now
            closing = getattr(pipeline, "_close_requested", None) is not None
            policy = pipeline.policy
            if isinstance(policy, ClipPolicy):
                policy.evidence_span = span
            try:
                extra = {"mode": kind, "clip": scores.seq if scores is not None else None}
                out = pipeline.decide(frame, features, now, label, started=started,
                                      extra=extra)
            finally:
                if isinstance(policy, ClipPolicy):
                    policy.evidence_span = 0.0
            events = getattr(pipeline, "events", None)
            rows = getattr(events, "rows", None)
            out["tick_seq"] = rows - 1 if isinstance(rows, int) and rows > 0 else None
            out["t"], out["kind"] = now, kind
            self.level = out["level"]
            if closing:
                # request_close_incident: the next scene is confirmed afresh.
                self.engine.forget()
                if self.fast_lane is not None:
                    self.fast_lane.reset()
            self._evidence(features, now, kind, scores)
            return out

    # ── evidence clips (pre-roll, post-roll) ──────────────────────────────────
    def _evidence(self, features: FeatureVector, now: float, kind: str,
                  scores: ClipScores | None) -> None:
        """Seal the clip around an evidence capture the decision just made.

        ``Pipeline._act`` sealed one frame (``incident.evidence_count`` went
        up); the ring buffer adds the seconds before it now and the seconds
        after it once they have been captured. Everything goes through
        ``Pipeline._submit`` - the FIFO side-effect worker - so a measurement
        pipeline, whose ``_submit`` is a no-op, never writes a bundle.
        """
        incident = getattr(self.pipeline.incidents, "current", None)
        if incident is not None:
            before = self._captured.get(incident.id, 0)
            if incident.evidence_count > before:
                self._captured = {incident.id: incident.evidence_count}
                self._flush_post_roll(force=True)
                fps = float(option("evidence_fps", self.cfg))
                pre = min(float(settings.evidence.pre_roll_seconds), self.buffer.keep)
                frames = _subsample(self.buffer.frames_between(now - pre, now), fps)
                peak = incident.peak
                meta = {"level": max(incident.peak_level, self.level),
                        "status": settings.policy.names.get(self.level, "UNKNOWN"),
                        "tci": peak.tci, "incident_type": peak.incident_type,
                        "reason": peak.reason, "features": dict(peak.features),
                        "incident_id": incident.id, "trigger_at": now,
                        "context": {"session": getattr(self.pipeline, "session_id", None),
                                    "mode": kind,
                                    "clip": scores.seq if scores is not None else None,
                                    "trigger": (features.extras or {}).get("evidence")}}
                if frames:
                    self.pipeline._submit(self._seal, incident, frames, meta, "pre_roll")
                post = float(option("post_roll_seconds", self.cfg))
                if post > 0:
                    self._post_roll = _PostRoll(incident, now + post, now, meta=meta)
        self._flush_post_roll()

    def _flush_post_roll(self, force: bool = False) -> None:
        pending = self._post_roll
        if pending is None:
            return
        latest = self.buffer.latest_at
        if latest is not None and latest > pending.last_at:
            fps = float(option("evidence_fps", self.cfg))
            upto = min(latest, pending.until)
            last = pending.frames[-1][0] if pending.frames else None
            pending.frames.extend(_subsample(self.buffer.frames_between(pending.last_at, upto),
                                             fps, after=last))
            pending.last_at = upto
        if force or (latest is not None and latest >= pending.until):
            self._post_roll = None
            if pending.frames:
                self.pipeline._submit(self._seal, pending.incident, pending.frames,
                                      pending.meta, "post_roll")

    @staticmethod
    def _seal(incident, frames, meta: dict, kind: str) -> None:
        """Worker thread: one clip bundle in the vault's manifest chain."""
        from sentrix.evidence.vault import vault
        record = vault.save_clip(frames, kind=kind, **meta)
        if record is not None:
            incident.actions.add(f"evidence_{kind}")

    # ── reporting ─────────────────────────────────────────────────────────────
    def status(self) -> dict:
        """The ``clip`` block of the live state (dashboard, Node panel)."""
        from sentrix.runtime import resolve_backend
        last = self.recent[-1] if self.recent else None
        return {"mode": "clip", "window_s": float(self.cfg.window_seconds),
                "hop_s": float(self.cfg.hop_seconds), **self.scheduler.stats(),
                "runtime": resolve_backend(), "threads": int(self.cfg.threads),
                "fast_lane": self.fast_lane.stats() if self.fast_lane is not None else None,
                "fast_lane_decisions": self.fast_decisions, "last": last}

    def run_flush(self) -> None:
        """Seal a pending post-roll now (end of a replay or shutdown)."""
        with self._lock:
            self._flush_post_roll(force=True)


# ── live strategy ─────────────────────────────────────────────────────────────
class ClipStrategy:
    """``InferenceStrategy`` for clip mode (``SENTRIX_PIPELINE_MODE=clip``).

    ``on_frame`` (capture thread) only buffers - a downscale, a thumbnail and a
    difference - and hands every Nth frame to the fast lane. ``run`` analyses
    one clip per hop on the inference thread; the fast lane has its own thread.
    """

    name = "clip"

    def __init__(self, pipeline, runner: ClipRunner | None = None) -> None:
        self.pipeline = pipeline
        self.runner = runner or ClipRunner(pipeline)
        self._cond = threading.Condition()
        self._fast_cond = threading.Condition()
        self._fast_slot: BufferedFrame | None = None
        self.fast_dropped = 0

    def on_frame(self, frame: np.ndarray, captured_at: float) -> None:
        kept = self.runner.buffer.add_frame(frame, captured_at)
        if kept is None:
            return
        with self._cond:
            self._cond.notify()
        lane = self.runner.fast_lane
        if lane is not None and lane.due(kept.seq):
            with self._fast_cond:
                if self._fast_slot is not None:
                    self.fast_dropped += 1          # newest frame wins
                self._fast_slot = kept
                self._fast_cond.notify()

    def wake(self) -> None:
        with self._cond:
            self._cond.notify_all()
        with self._fast_cond:
            self._fast_cond.notify_all()

    def _on_audio(self, samples: np.ndarray, end_at: float) -> None:
        self.runner.buffer.add_audio(samples, end_at)

    def run(self, stop: threading.Event) -> None:
        fast = None
        audio = getattr(self.pipeline, "audio", None)
        listen = getattr(audio, "add_sample_listener", None)
        if callable(listen):
            # S5 then classifies the clip's own window, not the engine's.
            listen(self._on_audio)
        if self.runner.fast_lane is not None:
            fast = threading.Thread(target=self._fast_loop, args=(stop,), daemon=True,
                                    name="sentrix-fastlane")
            fast.start()
        try:
            while not stop.is_set():
                with self._cond:
                    end = self.runner.scheduler.due(self.runner.buffer.latest_at)
                    if end is None:
                        self._cond.wait(timeout=0.25)
                        continue
                try:
                    self.runner.run_clip(end)
                except Exception as exc:
                    log.error("clip analysis failed: %s", exc, exc_info=True)
                    continue
                self.pipeline.note_inference(end)
                state.update(clip=self.runner.status())
        finally:
            unlisten = getattr(audio, "remove_sample_listener", None)
            if callable(unlisten):
                unlisten(self._on_audio)
            self.wake()
            if fast is not None:
                fast.join(timeout=5.0)

    def _fast_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            with self._fast_cond:
                if self._fast_slot is None:
                    self._fast_cond.wait(timeout=0.25)
                job, self._fast_slot = self._fast_slot, None
            if job is None:
                continue
            try:
                self.runner.run_fast_lane(job)
            except Exception as exc:
                log.error("fast lane failed: %s", exc, exc_info=True)


_REQUIRED = ("detector", "scene", "identity", "track_identity", "behaviour", "reid",
             "motion", "tci", "policy", "incidents", "events")


def _runtime_detector():
    """A detector on exported heads when ``SENTRIX_RUNTIME`` is not torch."""
    from sentrix.runtime import resolve_backend
    if resolve_backend() == "torch":
        return None
    from sentrix.runtime.detector import RuntimeDetector
    detector = RuntimeDetector.build()
    if not detector.available:
        log.error("no runtime person head; clip mode keeps the torch detector")
        return None
    log.info("clip engine detector on %s", detector.backends)
    return detector


def build_strategy(pipeline):
    """The clip-mode ``InferenceStrategy`` for ``Pipeline.run_live`` (docs/7 §2.4).

    Holds torch and OpenCV to ``ClipConfig.threads``, installs the
    :class:`ClipPolicy`, and falls back to the tick loop on a pipeline without
    the perception components the engine needs.
    """
    missing = [name for name in _REQUIRED if not hasattr(pipeline, name)]
    if missing:
        from sentrix.pipeline.engine import TickStrategy
        log.error("clip mode needs the pipeline's %s; running the tick loop",
                  ", ".join(missing))
        return TickStrategy(pipeline)
    from sentrix.runtime import configure_threads
    report = configure_threads(settings.clip.threads)
    if not isinstance(pipeline.policy, ClipPolicy):
        pipeline.policy = ClipPolicy()
    runner = ClipRunner(pipeline, detector=_runtime_detector())
    pipeline.clip_runner = runner
    log.info("clip mode: %g s window every %g s, %d keyframes, threads %s, fast lane %s",
             settings.clip.window_seconds, settings.clip.hop_seconds, settings.clip.keyframes,
             report, "on" if runner.fast_lane is not None else "off")
    return ClipStrategy(pipeline, runner)


def prepare_eval_pipeline(pipeline):
    """Clip mode on the harness's measuring pipeline (docs/7 §2.4).

    Adds the :class:`ClipPolicy` and a :class:`ClipRunner` as
    ``pipeline.clip_runner``. Nothing the harness disabled is touched
    (``_act``, ``_submit``, ``_save_open_incident``, the null SOS service,
    neutral identity, no microphone); the runner's only side effect, sealing
    evidence clips, goes through ``pipeline._submit``, which the harness has
    made a no-op. The fast lane follows ``SENTRIX_FAST_LANE`` (off by
    default); :meth:`ClipRunner.enable_fast_lane` switches it per run.
    """
    pipeline.policy = ClipPolicy()
    pipeline.clip_runner = ClipRunner(pipeline, detector=_runtime_detector())
    return pipeline


# ── replay ────────────────────────────────────────────────────────────────────
def _row(t: float, decision: dict | None) -> dict:
    from sentrix.evaluation.custom_dataset import incident_to_class
    from sentrix.evaluation.harness import SOURCE_COLUMNS
    if decision is None:
        return {"t": round(t, 3), "tci": 0.0, "level": 1, "incident_type": "normal",
                "pred": "normal", "weapon": 0.0, "fire": 0.0, "behaviour": 0.0,
                "latency_ms": 0.0, "decision_t": None, "kind": "none"}
    features = decision.get("features") or {}
    row = {"t": round(t, 3), "tci": round(decision["tci"], 4), "level": decision["level"],
           "incident_type": decision["incident_type"],
           "pred": incident_to_class(decision["incident_type"]),
           "weapon": round(features.get("weapon", 0.0), 4),
           "fire": round(features.get("fire", 0.0), 4),
           "behaviour": round(features.get("behaviour", 0.0), 4),
           "latency_ms": round(decision["latency"] * 1000, 1),
           "decision_t": round(decision["t"], 3), "kind": decision.get("kind", "clip")}
    for source, per_class in (decision.get("sources") or {}).items():
        for cls, vote in per_class.items():
            column = f"{source}_{cls}"
            if column in SOURCE_COLUMNS:
                row[column] = round(float(vote[0]), 4)
    return row


def replay_video_clip(pipeline, path, fps: float | None = None,
                      on_frame: Callable[[float, dict], dict] | None = None,
                      max_seconds: float | None = None, period: float | None = None,
                      fast_lane: bool | None = None,
                      on_decision: Callable[[dict], None] | None = None) -> list[dict]:
    """Replay one video in clip mode; rows on the tick grid, like ``replay_video``.

    Every decoded frame goes to the clip buffer at its true timestamp (the
    buffer keeps ``buffer_fps``); clips and fast-lane passes run as their
    times come. Rows are emitted every ``period`` (the tick replay's rate) and
    show the committed state at that instant - the latest decision at or
    before it - so episode, latency and false-onset metrics compare directly
    with the per-frame replay. A decision is stamped at its clip's end:
    compute time is not added (``tools/bench_clip.py`` measures it).
    """
    import cv2 as _cv2

    runner: ClipRunner = getattr(pipeline, "clip_runner", None)
    if runner is None:
        runner = prepare_eval_pipeline(pipeline).clip_runner
    if fast_lane is not None:
        runner.enable_fast_lane(fast_lane)
    cap = _cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise FileNotFoundError(f"cannot open video {path}")
    native = fps or float(cap.get(_cv2.CAP_PROP_FPS) or 25.0)
    period = period or settings.pipeline.inference_period

    pipeline.reset()
    runner.reset()
    rows: list[dict] = []
    latest: dict | None = None
    pending: deque[dict] = deque()
    index, next_tick, t = 0, 0.0, 0.0

    def emit(upto: float) -> None:
        nonlocal next_tick, latest
        while next_tick <= upto + 1e-9:
            while pending and pending[0]["t"] <= next_tick + 1e-9:
                latest = pending.popleft()
            row = _row(next_tick, latest)
            if on_frame is not None:
                row = on_frame(next_tick, row) or row
            rows.append(row)
            next_tick += period

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            t = index / native
            index += 1
            if max_seconds is not None and t > max_seconds:
                break
            for decision in runner.feed(frame, t):
                if on_decision is not None:
                    on_decision(decision)
                decision.pop("clip", None)
                pending.append(decision)
            emit(t)
    finally:
        cap.release()
        runner.run_flush()
    return rows


def read_clip_bundle(bundle_id: str, vault=None) -> list[tuple[float, bytes]] | None:
    """The JPEG frames of a clip bundle, ``[(seconds from trigger, jpeg)]``."""
    import json as _json

    from sentrix.evidence.vault import vault as default_vault
    vault = vault or default_vault
    meta_path = vault.dir / f"{bundle_id}.json"
    payload = vault.decrypt(bundle_id)
    if payload is None or not meta_path.exists():
        return None
    try:
        meta = _json.loads(meta_path.read_text(encoding="utf-8"))
        frames = meta["extras"]["clip"]["frames"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return [(float(f["t"]), payload[f["offset"]:f["offset"] + f["length"]]) for f in frames]


__all__ = ["BufferedFrame", "Clip", "ClipBuffer", "ClipEngine", "ClipPolicy", "ClipRunner",
           "ClipScheduler", "ClipScores", "ClipStrategy", "DEFAULTS", "FrameAnalysis",
           "Keyframe", "SensorEvent", "build_strategy", "evidence_span", "flame_fraction",
           "motion_energy", "option", "prepare_eval_pipeline", "read_clip_bundle",
           "replay_video_clip", "select_keyframes"]
