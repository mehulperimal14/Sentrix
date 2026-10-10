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
7. audio (read from the capture thread, never blocking)
8. assemble the feature vector; TCI, then hysteretic level commit on the raw
   (already confirmed) evidence
9. incident update, then incident-scoped side effects
10. publish state, append the event log, draw the HUD

Overlays are drawn on a copy. The previous code annotated ``combined_frame`` and
then passed the same array to the encoders on later ticks, so HUD text and
bounding boxes were baked into the pixels the classifiers saw — the models were
partly reading the system's own overlay.
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import numpy as np

from sentrix.alerting.dispatch import dispatch_service
from sentrix.alerting.notifier import notifier
from sentrix.config import settings
from sentrix.evidence.vault import vault
from sentrix.fusion.evidence import CLASS_ORDER, EvidenceAccumulator
from sentrix.fusion.features import FeatureVector
from sentrix.fusion.policy import LevelPolicy
from sentrix.fusion.tci import TCIEngine, TCIResult
from sentrix.io.actuators import build_siren
from sentrix.io.sources import FrameSource, _placeholder, build_source
from sentrix.perception.audio import AudioEngine
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
from sentrix.storage import database
from sentrix.utils.logging import get_logger, telemetry

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

    # ── background side effects ───────────────────────────────────────────────
    def _task_loop(self) -> None:
        while not self._stop.is_set():
            try:
                func, args, kwargs = self._tasks.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
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

    def _observe(self, frame: np.ndarray, now: float) -> FeatureVector:
        detections = self.detector.detect(frame)
        person_present = bool(detections.persons)
        if person_present:
            self._last_person_at = now
        person_recent: bool | None = (
            self._last_person_at is not None and
            now - self._last_person_at <= self.cfg.threat.person_memory_seconds)
        if not detections.person_ok:
            person_recent = None        # unknown: the weapon gate cannot apply

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
        evidence = self.evidence.update(now, sources, person_recent=person_recent)
        assault, robbery = evidence["assault"], evidence["robbery"]
        behaviour_score, behaviour_label = self.behaviour.classify(
            detections.persons, frame.shape,
            (assault.score, "fighting" if assault.confirmed else "normal"), now,
            robbery=(robbery.score, "robbery" if robbery.confirmed else "normal"))

        if self.audio is not None:
            audio = self.audio.result()
            audio_score, audio_label = audio.score, audio.label
        else:
            audio_score, audio_label = 0.0, "disabled"

        # Global ids for the dashboard; cheap, and only for tracked people.
        for person in detections.persons:
            x1, y1, x2, y2 = person.bbox
            crop = frame[max(0, y1):y2, max(0, x1):x2]
            person.global_id = self.reid.match_or_register(
                self.reid.embed(crop), now)

        return FeatureVector(
            vision=detections.person_score,
            audio=audio_score,
            motion=self.motion.score(frame),
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
                    "faces_matched": identity.matched},
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

        if plan.sms and claim(incident, "sms"):
            self._submit(notifier.send_sms, result, level, status)

        if plan.siren and claim(incident, "siren"):
            # Synchronous: a physical deterrent must not queue behind disk I/O.
            try:
                self.siren.activate()
            except Exception as exc:
                log.error("siren failed: %s", exc)

        if plan.dispatch and claim(incident, "dispatch"):
            # Queued after the evidence capture above (one FIFO worker), so the
            # package carries the sealed bundle instead of "n/a".
            self._submit(self._create_dispatch, incident, result, level)

        if plan.call and claim(incident, "call"):
            self._submit(notifier.make_call, result, level, status)

    # ── main entry point ──────────────────────────────────────────────────────
    def process(self, frame: np.ndarray, now: float | None = None,
                label: str | None = None) -> dict[str, Any]:
        """Score one frame end to end. Used by both live and replay."""
        now = time.monotonic() if now is None else now
        started = time.perf_counter()
        self._frame_count += 1

        # Inference operates on a pristine copy; the HUD is drawn separately.
        clean = frame
        features = self._observe(clean, now)
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
        plan = plan_for(level)
        if incident is not None and plan.any_side_effect:
            self._act(incident, plan, clean, result, level, status, now)
        if closed is not None:
            self._submit(database.save_incident, closed, self.session_id)

        latency = time.perf_counter() - started
        self._latencies.append(latency)
        self.events.write(result, level, status,
                          incident.id if incident else None, latency, label)

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
        return {"tci": result.tci, "level": level, "status": status,
                "incident_type": result.incident_type,
                "features": features.model_features(), "latency": latency,
                "incident_id": incident.id if incident else None,
                "sources": features.extras.get("sources") or {}}

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
    def run_live(self, stop: threading.Event) -> None:
        """Capture loop; runs until ``stop`` is set.

        Every captured frame is composited with the latest inference overlay and
        becomes the preview, so the video runs at the camera's rate whatever the
        AI rate is. Inference runs on its own thread and always takes the newest
        frame through a single slot: frames it cannot keep up with are dropped,
        never queued. In the first live test the preview was drawn only after
        inference, so the video froze at the AI rate (0.3 Hz, CLAUDE.md §5).
        """
        identity = getattr(self, "identity", None)
        if getattr(identity, "available", False) and self._identity_worker is None:
            self._identity_worker = IdentityWorker(identity)
        worker = threading.Thread(target=self._inference_loop, args=(stop,),
                                  daemon=True, name="sentrix-inference")
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
            with self._slot:
                self._slot.notify_all()
            worker.join(timeout=5.0)
            if self._identity_worker is not None:
                self._identity_worker.stop()
                self._identity_worker = None

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
        self._video_times.append(now)
        with self._slot:
            self._pending = (frame, captured_at)
            self._slot.notify()
        self._publish(self._draw(frame.copy(), self._overlay, now))
        if now - self._rates_at >= 1.0:
            self._rates_at = now
            self._video_fps = _rate(self._video_times, now)
            self._ai_hz = _rate(self._ai_times, now)
            state.update(video_fps=round(self._video_fps, 1),
                         ai_hz=round(self._ai_hz, 2), fps=round(self._ai_hz, 2),
                         camera_online=True)
        return True

    def _inference_loop(self, stop: threading.Event) -> None:
        """Run the neural stack on the newest frame, at most ``inference_fps``."""
        period = self.cfg.pipeline.inference_period
        next_at = 0.0
        while not stop.is_set():
            delay = next_at - time.monotonic()
            if delay > 0:
                stop.wait(delay)
                continue
            with self._slot:
                if self._pending is None:
                    self._slot.wait(timeout=0.5)
                job, self._pending = self._pending, None
            if job is None:
                continue
            frame, captured_at = job
            next_at = time.monotonic() + period
            try:
                # Pipeline time is the capture time: evidence windows and dwell
                # timers measure the scene, not the queue.
                self.process(frame, now=captured_at)
            except Exception as exc:
                log.error("frame processing failed: %s", exc, exc_info=True)
                continue
            done = time.monotonic()
            self._ai_times.append(done)
            state.update(frame_age_ms=round((done - captured_at) * 1000, 1))

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
        if self.violence is not None:
            self.violence.reset()
        self.behaviour.reset()
        self.reid.reset()
        self.detector.reset_tracks()
        self._latencies.clear()
        self._frame_count = 0

    # ── presentation ──────────────────────────────────────────────────────────
    def _draw(self, frame, overlay: "Overlay | None", now: float):
        """Composite the latest inference overlay onto a captured frame."""
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
            if age > self.cfg.pipeline.stale_overlay_seconds:
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
        self._stop.set()
        self._worker.join(timeout=3.0)
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


__all__ = ["Pipeline", "get_pipeline", "shutdown_pipeline"]
