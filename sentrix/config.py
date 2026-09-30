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
    #: Requested webcam mode; the driver may pick the nearest it supports.
    capture_width: int = _env_int("SENTRIX_CAPTURE_WIDTH", 640)
    capture_height: int = _env_int("SENTRIX_CAPTURE_HEIGHT", 480)
    inference_fps: float = _env_float("SENTRIX_INFERENCE_FPS", 6.0)
    #: Detection boxes older than this are not drawn on the preview.
    stale_overlay_seconds: float = 3.0

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
    weapon_conf: float = _env_float("SENTRIX_WEAPON_CONF", 0.55)
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
    """The five-class scene classifier.

    ``window_ticks`` averages class probabilities over the last few
    *observations*, not seconds: at 0.5-2 Hz on a loaded CPU a 0.5 s window held
    a single frame, so every frame decided alone (CLAUDE.md §5). Temporal
    confirmation proper lives in :class:`ThreatConfig` (k of n ticks).
    """

    enabled: bool = _env_bool("SENTRIX_SCENE_ENABLED", True)
    window_ticks: int = _env_int("SENTRIX_SCENE_WINDOW_TICKS", 1)
    #: Observations older than this are dropped from the window.
    max_age_seconds: float = 3.0


# ──────────────────────────────────────────────────────────────────────────────
# Threat evidence — confirmation and escalation caps (CLAUDE.md M1, D1-D3)
# ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ThreatConfig:
    """How per-class evidence is confirmed and how far each source may escalate.

    The first live run showed every false alarm came from one tick of the scene
    classifier barely over its threshold, in a room it had never seen. The rules
    here make that impossible without making real threats undetectable:

    * **Confirmation in observations.** A source only counts for a class once it
      crossed that class's threshold on ``confirm_k`` of the last ``confirm_n``
      ticks, with no gap longer than ``confirm_max_gap_seconds``.
    * **A weapon needs a person.** Weapon votes only count if a person was seen
      in the last ``person_memory_seconds`` (65 % of the live false weapon
      alarms had nobody in frame).
    * **One source is capped below L4.** A single confirmed source lands in the
      L3 band (``confirmed_floor``..``single_source_cap``): snapshot and evidence,
      never SMS or siren. The scene classifier and a detector box together can
      reach L4, and at full strength L5.
    * **Experimental classes never escalate past L2** (assault, robbery — they
      exist in 1-3 clips and cannot be validated; D1).
    * **Presence is context, not threat** (D2). In home mode an unknown person
      is logged only; in armed mode a person not recognised for
      ``intrusion_dwell_seconds`` opens an intrusion at ``intrusion_level`` (L3).

    Scene thresholds are the checkpoint's, raised to at least
    ``scene_threshold_floor`` (the checkpoint's were tuned for macro-F1 at 41 %
    threat prevalence), unless a deployment calibration file sets them
    (``var/calibration/scene.json`` or ``SENTRIX_SCENE_THRESHOLDS`` as JSON).
    """

    confirm_k: int = _env_int("SENTRIX_CONFIRM_K", 2)
    confirm_n: int = _env_int("SENTRIX_CONFIRM_N", 3)
    confirm_max_gap_seconds: float = 3.0
    person_memory_seconds: float = 2.0

    scene_threshold_floor: dict = field(default_factory=lambda: {
        "weapon": 0.50, "fire": 0.45, "assault": 0.60, "robbery": 0.60,
    })

    #: TCI-scale evidence for one confirmed source: floor at its threshold,
    #: cap at certainty. Both below the L4 band (0.65).
    confirmed_floor: float = 0.45
    single_source_cap: float = 0.62
    #: Unconfirmed evidence stays under the L2 band (0.20): shown, never acted on.
    unconfirmed_ceiling: float = 0.19
    #: Experimental classes stop in the L2 band.
    experimental_cap: float = 0.35
    #: A classifier and a detector both confirmed: floor in the L4 band, cap in
    #: L5. The weaker side sets the value, so L5 (0.85) needs both about 60 % of
    #: the way from threshold to certainty (e.g. scene fire 0.78, box 0.80).
    #: Noisy-OR of the single-source scores needed both at ~0.98, so no class
    #: could ever reach the siren with the current models.
    corroborated_floor: float = 0.70
    corroborated_cap: float = 0.95

    experimental_classes: tuple = ("assault", "robbery")
    #: Which sources vote: ``both`` (default), ``scene`` or ``detector``.
    sources: str = os.getenv("SENTRIX_THREAT_SOURCES", "both").strip().lower() or "both"
    #: Detector labels that count as fire. Smoke is dropped from the alarm path
    #: (D1): public-data smoke heads read a backlit webcam as smoke.
    detector_fire_labels: tuple = ("fire",)
    #: Detector labels that count as a weapon. The custom head's ``stick`` class
    #: (test mAP50 0.003) is experimental (D1): drawn and logged, never a vote.
    detector_weapon_labels: tuple = ("pistol", "knife", "long_gun", "handgun",
                                     "gun", "rifle")

    armed: bool = _env_bool("SENTRIX_ARMED", False)
    intrusion_dwell_seconds: float = 3.0
    intrusion_level: float = 0.45


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

    ``fail_closed`` is the most important flag in this file. A recognised
    resident disarms the armed-mode intrusion rule, so a weak matcher is not a
    weak feature — it is an authentication bypass. (Identity no longer relaxes
    weapon or fire evidence at all; CLAUDE.md M1.) When no real face-embedding
    backend is installed the system must report "not authorised" rather than
    fall back to comparing colour histograms, which is what the previous build
    did.

    Live, recognition runs on its own thread (:class:`IdentityWorker`): at most
    once per ``recheck_seconds``, at once when more people come into view, and
    every ``authorised_recheck_seconds`` while everyone in view is recognised.
    A verdict older than ``verdict_max_age_seconds`` is not trusted.
    """

    fail_closed: bool = _env_bool("SENTRIX_IDENTITY_FAIL_CLOSED", True)

    #: Cosine distance for deep 128-d/512-d embeddings.
    match_threshold: float = _env_float("SENTRIX_FACE_THRESHOLD", 0.45)

    #: Hold the authorised verdict this long after the last confirmed match, so
    #: a resident turning their head does not re-trigger the alarm.
    auth_hold_seconds: float = 5.0

    #: Identity score fed to fusion when an unknown person is present.
    unknown_person_score: float = 0.60

    #: InsightFace modules actually used: detection (for the 5-point alignment)
    #: and recognition (the ArcFace embedding). buffalo_l also ships 3D/2D
    #: landmark and gender/age models that cost CPU on every face and are unused.
    face_modules: tuple = ("detection", "recognition")
    #: Detector input size. 640 finds small CCTV-distance faces; 320-480 is
    #: enough for a close-range laptop webcam and ~2x cheaper.
    det_size: int = _env_int("SENTRIX_FACE_DET_SIZE", 640)
    #: onnxruntime intra-op threads per face model. Spinning is always disabled:
    #: with ORT's default busy-wait, the 5 buffalo_l sessions held ~20 of 24
    #: cores after every call and made the next YOLO pass 10-30x slower; the
    #: live pipeline ran at 0.32 Hz, and at 4.1 Hz with spinning off alone
    #: (CLAUDE.md §5, M2).
    ort_threads: int = _env_int("SENTRIX_FACE_ORT_THREADS", 4)

    recheck_seconds: float = 1.0
    authorised_recheck_seconds: float = 5.0
    verdict_max_age_seconds: float = 6.0
    #: A match stays with the person's *track* this long without a new look at
    #: the face (turning away, looking down), unless that track's face then
    #: fails to match twice in a row. Bound to the track, a stranger arriving
    #: next to a resident never inherits it.
    track_hold_seconds: float = 60.0
    #: Tracks not seen for this long are forgotten.
    track_forget_seconds: float = 30.0

    #: Enrolment quality gates: exactly one face, at least this many pixels
    #: across, sharp enough (variance of the Laplacian of the face crop) and
    #: neither dark nor blown out (mean grey level of the crop).
    enrol_min_face_px: int = 80
    enrol_min_sharpness: float = 40.0
    enrol_luma_range: tuple = (40.0, 220.0)


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

    The TCI is the strongest per-class threat evidence (:mod:`sentrix.fusion.
    evidence`), a transparent rule a reader can reproduce from any event row.
    The previous XGBoost booster was fitted to a synthetic formula in which
    ``identity`` meant familiarity while the runtime feeds unfamiliarity: it
    scored an authorised resident about 5x higher than a stranger, and on
    labelled replay it ranked threats worse than its own inputs (CLAUDE.md §5,
    D6). A fitted, calibrated model returns only once real session logs exist
    (M8).

    There is no smoothing. The evidence is already confirmed over several
    observations, so the TCI that is displayed, logged, texted and recorded on
    the incident is the value the level policy decides on. A display EMA made
    the gauge lag the committed level by more than a second at every onset
    ("TCI 0.37, L3"), which is what the first live test reported as a laggy TCI.
    """


# ──────────────────────────────────────────────────────────────────────────────
# Escalation policy
# ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PolicyConfig:
    """Level bands plus the hysteresis that stops them flapping.

    Bands follow the mid-semester report (Table 1.1). They should ultimately be
    *derived* from a cost matrix on held-out data rather than asserted; see
    ``notebooks/03_tci_thresholds.ipynb``, which does exactly that.

    Hysteresis is deliberately asymmetric: escalate quickly, de-escalate only
    after a long quiet period (5 s) so an intruder stepping behind a pillar does
    not clear the alarm.

    Escalation dwell is per threshold (L2, L3, L4, L5). The TCI only leaves the
    L1 band on evidence the evidence layer has already confirmed over several
    observations, so L2 and L3 (dashboard, snapshot, evidence) trust it at once
    — counting persistence twice made L3 take 4 s at 0.5 Hz. L4 and L5 (SMS,
    dispatch, siren) keep 0.6 s and two observations as a second, independent
    guard.
    """

    thresholds: tuple = (0.20, 0.40, 0.65, 0.85)

    escalate_dwell_seconds: tuple = (0.0, 0.0, 0.6, 0.6)
    deescalate_dwell_seconds: float = 5.0
    #: Observations at or above each threshold before it commits.
    min_escalate_observations: tuple = (1, 1, 2, 2)

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
    #: An open incident's row is rewritten at most this often as its peak moves.
    persist_interval_seconds: float = 5.0


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
    #: Minimum level that saves a plaintext incident snapshot (D4).
    snapshot_min_level: int = 3
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
    threat: ThreatConfig = field(default_factory=ThreatConfig)
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
