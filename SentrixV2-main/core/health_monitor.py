# core/health_monitor.py
#
# ARCHITECTURE: Tracks availability of all optional subsystems.
# Updated by each engine on failure/success. Read by /api/health endpoint.
# Uses a threading.Lock for safe concurrent access from multiple engine threads.

import threading
from dataclasses import dataclass, asdict


@dataclass
class HealthStatus:
    camera_available:  bool = False
    mic_available:     bool = False
    cloud_available:   bool = False
    twilio_available:  bool = False
    yolo_available:    bool = False
    face_available:    bool = False
    vosk_available:    bool = False


class HealthMonitor:
    """Central health tracker for all optional subsystems."""

    def __init__(self):
        self.status = HealthStatus()
        self._lock  = threading.Lock()

    def update(self, **kwargs):
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self.status, key):
                    setattr(self.status, key, value)

    def get(self) -> dict:
        with self._lock:
            return asdict(self.status)

    # Legacy helpers kept for backward compat with old system_engine references
    def report_success(self):
        self.update(cloud_available=True)

    def report_failure(self):
        pass

    def is_cloud_available(self):
        with self._lock:
            return self.status.cloud_available


# Module-level singleton
health_monitor = HealthMonitor()