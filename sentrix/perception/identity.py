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
from dataclasses import dataclass, field
from pathlib import Path

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

#: InsightFace model packs the identity engine runs (``SENTRIX_FACE_MODEL``).
#: ``buffalo_s`` is the Pi 5 pack (HARDWARE.md T1, S3): about 20x less face
#: detection compute and a much smaller recogniser. The packs' embeddings are
#: not comparable; the gallery is embedded from its images at start-up with
#: the configured pack, so switching packs re-embeds it. A pack missing from
#: ``~/.insightface/models`` is downloaded by insightface on first use.
FACE_MODELS: dict[str, str] = {
    "buffalo_l": "SCRFD-10G + ArcFace R50",
    "buffalo_s": "SCRFD-500M + MobileFaceNet",
}
DEFAULT_FACE_MODEL = "buffalo_l"


def face_model_name(name: str | None = None) -> str:
    """The configured pack, or the default when the name is not supported."""
    chosen = (name if name is not None else settings.identity.face_model) or ""
    chosen = chosen.strip().lower()
    if chosen in FACE_MODELS:
        return chosen
    log.error("unsupported SENTRIX_FACE_MODEL %r (use one of %s); using %s", chosen,
              ", ".join(FACE_MODELS), DEFAULT_FACE_MODEL)
    return DEFAULT_FACE_MODEL


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
    #: Per tracked person: {track_id: {"state": authorised|unknown|unverified,
    #: "name": str | None}} (pipeline only; empty from :meth:`IdentityEngine.check`).
    tracks: dict = field(default_factory=dict)


@dataclass(frozen=True)
class FaceMatch:
    """One face on a checked frame and its best gallery match, if any."""

    box: tuple
    name: str | None
    similarity: float


@dataclass(frozen=True)
class FaceCheck:
    """Faces found on one frame, with the person boxes seen on that frame."""

    faces: tuple = ()
    #: ((track_id, (x1, y1, x2, y2)), ...) at the checked frame.
    persons: tuple = ()
    at: float | None = None
    degraded: bool = False


def person_name(stem: str) -> str:
    """Several photos of one person enrol under one name: ``alice.jpg`` and
    ``alice__2.jpg`` are both ``alice``."""
    return stem.split("__")[0]


@dataclass
class EnrolmentCheck:
    """Outcome of the enrolment quality gates for one image."""

    ok: bool
    reason: str
    faces: int = 0
    embedding: np.ndarray | None = None


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b)) + 1e-9
    return float(np.dot(a, b) / denom)


class IdentityEngine:
    """Embedding-based face authorisation over the enrolled gallery."""

    def __init__(self) -> None:
        self.cfg = settings.identity
        self._lock = threading.Lock()
        self._gallery: list[tuple[str, np.ndarray]] = []
        #: filename -> (person, embedding); the gallery is derived from it.
        self._entries: dict[str, tuple[str, np.ndarray]] = {}
        self._reload_lock = threading.Lock()
        self._app = None
        self._last_match = 0.0
        self._last_name: str | None = None
        self.backend = _BACKEND
        #: The InsightFace pack in use (None for the dlib backend).
        self.model_name: str | None = None

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
            pack = face_model_name(self.cfg.face_model)
            app = FaceAnalysis(name=pack,
                               allowed_modules=list(self.cfg.face_modules),
                               providers=["CPUExecutionProvider"])
            self._quiet_sessions(app)
            size = int(self.cfg.det_size)
            app.prepare(ctx_id=-1, det_size=(size, size))
            self._app = app
            self.model_name = pack
            log.info("identity backend: insightface %s (%s, 512-d; modules %s, det %d, "
                     "ORT %d threads, no spinning)", pack, FACE_MODELS[pack],
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
        """Rebuild the enrolled gallery from ``var/authorized_faces``.

        Embeds every image, so it is slow: callers on the web event loop must
        run it in a thread (it used to freeze the video and websocket for
        2-20 s). Enrolment and removal use :meth:`enrol` / :meth:`remove`.
        """
        entries: dict[str, tuple[str, np.ndarray]] = {}
        FACES_DIR.mkdir(parents=True, exist_ok=True)
        with self._reload_lock:
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
                    entries[path.name] = (person_name(path.stem), embeddings[0])
            self._set_entries(entries)
        log.info("identity gallery: %d enrolled face(s) [backend=%s]",
                 len(entries), self.backend or "none")

    def _set_entries(self, entries: dict[str, tuple[str, np.ndarray]]) -> None:
        with self._lock:
            self._entries = dict(entries)
            self._gallery = list(self._entries.values())

    def enrol(self, filename: str, embedding: np.ndarray) -> str:
        """Add one already-assessed image to the gallery without re-embedding
        the others. Returns the person's name."""
        name = person_name(Path(filename).stem)
        with self._reload_lock:
            entries = dict(self._entries)
            entries[filename] = (name, np.asarray(embedding, dtype=np.float32))
            self._set_entries(entries)
        return name

    def remove(self, filename: str) -> None:
        with self._reload_lock:
            entries = dict(self._entries)
            entries.pop(filename, None)
            self._set_entries(entries)

    @property
    def enrolled(self) -> list[str]:
        with self._lock:
            return [name for name, _ in self._gallery]

    @property
    def enrolled_files(self) -> dict[str, str]:
        """filename -> person, for the files that actually enrolled."""
        with self._lock:
            return {f: name for f, (name, _) in self._entries.items()}

    def _faces(self, bgr: np.ndarray) -> list[tuple[tuple[int, int, int, int], np.ndarray]]:
        """(box, embedding) for every face; empty without a backend."""
        if bgr is None or getattr(bgr, "size", 0) == 0 or not self.available:
            return []
        try:
            if self.backend == "insightface" and self._app is not None:
                return [(tuple(int(v) for v in f.bbox[:4]),
                         np.asarray(f.normed_embedding, dtype=np.float32))
                        for f in self._app.get(bgr)]
            if self.backend == "face_recognition":
                import face_recognition
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                locations = face_recognition.face_locations(rgb, model="hog")
                encodings = face_recognition.face_encodings(rgb, locations)
                return [((left, top, right, bottom), np.asarray(e, dtype=np.float32))
                        for (top, right, bottom, left), e in zip(locations, encodings)]
        except Exception as exc:
            log.debug("face analysis failed: %s", exc)
        return []

    def assess(self, bgr: np.ndarray, name: str) -> EnrolmentCheck:
        """Quality gates for an enrolment image, with a reason a person can act on.

        One face, big enough, sharp, well exposed, and not already enrolled
        under a different name (the same person under two names, or two people
        who the matcher cannot tell apart, would make authorisation ambiguous).
        """
        if not self.available:
            return EnrolmentCheck(False, "no face-recognition backend is installed")
        faces = self._faces(bgr)
        if len(faces) != 1:
            what = "no face" if not faces else f"{len(faces)} faces"
            return EnrolmentCheck(False, f"{what} found; exactly one face is needed",
                                  faces=len(faces))
        (x1, y1, x2, y2), embedding = faces[0]
        cfg = self.cfg
        size = min(x2 - x1, y2 - y1)
        if size < cfg.enrol_min_face_px:
            return EnrolmentCheck(False, f"face is {size}px across; move closer "
                                  f"(needs {cfg.enrol_min_face_px}px)", faces=1)
        h, w = bgr.shape[:2]
        crop = bgr[max(0, y1):min(h, y2), max(0, x1):min(w, x2)]
        grey = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        sharpness = float(cv2.Laplacian(grey, cv2.CV_64F).var())
        if sharpness < cfg.enrol_min_sharpness:
            return EnrolmentCheck(False, f"image is blurred (sharpness {sharpness:.0f}, "
                                  f"needs {cfg.enrol_min_sharpness:.0f}); hold still", faces=1)
        luma = float(grey.mean())
        low, high = cfg.enrol_luma_range
        if not low <= luma <= high:
            hint = "too dark; add light" if luma < low else "overexposed; reduce backlight"
            return EnrolmentCheck(False, f"face is {hint} (brightness {luma:.0f})", faces=1)
        threshold = 1.0 - cfg.match_threshold
        with self._lock:
            gallery = list(self._gallery)
        for other, known in gallery:
            if other != person_name(name) and _cosine(embedding, known) >= threshold:
                return EnrolmentCheck(False, f"this face already matches '{other}'", faces=1)
        return EnrolmentCheck(True, "ok", faces=1, embedding=embedding)

    # ── verification ──────────────────────────────────────────────────────────
    def match_faces(self, frame: np.ndarray) -> list[FaceMatch]:
        """Every face on the frame with its best gallery match (name only at or
        above the match threshold)."""
        with self._lock:
            gallery = list(self._gallery)
        threshold = 1.0 - self.cfg.match_threshold      # config is a distance
        out: list[FaceMatch] = []
        with timed("identity", "inference"):
            for box, embedding in self._faces(frame):
                best_name, best = None, -1.0
                for name, known in gallery:
                    similarity = _cosine(embedding, known)
                    if similarity > best:
                        best, best_name = similarity, name
                out.append(FaceMatch(box, best_name if best >= threshold else None,
                                     max(best, 0.0)))
        return out

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


def _owner(box: tuple, persons: tuple) -> int | None:
    """The track whose person box contains the face centre (the smallest such
    box, so a face in front of someone else's box goes to its own person)."""
    cx, cy = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
    best, best_area = None, None
    for track_id, (x1, y1, x2, y2) in persons:
        if track_id is None or not (x1 <= cx <= x2 and y1 <= cy <= y2):
            continue
        area = (x2 - x1) * (y2 - y1)
        if best_area is None or area < best_area:
            best, best_area = track_id, area
    return best


@dataclass
class _Track:
    first_seen: float
    seen_at: float
    match_at: float | None = None
    name: str | None = None
    misses: int = 0            # consecutive unmatched looks at this track's face
    looked: bool = False       # any face ever associated


class TrackIdentity:
    """Who each tracked person is (CLAUDE.md M4).

    Identity was frame-global: one matching face authorised everyone in view
    for 5 s (a stranger walking in beside a resident inherited it), and one
    tick without a face made the resident a stranger. Here each face is
    attached to the person box that contains it, and each ByteTrack id has
    its own state:

    * ``authorised`` - its face matched, within ``track_hold_seconds``, and
      has not since failed to match twice in a row;
    * ``unknown`` - its face was seen and did not match;
    * ``unverified`` - no usable look at its face yet.

    The frame is authorised only when *everyone* in view is authorised, so an
    unverified or unknown person keeps the armed-mode intrusion rule live
    (fail closed). With no person boxes (e.g. a face too close for the person
    detector), every visible face must match.
    """

    def __init__(self, cfg=None) -> None:
        self.cfg = cfg or settings.identity
        self._tracks: dict[int, _Track] = {}
        self._folded_at: float | None = None

    def _fold(self, check: FaceCheck) -> None:
        for face in check.faces:
            track_id = _owner(face.box, check.persons)
            if track_id is None:
                continue
            track = self._tracks.setdefault(track_id, _Track(check.at, check.at))
            track.looked = True
            if face.name is not None:
                track.match_at, track.name, track.misses = check.at, face.name, 0
            else:
                track.misses += 1

    def _state(self, track: _Track, now: float) -> str:
        held = (track.match_at is not None
                and now - track.match_at <= self.cfg.track_hold_seconds)
        if held and track.misses < 2:
            return "authorised"
        if track.looked and (track.misses > 0 or not held):
            return "unknown" if track.misses > 0 else "unverified"
        return "unverified"

    def update(self, check: FaceCheck, persons: tuple, now: float) -> IdentityResult:
        """Fold a new face check (if any) and summarise the people in view."""
        if check.degraded:
            return IdentityResult(authorized=False, degraded=True)
        if check.at is not None and check.at != self._folded_at:
            self._folded_at = check.at
            self._fold(check)

        verdicts: dict[int, dict] = {}
        for track_id, _box in persons:
            if track_id is None:
                continue
            track = self._tracks.setdefault(track_id, _Track(now, now))
            track.seen_at = now
            state = self._state(track, now)
            verdicts[track_id] = {"state": state,
                                  "name": track.name if state == "authorised" else None}
        for track_id in [t for t, tr in self._tracks.items()
                         if now - tr.seen_at > self.cfg.track_forget_seconds]:
            del self._tracks[track_id]

        faces = len(check.faces)
        matched = sum(1 for f in check.faces if f.name is not None)
        stale = check.at is None or now - check.at > self.cfg.verdict_max_age_seconds
        if verdicts:
            everyone = all(v["state"] == "authorised" for v in verdicts.values())
        else:
            everyone = faces > 0 and matched == faces
        everyone = everyone and not stale
        names = sorted({v["name"] for v in verdicts.values() if v["name"]} |
                       ({f.name for f in check.faces if f.name} if not verdicts else set()))
        best = max((f.similarity for f in check.faces), default=0.0)
        return IdentityResult(authorized=everyone, name=", ".join(names) or None,
                              similarity=best, faces=faces, matched=matched,
                              tracks=verdicts)

    def reset(self) -> None:
        self._tracks.clear()
        self._folded_at = None


class IdentityWorker:
    """Face recognition off the inference thread (CLAUDE.md M2).

    Recognition was the largest per-tick cost in the live configuration (125 ms
    p50 with four faces on the laptop CPU). A face verdict does not have to be
    recomputed every tick - people do not change identity between frames - so
    the worker re-checks at most once per ``recheck_seconds``, at once when
    more people come into view, and every ``authorised_recheck_seconds`` while
    everyone in view is recognised. The pipeline reads the latest verdict and
    never waits for it.

    Fail-closed: until the first check nobody is authorised, and
    :class:`TrackIdentity` stops authorising when the latest check is older
    than ``verdict_max_age_seconds`` (worker stalled).
    """

    def __init__(self, engine: IdentityEngine, cfg=None) -> None:
        self.engine = engine
        self.cfg = cfg or settings.identity
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._job: tuple[np.ndarray, tuple, float] | None = None
        self._busy = False
        self._result = FaceCheck(degraded=not engine.available)
        self._submitted_at: float | None = None
        self._known: set = set()
        self._arrival = False
        self.checks = 0
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="sentrix-identity")
        self._thread.start()

    def observe(self, frame: np.ndarray, persons: tuple, now: float,
                settled: bool = False) -> FaceCheck:
        """The latest face check; schedules one on ``frame`` when due.

        ``persons`` are this frame's ``(track_id, box)`` pairs; ``settled``
        says everyone in view is already authorised.
        """
        with self._lock:
            ids = {track_id for track_id, _ in persons}
            if ids - self._known:
                self._arrival = True          # someone new: check as soon as idle
            self._known = ids
            if not self._busy and self._due(now, settled):
                self._job, self._busy = (frame, tuple(persons), now), True
                self._submitted_at, self._arrival = now, False
                self._wake.set()
            return self._result

    def _due(self, now: float, settled: bool) -> bool:
        if self._submitted_at is None or self._arrival:
            return True
        interval = (self.cfg.authorised_recheck_seconds if settled
                    else self.cfg.recheck_seconds)
        return now - self._submitted_at >= interval

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self._wake.wait(timeout=0.5):
                continue
            self._wake.clear()
            with self._lock:
                job, self._job = self._job, None
            if job is None:
                continue
            frame, persons, at = job
            try:
                result = FaceCheck(tuple(self.engine.match_faces(frame)), persons, at)
            except Exception as exc:
                log.error("identity check failed: %s", exc)
                result = FaceCheck(persons=persons, at=at, degraded=True)
            with self._lock:
                self._result, self._busy = result, False
                self.checks += 1

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=5.0)


__all__ = ["DEFAULT_FACE_MODEL", "EnrolmentCheck", "FACE_MODELS", "FaceCheck", "FaceMatch",
           "IdentityEngine", "IdentityResult", "IdentityWorker", "TrackIdentity",
           "face_model_name", "person_name"]
