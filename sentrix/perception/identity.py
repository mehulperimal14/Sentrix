"""Face authorisation, fail-closed.

Why this is the most safety-critical module in the system
---------------------------------------------------------
Recognising an enrolled resident forces TCI to a floor value **and suppresses
every downstream side effect** — no snapshot, no encrypted evidence, no SMS, no
siren, no database row. A weak matcher here is not a weak feature; it is an
authentication bypass that silently disables the entire product.

The previous implementation had two modes. Mode 1 used ``face_recognition``
(dlib, 128-d embeddings) — sound, but the package is not installed in this
environment, so it never ran. Mode 2, which is what actually executed, computed
a 512-bin HSV colour histogram over the centre 80% x 70% of the **whole frame**
and declared a match at cosine similarity >= 0.68.

That is scene-colour matching, not identity. Two frames of the same room
routinely exceed that threshold, so anyone standing where the enrolled person
stood was authorised — and the alarm was disabled for them.

This module removes the histogram path entirely. When no real face-embedding
backend is available it reports "not authorised" and logs a warning. A degraded
identity check must fail towards *raising* alarms, never towards suppressing
them. ``IdentityConfig.fail_closed`` guards the behaviour and defaults to True;
it exists so the trade-off is visible in configuration rather than buried.

Backends, in preference order
-----------------------------
1. **InsightFace** (``insightface`` + ``onnxruntime``) — ArcFace 512-d
   embeddings. Recommended: pip-installable on Windows with no CMake or Visual
   Studio build chain, and more accurate than dlib's 128-d model.
2. **face_recognition** (dlib) — 128-d embeddings. Works, but needs a compiler.
3. **None** — fail closed.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import numpy as np

from sentrix.config import settings
from sentrix.paths import FACES_DIR
from sentrix.utils.logging import get_logger, timed

log = get_logger("identity")

try:
    import cv2
except ImportError:                                    # pragma: no cover
    cv2 = None                                         # type: ignore[assignment]

_BACKEND = None
try:
    import insightface                                 # noqa: F401
    import onnxruntime                                 # noqa: F401
    _BACKEND = "insightface"
except ImportError:
    try:
        import face_recognition                        # noqa: F401
        _BACKEND = "face_recognition"
    except ImportError:
        _BACKEND = None

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}


@dataclass
class IdentityResult:
    authorized: bool = False
    name: str | None = None
    similarity: float = 0.0
    #: True when no usable backend exists, so the verdict is "unknown", not "no".
    degraded: bool = False
    #: Faces found on this frame, and how many of them matched the gallery.
    faces: int = 0
    matched: int = 0
    #: True when ``authorized`` is the held verdict of an earlier frame.
    held: bool = False


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b)) + 1e-9
    return float(np.dot(a, b) / denom)


class IdentityEngine:
    """Embedding-based face authorisation over the enrolled gallery."""

    def __init__(self) -> None:
        self.cfg = settings.identity
        self._lock = threading.Lock()
        self._gallery: list[tuple[str, np.ndarray]] = []
        self._app = None
        self._last_match = 0.0
        self._last_name: str | None = None
        self.backend = _BACKEND

        if self.backend is None:
            log.warning(
                "no face-embedding backend installed (tried insightface, "
                "face_recognition). Identity checks FAIL CLOSED: nobody will be "
                "authorised and alerts will not be suppressed. "
                "Install with: pip install insightface onnxruntime")
        else:
            self._init_backend()
        self.reload()

    # ── backend ───────────────────────────────────────────────────────────────
    def _init_backend(self) -> None:
        if self.backend != "insightface":
            log.info("identity backend: face_recognition (dlib, 128-d)")
            return
        try:
            from insightface.app import FaceAnalysis
            app = FaceAnalysis(name="buffalo_l",
                               allowed_modules=list(self.cfg.face_modules),
                               providers=["CPUExecutionProvider"])
            self._quiet_sessions(app)
            size = int(self.cfg.det_size)
            app.prepare(ctx_id=-1, det_size=(size, size))
            self._app = app
            log.info("identity backend: insightface buffalo_l (ArcFace, 512-d; "
                     "modules %s, det %d, ORT %d threads, no spinning)",
                     "+".join(sorted(app.models)), size, self.cfg.ort_threads)
        except Exception as exc:
            log.error("insightface init failed (%s) — falling back", exc)
            self._app = None
            try:
                import face_recognition                 # noqa: F401
                self.backend = "face_recognition"
                log.info("identity backend: face_recognition (dlib, 128-d)")
            except ImportError:
                self.backend = None
                log.warning("no identity backend available — failing closed")

    def _quiet_sessions(self, app) -> None:
        """Rebuild every ORT session without busy-wait spinning.

        insightface forwards only providers to onnxruntime, so the session
        options cannot be passed through FaceAnalysis; the sessions are rebuilt
        from their own model files with identical graphs, so outputs are
        unchanged. ORT's default spin-wait kept its thread pools burning the
        cores that the next torch (YOLO) call needed.
        """
        import onnxruntime as ort
        for model in app.models.values():
            options = ort.SessionOptions()
            options.add_session_config_entry("session.intra_op.allow_spinning", "0")
            options.add_session_config_entry("session.inter_op.allow_spinning", "0")
            options.intra_op_num_threads = max(1, int(self.cfg.ort_threads))
            model.session = ort.InferenceSession(
                model.model_file, sess_options=options,
                providers=["CPUExecutionProvider"])

    @property
    def available(self) -> bool:
        return self.backend is not None

    # ── embeddings ────────────────────────────────────────────────────────────
    def _embed_all(self, bgr: np.ndarray) -> list[np.ndarray]:
        """Every face embedding in a BGR frame. Empty list when none found."""
        if bgr is None or getattr(bgr, "size", 0) == 0:
            return []
        try:
            if self.backend == "insightface" and self._app is not None:
                return [np.asarray(f.normed_embedding, dtype=np.float32)
                        for f in self._app.get(bgr)]
            if self.backend == "face_recognition":
                import face_recognition
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                locations = face_recognition.face_locations(rgb, model="hog")
                if not locations:
                    return []
                return [np.asarray(e, dtype=np.float32)
                        for e in face_recognition.face_encodings(rgb, locations)]
        except Exception as exc:
            log.debug("embedding failed: %s", exc)
        return []

    # ── gallery ───────────────────────────────────────────────────────────────
    def reload(self) -> None:
        """Rebuild the enrolled gallery from ``var/authorized_faces``."""
        gallery: list[tuple[str, np.ndarray]] = []
        FACES_DIR.mkdir(parents=True, exist_ok=True)

        if self.available and cv2 is not None:
            for path in sorted(FACES_DIR.iterdir()):
                if path.suffix.lower() not in _IMAGE_SUFFIXES:
                    continue
                image = cv2.imread(str(path))
                if image is None:
                    log.warning("could not read enrolled image %s", path.name)
                    continue
                embeddings = self._embed_all(image)
                if not embeddings:
                    log.warning("no face found in %s — not enrolled", path.name)
                    continue
                gallery.append((path.stem, embeddings[0]))

        with self._lock:
            self._gallery = gallery
        log.info("identity gallery: %d enrolled face(s) [backend=%s]",
                 len(gallery), self.backend or "none")

    @property
    def enrolled(self) -> list[str]:
        with self._lock:
            return [name for name, _ in self._gallery]

    # ── verification ──────────────────────────────────────────────────────────
    def check(self, frame: np.ndarray, now: float | None = None) -> IdentityResult:
        """Authorise the frame against the enrolled gallery.

        Returns ``authorized=False`` whenever the answer is not a positive,
        high-confidence match — including when no backend is installed.
        """
        now = time.monotonic() if now is None else now

        if not self.available:
            # Degraded, so the caller can surface it. Never authorises.
            return IdentityResult(authorized=False, degraded=True)

        with self._lock:
            gallery = list(self._gallery)
        if not gallery:
            return IdentityResult(authorized=False)

        threshold = 1.0 - self.cfg.match_threshold      # config is a distance
        best_name, best_similarity = None, -1.0
        faces = matched = 0
        with timed("identity", "inference"):
            for embedding in self._embed_all(frame):
                faces += 1
                face_best = -1.0
                for name, known in gallery:
                    similarity = _cosine(embedding, known)
                    face_best = max(face_best, similarity)
                    if similarity > best_similarity:
                        best_similarity, best_name = similarity, name
                matched += face_best >= threshold

        if best_name is not None and best_similarity >= threshold:
            self._last_match, self._last_name = now, best_name
            return IdentityResult(True, best_name, best_similarity,
                                  faces=faces, matched=matched)

        # Hold the previous verdict briefly so a resident turning their head
        # does not immediately re-arm the alarm.
        if (self._last_name is not None
                and now - self._last_match < self.cfg.auth_hold_seconds):
            return IdentityResult(True, self._last_name, best_similarity,
                                  faces=faces, matched=matched, held=True)

        return IdentityResult(False, None, max(best_similarity, 0.0),
                              faces=faces, matched=matched)

    def score(self, result: IdentityResult, person_present: bool) -> float:
        """Identity contribution to the TCI feature vector."""
        if result.authorized:
            return 0.0
        return self.cfg.unknown_person_score if person_present else 0.0


__all__ = ["IdentityEngine", "IdentityResult"]
