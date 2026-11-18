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

**Body model** (CLAUDE.md D22): OSNet, IntrusionSentry's re-id model family -
OSNet-AIN x1_0 trained on MSMT17 (``sentrix/perception/osnet.py``) - when
``models/reid/`` holds its weights; else the 8x8x8 colour histogram. Body
embeddings are computed **with each face check** (:meth:`ReIDEngine.bodies_for`,
on the recognition thread when live), never per person per tick on the
inference thread: OSNet costs ~10 ms a crop on the laptop CPU. They serve two
purposes: global ids (:meth:`ReIDEngine.global_ids`) and, OSNet only, carrying
a face-verified resident's verdict to their own track when their face is not
in view (``TrackIdentity`` in ``identity.py``). No embedding is computed when
nobody is enrolled: there is nobody to recognise.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import numpy as np

from sentrix.config import settings
from sentrix.utils.logging import get_logger

log = get_logger("reid")

try:
    import cv2
except ImportError:                                    # pragma: no cover
    cv2 = None                                         # type: ignore[assignment]


@dataclass
class GalleryEntry:
    gid: int
    embedding: np.ndarray
    last_seen: float
    hits: int = 1


def _unit(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 1e-8 else vector


def _load_osnet(cfg):
    from sentrix.paths import MODELS_DIR
    from sentrix.perception.osnet import WEIGHTS_NAME, BodyEmbedder
    embedder = BodyEmbedder(MODELS_DIR / "reid" / WEIGHTS_NAME, threads=cfg.threads)
    return embedder if embedder.available else None


class ReIDEngine:
    """Body embeddings (OSNet or colour histogram) and a cosine-similarity
    gallery of global ids with TTL eviction."""

    def __init__(self, cfg=None, embedder=None) -> None:
        self.cfg = cfg or settings.reid
        self._lock = threading.Lock()
        self._gallery: list[GalleryEntry] = []
        self._next_gid = 1
        self._embedder = embedder
        #: track id -> (global id, pipeline time last folded)
        self._track_gid: dict[int, tuple[int, float]] = {}
        self._folded_at: float | None = None

        choice = self.cfg.model
        if choice == "off":
            self.backend = "off"
        elif self._embedder is not None:
            self.backend = "osnet"
        elif choice in ("auto", "osnet"):
            self._embedder = _load_osnet(self.cfg)
            self.backend = "osnet" if self._embedder is not None else "histogram"
            if self._embedder is None:
                log.info("re-id backend: colour histogram (OSNet weights missing: "
                         "python tools/fetch_models.py)")
        else:
            self.backend = "histogram"
        if self.backend == "osnet":
            log.info("re-id backend: %s (%s), %d-thread session",
                     getattr(self._embedder, "name", "osnet"),
                     getattr(self._embedder, "weights", "injected"), self.cfg.threads)

    @property
    def enabled(self) -> bool:
        return self.backend != "off"

    @property
    def deep(self) -> bool:
        """True for OSNet: an embedding that can tell people apart (D22)."""
        return self.backend == "osnet"

    @property
    def match_similarity(self) -> float:
        return (self.cfg.osnet_match_similarity if self.deep
                else self.cfg.match_similarity)

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
        """One BGR person crop -> unit embedding, or None."""
        if not self.enabled or crop is None or getattr(crop, "size", 0) == 0:
            return None
        if self.deep:
            try:
                return self._embedder.embed([crop])[0]
            except Exception as exc:
                log.debug("deep embedding failed: %s", exc)
                return None
        return self._histogram(crop)

    def embed_persons(self, frame: np.ndarray, persons: tuple) -> tuple:
        """``((track_id, embedding), ...)`` for the tracked people of a frame;
        ``persons`` are ``(track_id, box)`` pairs. One batch for OSNet."""
        if not self.enabled or frame is None:
            return ()
        from sentrix.perception.osnet import body_crop
        crops = [(track_id, body_crop(frame, box)) for track_id, box in persons
                 if track_id is not None]
        crops = [(t, c) for t, c in crops if c is not None]
        if not crops:
            return ()
        if self.deep:
            vectors = self._embedder.embed([c for _, c in crops])
            return tuple((t, v) for (t, _), v in zip(crops, vectors))
        out = ((t, self._histogram(c)) for t, c in crops)
        return tuple((t, v) for t, v in out if v is not None)

    def bodies_for(self, frame: np.ndarray, persons: tuple, identity) -> dict:
        """The body half of a face check: ``{"bodies": ..., "body_model": ...}``,
        or ``{}`` when re-id is off, nobody is enrolled (nobody to recognise; the
        evaluation harness's identity enrols nobody), or embedding fails."""
        if not self.enabled or not getattr(identity, "enrolled", None):
            return {}
        try:
            return {"bodies": self.embed_persons(frame, persons),
                    "body_model": self.backend}
        except Exception as exc:
            log.warning("body embeddings failed: %s", exc)
            return {}

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

            if best is not None and best_similarity >= self.match_similarity:
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

    def global_ids(self, check, now: float) -> dict[int, int]:
        """``{track_id: global id}``: a face check's body embeddings are folded
        into the gallery once (by the check's time), and a track keeps its id
        between checks."""
        at = getattr(check, "at", None)
        bodies = getattr(check, "bodies", ())
        with self._lock:
            fold = at is not None and at != self._folded_at and bool(bodies)
            if fold:
                self._folded_at = at
        if fold:
            for track_id, embedding in bodies:
                gid = self.match_or_register(embedding, at)
                if gid is not None:
                    self._track_gid[track_id] = (gid, now)
        ttl = self.cfg.gallery_ttl_seconds
        for track_id in [t for t, (_, seen) in self._track_gid.items() if now - seen > ttl]:
            del self._track_gid[track_id]
        return {t: gid for t, (gid, _) in self._track_gid.items()}

    @property
    def known_count(self) -> int:
        with self._lock:
            return len(self._gallery)

    def reset(self) -> None:
        with self._lock:
            self._gallery.clear()
            self._next_gid = 1
            self._folded_at = None
        self._track_gid.clear()


__all__ = ["ReIDEngine", "GalleryEntry"]
