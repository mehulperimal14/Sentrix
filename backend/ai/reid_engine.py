# ai/reid_engine.py
#
# ARCHITECTURE: Person Re-Identification across camera views.
# Maintains a gallery of appearance embeddings (colour histograms as fallback,
# torchreid osnet if installed). Assigns persistent global IDs by comparing
# new embeddings to the gallery using L2 distance.
# match_or_register() is the primary API — returns an integer global person ID.

import cv2
import numpy as np
from typing import Optional, List

try:
    from torchreid.utils import FeatureExtractor as TorchReIDExtractor
    _TORCHREID = True
except ImportError:
    _TORCHREID = False

REID_THRESHOLD  = 120.0   # L2 distance threshold for histogram embeddings
MAX_GALLERY_SIZE = 200    # Maximum number of identity embeddings kept in memory


class ReIDEngine:
    """
    Appearance-based person re-identification.
    Uses colour histogram embeddings when torchreid is unavailable.
    """

    def __init__(self):
        self.gallery: List[np.ndarray] = []
        self.next_gid: int = 1
        self._extractor = None

        if _TORCHREID:
            try:
                self._extractor = TorchReIDExtractor(
                    model_name="osnet_x0_25",
                    model_path=None,
                    device="cpu",
                )
                print("[ReIDEngine] torchreid osnet_x0_25 loaded.")
            except Exception as e:
                print(f"[ReIDEngine] torchreid load failed: {e}. Using histogram fallback.")
        else:
            print("[ReIDEngine] torchreid not installed. Using colour histogram fallback.")

    def _extract_histogram(self, crop: np.ndarray) -> np.ndarray:
        """8×8×8 BGR histogram as a normalised flat vector."""
        hist = cv2.calcHist(
            [crop], [0, 1, 2], None, [8, 8, 8],
            [0, 256, 0, 256, 0, 256],
        )
        return cv2.normalize(hist, hist).flatten()

    def extract_embedding(self, crop: np.ndarray) -> Optional[np.ndarray]:
        """Extract appearance embedding from a cropped person image."""
        import time
        start_time = time.time()
        if crop is None or crop.size == 0:
            from core.instrumentation import log_instrumentation
            log_instrumentation("ReIDEngine", "missing_output", {"reason": "crop is None"})
            return None
        try:
            if self._extractor is not None:
                rgb  = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
                feat = self._extractor([rgb])
                from core.instrumentation import log_instrumentation
                log_instrumentation("ReIDEngine", "inference", {"type": "deep", "latency": time.time() - start_time})
                return feat[0]
            from core.instrumentation import log_instrumentation
            log_instrumentation("ReIDEngine", "inference", {"type": "histogram", "latency": time.time() - start_time})
            return self._extract_histogram(crop)
        except Exception as e:
            print(f"[ReIDEngine] embedding error: {e}")
            return None

    def match_or_register(self, embedding: Optional[np.ndarray]) -> int:
        """
        Match embedding against gallery. If close enough, return existing GID.
        Otherwise register as a new person and return new GID.
        """
        if embedding is None:
            gid = self.next_gid
            self.next_gid += 1
            return gid

        if not self.gallery:
            self.gallery.append(embedding)
            gid = self.next_gid
            self.next_gid += 1
            return gid

        dists   = [np.linalg.norm(embedding - g) for g in self.gallery]
        min_idx = int(np.argmin(dists))
        if dists[min_idx] < REID_THRESHOLD:
            return min_idx + 1   # 1-indexed global ID
        else:
            self.gallery.append(embedding)
            # FIFO eviction: drop oldest embedding when gallery is too large
            if len(self.gallery) > MAX_GALLERY_SIZE:
                self.gallery.pop(0)
            gid = self.next_gid
            self.next_gid += 1
            return gid

    # Legacy API used by old system_engine
    def match(self, embedding: Optional[np.ndarray]) -> int:
        return self.match_or_register(embedding)