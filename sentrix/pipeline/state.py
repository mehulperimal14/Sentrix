"""Thread-safe live system state, read by the dashboard and the API."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    "uncertainty": 0.0,
    "confidence_band": [0.0, 0.0],
    "fusion_source": "unknown",
    "audio_label": "initializing",
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
}


class SystemState:
    """Single source of truth for live telemetry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data = dict(_INITIAL)
        self._data["timestamp"] = _iso_now()

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            self._data.update(kwargs)
            self._data["timestamp"] = _iso_now()

    def get(self) -> dict[str, Any]:
        with self._lock:
            return self._with_aliases(dict(self._data))

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
        data["mic_available"] = data.get("audio_label") not in (
            "disabled", "initializing", "mic_unavailable")
        # No cloud dependency remains — inference is fully local. Reported as
        # True so the existing status badge does not show a spurious outage.
        data.setdefault("cloud_online", True)
        data.setdefault("cloud_available", True)
        data.setdefault("dispatch_package", None)
        data.setdefault("latest_snapshot", None)
        return data

    def reset(self) -> None:
        with self._lock:
            self._data = dict(_INITIAL)
            self._data["timestamp"] = _iso_now()

    def ws_payload(self) -> str:
        """JSON for the websocket. Never raises — a serialisation failure
        must not take the dashboard connection down."""
        data = self.get()
        try:
            return json.dumps(data, default=str)
        except (TypeError, ValueError):
            return json.dumps({
                "tci": data.get("tci", 0.0),
                "level": data.get("level", 1),
                "status": data.get("status", "UNKNOWN"),
                "reason": "state serialisation error",
            })


state = SystemState()

__all__ = ["SystemState", "state"]
