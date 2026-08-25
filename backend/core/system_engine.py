# core/system_engine.py
#
# ARCHITECTURE: Orchestrates the full per-frame processing pipeline.
# Called repeatedly (~30 fps) in a background thread by app.py.
# Updates shared state after each frame. Never blocks more than ~100ms
# (cloud calls are cached/skipped). Returns an annotated numpy frame
# for MJPEG streaming via /video endpoint.
#
# CONCURRENCY MODEL:
#   Hot path (per-frame, ~30 fps): capture → inference → fusion → state update → HUD
#   Task queue (background worker):  snapshot → evidence → SMS → dispatch → DB log
#   All blocking I/O is offloaded to the task worker so frame processing never stalls.
#
# Pipeline order:
#   1. Get frames → tile → combined_frame
#   2. YOLO vision detection + frame-diff motion scoring
#   3. Behaviour classification from tracks
#   4. Audio (cached from background thread)
#   5. Face identity check
#   6. Cloud threat inference (every 10 frames, cached)
#   7. ReID tracking — assign global person IDs
#   8. Build score dictionary
#   9. Fusion + temporal smoothing
#  10. Voice SOS override
#  11. Enqueue escalation side-effects (snapshot, evidence, SMS, siren, dispatch, call)
#  12. Update shared state
#  13. Draw HUD overlay on frame

import os
import sys
import queue
import statistics
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from dotenv import load_dotenv

# Cross-platform base directory (backend/)
_BASE_DIR = Path(__file__).parent.parent.resolve()
_RUNTIME_DIR = _BASE_DIR / "runtime"

load_dotenv()

from hardware.camera_manager import CameraManager
from ai.vision_engine       import VisionEngine
from ai.behaviour_engine    import BehaviourEngine
from ai.audio_engine        import AudioEngine
from ai.face_engine         import FaceEngine
from ai.reid_engine         import ReIDEngine
from ai.tracking_engine     import TrackingEngine
from ai.cloud_engines       import CloudThreatEngine
from ai.local_fallback_engine import LocalFallbackEngine
from ai.fusion_engine       import FusionEngine, TCIResult
from ai.voice_sos_engine    import VoiceSosEngine
from core.escalation        import EscalationEngine
from core.encrypted_evidence import encrypted_evidence
from core.alert_service     import alert_service
from core.dispatch_service  import dispatch_service
from core.state             import state
from core.health_monitor    import health_monitor
from hardware.siren         import Siren
import db.database as database

# Cross-platform runtime directory creation
(_RUNTIME_DIR / "static" / "alerts").mkdir(parents=True, exist_ok=True)
(_RUNTIME_DIR / "static" / "alerts" / "evidence").mkdir(parents=True, exist_ok=True)
(_RUNTIME_DIR / "static" / "authorized_faces").mkdir(parents=True, exist_ok=True)

# String versions for cv2.imwrite and os.path ops (backward-compat)
_ALERTS_DIR     = str(_RUNTIME_DIR / "static" / "alerts")
_AUTH_FACES_DIR = str(_RUNTIME_DIR / "static" / "authorized_faces")

BLANK = np.zeros((480, 640, 3), dtype=np.uint8)
cv2.putText(BLANK, "No Camera Signal", (120, 240),
            cv2.FONT_HERSHEY_SIMPLEX, 1.0, (80, 80, 80), 2)

LEVEL_COLORS = {
    1: (0, 200, 80),    # Green
    2: (30, 200, 220),  # Yellow
    3: (0, 140, 255),   # Orange
    4: (0, 60, 240),    # Red-orange
    5: (0, 0, 220),     # Red
}

SIREN_COOLDOWN = 60    # seconds
CALL_COOLDOWN  = 120   # seconds
TASK_QUEUE_MAX = 50    # drop tasks if queue fills (alert storm safety valve)


class TemporalSmoother:
    """Returns the statistical mode of the last N threat levels."""

    def __init__(self, window: int = 5):
        self.history = deque(maxlen=window)

    def update(self, new_level: int) -> int:
        self.history.append(new_level)
        try:
            return statistics.mode(self.history)
        except statistics.StatisticsError:
            return new_level


def _tile_frames(frames: list) -> np.ndarray:
    """Tile up to 4 camera frames into a 2×2 grid (or return single frame)."""
    if not frames:
        return BLANK.copy()
    if len(frames) == 1:
        return frames[0]
    h, w = frames[0].shape[:2]
    # Ensure all frames are same size
    resized = [cv2.resize(f, (w, h)) for f in frames]
    if len(resized) == 2:
        return cv2.hconcat(resized)
    if len(resized) == 3:
        resized.append(np.zeros_like(resized[0]))
    top    = cv2.hconcat(resized[:2])
    bottom = cv2.hconcat(resized[2:4])
    return cv2.vconcat([top, bottom])


def _draw_hud(frame, tci, level, status, detections, authorized,
              weapon_score, fire_score, behaviour_label,
              latency_p95=0.0, fps=0.0):
    """Draw semi-transparent HUD overlay with TCI, level, and key scores."""
    h, w = frame.shape[:2]
    color = LEVEL_COLORS.get(level, (255, 255, 255))

    # Top banner
    overlay = frame.copy()
    cv2.rectangle(overlay, (0, 0), (w, 70), (10, 10, 20), -1)
    cv2.addWeighted(overlay, 0.7, frame, 0.3, 0, frame)

    cv2.putText(frame, f"SENTRIX  TCI: {tci:.2f}",
                (12, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
    cv2.putText(frame, f"L{level} {status}  [{behaviour_label}]",
                (12, 58), cv2.FONT_HERSHEY_SIMPLEX, 0.65, color, 2)

    # Right info strip
    info_x = w - 240
    cv2.putText(frame, f"WPN:{weapon_score:.2f} FIRE:{fire_score:.2f}",
                (info_x, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
    auth_text  = "AUTH" if authorized else "UNAUTH"
    auth_color = (0, 200, 80) if authorized else (0, 0, 220)
    cv2.putText(frame, auth_text, (info_x, 45),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, auth_color, 2)
    cv2.putText(frame, f"P95: {latency_p95:.2f}s | FPS: {fps:.1f}",
                (info_x, 65), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (200, 200, 200), 1)

    # Draw detection bounding boxes from YOLO
    for d in detections:
        bbox = d.get("bbox", [])
        if len(bbox) == 4:
            x1, y1, x2, y2 = bbox
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)

    return frame


class SystemEngine:
    """Main per-frame orchestrator. Runs in a background thread."""

    def __init__(self):
        print("[SystemEngine] Initialising engines...")

        self.camera_manager = CameraManager()
        self.vision         = VisionEngine()
        self.behaviour      = BehaviourEngine()
        self.audio          = AudioEngine()
        self.face           = FaceEngine()
        self.reid           = ReIDEngine()
        self.tracking       = TrackingEngine()
        self.cloud          = CloudThreatEngine()
        self.fallback       = LocalFallbackEngine()
        self.fusion         = FusionEngine()
        self.escalation     = EscalationEngine()
        self.voice          = VoiceSosEngine()
        self.smoother       = TemporalSmoother(window=5)
        self.siren          = Siren()

        self._last_siren_time: float = 0.0
        self._last_call_time:  float = 0.0
        self._tracks                 = []
        self._frame_count            = 0
        self._latencies              = deque(maxlen=100)
        self.inference_skip          = 6  # Run ML approx 5 FPS
        self.cached_annotated_frame  = None
        self._last_auth_log_time     = 0.0  # Rate-limit AUTH DEBUG log

        # Cache for skipped frames
        self._cached_vision_score    = 0.0
        self._cached_detections      = []
        self._cached_motion_score    = 0.0
        self._cached_behaviour_score = 0.10
        self._cached_behaviour_label = "normal"
        self._cached_authorized      = False
        self._cached_unauthorized    = False
        self._cached_identity_score  = 0.0

        # ── Async task queue for side-effects ─────────────────────────────────
        # Bounded queue prevents memory buildup during alert storms.
        # Worker thread processes tasks (disk/network I/O) independently of fps loop.
        self._task_queue  = queue.Queue(maxsize=TASK_QUEUE_MAX)
        self._stop_worker = threading.Event()
        self._worker_thread = threading.Thread(
            target=self._task_worker, daemon=True, name="sentrix-task-worker"
        )
        self._worker_thread.start()

        # Update health status for subsystems we know about
        health_monitor.update(
            camera_available  = any(c.available for c in self.camera_manager.cameras),
            mic_available     = self.audio.available,
            yolo_available    = self.vision._available,
            face_available    = self.face._available,
        )

        print("[SystemEngine] Ready.")

    # ──────────────────────────────────────────────────────────────────────────
    # Task worker — runs side-effects off the hot path
    # ──────────────────────────────────────────────────────────────────────────

    def _task_worker(self):
        """Background worker that processes enqueued side-effect tasks."""
        while not self._stop_worker.is_set():
            try:
                fn, args, kwargs = self._task_queue.get(timeout=1.0)
                try:
                    fn(*args, **kwargs)
                except Exception as e:
                    print(f"[TaskWorker] Task error: {e}")
                finally:
                    self._task_queue.task_done()
            except queue.Empty:
                continue
        print("[TaskWorker] Worker thread exited cleanly.")

    def _enqueue(self, fn, *args, **kwargs):
        """Non-blocking enqueue. Silently drops task if queue is full (safety valve)."""
        try:
            self._task_queue.put_nowait((fn, args, kwargs))
        except queue.Full:
            print("[TaskWorker] Queue full — dropping task (alert storm protection).")

    # ──────────────────────────────────────────────────────────────────────────
    # Graceful shutdown
    # ──────────────────────────────────────────────────────────────────────────

    def shutdown(self):
        """Release all hardware handles and stop background threads cleanly."""
        print("[SystemEngine] Shutting down...")

        # Stop task worker — give it 3s to drain pending tasks
        self._stop_worker.set()
        self._worker_thread.join(timeout=3.0)

        # Stop AI background threads
        try:
            self.audio.stop()
        except Exception:
            pass
        try:
            self.voice.stop()
        except Exception:
            pass

        # Release camera handles
        try:
            self.camera_manager.release_all()
        except Exception:
            pass

        print("[SystemEngine] Shutdown complete.")

    # ──────────────────────────────────────────────────────────────────────────
    # Main per-frame pipeline
    # ──────────────────────────────────────────────────────────────────────────

    def process(self) -> np.ndarray:
        """Process one frame through the full pipeline. Returns annotated frame."""
        start_time = time.time()
        self._frame_count += 1

        # STEP 1: Get frames
        frames = self.camera_manager.get_all_frames()
        if not frames:
            state.update(status="NORMAL", reason="No camera frames")
            return BLANK.copy()

        combined_frame = _tile_frames(frames)

        # Fast path: Skip ML Inference to boost FPS
        if self._frame_count % self.inference_skip != 0 and self.cached_annotated_frame is not None:
            # Re-calculate FPS even for fast path
            process_time = time.time() - start_time
            self._latencies.append(process_time)
            return self.cached_annotated_frame

        skip_ml = False # We no longer use the old skip_ml logic inside the block since we skip the whole block

        if not skip_ml:
            # STEP 2: Vision + Motion
            try:
                vision_score, detections = self.vision.detect(combined_frame)
                motion_score = self.vision.motion_score(combined_frame)
            except Exception as e:
                print(f"[SystemEngine] Vision error: {e}")
                vision_score, detections, motion_score = 0.0, [], 0.0

            self._cached_vision_score = vision_score
            self._cached_detections   = detections
            self._cached_motion_score = motion_score

            # STEP 3: Behaviour (needs tracks from previous frame)
            try:
                behaviour_score, behaviour_label = self.behaviour.classify(
                    self._tracks, detections
                )
            except Exception as e:
                print(f"[SystemEngine] Behaviour error: {e}")
                behaviour_score, behaviour_label = 0.10, "normal"

            self._cached_behaviour_score = behaviour_score
            self._cached_behaviour_label = behaviour_label

            # STEP 5: Identity
            try:
                authorized   = self.face.is_authorized(combined_frame)
                unauthorized = (not authorized) and (len(detections) > 0)
                identity_score = 0.0 if authorized else (0.6 if unauthorized else 0.0)
            except Exception as e:
                print(f"[SystemEngine] Face error: {e}")
                authorized, unauthorized, identity_score = False, False, 0.0

            self._cached_authorized     = authorized
            self._cached_unauthorized   = unauthorized
            self._cached_identity_score = identity_score
            self._cached_behaviour_score = behaviour_score
            self._cached_motion_score    = motion_score

            # STEP 7: ReID tracking
            try:
                self._tracks = self.tracking.update(detections, combined_frame)
                for track in self._tracks:
                    if hasattr(track, "to_ltrb"):
                        x1, y1, x2, y2 = [int(v) for v in track.to_ltrb()]
                        track_id = track.track_id
                    elif isinstance(track, dict):
                        x1, y1, x2, y2 = track.get("bbox", [0, 0, 1, 1])
                        track_id = track.get("id", 0)
                    else:
                        continue

                    crop = combined_frame[
                        max(0, y1):min(combined_frame.shape[0], y2),
                        max(0, x1):min(combined_frame.shape[1], x2),
                    ]
                    embedding  = self.reid.extract_embedding(crop)
                    global_id  = self.reid.match_or_register(embedding)
                    cv2.putText(combined_frame, f"GID-{global_id}",
                                (x1, max(y1 - 10, 12)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 2)
            except Exception as e:
                print(f"[SystemEngine] Tracking/ReID error: {e}")

        else:
            # Use cached values on skipped frames
            vision_score    = self._cached_vision_score
            detections      = self._cached_detections
            motion_score    = self._cached_motion_score
            behaviour_score = self._cached_behaviour_score
            behaviour_label = self._cached_behaviour_label
            authorized      = self._cached_authorized
            unauthorized    = self._cached_unauthorized
            identity_score  = self._cached_identity_score

            # Draw old tracks without ReID extraction
            for track in self._tracks:
                if hasattr(track, "to_ltrb"):
                    x1, y1, x2, y2 = [int(v) for v in track.to_ltrb()]
                    cv2.putText(combined_frame, f"TRACKING",
                                (x1, max(y1 - 10, 12)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 200, 200), 1)
                elif isinstance(track, dict):
                    x1, y1, x2, y2 = track.get("bbox", [0, 0, 1, 1])
                    cv2.putText(combined_frame, f"TRACKING",
                                (x1, max(y1 - 10, 12)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 200, 200), 1)

        # STEP 4: Audio (non-blocking cached result)
        try:
            audio_score, audio_label = self.audio.detect_safe()
        except Exception:
            audio_score, audio_label = 0.0, "error"

        # STEP 6: Cloud threat (cached every FRAME_SKIP frames)
        try:
            cloud_result  = self.cloud.process_safe(combined_frame)
            weapon_score  = cloud_result.weapon_score
            fire_score    = cloud_result.fire_score
            theft_score   = cloud_result.theft_score if cloud_result.theft_score > 0.0 else ((motion_score * 0.7) if unauthorized else 0.0)
            harmful_score = cloud_result.harmful_score if cloud_result.harmful_score > 0.0 else max(weapon_score, behaviour_score)
            health_monitor.update(cloud_available=cloud_result.cloud_online)
        except Exception as e:
            print(f"[SystemEngine] Cloud error: {e}")
            weapon_score = fire_score = 0.0
            theft_score = (motion_score * 0.7) if unauthorized else 0.0
            harmful_score = max(weapon_score, behaviour_score)
            cloud_result = type("CR", (), {"cloud_online": False})()

        # Local fallback when cloud has failed repeatedly
        if self.cloud.fail_count >= 5:
            fallback_score = self.fallback.detect_weapon(combined_frame)
            weapon_score = max(weapon_score, fallback_score)
            harmful_score = max(harmful_score, weapon_score)
            from core.instrumentation import log_instrumentation
            log_instrumentation("SystemEngine", "fallback_activation", {"type": "cloud_to_local", "fallback_score": fallback_score, "final_weapon_score": weapon_score})

        # STEP 8: Build score dictionary
        scores = {
            "vision":          vision_score,
            "audio":           audio_score,
            "motion":          motion_score,
            "behaviour":       behaviour_score,
            "behaviour_label": behaviour_label,
            "identity":        identity_score,
            "weapon":          weapon_score,
            "fire":            fire_score,
            "theft":           theft_score,
            "harmful":         harmful_score,
            "intrusion":       motion_score * 0.5 + identity_score * 0.5,
            "unauthorized":    unauthorized,
            "authorized":      authorized,
        }

        # AUTH DEBUG: throttled to once per second
        now_log = time.time()
        if now_log - self._last_auth_log_time >= 1.0:
            print(
                f"[AUTH DEBUG] authorized={authorized} "
                f"vision={vision_score:.2f} "
                f"behaviour={behaviour_score:.2f} "
                f"weapon={weapon_score:.2f}"
            )
            self._last_auth_log_time = now_log

        # STEP 9: Fusion + Temporal Smoothing
        try:
            raw_result   = self.fusion.compute(scores)
            smooth_level = self.smoother.update(raw_result.level)
            raw_result.level = smooth_level
            result = raw_result
        except Exception as e:
            print(f"[SystemEngine] Fusion error: {e}")
            result = TCIResult(0.0, 1, "NORMAL", "Fusion error", "normal", scores)

        # STEP 10: Voice SOS override
        try:
            voice_cmd = self.voice.get_command_safe()
            if voice_cmd == "EMERGENCY":
                result.level    = 5
                result.tci      = 1.0
                result.reason   = "Voice SOS emergency triggered by user"
                result.status   = "CRITICAL"
            elif voice_cmd == "SIREN":
                self.siren.activate()
        except Exception as e:
            print(f"[SystemEngine] Voice error: {e}")

        # STEP 11: Execute escalation actions
        # Authorized users NEVER generate evidence, snapshots, SMS, or DB events.
        # Weapon detection (weapon_score >= 0.5) is the ONLY exception.
        authorized_no_weapon = authorized and (weapon_score < 0.50)

        action = self.escalation.evaluate(result.level)
        now    = time.time()

        # ── Siren: stays synchronous — hardware must fire immediately ──────────
        from core.instrumentation import log_instrumentation
        if fire_score >= 0.70:
            log_instrumentation("SystemEngine", "override", {"type": "fire_override", "tci_level": result.level})
        elif weapon_score >= 0.50:
            log_instrumentation("SystemEngine", "override", {"type": "weapon_override", "tci_level": result.level})
            
        if unauthorized and result.level >= 2:
            log_instrumentation("SystemEngine", "escalation", {"type": "unknown_face", "tci_level": result.level})

        if action.siren and not authorized_no_weapon and (now - self._last_siren_time > SIREN_COOLDOWN):
            try:
                self.siren.activate()
                self._last_siren_time = now
            except Exception as e:
                print(f"[SystemEngine] Siren error: {e}")

        # Capture timestamps for cooldowns before enqueuing
        last_call_time = self._last_call_time
        if action.call and not authorized_no_weapon and (now - last_call_time > CALL_COOLDOWN):
            self._last_call_time = now
            call_enabled = True
        else:
            call_enabled = False

        # ── All disk/network I/O enqueued to background worker ────────────────
        if action.snapshot and not authorized_no_weapon:
            self._enqueue(alert_service.save_snapshot, combined_frame.copy(), result)

        if action.evidence and not authorized_no_weapon:
            self._enqueue(
                encrypted_evidence.save_encrypted_frame,
                combined_frame.copy(), result, dict(scores), list(detections),
            )

        if action.sms and not authorized_no_weapon:
            self._enqueue(alert_service.send_sms_safe, result)

        if action.form and not authorized_no_weapon:
            self._enqueue(dispatch_service.create_package, result)

        if call_enabled:
            self._enqueue(alert_service.make_call_safe, result)

        # DB log is also a side-effect — enqueue it
        if result.level >= 2 and not authorized_no_weapon:
            self._enqueue(database.log_event, result, scores, None, None)

        # ── STEP 12: Update shared state (synchronous — dashboard is real-time) ─
        # Calculate latency and FPS
        process_time = time.time() - start_time
        self._latencies.append(process_time)
        avg_latency = statistics.mean(self._latencies)
        p95_latency = np.percentile(self._latencies, 95) if len(self._latencies) > 1 else process_time
        fps = 1.0 / avg_latency if avg_latency > 0 else 0.0

        # Build explainability payload from fusion result (new fields, backward-compatible)
        top_factors  = getattr(result, "top_factors", [])
        uncertainty  = getattr(result, "uncertainty", 0.0)

        state.update(
            tci             = result.tci,
            level           = result.level,
            status          = result.status,
            reason          = result.reason,
            scores          = scores,
            incident_type   = result.incident_type,
            audio_label     = audio_label,
            latest_snapshot = None,   # snapshot path now set async; cleared here
            dispatch_package= None,   # dispatch also async
            cloud_online    = cloud_result.cloud_online,
            latency_avg     = round(avg_latency, 3),
            latency_p95     = round(p95_latency, 3),
            fps             = round(fps, 1),
            top_factors     = top_factors,
            uncertainty     = round(uncertainty, 3),
            queue_depth     = self._task_queue.qsize(),
        )

        # STEP 13: Draw HUD overlay
        try:
            annotated = _draw_hud(
                frame           = combined_frame,
                tci             = result.tci,
                level           = result.level,
                status          = result.status,
                detections      = detections,
                authorized      = authorized,
                weapon_score    = weapon_score,
                fire_score      = fire_score,
                behaviour_label = behaviour_label,
                latency_p95     = p95_latency,
                fps             = fps,
            )
        except Exception as e:
            print(f"[SystemEngine] HUD error: {e}")
            annotated = combined_frame

        self.cached_annotated_frame = annotated
        return annotated

    def get_latest_frame(self) -> np.ndarray:
        if self.cached_annotated_frame is not None:
            return self.cached_annotated_frame
        return BLANK.copy()

    def get_live_metrics(self) -> dict:
        """Legacy API used by old WebSocket handler."""
        return state.get()

    # ──────────────────────────────────────────────────────────────────────────
    # Evaluation-only entry point  (SENTRIX_EVAL_MODE=1)
    # ──────────────────────────────────────────────────────────────────────────

    def process_eval_frame(self, frame: np.ndarray) -> dict:
        """
        Run the FULL inference+fusion+TCI pipeline on a caller-supplied frame.

        Designed for the headless evaluation harness.  Differences vs process():
          * Bypasses the frame-counter skip — every call runs ML inference.
          * Bypasses camera acquisition — frame injected by caller.
          * Does NOT draw a HUD or return an annotated image.
          * Returns a structured dict with all scores + TCI result.
          * All side effects remain mocked when SENTRIX_EVAL_MODE=1.

        Never raises; returns an error-flagged dict on unexpected failure.
        """
        import time as _time
        from core.instrumentation import log_instrumentation

        _start = _time.time()
        try:
            combined_frame = frame.copy()

            # STEP 2: Vision + Motion
            try:
                vision_score, detections = self.vision.detect(combined_frame)
                motion_score = self.vision.motion_score(combined_frame)
            except Exception as e:
                print(f"[EvalFrame] Vision error: {e}")
                vision_score, detections, motion_score = 0.0, [], 0.0

            # STEP 3: Behaviour
            try:
                behaviour_score, behaviour_label = self.behaviour.classify(
                    self._tracks, detections
                )
            except Exception as e:
                print(f"[EvalFrame] Behaviour error: {e}")
                behaviour_score, behaviour_label = 0.10, "normal"

            # STEP 5: Identity
            try:
                authorized     = self.face.is_authorized(combined_frame)
                unauthorized   = (not authorized) and (len(detections) > 0)
                identity_score = 0.0 if authorized else (0.6 if unauthorized else 0.0)
            except Exception as e:
                print(f"[EvalFrame] Face error: {e}")
                authorized, unauthorized, identity_score = False, False, 0.0

            # STEP 4: Audio (cached from background thread)
            try:
                audio_score, audio_label = self.audio.detect_safe()
            except Exception:
                audio_score, audio_label = 0.0, "error"

            # STEP 6: Cloud threat (mocked in EVAL_MODE)
            try:
                cloud_result  = self.cloud.process_safe(combined_frame)
                weapon_score  = cloud_result.weapon_score
                fire_score    = cloud_result.fire_score
                theft_score   = cloud_result.theft_score if cloud_result.theft_score > 0.0 else ((motion_score * 0.7) if unauthorized else 0.0)
                harmful_score = cloud_result.harmful_score if cloud_result.harmful_score > 0.0 else max(weapon_score, behaviour_score)
            except Exception as e:
                print(f"[EvalFrame] Cloud error: {e}")
                weapon_score = fire_score = 0.0
                theft_score = (motion_score * 0.7) if unauthorized else 0.0
                harmful_score = max(weapon_score, behaviour_score)
                cloud_result = type("CR", (), {"cloud_online": False})()

            # Local fallback path (exercises real code; side-effect mocked in EVAL_MODE)
            if self.cloud.fail_count >= 5:
                fallback_score = self.fallback.detect_weapon(combined_frame)
                weapon_score   = max(weapon_score, fallback_score)
                harmful_score  = max(harmful_score, weapon_score)
                log_instrumentation("SystemEngine", "fallback_activation", {
                    "type": "cloud_to_local",
                    "fallback_score": fallback_score,
                    "final_weapon_score": weapon_score,
                })

            # STEP 8: Score dict
            scores = {
                "vision":          vision_score,
                "audio":           audio_score,
                "motion":          motion_score,
                "behaviour":       behaviour_score,
                "behaviour_label": behaviour_label,
                "identity":        identity_score,
                "weapon":          weapon_score,
                "fire":            fire_score,
                "theft":           theft_score,
                "harmful":         harmful_score,
                "intrusion":       motion_score * 0.5 + identity_score * 0.5,
                "unauthorized":    unauthorized,
                "authorized":      authorized,
            }

            # STEP 9: Fusion + temporal smoothing
            try:
                raw_result   = self.fusion.compute(scores)
                smooth_level = self.smoother.update(raw_result.level)
                raw_result.level = smooth_level
                result = raw_result
            except Exception as e:
                print(f"[EvalFrame] Fusion error: {e}")
                result = TCIResult(0.0, 1, "NORMAL", "Fusion error", "normal", scores)

            latency = _time.time() - _start

            log_instrumentation("SystemEngine", "eval_frame_complete", {
                "tci": result.tci, "level": result.level,
                "status": result.status, "latency": latency,
            })

            return {
                "vision_score":    vision_score,
                "audio_score":     audio_score,
                "behaviour_score": behaviour_score,
                "motion_score":    motion_score,
                "identity_score":  identity_score,
                "weapon_score":    weapon_score,
                "fire_score":      fire_score,
                "tci":             result.tci,
                "level":           result.level,
                "status":          result.status,
                "incident_type":   result.incident_type,
                "reason":          result.reason,
                "latency":         latency,
                "authorized":      authorized,
                "audio_label":     audio_label,
                "behaviour_label": behaviour_label,
                "error":           None,
            }

        except Exception as e:
            latency = _time.time() - _start
            print(f"[EvalFrame] Unexpected error: {e}")
            log_instrumentation("SystemEngine", "eval_frame_error",
                                {"error": str(e), "latency": latency})
            return {"error": str(e), "latency": latency}
