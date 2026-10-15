"""Acoustic threat classification over a sliding window.

Checkpoint-adaptive by design
-----------------------------
The repository contains two mutually incompatible audio checkpoints:

===================  ==========================  ==========================
                     ``audio_classifier.pt``     ``..._real_v3.pt``
===================  ==========================  ==========================
Parameter tensors    25                          52
Module names         ``conv.*`` / ``fc.*``       ``features.*`` / ``classifier.*``
Classes              5 (``normal_ambient`` …)    4 (``speech_normal`` …)
Front end            linear spectrogram, 64x64   log-mel, 64x251, 4 s
===================  ==========================  ==========================

The previous engine hardcoded the 5-class V2 architecture and its front end.
Following the documented upgrade procedure — copying the V3 file over the
deployed name — therefore raised a ``load_state_dict`` error that was swallowed
by a bare ``except``, leaving ``self._model = None``. Audio then reported
``0.02 / normal_ambient`` forever, silently.

This module inspects the state dict, builds the matching architecture, and
selects the front end that checkpoint was trained with. Both files load, and
promoting V3 really is just a file copy.

Threat weighting is keyed by *class name*, not index, so adding or removing a
class (dropping ``explosion``, adding ``glass_break`` or a
``nuisance_impulsive`` hard-negative class) needs no code change here.

General sound model and time alignment (D15)
--------------------------------------------
When ``models/audio/panns_cnn10_audioset.pth`` is present (``settings.audio.
model`` ``auto`` or ``panns``) the engine runs the PANNs CNN10 AudioSet tagger
(:mod:`sentrix.perception.sound_tagger`) and reports five *cues*
(aggression, weapon_sound, fire_sound, benign_social, siren); the 4-class CNN
is the fallback and maps its classes onto the same cues.

Every :class:`AudioResult` carries the window it describes: ``start``/``end``
on the pipeline's monotonic clock and ``wall_end``. :meth:`AudioEngine.
result_for` hands fusion a result only if that window overlaps the frame time
within ``settings.audio.align_tolerance_seconds``; otherwise audio is
*unavailable* for that frame (never "calm"). :class:`FileAudio` does the same
for a replayed video's own soundtrack (no microphone).
"""

from __future__ import annotations

import math
import queue
import threading
import time
from dataclasses import dataclass, field, replace

import numpy as np

from sentrix.config import settings
from sentrix.io import self_output
from sentrix.paths import model_path
from sentrix.utils.logging import get_logger, timed

log = get_logger("audio")

try:
    import sounddevice as sd
    _SD = True
except Exception:                                      # pragma: no cover
    sd = None                                          # type: ignore[assignment]
    _SD = False

_WEIGHTS = ("v3_real/audio_classifier_real_v3.pt", "audio_classifier.pt")

#: How dangerous each acoustic class is, by name. Unknown classes score 0.
#: Sirens sit low on purpose: a siren means help is already coming, and in
#: Indian urban soundscapes it is also the class most often confused with a
#: vehicle pressure horn.
THREAT_WEIGHTS: dict[str, float] = {
    "normal_ambient": 0.0,
    "speech_normal": 0.0,
    "ambient": 0.0,
    "nuisance_impulsive": 0.05,
    "siren": 0.45,
    "siren_like": 0.45,
    "alarm_siren": 0.55,
    "fire": 0.75,
    "scream": 0.80,
    "scream_like": 0.80,
    "glass_break": 0.80,
    "gunshot": 0.95,
    "gunshot_like": 0.95,
    "explosion_like": 0.95,
}

_QUIET = ("normal_ambient", "speech_normal", "ambient")


#: Old 4/5-class CNN labels -> the general cues (D15).
CNN_CUES: dict[str, str] = {
    "scream": "aggression", "scream_like": "aggression", "glass_break": "aggression",
    "gunshot": "weapon_sound", "gunshot_like": "weapon_sound",
    "explosion_like": "weapon_sound",
    "fire": "fire_sound", "alarm_siren": "fire_sound",
    "siren": "siren", "siren_like": "siren",
    "speech_normal": "calm_talk",
}
#: Every cue, zero (a quiet window: heard, nothing there).
CUE_NAMES = ("aggression", "weapon_sound", "fire_sound", "benign_social", "calm_talk",
             "background", "siren")
#: ``AudioResult.model`` values that are not a classification of the room.
UNUSABLE_MODELS = {"none": "unclassified", "error": "unclassified", "self": "self_output"}
#: The cue each class reads as source ``audio``. A weapon also reads
#: aggression, as source ``audio_aggression``: it corroborates a visible gun
#: but never stands alone (``evidence.CORROBORATE_ONLY``).
CLASS_CUES: dict[str, str] = {
    "weapon": "weapon_sound",
    "fire": "fire_sound",
    "assault": "aggression",
}


@dataclass
class AudioResult:
    score: float = 0.0
    label: str = "normal_ambient"
    confidence: float = 0.0
    #: Cue values in [0, 1] (``sound_tagger.CUES``); empty when unclassified.
    cues: dict = field(default_factory=dict)
    #: The window this result describes, on the pipeline's monotonic clock.
    start: float | None = None
    end: float | None = None
    wall_end: float | None = None
    rms: float | None = None
    #: ``panns_cnn10`` | ``cnn`` | ``quiet`` (below the RMS floor) | ``none``.
    model: str = "none"

    def to_dict(self) -> dict:
        return {"label": self.label, "score": round(float(self.score), 4),
                "confidence": round(float(self.confidence), 4), "model": self.model,
                "cues": {k: round(float(v), 4) for k, v in (self.cues or {}).items()},
                "start": None if self.start is None else round(self.start, 3),
                "end": None if self.end is None else round(self.end, 3),
                "wall_end": None if self.wall_end is None else round(self.wall_end, 3),
                "rms": None if self.rms is None else round(self.rms, 5)}


def audio_votes(result: AudioResult, fresh: bool = True) -> dict[str, dict[str, tuple]]:
    """An aligned result's votes for the evidence layer (D15):
    ``{"audio": {class: (cue, threshold, fresh)}, "audio_benign": {"assault": ...}}``.

    ``audio_aggression`` (weapon) corroborates a visible weapon only. The
    banter vote is laughter, or very calm talk at its strict bar
    (``calm_talk_is_benign``), whichever is further over its bar. A quiet
    window votes 0 for every class (heard: nothing there), which is different
    from no vote at all (audio unavailable). A window dominated by TV, music
    or radio (:func:`neutral_reason`) votes nothing.
    """
    if neutral_reason(result) is not None:
        return {}
    thresholds = settings.audio.cue_thresholds
    cues = getattr(result, "cues", None) or {}

    def vote(cue: str) -> tuple:
        return (float(cues.get(cue, 0.0)), float(thresholds.get(cue, 0.3)), bool(fresh))

    benign = vote("benign_social")
    if settings.audio.calm_talk_is_benign:
        calm = vote("calm_talk")
        if calm[0] / calm[1] > benign[0] / benign[1]:
            benign = calm
    return {"audio": {cls: vote(cue) for cls, cue in CLASS_CUES.items()},
            "audio_aggression": {"weapon": vote("aggression")},
            "audio_benign": {"assault": benign}}


def neutral_reason(result: AudioResult | None) -> str | None:
    """Why an aligned window must not vote, or None: TV, music or radio
    dominate it (a film's screams or gunfire say nothing about the room)."""
    cues = getattr(result, "cues", None) or {}
    bar = float(settings.audio.cue_thresholds.get("background", 0.4))
    if float(cues.get("background", 0.0)) >= bar:
        return "background (TV/music/radio)"
    return None


def alignment(result: AudioResult | None, at: float, tolerance: float | None = None) -> dict:
    """Does ``result`` describe the moment ``at``? ``{"status": aligned |
    stale | future | untimed | none, "lag_s": at - end}``. Aligned: the
    window ``[start, end]`` overlaps ``[at - tolerance, at + tolerance]``."""
    tolerance = settings.audio.align_tolerance_seconds if tolerance is None else tolerance
    if result is None:
        return {"status": "none", "lag_s": None, "tolerance_s": tolerance}
    unusable = UNUSABLE_MODELS.get(getattr(result, "model", ""))
    if unusable is not None:
        # Nothing classified (no model, inference failed) or the device's own
        # speaker/siren/buzzer: never "heard, nothing there".
        return {"status": unusable, "lag_s": None, "tolerance_s": tolerance}
    if result.end is None:
        return {"status": "untimed", "lag_s": None, "tolerance_s": tolerance}
    start = result.start if result.start is not None else result.end
    lag = at - result.end
    if result.end < at - tolerance:
        status = "stale"
    elif start > at + tolerance:
        status = "future"
    else:
        status = "aligned"
    return {"status": status, "lag_s": round(lag, 3), "tolerance_s": tolerance}


def _pick_device():
    import torch
    if torch.cuda.is_available():
        return torch.device("cuda")
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def _build_v3(num_classes: int):
    """Four VGG-style blocks -> global pool -> 2-layer head. 52 tensors."""
    import torch.nn as nn

    def block(cin, cout):
        return nn.Sequential(
            nn.Conv2d(cin, cout, 3, padding=1, bias=False), nn.BatchNorm2d(cout), nn.ReLU(inplace=True),
            nn.Conv2d(cout, cout, 3, padding=1, bias=False), nn.BatchNorm2d(cout), nn.ReLU(inplace=True),
            nn.MaxPool2d(2, 2),
        )

    class SentrixAudioCNN(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.features = nn.Sequential(block(1, 32), block(32, 64),
                                          block(64, 128), block(128, 256))
            self.classifier = nn.Sequential(
                nn.AdaptiveAvgPool2d((1, 1)), nn.Flatten(),
                nn.Linear(256, 128), nn.ReLU(inplace=True),
                nn.Dropout(0.3), nn.Linear(128, num_classes),
            )

        def forward(self, x):
            return self.classifier(self.features(x))

    return SentrixAudioCNN()


def _build_v2(num_classes: int):
    """Legacy 3-conv net with a 4x4 adaptive pool. 25 tensors."""
    import torch.nn as nn

    class AudioThreatCNN(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.conv = nn.Sequential(
                nn.Conv2d(1, 16, 3, padding=1), nn.BatchNorm2d(16), nn.ReLU(), nn.MaxPool2d(2, 2),
                nn.Conv2d(16, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(), nn.MaxPool2d(2, 2),
                nn.Conv2d(32, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(),
                nn.AdaptiveAvgPool2d((4, 4)),
            )
            self.fc = nn.Sequential(nn.Dropout(0.3), nn.Linear(64 * 16, 64),
                                    nn.ReLU(), nn.Linear(64, num_classes))

        def forward(self, x):
            return self.fc(self.conv(x).flatten(1))

    return AudioThreatCNN()


class AudioEngine:
    """Background microphone capture plus windowed acoustic classification.

    ``capture=False`` builds the classifier only (no microphone thread): the
    replay tools use it to classify a video's own soundtrack (:class:`FileAudio`).
    """

    def __init__(self, capture: bool = True) -> None:
        self.cfg = settings.windows
        self.available = False
        self._result = AudioResult()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stream = None
        self._blocks: queue.Queue = queue.Queue(maxsize=32)

        self._model = None
        self._device = None
        self._classes: list[str] = []
        self._front_end = "logmel"
        self._meta: dict = {}
        self._ring = np.zeros(0, dtype=np.float32)
        self.tagger = None
        #: ``callback(samples, end_at)`` per captured hop (the clip buffer).
        self._sample_listeners: list = []

        choice = settings.audio.model
        if choice in ("auto", "panns"):
            self._load_tagger()
        if choice == "cnn" or (choice == "auto" and self.tagger is None):
            self._load()
        if capture and choice != "off":
            self._start_capture()

    @property
    def model_name(self) -> str:
        if self.tagger is not None:
            return self.tagger.name
        return "cnn" if self._model is not None else "none"

    # ── loading ───────────────────────────────────────────────────────────────
    def _load_tagger(self) -> None:
        from sentrix.perception.sound_tagger import LABELS_NAME, WEIGHTS_NAME, SoundTagger
        tagger = SoundTagger(model_path("audio", WEIGHTS_NAME), model_path("audio", LABELS_NAME),
                             threads=settings.audio.threads)
        self.tagger = tagger if tagger.available else None

    def _load(self) -> None:
        path = None
        for name in _WEIGHTS:
            candidate = model_path(*name.split("/"))
            if candidate.exists():
                path = candidate
                break
        if path is None:
            log.warning("no audio weights found — acoustic engine disabled")
            return
        try:
            import torch

            self._device = _pick_device()
            checkpoint = torch.load(str(path), map_location=self._device,
                                    weights_only=False)
            state = checkpoint.get("model_state_dict", checkpoint)
            self._meta = {k: v for k, v in checkpoint.items()
                          if not hasattr(v, "shape") and k != "model_state_dict"}

            classes = checkpoint.get("classes")
            if not classes:
                log.error("checkpoint %s has no class list — refusing to guess",
                          path.name)
                return
            self._classes = [str(c) for c in classes]

            # Architecture is chosen from the parameter names, not from a
            # version string, so an unexpected checkpoint fails loudly here
            # rather than silently disabling audio at runtime.
            keys = list(state.keys())
            if any(k.startswith("features.") for k in keys):
                net = _build_v3(len(self._classes))
                self._front_end = "logmel"
            elif any(k.startswith("conv.") for k in keys):
                net = _build_v2(len(self._classes))
                self._front_end = "spectrogram"
            else:
                log.error("unrecognised audio architecture in %s", path.name)
                return

            net.load_state_dict(state)
            net.to(self._device).eval()
            self._model = net
            log.info("audio classifier loaded: %s (%s front end, classes=%s)",
                     path.name, self._front_end, self._classes)

            unknown = [c for c in self._classes if c not in THREAT_WEIGHTS]
            if unknown:
                log.warning("classes with no threat weight, scored 0.0: %s", unknown)
        except Exception as exc:
            log.error("audio classifier failed to load: %s", exc)
            self._model = None

    # ── capture ───────────────────────────────────────────────────────────────
    def _start_capture(self) -> None:
        """One persistent ``InputStream`` (no gaps between hops, unlike a
        fresh ``sd.rec`` per hop); its callback only queues the block with its
        capture time, and a worker thread classifies."""
        if not _SD:
            log.warning("sounddevice not installed — microphone disabled")
            return
        try:
            info = sd.query_devices(kind="input")
            log.info("microphone: %s", info.get("name", "default"))
        except Exception as exc:
            log.warning("no input device (%s) — microphone disabled", exc)
            return
        sr = self.cfg.audio_sample_rate
        try:
            self._stream = sd.InputStream(samplerate=sr, channels=1, dtype="float32",
                                          blocksize=int(sr * self.cfg.audio_hop_seconds),
                                          callback=self._on_block)
            self._stream.start()
        except Exception as exc:
            log.warning("microphone stream failed (%s) — microphone disabled", exc)
            self._stream = None
            return
        self.available = True
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="sentrix-audio")
        self._thread.start()

    def _on_block(self, indata, frames, time_info, status) -> None:
        """PortAudio callback: copy, stamp, hand over. Never blocks."""
        end_at = block_end_monotonic(time_info, frames, self.cfg.audio_sample_rate)
        item = (np.array(indata[:, 0] if getattr(indata, "ndim", 1) > 1 else indata,
                         dtype=np.float32, copy=True), end_at, time.time())
        try:
            self._blocks.put_nowait(item)
        except queue.Full:                    # the worker fell behind: drop the oldest
            try:
                self._blocks.get_nowait()
                self._blocks.put_nowait(item)
            except (queue.Empty, queue.Full):
                pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                samples, end_at, wall = self._blocks.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.process_block(samples, end_at, wall)
            except Exception as exc:
                log.debug("audio block failed: %s", exc)

    def process_block(self, samples: np.ndarray, end_at: float,
                      wall: float | None = None) -> AudioResult | None:
        """One captured hop: listeners (the clip buffer), the ring buffer and,
        once a full window is held, a classification stamped with that
        window (D15). Public so it can be driven without a microphone."""
        window = self.cfg.audio_window_seconds
        window_samples = int(self.cfg.audio_sample_rate * window)
        samples = np.nan_to_num(np.asarray(samples, dtype=np.float32).reshape(-1))
        self._notify_samples(samples, end_at)
        self._ring = np.concatenate([self._ring, samples])[-window_samples:]
        if len(self._ring) < window_samples:
            return None
        result = self.classify_window(self._ring, start=end_at - window, end=end_at,
                                      wall_end=wall if wall is not None else time.time())
        self._publish(result)
        return result

    def _publish(self, result: AudioResult) -> None:
        with self._lock:
            self._result = result

    # ── front ends ────────────────────────────────────────────────────────────
    def _logmel(self, samples: np.ndarray):
        """Log-mel spectrogram matching PRODUCTION_PREPROCESSING_CONTRACT.json."""
        import torch

        meta = self._meta
        n_mels = int(meta.get("n_mels", self.cfg.audio_n_mels))
        n_fft = int(meta.get("n_fft", self.cfg.audio_n_fft))
        hop = int(meta.get("hop_length", self.cfg.audio_hop_length))
        fmin = float(meta.get("f_min", self.cfg.audio_fmin))
        fmax = float(meta.get("f_max", self.cfg.audio_fmax))
        top_db = float(meta.get("top_db", self.cfg.audio_top_db))
        sr = int(meta.get("sample_rate", self.cfg.audio_sample_rate))

        try:
            import torchaudio
            wav = torch.from_numpy(samples).unsqueeze(0)
            mel = torchaudio.transforms.MelSpectrogram(
                sample_rate=sr, n_fft=n_fft, hop_length=hop, f_min=fmin,
                f_max=fmax, n_mels=n_mels, power=2.0)(wav)
            spec = torchaudio.transforms.AmplitudeToDB(stype="power",
                                                       top_db=top_db)(mel)
        except ImportError:
            import librosa
            mel = librosa.feature.melspectrogram(
                y=samples, sr=sr, n_fft=n_fft, hop_length=hop, n_mels=n_mels,
                fmin=fmin, fmax=fmax, power=2.0)
            spec = torch.from_numpy(
                librosa.power_to_db(mel, top_db=top_db)).unsqueeze(0)

        spec = (spec - spec.mean()) / (spec.std() + 1e-5)   # per-sample norm
        return spec.unsqueeze(0)

    def _legacy_spectrogram(self, samples: np.ndarray):
        """Linear spectrogram resized to 64x64, as the V2 checkpoint expects."""
        import cv2
        import scipy.signal
        import torch

        _, _, sxx = scipy.signal.spectrogram(
            samples.astype(np.float64), fs=self.cfg.audio_sample_rate,
            nperseg=512, noverlap=256)
        sxx = np.log(sxx + 1e-6)
        resized = cv2.resize(sxx, (64, 64)).astype(np.float32)
        return torch.from_numpy(resized).unsqueeze(0).unsqueeze(0)

    # ── inference ─────────────────────────────────────────────────────────────
    def _classify(self, samples: np.ndarray, rms: float) -> AudioResult:
        """Classify one window (no timing). The tagger when loaded, else the
        4-class CNN; both report cues."""
        if self.tagger is not None:
            return self._tag(samples, rms)
        if self._model is None:
            return AudioResult(0.0, "normal_ambient", 0.0, rms=rms)
        try:
            import torch
            with timed("audio", "inference", {"rms": round(rms, 4)}):
                if self._front_end == "logmel":
                    tensor = self._logmel(samples)
                else:
                    tensor = self._legacy_spectrogram(samples)
                tensor = tensor.to(self._device)
                with torch.no_grad():
                    probs = torch.softmax(self._model(tensor), dim=1)[0]
                    probs = probs.detach().cpu().numpy()

            idx = int(np.argmax(probs))
            label = self._classes[idx]
            confidence = float(probs[idx])
            cues = _empty_cues()
            for name, prob in zip(self._classes, probs):
                cue = CNN_CUES.get(name)
                if cue is not None:
                    cues[cue] = max(cues[cue], float(prob))

            if label in _QUIET:
                return AudioResult(0.0, label, confidence, cues=cues, rms=rms, model="cnn")

            # Threat score is weight * confidence, so a hesitant "maybe a
            # scream" contributes proportionally less than a confident one.
            score = THREAT_WEIGHTS.get(label, 0.0) * confidence
            return AudioResult(float(score), label, confidence, cues=cues, rms=rms,
                               model="cnn")
        except Exception as exc:
            log.debug("audio inference failed: %s", exc)
            return AudioResult(0.0, "normal_ambient", 0.0, rms=rms, model="error")

    def _tag(self, samples: np.ndarray, rms: float) -> AudioResult:
        try:
            with timed("audio", "tagger", {"rms": round(rms, 4)}):
                cues, top, top_score = self.tagger.tag(
                    samples, self.cfg.audio_sample_rate,
                    calm_talk=settings.audio.calm_talk_is_benign)
        except Exception as exc:
            log.debug("sound tagger failed: %s", exc)
            return AudioResult(0.0, "normal_ambient", 0.0, rms=rms, model="error")
        # The legacy scalar (``FeatureVector.audio``): the strongest threat cue,
        # weighted like the CNN classes it replaces.
        weights = {"weapon_sound": THREAT_WEIGHTS["gunshot"],
                   "aggression": THREAT_WEIGHTS["scream"],
                   "fire_sound": THREAT_WEIGHTS["fire"], "siren": THREAT_WEIGHTS["siren"]}
        score = max(weights[c] * cues.get(c, 0.0) for c in weights)
        return AudioResult(float(score), top, float(top_score), cues=cues, rms=rms,
                           model=self.tagger.name)

    def _notify_samples(self, samples: np.ndarray, end_at: float) -> None:
        for callback in list(self._sample_listeners):
            try:
                callback(samples, end_at)
            except Exception as exc:          # a listener never stops capture
                log.debug("audio sample listener failed: %s", exc)

    # ── public API ────────────────────────────────────────────────────────────
    def classify_window(self, samples: np.ndarray, rms: float | None = None, *,
                        start: float | None = None, end: float | None = None,
                        wall_end: float | None = None) -> AudioResult:
        """Classify one window of mono samples at ``audio_sample_rate`` (the
        live ring buffer, the clip engine's own 4 s window, a replayed
        soundtrack). Below the RMS floor the room is quiet and no inference
        runs (``model="quiet"``: heard, nothing to report). ``start``/``end``
        stamp the window on the pipeline clock (D15)."""
        samples = np.nan_to_num(np.asarray(samples, dtype=np.float32).reshape(-1))
        if rms is None:
            rms = float(np.sqrt(np.mean(samples ** 2))) if len(samples) else 0.0
        own = (self_output.overlapping(start, end)
               if start is not None and end is not None else None)
        if own is not None:
            # The device's own speaker, siren or buzzer: not the room.
            result = AudioResult(0.0, f"own {own}", 0.0, rms=rms, model="self")
        elif rms < self.cfg.audio_rms_floor:
            # Energy gate. Skipping inference on a quiet room is both a large
            # compute saving and the single most effective acoustic
            # false-alarm suppressor available.
            result = AudioResult(0.0, "normal_ambient", 1.0, cues=_empty_cues(), rms=rms,
                                 model="quiet")
        else:
            result = self._classify(samples, rms)
        result.start, result.end, result.wall_end = start, end, wall_end
        return result

    def add_sample_listener(self, callback) -> None:
        """``callback(samples, end_at)`` for every captured hop of microphone
        samples (``end_at`` = ``time.monotonic()`` of the last sample, the
        pipeline's clock). Called on the audio thread; must be cheap."""
        if callback not in self._sample_listeners:
            self._sample_listeners.append(callback)

    def remove_sample_listener(self, callback) -> None:
        try:
            self._sample_listeners.remove(callback)
        except ValueError:
            pass

    def result(self) -> AudioResult:
        """The latest result, whatever moment it describes."""
        with self._lock:
            return replace(self._result, cues=dict(self._result.cues or {}))

    def result_for(self, at: float, tolerance: float | None = None
                   ) -> tuple[AudioResult | None, dict]:
        """The latest result if its window describes ``at`` (D15), with the
        alignment record; ``(None, alignment)`` when audio is stale, untimed
        or absent - *unavailable*, not calm."""
        result = self.result()
        if result.end is None and result.model == "none":
            result = None                   # nothing classified yet
        align = alignment(result, at, tolerance)
        return (result if align["status"] == "aligned" else None), align

    def stop(self) -> None:
        self._stop.set()
        stream = getattr(self, "_stream", None)
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:
                log.debug("microphone stream close failed: %s", exc)
        if self._thread is not None:
            self._thread.join(timeout=2.0)


def block_end_monotonic(time_info, frames: int, rate: int, now: float | None = None) -> float:
    """The monotonic time of a block's last sample. PortAudio stamps the first
    sample's ADC time (``inputBufferAdcTime``) on its own clock, with that
    clock's "now" (``currentTime``); the difference is how long ago it was
    captured. Backends that report zeros fall back to the callback time."""
    now = time.monotonic() if now is None else now
    try:
        adc = float(getattr(time_info, "inputBufferAdcTime", 0.0) or 0.0)
        current = float(getattr(time_info, "currentTime", 0.0) or 0.0)
    except (TypeError, ValueError):
        return now
    age = current - adc
    if adc <= 0.0 or current <= 0.0 or not (0.0 <= age <= 5.0):
        return now
    return now - age + frames / float(rate)


def _empty_cues() -> dict[str, float]:
    return {cue: 0.0 for cue in CUE_NAMES}


class FileAudio:
    """An :class:`AudioEngine` stand-in for replay: a video's own soundtrack
    classified on the live engine's grid (a ``window`` second window every
    ``hop`` seconds, the newest one ending at or before the frame time), so a
    replayed frame sees the audio the live loop would have had. No microphone.
    """

    available = True

    def __init__(self, samples: np.ndarray, rate: int, classifier: AudioEngine) -> None:
        self.samples = np.asarray(samples, dtype=np.float32).reshape(-1)
        self.rate = int(rate)
        self.classifier = classifier
        self.window = settings.windows.audio_window_seconds
        self.hop = settings.windows.audio_hop_seconds
        self._cache: dict[int, AudioResult] = {}
        self._last: AudioResult | None = None

    @property
    def duration(self) -> float:
        return len(self.samples) / float(self.rate) if self.rate else 0.0

    @property
    def model_name(self) -> str:
        return getattr(self.classifier, "model_name", "none")

    def window_ending(self, end: float) -> AudioResult | None:
        """The result for the window ``[end - window, end]`` (None before the
        first full window or past the soundtrack's end)."""
        if end < self.window - 1e-9 or end > self.duration + 1e-9:
            return None
        key = int(round(end * 1000))
        cached = self._cache.get(key)
        if cached is None:
            chunk = self.samples_between(end - self.window, end)
            if self.rate != settings.windows.audio_sample_rate:
                from sentrix.perception.sound_tagger import resample
                chunk = resample(chunk, self.rate, settings.windows.audio_sample_rate)
            cached = self.classifier.classify_window(chunk, start=end - self.window, end=end)
            self._cache[key] = cached
        return cached

    def result_for(self, at: float, tolerance: float | None = None
                   ) -> tuple[AudioResult | None, dict]:
        end = math.floor(at / self.hop + 1e-9) * self.hop
        result = self.window_ending(end)
        if result is not None:
            self._last = result
        align = alignment(result, at, tolerance)
        return (result if align["status"] == "aligned" else None), align

    def result(self) -> AudioResult:
        return self._last or AudioResult()

    def classify_window(self, samples: np.ndarray, rms: float | None = None,
                        **stamp) -> AudioResult:
        return self.classifier.classify_window(samples, rms, **stamp)

    def samples_between(self, start: float, end: float) -> np.ndarray:
        """Raw samples of ``[start, end)`` at the track's rate."""
        lo = int(round(max(0.0, start) * self.rate))
        hi = int(round(max(0.0, end) * self.rate))
        return self.samples[lo:hi]

    def stop(self) -> None:
        return None


def decode_audio_file(path, rate: int | None = None) -> np.ndarray | None:
    """A media file's soundtrack as mono float32 at ``rate`` (default
    ``audio_sample_rate``) via the ``ffmpeg`` command, or None when there is
    no ffmpeg or no audio stream. Never opens a microphone."""
    import shutil
    import subprocess

    rate = int(rate or settings.windows.audio_sample_rate)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        log.warning("ffmpeg not found: replay audio unavailable")
        return None
    try:
        proc = subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-i", str(path), "-vn",
                               "-ac", "1", "-ar", str(rate), "-f", "f32le", "-"],
                              capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("cannot decode audio of %s: %s", path, exc)
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


__all__ = ["AudioEngine", "AudioResult", "CLASS_CUES", "CNN_CUES", "CUE_NAMES",
           "FileAudio", "THREAT_WEIGHTS", "alignment", "audio_votes", "block_end_monotonic",
           "decode_audio_file", "neutral_reason"]
