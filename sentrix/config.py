"""Single source of truth for every tunable in SENTRIX.

Design rule: **all temporal parameters are expressed in seconds, never in frames.**

The previous implementation mixed the two (``alpha = 0.3`` per frame, ``window = 5``
frames, ``LOITER_RADIUS = 100`` pixels). That silently couples behaviour to the
inference rate and the camera resolution: changing ``inference_skip`` from 6 to 3
halved the smoothing time constant without anyone noticing, and the loiter radius
meant something different on a 640x480 webcam than on a 1080p CCTV feed.

Everything here is either seconds, or a fraction of the frame diagonal, so the
system behaves identically at 5 FPS on a laptop and at 30 FPS on an Orange Pi.

Values are sourced from video-analytics practice. The rationale for each block is
in the "Configuration: the numbers, and why" section of ``README.md``, and each
dataclass below repeats the reasoning that matters for its own fields.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from typing import Any


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# ──────────────────────────────────────────────────────────────────────────────
# Pipeline timing
# ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PipelineConfig:
    """How often we capture, and how often we actually run the neural stack.

    Capture runs fast so the MJPEG preview stays smooth; inference runs slower
    because 30 FPS of YOLO buys nothing for threat detection. Human actions play
    out over hundreds of milliseconds, so 5-8 Hz of inference is the standard
    operating point for video analytics and leaves headroom on edge silicon.
    """

    capture_fps: float = _env_float("SENTRIX_CAPTURE_FPS", 30.0)
    inference_fps: float = _env_float("SENTRIX_INFERENCE_FPS", 6.0)

    #: Hard ceiling on one pipeline pass. Exceeding it is logged, not fatal.
    frame_budget_seconds: float = 0.20

    #: Dashboard websocket push rate.
    telemetry_fps: float = 2.0

    @property
    def inference_period(self) -> float:
        return 1.0 / max(self.inference_fps, 0.1)

    @property
    def capture_period(self) -> float:
        return 1.0 / max(self.capture_fps, 1.0)


# ──────────────────────────────────────────────────────────────────────────────
# Detection (YOLOv8)
# ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class DetectionConfig:
    """Confidence gates for the unified YOLOv8 detector.

    One detector instance serves person, weapon and fire/smoke. The previous code
    loaded ``yolov8n.pt`` twice (VisionEngine and CloudThreatEngine) plus two
    custom weights: four model loads for three jobs.

    Thresholds are asymmetric on purpose. Person detection is cheap to get wrong
    in either direction, so it sits at the usual 0.35. Weapon and fire feed hard
    overrides that skip fusion entirely and can trigger a siren, so they need a
    much higher bar than the 0.25 the previous build shipped with.
    """

    person_conf: float = _env_float("SENTRIX_PERSON_CONF", 0.35)
    #: 0.35 for the fine-tuned v4 weapon head, which scores lower than v3 did:
    #: on the validation frames 0.35 keeps zero false positives while lifting
    #: weapon recall from 0.20 (at 0.55) to 0.31. This is the *detection*
    #: floor only — the siren-capable weapon override is still
    #: ``FusionConfig.weapon_override_high`` (0.55), so a 0.35-0.55 detection
    #: types a weapon incident without forcing the TCI to HIGH.
    weapon_conf: float = _env_float("SENTRIX_WEAPON_CONF", 0.35)
    fire_conf: float = _env_float("SENTRIX_FIRE_CONF", 0.50)
    iou: float = 0.45
    imgsz: int = 640
    max_detections: int = 50

    #: Tracker config shipped with ultralytics. ByteTrack is lighter than
    #: BoT-SORT and does not need a re-id backbone, which suits edge hardware.
    tracker: str = "bytetrack.yaml"

    #: A detection whose box covers more than this fraction of the frame is
    #: almost always a false positive from a texture that fills the view.
    max_box_area_fraction: float = 0.85
    #: ...and below this it is too small to be trustworthy at 640px.
    min_box_area_fraction: float = 0.0008


# ──────────────────────────────────────────────────────────────────────────────
# Temporal windows — the "sliding window" question
# ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class WindowConfig:
    """Sliding-window geometry for the two temporal modalities.

    **Violence / action recognition.** A single 224x224 still cannot tell a hug
    from a shove; the discriminative signal is motion. The action-recognition
    literature converged on ~2-3 second clips sampled to a fixed frame count
    (I3D uses 64 frames at 25 FPS = 2.56 s; VideoMAE and TimeSformer use 16
    frames; RWF-2000, which this project trains on, is built from 5 s clips).
    We take 16 frames spanning 2.0 s and re-score every 1.0 s, i.e. 50 % overlap.
    Overlap matters: an action that straddles a window boundary is still caught
    whole by the neighbouring window.

    **Audio.** Two timescales are in tension. Impulsive events (gunshot, glass
    break) need a short hop or they are missed entirely; tonal events (siren,
    alarm) need a long window to be identifiable at all. The V3 preprocessing
    contract fixes the window at 4.0 s, so we keep that and shorten the hop to
    1.0 s (75 % overlap). That yields a 1 Hz decision rate with 4 s of context,
    and guarantees any impulsive event lands fully inside at least one window.
    """

    # Violence
    violence_window_seconds: float = 2.0
    violence_hop_seconds: float = 1.0
    violence_frames: int = 16          # frames sampled across the window
    violence_image_size: int = 224

    # Audio — must match models/v3_real/PRODUCTION_PREPROCESSING_CONTRACT.json
    audio_window_seconds: float = 4.0
    audio_hop_seconds: float = 1.0
    audio_sample_rate: int = 16000
    audio_n_mels: int = 64
    audio_n_fft: int = 1024
    audio_hop_length: int = 256
    audio_fmin: int = 20
    audio_fmax: int = 8000
    audio_top_db: int = 80

    #: RMS below this is treated as an empty room; the CNN is not run at all.
    #: Cheap, and it removes the single largest source of acoustic false alarms.
    audio_rms_floor: float = _env_float("SENTRIX_AUDIO_RMS_FLOOR", 0.020)

    @property
    def violence_buffer_len(self) -> int:
        """Frames to retain, given the pipeline inference rate."""
        return max(self.violence_frames, 2)


# ──────────────────────────────────────────────────────────────────────────────
# Scene classification (normal / fire / weapon / assault / robbery)
# ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SceneConfig:
    """The five-class scene classifier and how its output meets the detectors.

    ``window_seconds`` averages class probabilities over the last few ticks
    (three at 6 Hz). One ambiguous frame then cannot flip the incident type,
    and a weapon brandished for a second is still caught; the TCI's EMA and
    hysteresis apply on top.

    ``fusion`` decides how the classifier's weapon and fire scores combine with
    the YOLO detectors': ``max`` takes the stronger of the two, ``scene`` lets
    the classifier alone drive them (boxes are still drawn), ``detector``
    ignores the classifier for weapon and fire. Assault and robbery always come
    from the classifier — no detector produces them. The default was chosen on
    the validation videos; see ``docs/6_CUSTOM_DATASET_EVALUATION.md``.
    """

    enabled: bool = _env_bool("SENTRIX_SCENE_ENABLED", True)
    window_seconds: float = _env_float("SENTRIX_SCENE_WINDOW", 0.5)
    fusion: str = os.getenv("SENTRIX_SCENE_FUSION", "max").strip().lower() or "max"


# ──────────────────────────────────────────────────────────────────────────────
# Behaviour / kinematics
# ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class BehaviourConfig:
    """Trajectory heuristics, expressed resolution-independently.

    Speeds are in *frame diagonals per second* rather than pixels per frame, so
    the same value works on a 640x480 webcam and a 1080p camera. Loiter dwell
    follows the video-analytics convention of 30-60 s stationary in a zone.
    """

    #: Fraction of the frame diagonal traversed per second.
    run_speed: float = 0.45
    walk_speed: float = 0.12

    #: h/w below this means the person is horizontal — crawling or fallen.
    crawl_aspect_ratio: float = 0.60

    loiter_dwell_seconds: float = _env_float("SENTRIX_LOITER_SECONDS", 30.0)
    #: Radius counted as "the same place", as a fraction of the frame diagonal.
    loiter_radius: float = 0.08

    #: Drop per-track history this long after the track was last seen.
    track_memory_seconds: float = 10.0

    scores: dict = field(default_factory=lambda: {
        "fighting": 0.90,
        "robbery": 0.85,
        "crawling": 0.70,
        "running": 0.65,
        "loitering": 0.55,
        "walking": 0.20,
        "normal": 0.05,
    })


# ──────────────────────────────────────────────────────────────────────────────
# Identity
# ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class IdentityConfig:
    """Face authorisation.

    ``fail_closed`` is the most important flag in this file. Recognising a
    resident forces TCI to a floor value and suppresses *all* evidence capture
    and alerting, so a weak matcher is not a weak feature — it is an
    authentication bypass. When no real face-embedding backend is installed the
    system must report "not authorised" rather than fall back to comparing
    colour histograms, which is what the previous build did.
    """

    fail_closed: bool = _env_bool("SENTRIX_IDENTITY_FAIL_CLOSED", True)

    #: Cosine distance for deep 128-d/512-d embeddings.
    match_threshold: float = _env_float("SENTRIX_FACE_THRESHOLD", 0.45)

    #: Hold the authorised verdict this long after the last confirmed match, so
    #: a resident turning their head does not re-trigger the alarm.
    auth_hold_seconds: float = 5.0

    #: Identity score fed to fusion when an unknown person is present.
    unknown_person_score: float = 0.60


@dataclass(frozen=True)
class ReIDConfig:
    """Appearance re-identification across views.

    Embeddings are L2-normalised, so the only meaningful comparison is cosine
    similarity in [-1, 1]. The previous build compared them against an L2
    *distance* threshold of 120.0 — unreachable for unit vectors, whose maximum
    separation is 2.0 — so every person collapsed onto a single global id.
    """

    match_similarity: float = 0.72
    gallery_size: int = 200
    #: Forget a gallery entry that has not been matched for this long.
    gallery_ttl_seconds: float = 600.0


# ──────────────────────────────────────────────────────────────────────────────
# Fusion — the Threat Confidence Index
# ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class FusionConfig:
    """TCI computation.

    ``ema_tau_seconds`` replaces the old per-frame ``alpha``. The relationship
    is ``alpha = 1 - exp(-dt / tau)``, recomputed from the true elapsed time on
    every tick, so smoothing is identical whether inference runs at 3 Hz or
    30 Hz. The old fixed ``alpha = 0.30`` at 5 Hz corresponded to tau = 0.56 s.

    ``calibrate_inputs`` defaults to False. The shipped booster was fitted on
    raw modality scores in [0, 1]; the serving path additionally squashed them
    through a sigmoid with hardcoded constants, compressing the input range to
    [0.378, 0.731]. The model therefore never saw an input resembling its
    training distribution, and an empty room scored 0.425 instead of 0.012.
    Feeding raw scores restores the contract the model was actually trained on.
    """

    model_filename: str = "tci_xgboost.json"
    calibrate_inputs: bool = _env_bool("SENTRIX_CALIBRATE_INPUTS", False)
    ema_tau_seconds: float = _env_float("SENTRIX_EMA_TAU", 1.2)

    #: Additive contextual boosters, applied after the model.
    unknown_person_boost: float = 0.15
    suspicious_behaviour_boost: float = 0.12

    #: Hard overrides. These bypass fusion entirely, so they must be high.
    fire_override: float = 0.70
    weapon_override_critical: float = 0.70
    weapon_override_high: float = 0.55
    intrusion_override: float = 0.80

    #: TCI forced when an enrolled resident is recognised and unarmed.
    authorised_floor: float = 0.10

    #: Weights for the transparent linear fallback used when no booster loads.
    #: Deliberately published rather than hidden: a reader can reproduce the
    #: fallback TCI by hand from the score vector in any evidence sidecar.
    fallback_weights: dict = field(default_factory=lambda: {
        "weapon": 0.28,
        "fire": 0.22,
        "behaviour": 0.16,
        "vision": 0.10,
        "audio": 0.10,
        "identity": 0.08,
        "motion": 0.06,
    })


# ──────────────────────────────────────────────────────────────────────────────
# Escalation policy
# ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PolicyConfig:
    """Level bands plus the hysteresis that stops them flapping.

    Bands follow the mid-semester report (Table 1.1). They should ultimately be
    *derived* from a cost matrix on held-out data rather than asserted; see
    ``notebooks/03_tci_thresholds.ipynb``, which does exactly that.

    Hysteresis is deliberately asymmetric: escalate after a short confirmation
    (0.6 s) so response stays fast, de-escalate only after a long quiet period
    (5 s) so an intruder stepping behind a pillar does not clear the alarm.
    This is standard alarm-system practice and it is what actually suppresses
    single-frame false positives — EMA smoothing alone cannot, because one large
    spike still drags the average across a threshold.
    """

    thresholds: tuple = (0.20, 0.40, 0.65, 0.85)

    escalate_dwell_seconds: float = 0.6
    deescalate_dwell_seconds: float = 5.0

    names: dict = field(default_factory=lambda: {
        1: "SECURE", 2: "SUSPICIOUS", 3: "ELEVATED", 4: "HIGH", 5: "CRITICAL",
    })


# ──────────────────────────────────────────────────────────────────────────────
# Incidents, evidence and alerting
# ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class IncidentConfig:
    """Incident grouping — the fix for the alert storm.

    The previous build acted per *frame*: at level 2 it wrote a snapshot, sent an
    SMS and inserted a database row on every inference tick. A single sustained
    false positive produced 1,197 level-5 evidence bundles at one file per
    second, and because filenames had one-second resolution, same-second writes
    silently overwrote each other.

    Here, consecutive frames above level 1 are grouped into one Incident. Every
    side effect is bound to the incident, not the frame, so a ten-minute event
    produces one evidence bundle and one alert.
    """

    #: Close an incident after this long below its trigger level.
    close_after_quiet_seconds: float = 20.0
    #: Ignore anything shorter than this — it is noise, not an event.
    min_duration_seconds: float = 0.5
    #: Cap on a single incident before it is closed and a new one opened.
    max_duration_seconds: float = 300.0
    #: Re-capture evidence within a long-running incident at most this often.
    evidence_interval_seconds: float = 30.0


@dataclass(frozen=True)
class EvidenceConfig:
    """Encrypted evidence vault.

    ``pre_roll``/``post_roll`` follow VMS convention (5-10 s before, 10-30 s
    after) so an evidence bundle shows the approach, not just the aftermath.
    """

    pre_roll_seconds: float = 8.0
    post_roll_seconds: float = 15.0
    jpeg_quality: int = 90
    #: Minimum level that produces an encrypted bundle.
    min_level: int = 3
    #: Write a signed manifest covering every bundle in the session. Per-file
    #: hashes in an unsigned sidecar prove nothing on their own: an attacker who
    #: can rewrite the evidence can rewrite the hash next to it.
    session_manifest: bool = True


@dataclass(frozen=True)
class AlertConfig:
    """Notification thresholds and debounce.

    Minimum levels are raised relative to the previous build, which sent an SMS
    at level 2 and fired the siren at level 3 — both far more aggressive than
    the documented escalation matrix, and the direct cause of the alert storm.
    """

    sms_min_level: int = 4
    call_min_level: int = 5
    siren_min_level: int = 5
    dispatch_min_level: int = 4

    sms_cooldown_seconds: float = 300.0
    call_cooldown_seconds: float = 600.0
    siren_cooldown_seconds: float = 60.0
    siren_duration_seconds: float = 5.0


# ──────────────────────────────────────────────────────────────────────────────
# Root
# ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Config:
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    windows: WindowConfig = field(default_factory=WindowConfig)
    scene: SceneConfig = field(default_factory=SceneConfig)
    behaviour: BehaviourConfig = field(default_factory=BehaviourConfig)
    identity: IdentityConfig = field(default_factory=IdentityConfig)
    reid: ReIDConfig = field(default_factory=ReIDConfig)
    fusion: FusionConfig = field(default_factory=FusionConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    incident: IncidentConfig = field(default_factory=IncidentConfig)
    evidence: EvidenceConfig = field(default_factory=EvidenceConfig)
    alert: AlertConfig = field(default_factory=AlertConfig)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


#: Import this. Constructed once at process start; treat as immutable.
settings = Config()

__all__ = ["Config", "settings"]
