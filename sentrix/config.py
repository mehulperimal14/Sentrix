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

from sentrix.alerting.guard import test_mode_flag


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


def _env_str(name: str, default: str) -> str:
    return os.getenv(name, "").strip() or default


def _env_pin(name: str) -> int | None:
    """An optional BCM GPIO number (0-27); unset or invalid -> None."""
    pin = _env_int(name, -1)
    return pin if 0 <= pin <= 27 else None


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
    #: Microphone capture and acoustic classification. SENTRIX_AUDIO=0 for a
    #: machine without a microphone, or to keep the mic closed.
    audio_enabled: bool = _env_bool("SENTRIX_AUDIO", True)
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
    * **Presence is context, not threat** (D2 as amended by D15). A person who
      stays unrecognised for the dwell (``intrusion_dwell_seconds`` armed,
      ``intrusion_home_dwell_seconds`` at home) raises an "unrecognised
      person" at ``intrusion_level`` - the L2 band in **both** modes, never L3:
      levels 3-5 need a real threat. ``unknown_alert_home=False`` keeps the
      pre-D15 home rule (logged only).
    * **Modalities corroborate** (D15). Video detector, video classifier
      (scene, violence: one modality) and *time-aligned* audio are independent
      modalities. One visual source confirmed: L3. Two modalities confirmed for
      the same class: L4 (the weaker sets the value); three: L5 at least. Audio
      alone is capped at L2 (``audio_only_cap``).
    * **Fight vs banter** (D15). A fight (``assault``) stays experimental
      (L2) unless the video fight *and* aligned aggression audio are both
      confirmed with ``fight_min_persons`` people and motion over
      ``fight_motion_floor``: then L3 (``fight_max_level``, a hard cap). Aligned
      laughter or very calm talk with no aggression keeps it at L2, labelled
      banter; TV/music/radio make the audio neutral. Unvalidated until room
      recordings exist (M6).

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
    #: L2 band (0.20-0.40): an unknown person is never a critical level (D15).
    intrusion_level: float = 0.30
    #: Home mode raises the same L2 after a longer dwell (residents walk past).
    unknown_alert_home: bool = _env_bool("SENTRIX_UNKNOWN_ALERT_HOME", True)
    intrusion_home_dwell_seconds: float = _env_float("SENTRIX_UNKNOWN_HOME_DWELL", 10.0)
    #: A PIR trigger or door change this recent counts as presence (context:
    #: armed mode only, and never above ``intrusion_level``).
    sensor_memory_seconds: float = 10.0

    #: Aligned audio votes in the per-class evidence (D15). Off: audio is shown
    #: and logged but never changes a score.
    audio_votes: bool = _env_bool("SENTRIX_AUDIO_VOTES", True)
    #: Audio confirmed with no visual source of that class: L2 at most.
    audio_only_cap: float = 0.35
    #: Three independent modalities confirmed for one class: at least L5 -
    #: only if the audio among them is at least this strong (0 at its bar).
    three_modality_floor: float = 0.86
    three_modality_audio_min_strength: float = 0.3
    #: Counted audio windows never overlap (4 s windows): the confirmation
    #: window allows this gap between them.
    audio_confirm_max_gap_seconds: float = 6.0
    #: Highest level a fight may reach: 2 = always experimental, 3 = with
    #: aligned aggression audio, 4 = also with a second visual source.
    fight_max_level: int = _env_int("SENTRIX_FIGHT_MAX_LEVEL", 3)
    fight_min_persons: int = 2
    #: Motion (``MotionEngine.score``, 0..1) a fight needs to leave L2.
    fight_motion_floor: float = _env_float("SENTRIX_FIGHT_MOTION_FLOOR", 0.02)
    #: Kept for configuration compatibility; banter now holds a fight AT L2
    #: (``experimental_cap``), never below it.
    banter_cap: float = 0.19


# ──────────────────────────────────────────────────────────────────────────────
# Audio: the sound model, its cues and time alignment (D15)
# ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class AudioConfig:
    """Which sound model runs, what its cues mean, and when audio is "now".

    ``model``: ``auto`` (PANNs CNN10 AudioSet tagger if its weights are in
    ``models/audio/``, else the 4-class CNN), ``panns``, ``cnn`` or ``off``.

    **Alignment.** Every audio result carries its window (monotonic start and
    end on the pipeline clock, and wall time). Fusion uses a result for a frame
    or clip at time ``t`` only if the window overlaps ``[t - tolerance, t +
    tolerance]``; otherwise audio is *unavailable* - never "calm". A stale
    "calm" would let yesterday's silence lower today's fight.

    Cue thresholds are a priori values (AudioSet sigmoid scores), not fitted on
    any SENTRIX recording; M6 records the data to check them.
    """

    model: str = _env_str("SENTRIX_AUDIO_MODEL", "auto").lower()
    align_tolerance_seconds: float = _env_float("SENTRIX_AUDIO_ALIGN_TOLERANCE", 1.5)
    threads: int = _env_int("SENTRIX_AUDIO_THREADS", 2)
    cue_thresholds: dict = field(default_factory=lambda: {
        "aggression": 0.30, "weapon_sound": 0.30, "fire_sound": 0.30,
        "benign_social": 0.40, "calm_talk": 0.85, "background": 0.40, "siren": 0.30,
    })
    #: Very calm talk (``calm_talk`` over its strict bar) also counts as banter.
    calm_talk_is_benign: bool = _env_bool("SENTRIX_AUDIO_CALM_TALK_BENIGN", True)
    #: Seconds between looks for a microphone while none is connected, and
    #: the silence after which a connected one counts as unplugged. A USB
    #: microphone plugged in (or back in) later is picked up without a restart.
    mic_retry_seconds: float = _env_float("SENTRIX_MIC_RETRY_SECONDS", 5.0)
    mic_stall_seconds: float = _env_float("SENTRIX_MIC_STALL_SECONDS", 5.0)


# ──────────────────────────────────────────────────────────────────────────────
# Talk-down speaker (D16)
# ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SpeakerConfig:
    """Offline text-to-speech to a speaker at the device (D16).

    Off by default (it makes sound in the room). ``backend``: ``auto``
    (pyttsx3 - SAPI5 on Windows, espeak-ng on Linux - else the ``espeak-ng``
    command), ``pyttsx3``, ``espeak`` or ``console`` (logs only).
    """

    enabled: bool = _env_bool("SENTRIX_SPEAKER_ENABLED", False)
    backend: str = _env_str("SENTRIX_SPEAKER_BACKEND", "auto").lower()
    #: ALSA/PortAudio device name for espeak-ng (``-d``); empty = default.
    device: str = _env_str("SENTRIX_SPEAKER_DEVICE", "")
    volume: float = _env_float("SENTRIX_SPEAKER_VOLUME", 0.9)
    rate_wpm: int = _env_int("SENTRIX_SPEAKER_RATE", 150)
    max_chars: int = 200
    #: Messages per minute for the whole device.
    per_minute: int = _env_int("SENTRIX_SPEAKER_PER_MINUTE", 3)
    repeat: int = 1


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

    #: InsightFace model pack. ``buffalo_s`` (SCRFD-500M + MobileFaceNet) is
    #: the Pi 5 option (docs/8_RASPBERRY_PI5_INTEGRATION.md T1, S3); embeddings of the two packs are
    #: not comparable, so switching re-embeds the gallery.
    face_model: str = _env_str("SENTRIX_FACE_MODEL", "buffalo_l")

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
# Portal, SOS and device (docs/7_PORTAL_AND_SOS.md)
#
# Every service below is OFF unless its own env flag is set, so a tool, a test
# or a replay that imports the package never starts a link, a modem or a
# sweeper (decision D13). Secrets (bot tokens, API keys, the device token) are
# never fields here: ``/api/config`` publishes ``to_dict()``.
# ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SosConfig:
    """Assistance requests opened at a confirmed L3 commit (decisions D8, D9).

    A request opens only for the alarm classes (``alarm_classes``: weapon,
    fire, and a fight corroborated by video and aligned audio - the only way a
    fight reaches L3, D15). An unrecognised person is an L2 in both modes and
    opens no request (D8 as amended by D15); ``intrusion_requests=True``
    (``SENTRIX_SOS_INTRUSION=1``) restores the pre-D15 armed-intrusion request
    after the SIA-style entry delay, should a later level rule produce one. The
    household's window (30/45/60 min) bounds the *outside-help question*; the
    safety defaults alert confirmed contacts without waiting for it when the
    evidence is strong (corroborated L4/L5), the class is fire, the intrusion
    is armed, or the device drops off the link during an open L3+ alarm
    (an L2 - unrecognised person, audio only - gives the owner a notice only).
    No answer ends in EXPIRED ("unanswered"), never "safe". The policy is
    frozen on each request when it opens.
    """

    enabled: bool = _env_bool("SENTRIX_SOS_ENABLED", False)
    min_level: int = 3
    alarm_classes: tuple = ("weapon", "fire", "assault")
    #: Pre-D15 rule: an armed intrusion at L3+ opens a request (after the entry
    #: delay). Off: the pipeline caps unknown people at L2 anyway.
    intrusion_requests: bool = _env_bool("SENTRIX_SOS_INTRUSION", False)
    #: Armed-mode intrusion opens a request only after this entry delay, unless
    #: disarmed by the SOS PIN or an authorised face in the meantime.
    intrusion_entry_delay_seconds: float = 30.0

    window_seconds: int = _env_int("SENTRIX_SOS_WINDOW_SECONDS", 1800)
    window_choices: tuple = (1800, 2700, 3600)
    #: Test mode only (ignored otherwise), clamped to >= 60 s: demo window.
    demo_window_seconds: int = _env_int("SENTRIX_SOS_DEMO_WINDOW_SECONDS", 0)
    reminder_offsets_seconds: tuple = (120, 300, 600, 1200)
    lapse_warning_seconds: float = 300.0
    #: "I'm checking" pauses reminders only, never the expiry or safety timers.
    seen_snooze_seconds: float = 300.0
    #: Owner SMS (modem) when the link is down, or the prompt is unseen this long.
    owner_sms_unseen_after_seconds: float = 600.0
    #: A remote channel that has not delivered within this raises "undelivered".
    delivery_warning_seconds: float = 60.0

    safety_defaults: bool = True
    #: Committed levels that alert confirmed contacts at once (corroborated).
    contacts_immediate_levels: tuple = (4, 5)
    fire_contacts_after_seconds: float = 120.0
    #: Single-source weapon L3 alerts the owner only until gate P8 passes.
    contacts_for_single_source_l3: bool = _env_bool(
        "SENTRIX_SOS_CONTACTS_SINGLE_SOURCE_L3", False)
    single_source_contacts_after_seconds: float = 300.0
    #: Cloud side: no message from the device for this long = heartbeat lost.
    heartbeat_lost_seconds: float = 45.0

    #: After a decline, the same threat re-prompts at most once per window.
    repeat_window_seconds: float = 600.0
    pin_length: int = 6
    pin_attempts: int = 5
    pin_window_seconds: float = 600.0
    duress_min_digit_difference: int = 2

    #: Approved requests never expire; the Desk and contacts are re-paged.
    repage_offsets_seconds: tuple = (120, 300, 600)
    repage_interval_seconds: float = 3600.0
    repage_cap: int = 10
    repage_cap_test_mode: int = 3
    stale_close_seconds: float = 86400.0
    sweep_interval_seconds: float = 15.0
    #: Contact pages and action links live until the request closes + this.
    link_grace_seconds: float = 7200.0


@dataclass(frozen=True)
class LinkConfig:
    """The device's outbound WSS link to the Portal (protocol v1, docs/7 §5).

    The device never accepts inbound connections (home lines sit behind
    CGNAT). Identity (device id, bootstrap secret, device token) lives in
    ``identity_dir``, outside ``var/`` so a factory reset cannot strand the
    unit: ``/etc/sentrix`` when it exists, otherwise ``var/link``.
    """

    enabled: bool = _env_bool("SENTRIX_LINK_ENABLED", False)
    #: Portal base URL, e.g. https://sentrix.example.in; the socket is
    #: ``wss://<host>/device/v1/link`` (``ws://`` for an http:// dev Portal).
    portal_url: str = _env_str("SENTRIX_PORTAL_URL", "")
    identity_dir: str = _env_str("SENTRIX_DEVICE_IDENTITY_DIR", "")
    protocol_version: int = 1
    heartbeat_seconds: float = 10.0
    #: Device: no message from the Portal for this long = offline (SMS ladder).
    offline_after_seconds: float = 30.0
    reconnect_min_seconds: float = 1.0
    reconnect_max_seconds: float = 60.0
    #: Outbox events in flight before the device waits for an ack.
    outbox_window: int = 64
    state_hz_subscribed: float = 2.0
    state_idle_seconds: float = 10.0
    #: Subscriptions are leases the Portal renews; an orphan stops itself.
    subscription_lease_seconds: float = 75.0
    #: Remote video: adaptive up to ``video_max_fps``; JPEG quality equals the
    #: MJPEG stream's so the once-per-frame encode cache is reused.
    video_max_fps: float = 15.0
    video_min_fps: float = 2.0
    video_jpeg_quality: int = 70
    video_idle_stop_seconds: float = 10.0
    max_viewers: int = 3
    tunnel_timeout_seconds: float = 20.0
    tunnel_upload_timeout_seconds: float = 60.0
    tunnel_max_inflight: int = 8
    tunnel_inline_body_bytes: int = 256 * 1024
    tunnel_chunk_bytes: int = 256 * 1024
    tunnel_max_request_bytes: int = 11 * 1024 * 1024
    tunnel_max_response_bytes: int = 16 * 1024 * 1024
    #: Device-enforced limits on commands from the cloud (decisions).
    sms_daily_cap: int = 30
    siren_test_min_interval_seconds: float = 60.0
    siren_test_seconds: float = 1.0
    media_requests_per_minute: int = 60


@dataclass(frozen=True)
class ChannelConfig:
    """Outbound channels and the test-mode send guard (decision D13).

    Test mode is the default and the only mode this semester. A message leaves
    the machine only when SENTRIX_LIVE_ALERTS=1 *and* the channel's own flag is
    set; in test mode it may then go only to SENTRIX_TEST_RECIPIENTS and starts
    with the test banner. ``sentrix/alerting/guard.py`` re-reads the
    environment on every send; these fields mirror it for display.
    """

    #: The channel router and its outbox worker (a service).
    enabled: bool = _env_bool("SENTRIX_CHANNELS_ENABLED", False)
    test_mode: bool = test_mode_flag("SENTRIX_TEST_MODE")
    live_alerts: bool = _env_bool("SENTRIX_LIVE_ALERTS", False)
    modem_live: bool = _env_bool("SENTRIX_MODEM_LIVE", False)
    twilio_enabled: bool = _env_bool("SENTRIX_TWILIO_ENABLED", False)
    telegram_live: bool = _env_bool("SENTRIX_TELEGRAM_LIVE", False)
    email_live: bool = _env_bool("SENTRIX_EMAIL_LIVE", False)
    #: The USB 4G modem worker (AT commands over pyserial).
    modem_enabled: bool = _env_bool("SENTRIX_MODEM_ENABLED", False)
    modem_port: str = _env_str("SENTRIX_MODEM_PORT", "")
    modem_baud: int = _env_int("SENTRIX_MODEM_BAUD", 115200)
    telegram_mode: str = _env_str("SENTRIX_TELEGRAM_MODE", "polling")
    email_provider: str = _env_str("SENTRIX_EMAIL_PROVIDER", "")
    #: Each message: this many attempts, waiting these offsets between them
    #: ("3 tries over 2 min"); then FAILED, retried every failed_retry_seconds.
    max_attempts: int = 3
    retry_backoff_seconds: tuple = (30.0, 90.0)
    failed_retry_seconds: float = 120.0
    request_timeout_seconds: float = 15.0


@dataclass(frozen=True)
class GpioConfig:
    """Pi 5 header (roadmap v2 pin table; gpiozero on lgpio, mock elsewhere)."""

    enabled: bool = _env_bool("SENTRIX_GPIO_ENABLED", False)
    #: "" = auto (lgpio on a Pi, gpiozero's mock factory elsewhere).
    pin_factory: str = _env_str("SENTRIX_GPIO_PIN_FACTORY", "")
    siren_pin: int = 17
    #: NPN driver with ``gpio=17=op,dl`` in config.txt: HIGH = siren on.
    siren_active_high: bool = _env_bool("SENTRIX_SIREN_ACTIVE_HIGH", True)
    led_green_pin: int = 5
    led_amber_pin: int = 12
    led_red_pin: int = 13
    buzzer_pin: int = 26
    pir_pin: int = 23
    door_pin: int = 24
    door_pull_up: bool = True
    mains_sense_pin: int = 16
    #: PC817 output is pulled low while mains power is present.
    mains_present_low: bool = True
    input_bounce_seconds: float = 0.05
    #: One relay on-period never exceeds this, whatever asked for it (hearing
    #: note: ~90 s daily dose at 110 dB at the rated distance). Alarms use 5 s.
    siren_max_on_seconds: float = _env_float("SENTRIX_SIREN_MAX_ON_SECONDS", 90.0)
    #: GPIO of the siren arming key's second contact (closed to GND = armed);
    #: None = no input, ``siren_armed`` states the key position.
    siren_arm_pin: int | None = _env_pin("SENTRIX_SIREN_ARM_PIN")
    #: Arming state without that input; False refuses siren tests (409).
    siren_armed: bool = _env_bool("SENTRIX_SIREN_ARMED", True)
    #: Enclosure lid switch (closed to GND = shut); None = not fitted.
    tamper_pin: int | None = _env_pin("SENTRIX_TAMPER_PIN")
    #: Local buzzer chirps on level rises (docs/8_RASPBERRY_PI5_INTEGRATION.md §6).
    buzzer_enabled: bool = _env_bool("SENTRIX_BUZZER_ENABLED", True)
    #: True for a buzzer through an NPN transistor and for "high-level trigger"
    #: modules (pin HIGH = sound). Many 3-pin buzzer modules are "low-level
    #: trigger" (pin LOW = sound): set 0 for those, or they sound non-stop.
    buzzer_active_high: bool = _env_bool("SENTRIX_BUZZER_ACTIVE_HIGH", True)
    #: Inputs actually wired, of ``pir``, ``door``, ``mains`` ("none" = none).
    #: An unwired door contact reads "open" and an unwired PC817 reads "mains
    #: lost" (a fault on the amber LED and a node alert), so leave those out.
    inputs: str = _env_str("SENTRIX_GPIO_INPUTS", "pir,door,mains")


@dataclass(frozen=True)
class ClipConfig:
    """The 4-second clip engine (docs/8_RASPBERRY_PI5_INTEGRATION.md T1). ``mode=tick`` is today's loop."""

    mode: str = _env_str("SENTRIX_PIPELINE_MODE", "tick")
    window_seconds: float = 4.0
    hop_seconds: float = 2.0
    keyframes: int = 6
    uniform_keyframes: int = 4
    motion_keyframes: int = 2
    #: A class is confirmed in a clip on this many keyframes of one track.
    confirm_keyframes: int = 2
    #: Clip compute must stay under this fraction of the hop at p95.
    budget_fraction: float = 0.8
    fast_lane: bool = _env_bool("SENTRIX_FAST_LANE", False)
    fast_lane_imgsz: int = 320
    fast_lane_every_n: int = 4
    fast_lane_hits: int = 2
    #: Model runtime for exported models: torch | onnx | ncnn.
    runtime: str = _env_str("SENTRIX_RUNTIME", "torch")
    threads: int = _env_int("SENTRIX_CLIP_THREADS", 4)
    #: A clip is one observation of 4 s that already needed confirm_keyframes
    #: hits, so L4/L5 commit on one clip (the tick loop needs 2 ticks:
    #: PolicyConfig.min_escalate_observations). The clip strategy builds its
    #: LevelPolicy with this.
    min_escalate_observations: int = 1
    # Engine tunables (``sentrix.pipeline.clip.option``; reasons in clip.DEFAULTS).
    #: Frames per second kept in the ring (the Pi's lores analysis stream).
    buffer_fps: float = 15.0
    #: Frames wider than this are downscaled on capture (640x480 lores).
    analysis_width: int = 640
    #: New scene-classifier inferences per clip (S2).
    scene_keyframes: int = 2
    #: S0: clip motion peak that opens the cascade (a still, empty room gate).
    gate_motion_floor: float = 0.015
    #: S0 stays open this long after a clip saw a person or a fire candidate.
    gate_hold_seconds: float = 10.0
    #: A full cascade runs at least this often, whatever the gate says.
    idle_refresh_seconds: float = 10.0
    #: S3 (identity) at most this often (a new track is checked at once).
    identity_interval_seconds: float = 2.0
    #: S4 runs the action model only above this clip motion peak.
    action_motion_floor: float = 0.10
    #: Share of bright flame-coloured pixels that makes a fire candidate.
    flame_fraction: float = 0.0005
    #: Frames per second sealed into an evidence clip bundle.
    evidence_fps: float = 5.0
    #: Seconds sealed after an evidence capture (docs/8_RASPBERRY_PI5_INTEGRATION.md T1: 4-8 s).
    post_roll_seconds: float = 8.0


@dataclass(frozen=True)
class NodeConfig:
    """Device telemetry (Node page, heartbeat)."""

    enabled: bool = _env_bool("SENTRIX_NODE_ENABLED", False)
    #: laptop | pi5-clip | pi5-hailo | pi5-node | hub (docs/8_RASPBERRY_PI5_INTEGRATION.md §9).
    profile: str = _env_str("SENTRIX_PROFILE", "laptop")
    interval_seconds: float = 5.0
    temp_warn_c: float = 80.0
    temp_critical_c: float = 85.0
    #: Room sensors through the kernel IIO drivers (off unless fitted).
    bme280: bool = _env_bool("SENTRIX_SENSOR_BME280", False)
    dht22: bool = _env_bool("SENTRIX_SENSOR_DHT22", False)
    #: Picamera2 streams: main (evidence/preview) and lores (analysis).
    picamera_main: str = _env_str("SENTRIX_PICAMERA_MAIN", "1280x720")
    picamera_lores: str = _env_str("SENTRIX_PICAMERA_LORES", "640x480")


@dataclass(frozen=True)
class WebConfig:
    """Local Console (LC) web hardening (docs/7 §9)."""

    #: FastAPI /docs, /redoc, /openapi.json (off unless asked for).
    api_docs: bool = _env_bool("SENTRIX_API_DOCS", False)
    cookie_name: str = "lc_session"
    csrf_header: str = "X-CSRF-Token"
    csp: bool = _env_bool("SENTRIX_CSP", True)
    #: /setup/wifi exists only while the unit is in physical setup mode.
    setup_mode: bool = _env_bool("SENTRIX_SETUP_MODE", False)


@dataclass(frozen=True)
class RetentionConfig:
    """Hard retention caps (decisions, data protection). They override every
    prune exemption (``database.prune`` keeps L4/L5 and unreviewed incidents
    only *inside* these caps); pruned evidence leaves a signed tombstone so the
    chain reports a retention gap, not tampering. Device prune and vault
    tombstones: integration; Portal prune: T-ACCT's retention worker."""

    #: Reviewed L1-L3 incidents (the pre-portal RETENTION_DAYS rule).
    reviewed_low_days: int = _env_int("RETENTION_DAYS", 30)
    sos_snapshot_days: int = 30
    unreviewed_days: int = 180
    true_days: int = 365
    audit_days: int = 365
    #: Declined emergency contacts are erased within this many days.
    declined_contact_days: int = 7


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
    audio: AudioConfig = field(default_factory=AudioConfig)
    speaker: SpeakerConfig = field(default_factory=SpeakerConfig)
    behaviour: BehaviourConfig = field(default_factory=BehaviourConfig)
    identity: IdentityConfig = field(default_factory=IdentityConfig)
    reid: ReIDConfig = field(default_factory=ReIDConfig)
    fusion: FusionConfig = field(default_factory=FusionConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    incident: IncidentConfig = field(default_factory=IncidentConfig)
    evidence: EvidenceConfig = field(default_factory=EvidenceConfig)
    alert: AlertConfig = field(default_factory=AlertConfig)
    sos: SosConfig = field(default_factory=SosConfig)
    link: LinkConfig = field(default_factory=LinkConfig)
    channels: ChannelConfig = field(default_factory=ChannelConfig)
    gpio: GpioConfig = field(default_factory=GpioConfig)
    clip: ClipConfig = field(default_factory=ClipConfig)
    node: NodeConfig = field(default_factory=NodeConfig)
    web: WebConfig = field(default_factory=WebConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)

    @property
    def test_mode(self) -> bool:
        """Decision D13: on unless SENTRIX_TEST_MODE=0 (display value; the send
        guard re-reads the environment on every send)."""
        return self.channels.test_mode

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


#: Import this. Constructed once at process start; treat as immutable.
settings = Config()

__all__ = ["Config", "settings"]
