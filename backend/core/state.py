# core/state.py
#
# ARCHITECTURE: Single shared state object updated by SystemEngine each frame.
# Read by WebSocket handler, /api/metrics endpoint, and web routes.
# All updates are atomic dict replacements under a threading.Lock, making
# this the single source of truth for live dashboard data.

import json
import threading
from datetime import datetime, timezone


def _iso_now():
    return datetime.now(timezone.utc).isoformat()


class SystemState:
    """Thread-safe key/value store for live system metrics."""

    def __init__(self):
        self._lock = threading.Lock()
        self._data = {
            "tci":              0.0,
            "level":            1,
            "status":           "NORMAL",
            "reason":           "System starting",
            "incident_type":    "normal",
            "scores":           {},
            "audio_label":      "initializing",
            "latest_snapshot":  None,
            "dispatch_package": None,
            "cloud_online":     False,
            "timestamp":        _iso_now(),
        }

    def update(self, **kwargs):
        with self._lock:
            self._data.update(kwargs)
            self._data["timestamp"] = _iso_now()

    def get(self) -> dict:
        with self._lock:
            return dict(self._data)

    def get_ws_payload(self) -> str:
        """Return a JSON string safe for WebSocket transmission."""
        data = self.get()
        raw_scores = data.get("scores", {})
        data["scores"] = {
            k: round(v, 3)
            for k, v in raw_scores.items()
            if isinstance(v, (int, float))
        }
        try:
            return json.dumps(data)
        except Exception:
            return json.dumps({"tci": 0.0, "level": 1, "status": "NORMAL",
                               "reason": "serialization error", "scores": {}})


# Module-level singleton — imported by system_engine, routes, websocket handler
state = SystemState()