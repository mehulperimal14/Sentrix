# hardware/camera_manager.py
#
# ARCHITECTURE: Manages one or more Camera instances.
# Reads CAMERA_SOURCES from environment (comma-separated list of ints/URLs).
# get_all_frames() returns a list of frames (one per camera).
# If no frames are available, returns an empty list — caller must handle this.

import os
from typing import List

import numpy as np
from dotenv import load_dotenv

from hardware.camera import Camera

load_dotenv()


class CameraManager:
    """Manages a pool of Camera objects parsed from CAMERA_SOURCES env var."""

    def __init__(self, sources=None):
        if sources is None:
            raw = os.getenv("CAMERA_SOURCES", "0")
            sources = []
            for part in raw.split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    sources.append(int(part))
                except ValueError:
                    sources.append(part)  # URL string

        if not sources:
            sources = [0]

        self.cameras: List[Camera] = []
        for src in sources:
            self.cameras.append(Camera(src))

        print(f"[CameraManager] Initialised {len(self.cameras)} camera(s).")

    def get_all_frames(self) -> List[np.ndarray]:
        """Return one frame per camera. Always returns at least one frame."""
        frames = [cam.get_frame() for cam in self.cameras]
        return frames

    def release_all(self):
        for cam in self.cameras:
            cam.release()