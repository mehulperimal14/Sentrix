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
from sentrix.perception.identity import IdentityEngine
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
        self._annotated: np.ndarray | None = None
        self._last_inference = 0.0
        self._frame_count = 0

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

        identity = self.identity.check(frame, now)
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
            extras={"detections": detections.to_dict(),
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
                          level: int, status: str, detections: list) -> None:
        """Seal an evidence bundle and record its identifiers on the incident."""
        record = vault.save(frame, result, level, status, incident.id, detections)
        if record is not None and incident.evidence_bundle is None:
            # Keep the first bundle of the incident: it is the one closest to
            # the trigger, which is what a reviewer wants to see first.
            incident.evidence_bundle = record.bundle_id
            incident.evidence_hash = record.sha256

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
            detections = result.features.extras.get("detections", {}).get("all", [])
            self._submit(self._capture_evidence, incident, frame.copy(), result,
                         level, status, detections)

        if plan.sms and claim(incident, "sms"):
            self._submit(notifier.send_sms, result, level, status)

        if plan.siren and claim(incident, "siren"):
            # Synchronous: a physical deterrent must not queue behind disk I/O.
            try:
                self.siren.activate()
            except Exception as exc:
                log.error("siren failed: %s", exc)

        if plan.dispatch and claim(incident, "dispatch"):
            self._submit(dispatch_service.create, result, level, incident.id)

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
            features=features.model_features(), reason=result.reason)
        plan = plan_for(level)
        if incident is not None and plan.any_side_effect:
            self._act(incident, plan, clean, result, level, status, now)
        if closed is not None:
            self._submit(database.save_incident, closed, self.session_id)

        latency = time.perf_counter() - started
        self._latencies.append(latency)
        self.events.write(result, level, status,
                          incident.id if incident else None, latency, label)

        fps = 1.0 / max(float(np.mean(self._latencies)), 1e-6)
        ordered = sorted(self._latencies)
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]

        state.update(
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
            fps=round(fps, 1), latency_ms=round(latency * 1000, 2),
            latency_p95_ms=round(p95 * 1000, 2),
            queue_depth=self._tasks.qsize(), camera_online=self.source.online,
            events_logged=self.events.rows,
        )
        telemetry.record("pipeline", "frame", None, latency)

        self._annotated = self._draw(frame.copy(), result, level, status, features)
        return {"tci": result.tci, "level": level, "status": status,
                "incident_type": result.incident_type,
                "features": features.model_features(), "latency": latency,
                "incident_id": incident.id if incident else None}

    # ── live loop ─────────────────────────────────────────────────────────────
    def tick(self) -> None:
        """One iteration of the live loop, rate-limited to the inference rate."""
        ok, frame = self.source.read()
        if not ok or frame is None:
            state.update(camera_online=False, reason="No camera signal")
            self._annotated = _placeholder()
            return
        now = time.monotonic()
        if now - self._last_inference < self.cfg.pipeline.inference_period:
            return
        self._last_inference = now
        try:
            self.process(frame, now)
        except Exception as exc:
            log.error("frame processing failed: %s", exc, exc_info=True)

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
        self._last_person_at = None
        if self.violence is not None:
            self.violence.reset()
        self.behaviour.reset()
        self.reid.reset()
        self.detector.reset_tracks()
        self._latencies.clear()
        self._frame_count = 0

    # ── presentation ──────────────────────────────────────────────────────────
    def _draw(self, frame, result, level, status, features):
        if cv2 is None or frame is None:
            return frame
        try:
            height, width = frame.shape[:2]
            colour = LEVEL_COLOURS.get(level, (255, 255, 255))

            banner = frame.copy()
            cv2.rectangle(banner, (0, 0), (width, 74), (10, 10, 20), -1)
            cv2.addWeighted(banner, 0.7, frame, 0.3, 0, frame)

            cv2.putText(frame, f"SENTRIX  TCI {result.tci:.2f}", (12, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, colour, 2)
            cv2.putText(frame,
                        f"L{level} {status}  [{features.behaviour_label}]",
                        (12, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.6, colour, 2)

            info_x = max(width - 250, 10)
            cv2.putText(frame,
                        f"WPN {features.weapon:.2f}  FIRE {features.fire:.2f}",
                        (info_x, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.5, colour, 1)
            if features.identity_degraded:
                auth_text, auth_colour = "ID N/A", (0, 165, 255)
            elif features.authorized:
                auth_text, auth_colour = "AUTHORISED", (0, 200, 80)
            else:
                auth_text, auth_colour = "UNKNOWN", (0, 0, 220)
            cv2.putText(frame, auth_text, (info_x, 46),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, auth_colour, 2)
            cv2.putText(frame, f"{state.get().get('fps', 0):.1f} FPS",
                        (info_x, 66), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                        (200, 200, 200), 1)

            detections = features.extras.get("detections", {})
            palette = {"persons": colour, "weapons": (0, 0, 255),
                       "fires": (0, 140, 255)}
            for group, box_colour in palette.items():
                for det in detections.get(group, []):
                    x1, y1, x2, y2 = det["bbox"]
                    cv2.rectangle(frame, (x1, y1), (x2, y2), box_colour, 2)
                    tag = det["label"]
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
        return self._annotated if self._annotated is not None else _placeholder()

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def shutdown(self) -> None:
        log.info("shutting down pipeline")
        self._stop.set()
        self._worker.join(timeout=3.0)
        if self.audio is not None:
            self.audio.stop()
        self.source.release()
        self.events.close()
        telemetry.close()
        log.info("pipeline stopped")


_pipeline: Pipeline | None = None
_pipeline_lock = threading.Lock()


def get_pipeline() -> Pipeline:
    """Process-wide singleton, created on first use."""
    global _pipeline
    with _pipeline_lock:
        if _pipeline is None:
            _pipeline = Pipeline()
        return _pipeline


def shutdown_pipeline() -> None:
    global _pipeline
    with _pipeline_lock:
        if _pipeline is not None:
            _pipeline.shutdown()
            _pipeline = None


__all__ = ["Pipeline", "get_pipeline", "shutdown_pipeline"]
