"""Appearance re-identification across views.

The previous implementation could only ever return one global id. It
L2-normalised its histogram embeddings with ``cv2.normalize`` and then compared
them against ``REID_THRESHOLD = 120.0`` interpreted as a Euclidean distance —
but the maximum possible distance between two unit vectors is 2.0, so the
condition ``dist < 120.0`` was always true and every person on every camera
collapsed onto ``GID-1``.

It also assigned ids inconsistently: a match returned ``gallery_index + 1`` while
a new registration returned a separate incrementing counter, and the gallery
used FIFO eviction that shifted every index underneath it. Ids were therefore
not stable even in principle.

Here embeddings are explicitly unit-normalised, compared by cosine similarity,
and each gallery entry carries its own immutable id. Entries expire on a TTL
rather than by position, so ids stay stable for as long as an entry lives.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import numpy as np

from sentrix.config import settings
from sentrix.utils.logging import get_logger

log = get_logger("reid")

try:
    import cv2
except ImportError:                                    # pragma: no cover
    cv2 = None                                         # type: ignore[assignment]

try:
    from torchreid.utils import FeatureExtractor
    _TORCHREID = True
except ImportError:
    FeatureExtractor = None                            # type: ignore[assignment]
    _TORCHREID = False


@dataclass
class GalleryEntry:
    gid: int
    embedding: np.ndarray
    last_seen: float
    hits: int = 1


def _unit(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 1e-8 else vector


class ReIDEngine:
    """Cosine-similarity gallery matching with TTL eviction."""

    def __init__(self) -> None:
        self.cfg = settings.reid
        self._lock = threading.Lock()
        self._gallery: list[GalleryEntry] = []
        self._next_gid = 1
        self._extractor = None

        if _TORCHREID:
            try:
                self._extractor = FeatureExtractor(model_name="osnet_x0_25",
                                                   model_path=None, device="cpu")
                log.info("re-id backend: torchreid osnet_x0_25")
            except Exception as exc:
                log.warning("torchreid unavailable (%s) — histogram fallback", exc)
        else:
            log.info("re-id backend: colour histogram fallback "
                     "(install torchreid for appearance embeddings)")

    # ── embedding ─────────────────────────────────────────────────────────────
    def _histogram(self, crop: np.ndarray) -> np.ndarray | None:
        if cv2 is None or crop is None or crop.size == 0:
            return None
        try:
            hist = cv2.calcHist([crop], [0, 1, 2], None, [8, 8, 8],
                                [0, 256, 0, 256, 0, 256])
            return _unit(hist.flatten().astype(np.float32))
        except Exception:
            return None

    def embed(self, crop: np.ndarray) -> np.ndarray | None:
        if crop is None or getattr(crop, "size", 0) == 0:
            return None
        if self._extractor is not None:
            try:
                rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
                return _unit(np.asarray(self._extractor([rgb])[0],
                                        dtype=np.float32))
            except Exception as exc:
                log.debug("deep embedding failed: %s", exc)
        return self._histogram(crop)

    # ── matching ──────────────────────────────────────────────────────────────
    def _evict(self, now: float) -> None:
        ttl = self.cfg.gallery_ttl_seconds
        self._gallery = [e for e in self._gallery if now - e.last_seen < ttl]
        if len(self._gallery) > self.cfg.gallery_size:
            self._gallery.sort(key=lambda e: e.last_seen, reverse=True)
            self._gallery = self._gallery[: self.cfg.gallery_size]

    def match_or_register(self, embedding: np.ndarray | None,
                          now: float | None = None) -> int | None:
        """Return a stable global id, or None when no embedding is available.

        Returning None is deliberate. The previous code minted a fresh id for a
        missing embedding, which inflated the person count on every frame where
        a crop happened to be empty.
        """
        if embedding is None:
            return None
        now = time.monotonic() if now is None else now

        with self._lock:
            self._evict(now)
            best, best_similarity = None, -1.0
            for entry in self._gallery:
                similarity = float(np.dot(embedding, entry.embedding))
                if similarity > best_similarity:
                    best_similarity, best = similarity, entry

            if best is not None and best_similarity >= self.cfg.match_similarity:
                # Running mean keeps the prototype stable as appearance drifts.
                best.embedding = _unit(
                    best.embedding * best.hits + embedding) if best.hits else embedding
                best.hits += 1
                best.last_seen = now
                return best.gid

            gid = self._next_gid
            self._next_gid += 1
            self._gallery.append(GalleryEntry(gid, embedding, now))
            return gid

    @property
    def known_count(self) -> int:
        with self._lock:
            return len(self._gallery)

    def reset(self) -> None:
        with self._lock:
            self._gallery.clear()
            self._next_gid = 1


__all__ = ["ReIDEngine", "GalleryEntry"]
