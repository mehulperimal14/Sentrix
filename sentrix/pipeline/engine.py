"""The SENTRIX pipeline.

One ``process()`` method serves live capture and offline replay. The previous
build had ``process()`` and ``process_eval_frame()`` — two ~120-line copies of
the same logic that had already diverged, which meant every published number
came from a code path that was not the product.

Per-frame order:

1. acquire frame, keep a pristine copy for inference
2. detect (people, weapons, fire) in one YOLOv8 component
3. scene classification (normal/fire/weapon/assault/robbery), or - when no
   scene model is installed - the windowed RWF-2000 violence score
4. identity check (fail-closed)
5. per-class threat evidence: every source's votes, k-of-n confirmation, the
   person gate and corroboration caps (fusion/evidence.py, CLAUDE.md M1)
6. behaviour kinematics from tracked people
7. audio (read from the capture thread, never blocking), used only when its
   window describes this frame (time-aligned, D15): its cues vote with the
   video sources; PIR/door events are presence context
8. assemble the feature vector; TCI, then hysteretic level commit on the raw
   (already confirmed) evidence
9. incident update, then incident-scoped side effects
10. publish state, append the event log, draw the HUD

Overlays are drawn on a copy. The previous code annotated ``combined_frame`` and
then passed the same array to the encoders on later ticks, so HUD text and
bounding boxes were baked into the pixels the classifiers saw — the models were
partly reading the system's own overlay.

Seams for other tracks (docs/7 §2.4): ``observe`` (steps 2-7, perception into
a feature vector) and ``decide`` (steps 8-10) are public so the 4-second clip
engine (``sentrix/pipeline/clip.py``) can feed its own clip-level features
into the same decision path; ``run_live`` takes an :class:`InferenceStrategy`
so clip mode keeps the capture, preview, identity-worker and HUD threads.

Training recordings (D20, ``sentrix/io/recorder.py``) tap the capture thread:
the camera's own frame goes to the recorder before the HUD is drawn on a copy,
and the microphone's blocks arrive through the audio engine's sample listener.
Nothing records until ``start_recording`` is called.
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Protocol, runtime_checkable

import numpy as np

from sentrix import listeners
from sentrix import mode as runtime_mode
from sentrix.alerting.dispatch import dispatch_service
from sentrix.alerting.notifier import notifier
from sentrix.config import settings
from sentrix.evidence.vault import vault
from sentrix.fusion.evidence import CLASS_ORDER, EvidenceAccumulator
from sentrix.fusion.features import FeatureVector
from sentrix.fusion.policy import LevelPolicy
from sentrix.fusion.tci import TCIEngine, TCIResult
from sentrix.io.actuators import build_siren
from sentrix.io.recorder import Recorder, RecorderError
from sentrix.io.sources import FrameSource, _placeholder, build_source
from sentrix.perception.audio import AudioEngine, AudioResult, audio_votes, neutral_reason
from sentrix.perception.behaviour import BehaviourEngine
from sentrix.perception.detector import Detector
from sentrix.perception.identity import (FaceCheck, IdentityEngine, IdentityWorker,
                                         TrackIdentity)
from sentrix.perception.motion import MotionEngine
from sentrix.perception.reid import ReIDEngine
from sentrix.perception.scene import SceneEngine
from sentrix.perception.violence import ViolenceEngine
from sentrix.pipeline.escalation import plan_for
from sentrix.pipeline.events import EventLog
from sentrix.pipeline.incidents import IncidentManager
from sentrix.pipeline.state import state
from sentrix.sos import LevelChange, NullSosService, SosTrigger
from sentrix.storage import database
from sentrix.utils.logging import get_logger, telemetry
from sentrix.utils.redact import redact_url

log = get_logger("engine")

try:
    import cv2
except ImportError:                                    # pragma: no cover
    cv2 = None                                         # type: ignore[assignment]

LEVEL_COLOURS = {
    1: (0, 200, 80), 2: (30, 200, 220), 3: (0, 140, 255),
    4: (0, 60, 240), 5: (0, 0, 220),
}
TASK_QUEUE_MAX = 64
#: Queued behind every pending side effect at shutdown: the worker drains
#: what is ahead of it (an SOS trigger, the last incident save) and stops.
_DRAIN = object()
#: Frame rates a measured camera rate snaps to for a training recording (D20):
#: dataset tools address a frame as time x fps, so the file's rate should be
#: the camera's real one, not the 30 a webcam was asked for. Whole numbers
#: (the AVI header stores the rate rounded): a webcam that falls to 30/4 =
#: 7.5 fps in a dark room is recorded at 8.
RECORDING_FPS_STEPS = (5, 8, 10, 12, 15, 20, 24, 25, 30)
#: A measured rate up to this fraction over a step snaps to that step (the 3 s
#: window jitters by about a frame); anything faster takes the next step up,
#: so a near miss repeats a few frames instead of dropping real ones.
RECORDING_FPS_TOLERANCE = 0.04
#: How long shutdown waits for a recording in progress to be saved (s).
RECORDING_CLOSE_SECONDS = 10.0


@dataclass(frozen=True)
class Overlay:
    """What the last inference said, drawn onto every captured frame."""

    tci: float
    level: int
    status: str
    behaviour_label: str
    weapon: float
    fire: float
    authorized: bool
    identity_degraded: bool
    detections: dict
    #: Capture time of the frame the overlay was computed on.
    at: float


@runtime_checkable
class InferenceStrategy(Protocol):
    """How the live loop turns captured frames into decisions.

    ``Pipeline.run_live`` owns capture, preview (HUD, MJPEG encode), the
    identity worker and the rates; the strategy owns the inference thread.
    ``on_frame`` runs on the capture thread for every new camera frame and
    must be O(1) (hand the frame over, never infer); ``run`` is the inference
    thread's body until ``stop`` is set, calling ``pipeline.process`` (or
    ``observe`` + ``decide``) and ``pipeline.note_inference`` after each
    decision; ``wake`` makes a blocked ``run`` return promptly.

    The tick loop is :class:`TickStrategy`; clip mode
    (``SENTRIX_PIPELINE_MODE=clip``) uses ``sentrix.pipeline.clip.
    build_strategy(pipeline)`` (track T-CLIP).
    """

    name: str

    def on_frame(self, frame: np.ndarray, captured_at: float) -> None: ...

    def run(self, stop: threading.Event) -> None: ...

    def wake(self) -> None: ...


class TickStrategy:
    """Today's loop: the newest frame through a single slot, at most
    ``inference_fps``; frames inference cannot keep up with are dropped,
    never queued."""

    name = "tick"

    def __init__(self, pipeline: "Pipeline") -> None:
        self.pipeline = pipeline

    def on_frame(self, frame: np.ndarray, captured_at: float) -> None:
        pipeline = self.pipeline
        with pipeline._slot:
            pipeline._pending = (frame, captured_at)
            pipeline._slot.notify()

    def wake(self) -> None:
        with self.pipeline._slot:
            self.pipeline._slot.notify_all()

    def run(self, stop: threading.Event) -> None:
        pipeline = self.pipeline
        period = pipeline.cfg.pipeline.inference_period
        next_at = 0.0
        while not stop.is_set():
            delay = next_at - time.monotonic()
            if delay > 0:
                stop.wait(delay)
                continue
            with pipeline._slot:
                if pipeline._pending is None:
                    pipeline._slot.wait(timeout=0.5)
                job, pipeline._pending = pipeline._pending, None
            if job is None:
                continue
            frame, captured_at = job
            next_at = time.monotonic() + period
            try:
                # Pipeline time is the capture time: evidence windows and dwell
                # timers measure the scene, not the queue.
                pipeline.process(frame, now=captured_at)
            except Exception as exc:
                log.error("frame processing failed: %s", exc, exc_info=True)
                continue
            pipeline.note_inference(captured_at)


class Pipeline:
    """Owns every engine and runs the per-frame loop."""

    def __init__(self, source: FrameSource | None = None,
                 enable_audio: bool = True, event_source: str = "live") -> None:
        log.info("initialising SENTRIX pipeline")
        self.cfg = settings

        self.source = source or build_source()
        self.detector = Detector()
        self.motion = MotionEngine()
        self.scene = SceneEngine()
        # The scene classifier's assault head supersedes the RWF-2000 violence
        # model: on this deployment's footage that model scores an empty CCTV
        # corridor as a fight (docs/6). Loading both would cost 42 MB and a
        # 16-frame window of inference per second for a signal that is ignored.
        self.violence = None if self.scene.available else ViolenceEngine()
        self.behaviour = BehaviourEngine()
        self.identity = IdentityEngine()
        self.track_identity = TrackIdentity()
        self._settled = False
        self.reid = ReIDEngine()
        self.audio = AudioEngine() if enable_audio else None
        self.evidence = EvidenceAccumulator()
        self._last_person_at: float | None = None
        self.tci = TCIEngine()
        self.policy = LevelPolicy()
        self.incidents = IncidentManager()
        self.siren = build_siren()
        self.events = EventLog(source=event_source)
        self.session_id = self.events.session_id
        #: SOS assistance requests (docs/7 §7). Null unless the app lifespan
        #: installs the real service (SENTRIX_SOS_ENABLED=1); measurement
        #: pipelines must keep the null one (services.assert_measurement_safe).
        self.sos = NullSosService()
        self._committed_level = 1

        self._latencies: deque[float] = deque(maxlen=200)
        self._frame_count = 0
        self._init_live()

        self._tasks: queue.Queue = queue.Queue(maxsize=TASK_QUEUE_MAX)
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._task_loop, daemon=True,
                                        name="sentrix-tasks")
        self._worker.start()

        log.info("pipeline ready — detector=%s scene=%s identity=%s audio=%s "
                 "siren=%s fusion=%s", self.detector.available,
                 self.scene.weights_name or "off",
                 self.identity.backend or "FAIL-CLOSED",
                 bool(self.audio and self.audio.available),
                 self.siren.name, self.tci.model_source)

    def _init_live(self) -> None:
        """State shared by the capture, inference and preview paths."""
        self._identity_worker: IdentityWorker | None = None
        self._raw: np.ndarray | None = None
        self._slot = threading.Condition()
        self._pending: tuple[np.ndarray, float] | None = None
        self._overlay: Overlay | None = None
        self._preview_lock = threading.Lock()
        self._preview: np.ndarray | None = None
        self._preview_seq = 0
        self._jpeg: tuple[int, bytes] | None = None
        self._last_seq = -1
        self._video_times: deque[float] = deque(maxlen=90)
        self._ai_times: deque[float] = deque(maxlen=30)
        self._video_fps = 0.0
        self._ai_hz = 0.0
        self._rates_at = 0.0
        self._offline = False
        self._strategy: InferenceStrategy = TickStrategy(self)
        #: Set by request_close_incident (any thread), applied by decide().
        self._close_requested: str | None = None
        #: (time, people in view) for the fight rule's "2+ people recently".
        self._persons_seen: deque[tuple[float, int]] = deque(maxlen=64)
        #: End of the last audio window seen, and of the last one *counted* as
        #: a new observation (counted windows never overlap: D15, review F4).
        self._audio_last_end: float | None = None
        self._audio_counted_end: float | None = None
        #: Last PIR trigger / door change on the pipeline clock (gpio thread
        #: writes, the inference thread reads: guarded).
        self._sensor_at: dict[str, float] = {}
        self._sensor_lock = threading.Lock()
        #: The training recorder (D20): built only by get_recorder(), so a
        #: replay, an evaluation or a bench never records anything.
        self._recorder: Recorder | None = None
        self._recorder_lock = threading.Lock()
        self._recorder_listener = None

    # ── background side effects ───────────────────────────────────────────────
    def _task_loop(self) -> None:
        """FIFO side effects. At shutdown ``_DRAIN`` is queued behind whatever
        is pending, so a queued SOS trigger or incident save still runs."""
        while True:
            try:
                item = self._tasks.get(timeout=0.5)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            try:
                if item is _DRAIN:
                    return
                func, args, kwargs = item
                func(*args, **kwargs)
            except Exception as exc:
                log.error("background task failed: %s", exc)
            finally:
                self._tasks.task_done()

    def _submit(self, func, *args, **kwargs) -> None:
        try:
            self._tasks.put_nowait((func, args, kwargs))
        except queue.Full:
            log.warning("task queue full — dropping %s", getattr(func, "__name__", func))

    # ── perception ────────────────────────────────────────────────────────────
    def _sources(self, detections, scene, violence) -> dict:
        """This tick's votes: {source: {class: (value, threshold, fresh)}}."""
        threat = self.cfg.threat
        sources: dict[str, dict[str, tuple]] = {}
        if scene is not None:
            sources["scene"] = {c: (scene.probs.get(c, 0.0), scene.thresholds.get(c, 1.0),
                                    scene.fresh)
                                for c in CLASS_ORDER if c in scene.probs}
        if violence is not None:
            sources["violence"] = {"assault": violence}
        fire_conf = max((d.confidence for d in detections.fires
                         if d.label in threat.detector_fire_labels), default=0.0)
        weapon_conf = max((d.confidence for d in detections.weapons
                           if d.label in threat.detector_weapon_labels), default=0.0)
        sources["detector"] = {
            "weapon": (weapon_conf, self.cfg.detection.weapon_conf, True),
            "fire": (fire_conf, self.cfg.detection.fire_conf, True),
        }
        return sources

    # ── context inputs ────────────────────────────────────────────────────────
    def note_sensor(self, kind: str, at: float | None = None, **detail: Any) -> None:
        """A PIR trigger or door change (``sentrix.io.gpio``), on the
        monotonic clock. Presence context only: never a threat alone (D15)."""
        with self._sensor_lock:
            self._sensor_at[str(kind)] = time.monotonic() if at is None else float(at)

    def sensor_context(self, now: float) -> dict:
        """Seconds since the last PIR trigger / door change."""
        with self._sensor_lock:
            items = list(self._sensor_at.items())
        return {kind: round(now - at, 3) for kind, at in items if now >= at - 1.0}

    def _persons_recent(self, now: float, count: int, known: bool) -> int | None:
        """Most people in view over the last ``person_memory_seconds``."""
        self._persons_seen.append((now, count))
        if not known:
            return None
        memory = self.cfg.threat.person_memory_seconds
        return max((n for t, n in self._persons_seen if 0.0 <= now - t <= memory),
                   default=count)

    def _audio_is_new(self, audio: AudioResult) -> bool:
        """A window is a new observation only if it was not seen before and
        does not overlap the last one counted: with 4 s windows every 1 s, one
        impulse would otherwise confirm itself on three overlapping windows."""
        if audio.end == self._audio_last_end:
            return False
        self._audio_last_end = audio.end
        start = audio.start if audio.start is not None else audio.end
        if self._audio_counted_end is not None and start < self._audio_counted_end - 1e-6:
            return False
        self._audio_counted_end = audio.end
        return True

    def audio_for(self, now: float) -> tuple[AudioResult | None, dict]:
        """The audio result describing ``now`` and its alignment record; None
        when audio is off, stale or not yet classified (unavailable, D15)."""
        audio = self.audio
        if audio is None:
            return None, {"status": "disabled", "lag_s": None}
        lookup = getattr(audio, "result_for", None)
        if lookup is None:                      # an untimed stand-in
            return None, {"status": "untimed", "lag_s": None}
        try:
            return lookup(now)
        except Exception as exc:
            log.debug("audio lookup failed: %s", exc)
            return None, {"status": "error", "lag_s": None}

    def observe(self, frame: np.ndarray, now: float) -> FeatureVector:
        """Perception for one frame (steps 2-7): detections, scene, identity,
        per-class evidence, behaviour, audio -> the feature vector ``decide``
        scores. Public seam for the clip engine (docs/7 §2.4)."""
        detections = self.detector.detect(frame)
        person_present = bool(detections.persons)
        if person_present:
            self._last_person_at = now
        person_recent: bool | None = (
            self._last_person_at is not None and
            now - self._last_person_at <= self.cfg.threat.person_memory_seconds)
        if not detections.person_ok:
            person_recent = None        # unknown: the weapon gate cannot apply
        persons_recent = self._persons_recent(now, len(detections.persons),
                                              detections.person_ok)
        motion = self.motion.score(frame)

        scene = self.scene.observe(frame, now) if self.scene.available else None
        violence = None
        if self.violence is not None:
            self.violence.observe(frame, now)
            violence_score, _ = self.violence.result(now)
            violence = (violence_score, self.violence.threshold, self.violence.fresh)

        persons = tuple((p.track_id, tuple(p.bbox)) for p in detections.persons)
        if self._identity_worker is not None:
            # Live: the latest face check from the recognition thread.
            check = self._identity_worker.observe(frame, persons, now,
                                                  settled=self._settled)
        else:
            # Replay and evaluation: synchronous, so results are reproducible.
            check = FaceCheck(tuple(self.identity.match_faces(frame)), persons, now,
                              degraded=not getattr(self.identity, "available", False))
        identity = self.track_identity.update(check, persons, now)
        self._settled = identity.authorized
        identity_score = self.identity.score(identity, person_present)

        # Identity never relaxes threat evidence (evidence.py); it only feeds
        # the armed-mode intrusion rule and the dashboard.
        sources = self._sources(detections, scene, violence)
        # Audio votes only when its window describes this frame (D15); a new
        # window is a fresh observation, the same window again is not.
        audio, audio_align = self.audio_for(now)
        if audio is not None:
            fresh = self._audio_is_new(audio)
            audio_align = dict(audio_align, counted=fresh)
            sources.update(audio_votes(audio, fresh))
        context = {"persons": persons_recent, "motion": motion}
        evidence = self.evidence.update(now, sources, person_recent=person_recent,
                                        context=context)
        assault, robbery = evidence["assault"], evidence["robbery"]
        behaviour_score, behaviour_label = self.behaviour.classify(
            detections.persons, frame.shape,
            (assault.score, "fighting" if assault.confirmed else "normal"), now,
            robbery=(robbery.score, "robbery" if robbery.confirmed else "normal"))

        if audio is not None:
            audio_score, audio_label = audio.score, audio.label
        elif self.audio is None:
            audio_score, audio_label = 0.0, "disabled"
        else:
            audio_score, audio_label = 0.0, "audio unavailable"

        # Global ids for the dashboard; cheap, and only for tracked people.
        for person in detections.persons:
            x1, y1, x2, y2 = person.bbox
            crop = frame[max(0, y1):y2, max(0, x1):x2]
            person.global_id = self.reid.match_or_register(
                self.reid.embed(crop), now)

        return FeatureVector(
            vision=detections.person_score,
            audio=audio_score,
            motion=motion,
            behaviour=behaviour_score,
            identity=identity_score,
            weapon=evidence["weapon"].score,
            fire=evidence["fire"].score,
            is_night=1.0 if self.motion.is_night(frame) else 0.0,
            authorized=identity.authorized,
            person_present=person_present,
            behaviour_label=behaviour_label,
            audio_label=audio_label,
            person_count=len(detections.persons),
            identity_degraded=identity.degraded,
            extras={"detections": _with_identity(detections.to_dict(), identity.tracks),
                    "identity_name": identity.name,
                    "violence_score": round(violence[0], 4) if violence else None,
                    "scene": scene.to_dict() if scene is not None else None,
                    "evidence": {c: e.to_dict() for c, e in evidence.items()},
                    "sources": sources,
                    "person_recent": person_recent,
                    "faces": identity.faces,
                    "faces_matched": identity.matched,
                    "audio": audio_block(audio, audio_align, self.audio),
                    "fusion_context": {"persons_recent": persons_recent,
                                       "motion": round(motion, 4)},
                    "sensors": self.sensor_context(now)},
        )

    # ── side effects ──────────────────────────────────────────────────────────
    def _capture_snapshot(self, incident, frame: np.ndarray) -> None:
        """Save a snapshot and record its path on the incident.

        Runs on the worker thread. The result has to be written back, otherwise
        the incident row persists a null snapshot path — which is what happened
        before, because the worker's return value was simply discarded.
        """
        path = notifier.save_snapshot(frame, incident.id)
        if path is not None:
            incident.snapshot_path = path.name
            listeners.emit("snapshot", lambda: {
                "name": path.name, "incident_id": incident.id,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})

    def _capture_evidence(self, incident, frame: np.ndarray, result: TCIResult,
                          level: int, status: str, detections: list,
                          context: dict | None = None) -> None:
        """Seal an evidence bundle and record its identifiers on the incident."""
        record = vault.save(frame, result, level, status, incident.id, detections,
                            context=context)
        if record is not None and incident.evidence_bundle is None:
            # Keep the first bundle of the incident: it is the one closest to
            # the trigger, which is what a reviewer wants to see first.
            incident.evidence_bundle = record.bundle_id
            incident.evidence_hash = record.sha256

    @staticmethod
    def _create_dispatch(incident, result: TCIResult, level: int) -> None:
        evidence = None
        if incident.evidence_bundle is not None:
            evidence = SimpleNamespace(bundle_id=incident.evidence_bundle,
                                       sha256=incident.evidence_hash)
        dispatch_service.create(result, level, incident.id, evidence=evidence)

    def _evidence_context(self, result: TCIResult) -> dict:
        """Why this frame is being sealed, from the same tick (CLAUDE.md M3).

        The first live test's sidecars held only YOLO boxes as repr strings,
        so a "weapon" bundle showed three person boxes and nothing about the
        scene classifier that had raised it.
        """
        features = result.features
        extras = features.extras
        return {
            "session": self.session_id,
            "trigger": extras.get("evidence"),
            "scene": extras.get("scene"),
            "person_recent": extras.get("person_recent"),
            "identity": {"authorized": features.authorized,
                         "name": extras.get("identity_name"),
                         "degraded": features.identity_degraded,
                         "faces": extras.get("faces"),
                         "faces_matched": extras.get("faces_matched")},
        }

    def _act(self, incident, plan, frame: np.ndarray, result: TCIResult,
             level: int, status: str, now: float) -> None:
        """Incident-scoped side effects. Each fires at most once per incident."""
        if incident is None:
            return
        claim = self.incidents.claim

        if plan.snapshot and claim(incident, "snapshot"):
            self._submit(self._capture_snapshot, incident, frame.copy())

        if plan.evidence and self.incidents.should_capture_evidence(incident, now):
            incident.evidence_count += 1
            incident.last_evidence_at = now
            extras = result.features.extras
            detections = extras.get("detections", {}).get("all", [])
            self._submit(self._capture_evidence, incident, frame.copy(), result,
                         level, status, detections, self._evidence_context(result))

        evidence_state = ((result.features.extras or {}).get("evidence") or {}).get(
            result.incident_type, {}).get("state")
        if (plan.sos and "sos" not in incident.actions
                and self.sos.wants(result.incident_type, level, evidence_state)):
            # Claimed only once the service wants this incident type, so an
            # incident that becomes a weapon after an experimental L2 still
            # opens a request. Queued after the snapshot and evidence above,
            # so the trigger carries their paths; never dropped (D8).
            incident.actions.add("sos")
            self._submit_or_run(self._sos_trigger, incident, result, level, now)

        # Once the SOS service owns an incident it owns every message about it
        # (D8: SMS only offline or unseen after 10 min; the legacy L4 SMS folds
        # into the request's level_rise reminder, the L5 call into its rise5
        # step) and its one dispatch package (opened with the request at L3).
        # The siren stays: a physical deterrent is not a message.
        sos_owned = "sos" in incident.actions

        if plan.sms and not sos_owned and claim(incident, "sms"):
            self._submit(notifier.send_sms, result, level, status)

        if plan.siren and claim(incident, "siren"):
            # Synchronous: a physical deterrent must not queue behind disk I/O.
            try:
                self.siren.activate()
            except Exception as exc:
                log.error("siren failed: %s", exc)

        if plan.dispatch and not sos_owned and claim(incident, "dispatch"):
            # Queued after the evidence capture above (one FIFO worker), so the
            # package carries the sealed bundle instead of "n/a".
            self._submit(self._create_dispatch, incident, result, level)

        if plan.call and not sos_owned and claim(incident, "call"):
            self._submit(notifier.make_call, result, level, status)

    def _submit_or_run(self, func, *args) -> None:
        """Queue behind earlier side effects; run inline if the queue is full
        (an SOS trigger must never be dropped)."""
        try:
            self._tasks.put_nowait((func, args, {}))
        except queue.Full:
            log.warning("task queue full — running %s inline",
                        getattr(func, "__name__", func))
            try:
                func(*args)
            except Exception as exc:
                log.error("%s failed: %s", getattr(func, "__name__", func), exc)

    def _sos_trigger(self, incident, result: TCIResult, level: int, now: float) -> None:
        """Hand the incident to the SOS service (worker thread, after its
        snapshot and first evidence capture)."""
        extras = result.features.extras or {}
        votes = extras.get("evidence") or {}
        self.sos.trigger(SosTrigger(
            incident_id=incident.id, level=level, incident_type=result.incident_type,
            evidence_state=(votes.get(result.incident_type) or {}).get("state", "none"),
            tci=result.tci, reason=result.reason, session_id=self.session_id,
            camera_id="CAM_0", armed=runtime_mode.is_armed(), at_wall=time.time(),
            at_mono=now, snapshot_path=incident.snapshot_path,
            evidence_bundle=incident.evidence_bundle,
            evidence_hash=incident.evidence_hash, votes=votes))

    def _level_changed(self, level: int, status: str, result: TCIResult,
                       incident, now: float) -> None:
        """Committed level moved: Event Log history and the SOS service."""
        previous, self._committed_level = self._committed_level, level
        votes = (result.features.extras or {}).get("evidence") or {}
        state.record_level_change(level, previous, status, result.reason,
                                  incident.id if incident else None,
                                  result.incident_type)
        try:
            self.sos.on_level(LevelChange(
                level=level, previous_level=previous,
                incident_id=incident.id if incident else None,
                incident_type=result.incident_type,
                evidence_state=(votes.get(result.incident_type) or {}).get("state", "none"),
                tci=result.tci, at_wall=time.time(), at_mono=now))
        except Exception as exc:
            log.error("sos.on_level failed: %s", exc)

    # ── main entry point ──────────────────────────────────────────────────────
    def process(self, frame: np.ndarray, now: float | None = None,
                label: str | None = None) -> dict[str, Any]:
        """Score one frame end to end (``observe`` then ``decide``). Used by
        both live and replay."""
        now = time.monotonic() if now is None else now
        started = time.perf_counter()
        # Inference operates on a pristine copy; the HUD is drawn separately.
        features = self.observe(frame, now)
        return self.decide(frame, features, now, label, started=started)

    def decide(self, frame: np.ndarray, features: FeatureVector, now: float,
               label: str | None = None, *, started: float | None = None,
               extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Steps 8-10 for one observation: TCI, committed level, incident and
        its side effects, event log, state, overlay. ``frame`` is the clean
        frame the snapshot/evidence capture (the clip engine passes the
        trigger keyframe); ``started`` is the ``perf_counter`` at which the
        observation began, for the latency figures; ``extra`` goes into the
        tick row's ``extra`` key (the clip engine marks its decisions
        ``{"mode": "clip" | "fast_lane", "clip": seq}``)."""
        started = time.perf_counter() if started is None else started
        self._frame_count += 1
        clean = frame
        result = self.tci.compute(features, now)
        # The policy reads the raw evidence: it is already confirmed over
        # several ticks, so smoothing it again would only delay the alarm.
        level_state = self.policy.update(result.raw_tci, now)
        level, status = level_state.level, level_state.status

        incident, closed = self.incidents.update(
            level, result.tci, result.incident_type, now,
            features=features.model_features(), reason=result.reason,
            authorized=features.authorized, person_present=features.person_present,
            trigger=features.extras.get("evidence"))
        if incident is not None and closed is None:
            self._persist_open(incident, now)
        if level != self._committed_level:
            self._level_changed(level, status, result, incident, now)
        plan = plan_for(level)
        if incident is not None and plan.any_side_effect:
            self._act(incident, plan, clean, result, level, status, now)
        if closed is not None:
            self._submit(database.save_incident, closed, self.session_id)

        latency = time.perf_counter() - started
        self._latencies.append(latency)
        self.events.write(result, level, status,
                          incident.id if incident else None, latency, label, extra=extra)

        ordered = sorted(self._latencies)
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]

        state.update(
            alarm=self._alarm_block(level, status, level_state, incident, now),
            evidence_state=(features.extras.get("evidence") or {}).get(
                result.incident_type, {}).get("state", "none"),
            tci=result.tci, raw_tci=result.raw_tci, level=level, status=status,
            reason=result.reason, incident_type=result.incident_type,
            incident_id=incident.id if incident else None,
            features=features.model_features(), top_factors=result.top_factors,
            contributions=result.contributions,
            audio=features.extras.get("audio") or {"status": "disabled"},
            fusion=_fusion_block(features, result),
            uncertainty=result.uncertainty,
            confidence_band=list(result.confidence_band),
            fusion_source=result.source, audio_label=features.audio_label,
            behaviour_label=features.behaviour_label,
            person_count=features.person_count, authorized=features.authorized,
            scene=features.extras.get("scene"),
            identity_degraded=features.identity_degraded,
            latency_ms=round(latency * 1000, 2),
            latency_p95_ms=round(p95 * 1000, 2),
            queue_depth=self._tasks.qsize(), camera_online=self.source.online,
            events_logged=self.events.rows,
        )
        telemetry.record("pipeline", "frame", None, latency)

        self._overlay = Overlay(
            tci=result.tci, level=level, status=status,
            behaviour_label=features.behaviour_label, weapon=features.weapon,
            fire=features.fire, authorized=features.authorized,
            identity_degraded=features.identity_degraded,
            detections=features.extras.get("detections", {}), at=now)
        if self._close_requested is not None:
            self._apply_close_request(result, now)
        return {"tci": result.tci, "level": level, "status": status,
                "incident_type": result.incident_type,
                "features": features.model_features(), "latency": latency,
                "incident_id": incident.id if incident else None,
                "sources": features.extras.get("sources") or {}}

    # ── incident control (demo reset) ─────────────────────────────────────────
    def request_close_incident(self, reason: str = "demo") -> bool:
        """Close the open incident after the next decision, on the inference
        thread, and clear the temporal state (level dwell, evidence windows,
        intrusion timer) so the next scene has to be confirmed afresh and opens
        a new incident (and request). Safe from any thread. Returns whether an
        incident was open when asked. Used by ``demo.close_incident`` (test
        mode only; the SOS service checks)."""
        self._close_requested = reason or "demo"
        return self.incidents.current is not None

    def _apply_close_request(self, result: TCIResult, now: float) -> None:
        reason, self._close_requested = self._close_requested, None
        closed = self.incidents.close_now(now)
        if closed is not None:
            self._submit(database.save_incident, closed, self.session_id)
            self.events.audit("incident_closed", incident_id=closed.id, reason=reason)
            log.info("incident %s closed on request (%s)", closed.short_id, reason)
        self.policy.reset()
        self.evidence.reset()
        self.tci.reset()
        if self._committed_level != 1:
            self._level_changed(1, "SECURE", result, None, now)

    def _save_open_incident(self, incident) -> None:
        """Synchronous save at shutdown. Measurement pipelines replace it with a
        no-op (like ``_submit``), so a replay never writes the user's database."""
        database.save_incident(incident, self.session_id)

    def _persist_open(self, incident, now: float) -> None:
        """Write the open incident when it opens, escalates, or its peak moves.

        A row existed only once an incident closed, so a crash or a hard kill
        lost it (CLAUDE.md section 5). A higher level is written at once; other
        peak moves at most every ``persist_interval_seconds``.
        """
        moved = incident.saved_peak is not incident.peak
        escalated = incident.peak_level != incident.saved_level
        due = (incident.saved_at is None or escalated or
               (moved and now - incident.saved_at >= self.cfg.incident.persist_interval_seconds))
        if due:
            incident.saved_at, incident.saved_peak = now, incident.peak
            incident.saved_level = incident.peak_level
            self._submit(database.save_incident, incident, self.session_id, True)

    def _alarm_block(self, level: int, status: str, level_state, incident,
                     now: float) -> dict:
        """The committed alarm, kept apart from the per-tick reading.

        The dashboard showed the committed level next to the latest tick's TCI
        and reason ("TCI 0.11 L4 HIGH"). The alarm block is what the level
        means: since when, which incident, its peak type and reason, and when
        it may step down.
        """
        peak = incident.peak if incident is not None else None
        return {
            "level": level, "status": status,
            "since_s": round(max(0.0, now - level_state.since), 1),
            "incident_id": incident.id if incident is not None else None,
            "incident_type": peak.incident_type if peak is not None else "normal",
            "headline": peak.reason if peak is not None else "",
            "peak_tci": round(peak.tci, 4) if peak is not None else 0.0,
            "deescalate_in": (round(level_state.deescalate_in, 1)
                              if level_state.deescalate_in is not None else None),
        }

    # ── live loop: capture, inference and preview are decoupled ──────────────
    def run_live(self, stop: threading.Event,
                 strategy: InferenceStrategy | None = None) -> None:
        """Capture loop; runs until ``stop`` is set.

        Every captured frame is composited with the latest inference overlay and
        becomes the preview, so the video runs at the camera's rate whatever the
        AI rate is. Inference runs on its own thread (``strategy.run``; the tick
        loop by default, the clip engine in clip mode), fed by
        ``strategy.on_frame`` from this thread. In the first live test the
        preview was drawn only after inference, so the video froze at the AI
        rate (0.3 Hz, CLAUDE.md §5).
        """
        self._strategy = strategy or self._build_strategy()
        identity = getattr(self, "identity", None)
        if getattr(identity, "available", False) and self._identity_worker is None:
            self._identity_worker = IdentityWorker(identity)
        worker = threading.Thread(target=self._strategy.run, args=(stop,),
                                  daemon=True, name=f"sentrix-inference-{self._strategy.name}")
        worker.start()
        try:
            while not stop.is_set():
                try:
                    fresh = self._capture_step()
                except Exception as exc:
                    log.error("capture step failed (non-fatal): %s", exc)
                    fresh = False
                if not fresh:
                    stop.wait(0.004)
        finally:
            if stop.is_set():
                # No frame reaches a recording from here on, and shutdown may
                # wait up to 5 s for the inference thread (a clip mid-run)
                # before it closes the recorder: stop it now, as ``shutdown``,
                # so the writer's camera-gap check does not save it as
                # ``camera_lost``.
                self._stop_recording_quietly("shutdown")
            try:
                self._strategy.wake()
            except Exception as exc:
                log.debug("strategy wake failed: %s", exc)
            worker.join(timeout=5.0)
            if self._identity_worker is not None:
                self._identity_worker.stop()
                self._identity_worker = None

    def _build_strategy(self) -> InferenceStrategy:
        if self.cfg.clip.mode == "clip":
            # The 4-second clip engine (docs/8_RASPBERRY_PI5_INTEGRATION.md T1, track T-CLIP).
            try:
                from sentrix.pipeline.clip import build_strategy
            except ImportError as exc:
                log.error("SENTRIX_PIPELINE_MODE=clip but the clip engine is "
                          "missing (%s); running the tick loop", exc)
            else:
                return build_strategy(self)
        return TickStrategy(self)

    def note_inference(self, captured_at: float) -> None:
        """An inference finished on a frame captured at ``captured_at``: the AI
        rate and the frame age on the dashboard (strategies call this)."""
        done = time.monotonic()
        self._ai_times.append(done)
        state.update(frame_age_ms=round((done - captured_at) * 1000, 1))

    def _capture_step(self) -> bool:
        """Publish the newest camera frame as the preview and offer it to AI."""
        frame, seq, captured_at = self.source.latest(self._last_seq)
        now = time.monotonic()
        if frame is None:
            if not self.source.online and not self._offline:
                self._offline = True
                state.update(camera_online=False, reason="No camera signal",
                             video_fps=0.0)
                self._publish(_placeholder())
            return False
        self._offline = False
        self._last_seq = seq
        self._raw = frame
        recorder = getattr(self, "_recorder", None)
        if recorder is not None and recorder.wants_frames:
            # The source's own copy, before the HUD is drawn on another copy:
            # a recording holds the camera's pixels only (D20). The tap is
            # O(1); it must never cost the camera a frame.
            try:
                recorder.on_frame(frame, seq, captured_at)
            except Exception as exc:
                log.debug("recorder tap failed: %s", exc)
        self._video_times.append(now)
        self._strategy.on_frame(frame, captured_at)
        self._publish(self._draw(frame.copy(), self._overlay, now))
        if now - self._rates_at >= 1.0:
            self._rates_at = now
            self._video_fps = _rate(self._video_times, now)
            self._ai_hz = _rate(self._ai_times, now)
            state.update(video_fps=round(self._video_fps, 1),
                         ai_hz=round(self._ai_hz, 2), fps=round(self._ai_hz, 2),
                         camera_online=True, mic_delivering=self._mic_delivering())
        return True

    def _mic_delivering(self) -> bool:
        """Is an open microphone delivering sound (``AudioEngine.available``,
        what ``/api/health`` ``audio`` says)? Whether anything classified it
        is another matter: with the sound model off, or before the first
        full window, it records but labels nothing."""
        audio = getattr(self, "audio", None)
        return bool(audio is not None and getattr(audio, "available", False))

    def _inference_loop(self, stop: threading.Event) -> None:
        """The tick loop's inference thread (kept for callers of the old name)."""
        TickStrategy(self).run(stop)

    def latest_raw(self) -> np.ndarray | None:
        """The newest camera frame without the HUD, for enrolment from the
        server's own camera (the browser cannot open a webcam the server holds)."""
        frame = self._raw
        return None if frame is None else frame.copy()

    def _publish(self, frame: np.ndarray) -> None:
        with self._preview_lock:
            self._preview = frame
            self._preview_seq += 1

    @property
    def preview_seq(self) -> int:
        return self._preview_seq

    def latest_jpeg(self, quality: int = 70) -> tuple[int, bytes | None]:
        """The preview as JPEG, encoded once per frame however many viewers."""
        with self._preview_lock:
            seq, frame, cached = self._preview_seq, self._preview, self._jpeg
        if cached is not None and cached[0] == seq:
            return cached
        if frame is None or cv2 is None:
            return seq, None
        ok, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            return seq, None
        encoded = (seq, buffer.tobytes())
        with self._preview_lock:
            if self._preview_seq == seq:
                self._jpeg = encoded
        return encoded

    # ── training recordings (D20) ─────────────────────────────────────────────
    @property
    def recorder(self) -> Recorder | None:
        """The training recorder once :meth:`get_recorder` built it, else None.
        Status readers (``/api/health``) use this and never build it."""
        return getattr(self, "_recorder", None)

    def get_recorder(self) -> Recorder:
        """The training recorder, built on first use (the dashboard, or the
        app's start-up): frames come from the capture thread (``_capture_step``),
        sound from the microphone's sample listener. Nothing records until
        :meth:`start_recording` is called, so the replay, evaluation and bench
        tools that drive this pipeline never write a file.

        On creation the ``.part`` files a crash left are finished
        (``recover``) and recordings past ``keep_days`` deleted (``prune``),
        outside the pipeline's lock: a remux can take seconds, and the
        recorder's own lock already keeps a ``start`` from racing them."""
        with self._recorder_lock:
            recorder = self._recorder
            if recorder is not None:
                return recorder
            events = getattr(self, "events", None)
            recorder = Recorder(audit=getattr(events, "audit", None))
            self._recorder = recorder
            self._listen_for_recording(recorder)
        for step in (recorder.recover, recorder.prune):
            try:
                step()
            except Exception as exc:
                log.error("recordings: %s failed: %s", step.__name__, exc)
        return recorder

    def _listen_for_recording(self, recorder: Recorder) -> None:
        """Hand every captured microphone block to the recorder. The engine
        keeps the listener across microphone hot-plugs; a replayed soundtrack
        (``FileAudio``) has none and the recording is then video only."""
        add = getattr(getattr(self, "audio", None), "add_sample_listener", None)
        if not callable(add):
            return
        rate = int(self.cfg.windows.audio_sample_rate)

        def listener(samples: np.ndarray, end_at: float) -> None:
            recorder.on_audio(samples, end_at, rate)

        try:
            add(listener)
            self._recorder_listener = listener
        except Exception as exc:
            log.error("recordings: the microphone listener was not added: %s", exc)

    def microphone_status(self) -> dict | None:
        """The microphone as the audio engine reports it, or None when audio
        is off (``SENTRIX_AUDIO=0``) or the engine cannot say."""
        return audio_status(getattr(self, "audio", None))

    def recording_fps(self) -> float:
        """The recording's frame rate: the rate the camera declares
        (Picamera2's ``status()["fps"]``), else the measured video rate
        snapped to a common one (a webcam asked for 30 fps delivers 15 in a
        dark room), else the configured capture rate; within 5-30 fps."""
        fps = None
        status = getattr(self.source, "status", None)
        if callable(status):
            try:
                fps = float((status() or {}).get("fps") or 0.0)
            except Exception as exc:            # a camera that cannot say
                log.debug("camera status failed: %s", exc)
                fps = None
        if not fps or not np.isfinite(fps) or fps <= 0:
            measured = float(getattr(self, "_video_fps", 0.0) or 0.0)
            if measured > 0:
                # The slowest step the camera is not faster than: a slot per
                # real frame, never two real frames for one slot (one dropped).
                fps = float(next((step for step in RECORDING_FPS_STEPS
                                  if measured <= step * (1.0 + RECORDING_FPS_TOLERANCE)),
                                 RECORDING_FPS_STEPS[-1]))
            else:
                fps = float(self.cfg.pipeline.capture_fps)
        return min(30.0, max(5.0, fps))

    def _recording_context(self, frame: np.ndarray, fps: float) -> dict:
        """What the sidecar says about where the footage came from."""
        link = state.get().get("link") or {}
        return {"camera": redact_url(str(getattr(self.source, "name", "camera"))),
                "profile": self.cfg.node.profile,
                "device_id": link.get("device_id"),
                "frame_size": [int(frame.shape[1]), int(frame.shape[0])],
                "fps": fps,
                "video_fps_measured": round(float(getattr(self, "_video_fps", 0.0) or 0.0), 2),
                "session": getattr(self, "session_id", None)}

    def start_recording(self, *, label: str = "", by: str = "") -> dict:
        """Start a training recording of the camera's raw frames (and the
        microphone's sound while one is connected). Raises
        :class:`RecorderError` (``conflict``) before the camera's first frame,
        while it is offline, while already recording or saving, and when the
        disk or the recordings' quota is short. Returns :meth:`recording_status`."""
        frame = getattr(self, "_raw", None)
        if frame is None or getattr(self, "_offline", False):
            raise RecorderError("conflict", "the camera has not delivered a frame: "
                                            "nothing to record yet")
        recorder = self.get_recorder()
        microphone = self.microphone_status()
        device = microphone.get("device") if microphone and microphone.get("available") else None
        fps = self.recording_fps()
        status = recorder.start(label=label, by=by, fps=fps, audio_device=device,
                                meta=self._recording_context(frame, fps))
        return dict(status, microphone=microphone)

    def stop_recording(self, *, by: str = "", reason: str = "user") -> dict:
        """Stop the recording; the files are saved on the recorder's own thread
        (``finalizing`` until then). ``conflict`` when nothing is recording."""
        status = self.get_recorder().stop(by=by, reason=reason)
        return dict(status, microphone=self.microphone_status())

    def recording_status(self) -> dict:
        """The recorder's status plus ``microphone`` (the audio engine's)."""
        return dict(self.get_recorder().status(), microphone=self.microphone_status())

    def _stop_recording_quietly(self, reason: str) -> None:
        """Ask a recording in progress to stop without waiting for its files
        (never builds the recorder, never raises)."""
        recorder = getattr(self, "_recorder", None)
        if recorder is None or not recorder.wants_frames:
            return
        try:
            recorder.stop(reason=reason)
        except RecorderError:
            pass                                # it stopped by itself meanwhile
        except Exception as exc:
            log.error("stopping the recording failed: %s", exc)

    def _close_recorder(self) -> None:
        """Finish a recording in progress and stop listening to the microphone."""
        recorder = getattr(self, "_recorder", None)
        if recorder is None:
            return
        try:
            if not recorder.close(timeout=RECORDING_CLOSE_SECONDS):
                log.warning("the recording was still being saved at shutdown; "
                            "the next start recovers it")
        except Exception as exc:
            log.error("recorder close failed: %s", exc)
        listener, self._recorder_listener = getattr(self, "_recorder_listener", None), None
        remove = getattr(getattr(self, "audio", None), "remove_sample_listener", None)
        if listener is not None and callable(remove):
            try:
                remove(listener)
            except Exception as exc:
                log.debug("recorder listener removal failed: %s", exc)

    def replay(self, source: FrameSource, label: str | None = None,
               max_frames: int | None = None) -> list[dict[str, Any]]:
        """Run a finite source through the identical pipeline.

        This is how evaluation numbers are produced: same code, same config,
        same event-log schema as a live run.
        """
        self.reset()
        rows: list[dict[str, Any]] = []
        index = 0
        period = self.cfg.pipeline.inference_period
        while True:
            ok, frame = source.read()
            if not ok or frame is None:
                break
            rows.append(self.process(frame, now=index * period, label=label))
            index += 1
            if max_frames and index >= max_frames:
                break
        return rows

    def reset(self) -> None:
        """Clear all temporal state. Call between clips when replaying."""
        self.tci.reset()
        self.policy.reset()
        self.motion.reset()
        self.scene.reset()
        self.evidence.reset()
        self.track_identity.reset()
        self._settled = False
        self._last_person_at = None
        self._persons_seen.clear()
        self._audio_last_end = None
        self._audio_counted_end = None
        with self._sensor_lock:
            self._sensor_at.clear()
        if self.violence is not None:
            self.violence.reset()
        self.behaviour.reset()
        self.reid.reset()
        self.detector.reset_tracks()
        self._latencies.clear()
        self._frame_count = 0
        self._committed_level = 1
        self._close_requested = None

    # ── presentation ──────────────────────────────────────────────────────────
    def _stale_overlay_seconds(self) -> float:
        """Overlay age at which the HUD says "AI STALLED". The clip engine
        refreshes the overlay once per hop plus the clip compute, so in clip
        mode the limit is at least ``hop * (1 + budget_fraction)``."""
        limit = self.cfg.pipeline.stale_overlay_seconds
        if getattr(self._strategy, "name", "tick") == "clip":
            clip = self.cfg.clip
            limit = max(limit, clip.hop_seconds * (1.0 + clip.budget_fraction))
        return limit

    def _draw(self, frame, overlay: "Overlay | None", now: float):
        """Composite the latest inference overlay, and the REC badge while
        recording, onto a captured frame (the preview's copy)."""
        return self._draw_recording(self._draw_hud(frame, overlay, now), now)

    def _draw_recording(self, frame, now: float):
        """A red dot and ``REC mm:ss`` under the banner while a training
        recording runs, so whoever watches the dashboard knows. The recorder
        took the camera's frame before this copy was drawn on: the badge is
        never in the recording."""
        elapsed = self._recording_elapsed(now)
        if cv2 is None or frame is None or elapsed is None:
            return frame
        try:
            x = max(10, frame.shape[1] - 120)
            cv2.circle(frame, (x, 92), 6, (0, 0, 255), -1)
            cv2.putText(frame, f"REC {elapsed // 60:02d}:{elapsed % 60:02d}", (x + 12, 97),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
        except Exception as exc:
            log.debug("REC badge draw failed: %s", exc)
        return frame

    def _recording_elapsed(self, now: float) -> int | None:
        """Whole seconds the recording in progress has run (the REC badge),
        or None while nothing records. Read from the recorder's own session:
        a second start that is refused must not reset it."""
        recorder = getattr(self, "_recorder", None)
        if recorder is None or not recorder.wants_frames:
            return None
        since = getattr(recorder, "recording_since", None)
        return max(0, int(now - since)) if since is not None else 0

    def _draw_hud(self, frame, overlay: "Overlay | None", now: float):
        """The inference overlay: banner, level, identity, rates and boxes."""
        if cv2 is None or frame is None:
            return frame
        try:
            height, width = frame.shape[:2]
            # Darken the banner strip only: this runs on every captured frame,
            # and blending a full-frame copy cost ~10 ms at 1080p.
            top = frame[:75]
            cv2.addWeighted(np.full_like(top, (10, 10, 20)), 0.7, top, 0.3, 0, top)
            info_x = max(width - 250, 10)
            rates = f"VIDEO {self._video_fps:.0f} fps  AI {self._ai_hz:.1f} Hz"
            if overlay is None:
                cv2.putText(frame, "SENTRIX  starting analysis", (12, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (200, 200, 200), 2)
                cv2.putText(frame, rates, (info_x, 66), cv2.FONT_HERSHEY_SIMPLEX,
                            0.45, (200, 200, 200), 1)
                return frame

            colour = LEVEL_COLOURS.get(overlay.level, (255, 255, 255))
            age = now - overlay.at
            cv2.putText(frame, f"SENTRIX  TCI {overlay.tci:.2f}", (12, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, colour, 2)
            cv2.putText(frame,
                        f"L{overlay.level} {overlay.status}  [{overlay.behaviour_label}]",
                        (12, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.6, colour, 2)
            cv2.putText(frame,
                        f"WPN {overlay.weapon:.2f}  FIRE {overlay.fire:.2f}",
                        (info_x, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.5, colour, 1)
            if overlay.identity_degraded:
                auth_text, auth_colour = "ID N/A", (0, 165, 255)
            elif overlay.authorized:
                auth_text, auth_colour = "AUTHORISED", (0, 200, 80)
            else:
                auth_text, auth_colour = "UNKNOWN", (0, 0, 220)
            cv2.putText(frame, auth_text, (info_x, 46),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, auth_colour, 2)
            viewers = state.remote_viewers
            if viewers:
                # Anyone watching through the Portal is visible in the room.
                cv2.putText(frame, f"REMOTE VIEW: {viewers}", (12, 92),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 255), 2)
            if age > self._stale_overlay_seconds():
                # Boxes this old describe a scene that has moved on.
                cv2.putText(frame, f"AI STALLED {age:.0f}s  VIDEO {self._video_fps:.0f} fps",
                            (info_x, 66), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                            (0, 165, 255), 1)
                return frame
            cv2.putText(frame, rates, (info_x, 66), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                        (200, 200, 200), 1)

            detections = overlay.detections
            palette = {"persons": colour, "weapons": (0, 0, 255),
                       "fires": (0, 140, 255)}
            for group, box_colour in palette.items():
                for det in detections.get(group, []):
                    x1, y1, x2, y2 = det["bbox"]
                    cv2.rectangle(frame, (x1, y1), (x2, y2), box_colour, 2)
                    tag = det.get("identity") or det["label"]
                    if det.get("track_id") is not None:
                        tag += f" #{det['track_id']}"
                    cv2.putText(frame, f"{tag} {det['conf']:.2f}",
                                (x1, max(y1 - 8, 14)), cv2.FONT_HERSHEY_SIMPLEX,
                                0.5, box_colour, 2)
            return frame
        except Exception as exc:
            log.debug("HUD draw failed: %s", exc)
            return frame

    def latest_frame(self) -> np.ndarray:
        with self._preview_lock:
            frame = self._preview
        return frame if frame is not None else _placeholder()

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def shutdown(self) -> None:
        log.info("shutting down pipeline")
        # First, while the microphone still delivers the recording's last
        # block and before the camera is released: its files must be whole.
        self._close_recorder()
        self._stop.set()
        try:
            # Behind every queued side effect (an SOS trigger, an incident
            # save), so they run before the worker exits.
            self._tasks.put(_DRAIN, timeout=1.0)
        except queue.Full:
            log.warning("task queue full at shutdown; some side effects may be lost")
        self._worker.join(timeout=10.0)
        if self.incidents.current is not None:
            # Keep the open incident: it would otherwise vanish on restart.
            self._save_open_incident(self.incidents.current)
        if self.audio is not None:
            self.audio.stop()
        self.source.release()
        self.events.close()
        telemetry.close()
        log.info("pipeline stopped")


def _with_identity(detections: dict, tracks: dict) -> dict:
    """Label each person box with its track's identity for the HUD and logs."""
    for person in detections.get("persons", []):
        verdict = tracks.get(person.get("track_id"))
        if verdict is not None:
            person["identity"] = verdict["name"] or verdict["state"]
    return detections


def _rate(times: deque, now: float, window: float = 3.0) -> float:
    """Events per second over the last ``window`` seconds."""
    recent = [t for t in times if now - t <= window]
    if len(recent) < 2:
        return 0.0
    return (len(recent) - 1) / max(recent[-1] - recent[0], 1e-6)


def _fusion_block(features: FeatureVector, result: TCIResult) -> dict:
    """The leading class's evidence state, rule and modalities (D15)."""
    evidence = (features.extras or {}).get("evidence") or {}
    leading = evidence.get(result.incident_type) or {}
    if result.source == "intrusion":
        # Not a threat class: presence context, held in the L2 band (D15).
        leading = {"state": "context", "rule": "unrecognised person (L2 only)",
                   "modalities": {}}
    return {"class": result.incident_type, "state": leading.get("state", "none"),
            "rule": leading.get("rule"), "modalities": leading.get("modalities") or {},
            "context": (features.extras or {}).get("fusion_context") or {},
            "sensors": (features.extras or {}).get("sensors") or {}}


def audio_status(engine: Any) -> dict | None:
    """``AudioEngine.status()`` (which microphone, its native rate, ...), or
    None when there is no engine or it cannot say (an older engine, a replayed
    soundtrack). For the recorder, ``/api/health`` and ``/api/node``."""
    status = getattr(engine, "status", None)
    if not callable(status):
        return None
    try:
        result = status()
    except Exception as exc:
        log.debug("audio status failed: %s", exc)
        return None
    return dict(result) if isinstance(result, dict) else None


def audio_block(result: AudioResult | None, align: dict, engine: Any = None) -> dict:
    """The ``audio`` record of a tick (state, event log): what was heard, the
    window it covers and whether it was aligned with the frame (D15)."""
    block = {"status": align.get("status"), "lag_s": align.get("lag_s"),
             "tolerance_s": align.get("tolerance_s"),
             "model": getattr(engine, "model_name", None) if engine is not None else None}
    if result is not None:
        block.update(result.to_dict())
        block["counted"] = align.get("counted")
        block["neutral"] = neutral_reason(result)
    return block


_pipeline: Pipeline | None = None
_pipeline_lock = threading.Lock()


def get_pipeline() -> Pipeline:
    """Process-wide singleton, created on first use."""
    global _pipeline
    with _pipeline_lock:
        if _pipeline is None:
            _pipeline = Pipeline(enable_audio=settings.pipeline.audio_enabled)
        return _pipeline


def shutdown_pipeline() -> None:
    global _pipeline
    with _pipeline_lock:
        if _pipeline is not None:
            _pipeline.shutdown()
            _pipeline = None


__all__ = ["Pipeline", "InferenceStrategy", "TickStrategy", "get_pipeline",
           "shutdown_pipeline", "audio_status", "RECORDING_FPS_STEPS"]
