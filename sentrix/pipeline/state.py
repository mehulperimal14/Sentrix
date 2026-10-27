"""Thread-safe live system state, read by the dashboard and the API."""

from __future__ import annotations

import copy
import json
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _test_mode_default() -> bool:
    try:
        from sentrix.config import settings
        return bool(settings.test_mode)
    except Exception:                  # never let config break the dashboard
        return True


#: The ``sos`` block while no request is open (docs/7 §9.4). Owner-facing: it
#: never carries the duress flag (``sentrix.sos.states.owner_view``). One
#: payload reaches LC and tunnel viewers alike, so paths in it are
#: base-relative (``snapshot: "media/snapshot/<name>"``, no leading slash);
#: the page prefixes ``document.body.dataset.base``. ``actions`` holds names
#: from ``sentrix.sos.SOS_ACTIONS``.
SOS_IDLE: dict[str, Any] = {
    "active": False, "request_id": None, "short_id": None, "state": None,
    "label": None, "incident_id": None, "incident_type": None, "level": None,
    "urgency": None, "evidence_state": None, "created_at": None,
    "expires_at": None, "remaining_s": None, "seen": False, "repeat": False,
    "authority": None, "recommended_authority": None, "sos_text": None,
    "snapshot": None, "actions": [], "last": None,
}
#: The ``link`` block (track T-LINK fills it).
LINK_IDLE: dict[str, Any] = {
    "enabled": False, "state": "disabled", "connected": False, "claimed": False,
    "device_id": None, "portal": None, "since": None, "last_message_at": None,
    "outbox_depth": 0, "profile_version": None,
}
#: The ``node`` block (track T-HW fills it).
NODE_IDLE: dict[str, Any] = {
    "profile": "laptop", "cpu_temp_c": None, "throttled": None,
    "under_voltage": None, "ext5v_v": None, "mains": None, "pir": None,
    "door": None, "siren_armed": None, "gpio": None,
    "uptime_s": None, "updated_at": None,
    # written by sentrix.io.node / gpio once they run (same shape idle)
    "temp_state": None, "throttled_raw": None, "throttle_flags": None,
    "under_voltage_occurred": None, "power_w": None, "environment": None,
    "tamper": None, "platform": None, "telemetry": False,
}
#: The ``clip`` block (track T-CLIP: ``ClipStrategy`` writes
#: ``ClipRunner.status()`` after every clip). Idle = the tick loop.
CLIP_IDLE: dict[str, Any] = {
    "mode": "tick", "window_s": None, "hop_s": None, "clips": 0, "dropped": 0,
    "dropped_fraction": 0.0, "compute_ms": {"p50": None, "p95": None, "max": None},
    "budget_ms": None, "runtime": None, "threads": None, "fast_lane": None,
    "fast_lane_decisions": 0, "last": None,
}
#: The ``channels`` block (track T-CH fills it; the app lifespan reports the
#: legacy Twilio notifier while no router runs): is anything able to leave?
#: ``outbound_ready`` counts the legacy notifier; ``warn`` (the red "no
#: outbound channel" banner) is True only while SOS is enabled and nothing can
#: leave. ``modem``: {present, registered, signal_dbm, sms_today} or None.
CHANNELS_IDLE: dict[str, Any] = {
    "enabled": False, "outbound_ready": False, "live": False, "channels": {},
    "outbox_depth": 0, "last_error": None, "modem": None, "legacy_twilio": False,
    "warn": False,
}
#: Runtime HOME/ARMED mode (``sentrix.mode``).
MODE_IDLE: dict[str, Any] = {"armed": False, "source": "config",
                             "changed_by": None, "changed_at": None}

_INITIAL: dict[str, Any] = {
    "tci": 0.0,
    "raw_tci": 0.0,
    "level": 1,
    "status": "SECURE",
    "reason": "System starting",
    "incident_type": "normal",
    "incident_id": None,
    "features": {},
    "top_factors": [],
    # D15: what each input did for the leading class, the audio window heard
    # (and whether it was aligned with the frame), the fusion rule applied.
    "contributions": [],
    "audio": {"status": "disabled"},
    "fusion": {"class": "normal", "state": "none", "rule": None, "modalities": {},
               "context": {}, "sensors": {}},
    "uncertainty": 0.0,
    "confidence_band": [0.0, 0.0],
    "fusion_source": "unknown",
    "audio_label": "initializing",
    # Is an open microphone delivering sound (the live loop writes it once a
    # second from AudioEngine.available)? None until it has said.
    "mic_delivering": None,
    "behaviour_label": "normal",
    "person_count": 0,
    "authorized": False,
    "identity_degraded": False,
    "alarm": {"level": 1, "status": "SECURE", "since_s": 0.0, "incident_id": None,
              "incident_type": "normal", "headline": "", "peak_tci": 0.0,
              "deescalate_in": None},
    "evidence_state": "none",
    "fps": 0.0,
    "video_fps": 0.0,
    "ai_hz": 0.0,
    "frame_age_ms": None,
    "latency_ms": 0.0,
    "latency_p95_ms": 0.0,
    "queue_depth": 0,
    "camera_online": False,
    "events_logged": 0,
    # Portal and SOS (docs/7 §9.4); every block has a safe idle value.
    "sos": SOS_IDLE,
    "link": LINK_IDLE,
    "node": NODE_IDLE,
    "channels": CHANNELS_IDLE,
    "clip": CLIP_IDLE,
    "mode": MODE_IDLE,
    "remote_viewers": 0,
}

#: Committed level changes kept for the dashboard's Event Log (survives a reload).
LEVEL_HISTORY_MAX = 50

#: ``audio_label`` values that are not a sound result: nothing was heard.
_NOT_LISTENING = ("disabled", "initializing", "mic_unavailable")


def mic_listening(audio_label: Any) -> bool:
    """Is the microphone producing *classified* results? ``audio_label``
    names a sound result only then; the pipeline writes "audio unavailable"
    (nothing plugged in, the sound model off, no full window yet, or no
    window aligned with the frame), "disabled" or the start-up
    "initializing" otherwise. ``mic_classifying`` reads this; whether the
    microphone captures at all is ``mic_delivering``."""
    if not isinstance(audio_label, str) or not audio_label.strip():
        return False
    label = audio_label.strip().lower()
    return label not in _NOT_LISTENING and not label.startswith("audio unavailable")


def mic_state(data: dict[str, Any]) -> tuple[bool, bool]:
    """``(capturing, classifying)`` for the dashboard's dot and the link's
    heartbeat. Capturing is the audio engine's own word
    (``mic_delivering``, the same as ``/api/health`` ``audio``) once the
    live loop has written it; a microphone that records the room is never
    reported as off because nothing classified a window (the sound model
    off, or the first seconds after it connected). Before that, and in a
    replay, the sound label is all there is."""
    classifying = mic_listening(data.get("audio_label"))
    delivering = data.get("mic_delivering")
    return (bool(delivering) if delivering is not None else classifying), classifying


class SystemState:
    """Single source of truth for live telemetry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data = copy.deepcopy(_INITIAL)
        self._data["test_mode"] = _test_mode_default()
        self._data["timestamp"] = _iso_now()
        self._history: deque[dict[str, Any]] = deque(maxlen=LEVEL_HISTORY_MAX)
        #: Read on every preview frame by the HUD, so a plain attribute.
        self.remote_viewers = 0

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            self._data.update(kwargs)
            self._data["timestamp"] = _iso_now()

    def get(self) -> dict[str, Any]:
        with self._lock:
            return self._with_aliases(dict(self._data))

    def set_remote_viewers(self, count: int) -> None:
        """Remote (Portal) viewers of the live video; shown on the LC and HUD."""
        count = max(0, int(count))
        self.remote_viewers = count
        self.update(remote_viewers=count)

    def record_level_change(self, level: int, previous: int, status: str,
                            reason: str = "", incident_id: str | None = None,
                            incident_type: str = "normal") -> None:
        """A committed level change, for ``level_history()``."""
        with self._lock:
            self._history.append({"at": _iso_now(), "level": int(level),
                                  "previous": int(previous), "status": status,
                                  "reason": reason, "incident_id": incident_id,
                                  "incident_type": incident_type})

    def level_history(self) -> list[dict[str, Any]]:
        """Newest last; at most ``LEVEL_HISTORY_MAX`` entries."""
        with self._lock:
            return list(self._history)

    @staticmethod
    def _with_aliases(data: dict[str, Any]) -> dict[str, Any]:
        """Add legacy key names the existing dashboard templates read.

        The state schema was renamed for clarity (``scores`` -> ``features``,
        ``latency_avg`` -> ``latency_ms``, and so on). Rather than rewrite the
        templates and the vanilla-JS client in this pass, both spellings are
        published. Remove this once the frontend is rebuilt.
        """
        data["scores"] = data.get("features", {})
        data["latency_avg"] = round(data.get("latency_ms", 0.0) / 1000.0, 4)
        data["latency_p95"] = round(data.get("latency_p95_ms", 0.0) / 1000.0, 4)
        data["camera_available"] = data.get("camera_online", False)
        data["mic_available"], data["mic_classifying"] = mic_state(data)
        # Inference is fully local; "cloud" now means the Portal link, so the
        # legacy keys follow the link block (False while no link runs).
        link = data.get("link") or {}
        data.setdefault("cloud_online", bool(link.get("connected")))
        data.setdefault("cloud_available", bool(link.get("enabled")))
        data.setdefault("dispatch_package", None)
        data.setdefault("latest_snapshot", None)
        return data

    def reset(self) -> None:
        with self._lock:
            self._data = copy.deepcopy(_INITIAL)
            self._data["test_mode"] = _test_mode_default()
            self._data["timestamp"] = _iso_now()
            self._history.clear()
        self.remote_viewers = 0

    def ws_payload(self) -> str:
        """JSON for the websocket and the link's state messages, with
        credentials redacted from every string (docs/7 §12). Never raises — a
        serialisation failure must not take the dashboard connection down."""
        from sentrix.utils.redact import redact_tree
        data = self.get()
        try:
            return json.dumps(redact_tree(data), default=str)
        except (TypeError, ValueError):
            return json.dumps({
                "tci": data.get("tci", 0.0),
                "level": data.get("level", 1),
                "status": data.get("status", "UNKNOWN"),
                "reason": "state serialisation error",
            })


state = SystemState()

__all__ = ["SystemState", "state", "SOS_IDLE", "LINK_IDLE", "NODE_IDLE",
           "CHANNELS_IDLE", "CLIP_IDLE", "MODE_IDLE", "LEVEL_HISTORY_MAX", "mic_listening",
           "mic_state"]
