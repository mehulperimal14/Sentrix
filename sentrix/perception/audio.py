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

Microphone: any USB port, any time
----------------------------------
The engine opens the best input there is *now* (:func:`rank_inputs`): the one
named in ``SENTRIX_MIC_DEVICE``, else a USB input (one plugged in while
SENTRIX runs first), else another input plugged in while it runs, else the
system default; display audio, loopbacks, headsets (Bluetooth hands-free
included) and virtual microphones only when asked for, since none of them
hears the room. On Windows and macOS it opens at 16 kHz if the device takes
it (the OS converts well); on Linux, where ``plughw`` would accept any rate
and convert with ALSA's linear converter (no anti-alias filter: 8-24 kHz
would fold into the band), at the device's own rate. :class:`Resampler`
brings every block to 16 kHz before anything hears it, so listeners (the
clip buffer, the recorder) always get 16 kHz mono float32.

PortAudio lists devices only when it initialises, and re-initialising closes
every stream in the process and (inside sounddevice) sends stderr - the log -
to /dev/null while it runs. So the list is rebuilt only when the system's own
list changes (:func:`devices_fingerprint`: ``/proc/asound/cards`` on Linux,
the wave-in count on Windows, both read without opening anything), a few
times after such a change while no microphone has opened yet (a sound server
holds a new USB microphone for a few seconds, and PortAudio leaves out what it
cannot open), and every ``mic_rescan_seconds`` while no microphone is open;
never on every retry. A microphone counts as connected from its first block,
not from the open.
"""

from __future__ import annotations

import functools
import math
import os
import queue
import re
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from sentrix.config import settings
from sentrix.io import self_output
from sentrix.paths import model_path
from sentrix.utils.logging import get_logger, timed

log = get_logger("audio")

_LINUX = sys.platform.startswith("linux")
_WINDOWS = sys.platform == "win32"

#: PortAudio names ALSA devices "plughw" (below): any rate opens, converted by
#: ALSA. The stream is then opened at the device's own rate first
#: (:meth:`AudioEngine._open`), so the conversion to 16 kHz is ours, filtered.
_PLUGHW = _LINUX

if _LINUX:
    # A USB microphone is a raw ALSA device ("hw:2,0") that opens only at its
    # own channel count and formats (often mono, s16); "plughw" converts those.
    # PortAudio reads this when it initialises, which sounddevice does on import.
    os.environ.setdefault("PA_ALSA_PLUGHW", "1")

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
        # siren: never "heard, nothing there".
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


# ── microphone: which device, when to look again, 16 kHz from any rate ────────
#: Inputs never chosen automatically: display audio, and loopbacks or monitors
#: of what the machine itself plays.
_NOT_A_MICROPHONE = re.compile(r"hdmi|vc4|monitor|loopback|stereo mix|what u hear", re.I)
#: Inputs that may not hear the room, chosen automatically only as a last
#: resort (no other microphone at all): a headset's microphone hears its
#: wearer (opening a Bluetooth headset's also drops the wearer's audio to its
#: narrow-band telephone profile), and a virtual microphone carries whatever
#: a program feeds it. Not left out altogether: some cheap USB microphone
#: adapters call themselves a "headset", and on a Pi that may be the only one.
_NOT_THE_ROOM = re.compile(
    r"headset|hands-?free|bluetooth|bluez|airpods|virtual|vb-audio|voicemeeter|"
    r"cable output|nvidia broadcast|krisp|microsoft teams|zoomaudiodevice|"
    r"steam streaming", re.I)
#: The sound card's number in an ALSA device name: "... (hw:2,0)", "(plughw:2,0)".
_ALSA_CARD = re.compile(r"\((?:plug)?hw:(\d+),")
#: Where Linux describes its sound cards (``cards``, ``card<N>/usbid``).
_ASOUND = Path("/proc/asound")
#: What each reason adds to a candidate's score (:func:`rank_inputs`). USB
#: outweighs "plugged in": a USB microphone plugged in later beats one that
#: was there at start (300 to 200), but no other input ever beats a USB one.
_SCORES = {"asked for": 1000, "usb": 200, "plugged in": 100, "default": 10,
           "last resort": -1000}
#: The rates USB microphones take, tried after the engine's own (16 kHz, no
#: resampling) and before the device's default; on ``plughw`` (Linux) after
#: the device's default (its own rate) and before 16 kHz.
_RATES = (48000, 32000)
#: Seconds to wait for a closing stream before abandoning it, and for the
#: PortAudio lock before giving up on this look.
_CLOSE_TIMEOUT = 2.0
_LOCK_TIMEOUT = 2.0
#: Device-list rebuilds after the system's list changed, while no microphone
#: has opened yet: PortAudio leaves out a device it cannot open while listing,
#: and the sound server (WirePlumber) holds a new USB microphone for a moment.
_SETTLE_SCANS = 3
#: Every PortAudio call that lists, opens or closes devices runs under this
#: lock: re-initialising frees the streams another thread may be using.
_PA_LOCK = threading.RLock()


class PortAudioBusy(RuntimeError):
    """The PortAudio lock stayed taken: an old stream is still closing."""


@contextmanager
def _portaudio():
    """Hold the PortAudio lock, or raise :class:`PortAudioBusy` after
    ``_LOCK_TIMEOUT`` s rather than hang behind a stream stuck in its driver."""
    if not _PA_LOCK.acquire(timeout=_LOCK_TIMEOUT):
        raise PortAudioBusy("PortAudio is busy: an old microphone stream is still closing")
    try:
        yield
    finally:
        _PA_LOCK.release()


def devices_fingerprint() -> str | None:
    """A cheap summary of the system's sound devices that changes when one is
    plugged in or out, read without opening anything: ``/proc/asound/cards``
    on Linux, the wave-in device count on Windows. None elsewhere (or when it
    cannot be read): the device list is then rebuilt on a timer only."""
    if _LINUX:
        try:
            return (_ASOUND / "cards").read_text()
        except OSError:
            return None
    if _WINDOWS:
        try:
            import ctypes
            return str(ctypes.windll.winmm.waveInGetNumDevs())
        except Exception:
            return None
    return None


#: The fingerprint when PortAudio last listed its devices (sounddevice's
#: import, then every rebuild).
_listed_fingerprint = devices_fingerprint() if _SD else None


def is_usb(name: str) -> bool:
    """Is this input a USB device? On Linux its ALSA card has a ``usbid``
    file; Windows and macOS put "USB" in the name."""
    if _LINUX:
        match = _ALSA_CARD.search(name)
        if match and (_ASOUND / f"card{match.group(1)}" / "usbid").exists():
            return True
    return "usb" in name.lower()


@dataclass(frozen=True)
class MicCandidate:
    """One input device worth trying, with why it scored what it did."""

    index: int
    name: str
    hostapi: int
    channels: int
    default_rate: int
    score: int
    reasons: tuple[str, ...] = ()


def rank_inputs(devices, *, hostapi: int, default_input: int | None = None,
                wanted: str = "", known=None) -> list[MicCandidate]:
    """The input devices worth trying, best first.

    Scores add up: the device asked for (``SENTRIX_MIC_DEVICE``, an index or a
    name substring) 1000; USB 200; one missing from the first look (``known``
    names: plugged in while running) 100; PortAudio's default input 10. So a
    newer USB microphone beats an older one, and nothing but the one asked
    for beats a USB microphone. Left out: devices with no input channels,
    devices of another host API (Windows lists the same microphone again
    under DirectSound, WASAPI and WDM-KS) and inputs named like display audio
    or a loopback - unless asked for (by index on any host API, by name on the
    default one). Headsets (Bluetooth hands-free included) and virtual
    microphones come last ("last resort", -1000): used only when there is no
    other input.
    """
    wanted = (wanted or "").strip()
    ranked: list[MicCandidate] = []
    for position, device in enumerate(devices):
        index = int(device.get("index", position))
        name = str(device.get("name", f"device {index}"))
        channels = int(device.get("max_input_channels", 0) or 0)
        api = int(device.get("hostapi", hostapi))
        if channels <= 0:
            continue
        by_index = wanted.isdigit() and int(wanted) == index
        if api != hostapi and not by_index:
            continue
        asked = by_index or (bool(wanted) and not wanted.isdigit()
                             and wanted.lower() in name.lower())
        if not asked and _NOT_A_MICROPHONE.search(name):
            continue
        reasons = [reason for reason, applies in (
            ("asked for", asked),
            ("plugged in", known is not None and name not in known),
            ("usb", is_usb(name)),
            ("default", default_input is not None and index == default_input),
            ("last resort", not asked and bool(_NOT_THE_ROOM.search(name)))) if applies]
        ranked.append(MicCandidate(
            index=index, name=name, hostapi=api, channels=channels,
            default_rate=int(float(device.get("default_samplerate", 0) or 0)),
            score=sum(_SCORES[r] for r in reasons), reasons=tuple(reasons)))
    ranked.sort(key=lambda c: (-c.score, c.index))
    return ranked


class Resampler:
    """One stream's blocks from the device's rate to ``rate_out``, with the
    filter state carried from block to block so block edges leave no seams.

    Integer ratios (48 or 32 kHz to 16 kHz): an anti-aliasing FIR low-pass
    (``resample_poly``'s own design) whose state runs on across blocks, then
    every ``factor``-th sample, the phase carried too. Other ratios (44.1 kHz):
    ``resample_poly`` over each block with ``2 * pad`` samples of history in
    front, keeping only the output whose filter reach lies on real samples,
    ``pad`` samples late. Calling it returns ``(samples, delay_s)``: how long
    before the input block's last sample the last output sample lies.
    """

    def __init__(self, rate_in: int, rate_out: int) -> None:
        self.rate_in, self.rate_out = int(rate_in), int(rate_out)
        self.mode = "same"
        if self.rate_in == self.rate_out:
            return
        from scipy.signal import firwin
        if self.rate_in % self.rate_out == 0:
            self.mode = "decimate"
            self.factor = self.rate_in // self.rate_out
            self.taps = firwin(20 * self.factor + 1, 1.0 / self.factor, window=("kaiser", 5.0))
            self.state = np.zeros(len(self.taps) - 1)
            self.phase = 0                    # index in the next block of the next kept sample
        else:
            self.mode = "poly"
            g = math.gcd(self.rate_in, self.rate_out)
            self.up, self.down = self.rate_out // g, self.rate_in // g
            # resample_poly's filter reaches 10 * max(up, down) taps at the
            # upsampled rate; pad is a multiple of down so outputs stay whole.
            reach = math.ceil(10 * max(self.up, self.down) / self.up) + 1
            self.pad = self.down * math.ceil(reach / self.down)
            self.history = np.zeros(2 * self.pad)
            self.pending = np.zeros(0)        # input left over (not a multiple of down)

    def __call__(self, samples: np.ndarray) -> tuple[np.ndarray, float]:
        samples = np.asarray(samples, dtype=np.float32).reshape(-1)
        if self.mode == "same":
            return samples, 0.0
        if self.mode == "decimate":
            return self._decimate(samples)
        return self._poly(samples)

    def _decimate(self, samples: np.ndarray) -> tuple[np.ndarray, float]:
        from scipy.signal import lfilter
        filtered, self.state = lfilter(self.taps, 1.0, samples.astype(np.float64), zi=self.state)
        out = filtered[self.phase::self.factor]
        if not out.size:
            self.phase -= len(samples)
            return np.zeros(0, np.float32), 0.0
        last = self.phase + (len(out) - 1) * self.factor
        self.phase = (self.phase - len(samples)) % self.factor
        delay = (len(samples) - 1 - last) + (len(self.taps) - 1) / 2.0   # + group delay
        return out.astype(np.float32), delay / self.rate_in

    def _poly(self, samples: np.ndarray) -> tuple[np.ndarray, float]:
        from scipy.signal import resample_poly
        data = np.concatenate([self.pending, samples.astype(np.float64)])
        usable = len(data) // self.down * self.down
        self.pending = data[usable:]
        if usable == 0:
            return np.zeros(0, np.float32), 0.0
        segment = np.concatenate([self.history, data[:usable]])
        resampled = resample_poly(segment, self.up, self.down)
        lo = self.pad * self.up // self.down
        out = resampled[lo:lo + usable * self.up // self.down]
        self.history = segment[-2 * self.pad:]
        delay = self.pad - 1 + self.down / self.up + len(self.pending)
        return out.astype(np.float32), delay / self.rate_in


class _MicSession:
    """One open input stream: the device, the format it opened at and the
    resampler that brings its blocks to the engine's rate. The stream's
    callback is bound to its session, so a block always travels with the rate
    it was captured at, even while the engine switches devices."""

    def __init__(self, candidate: MicCandidate, rate: int, channels: int, hostapi: str,
                 target_rate: int) -> None:
        self.index, self.name, self.score = candidate.index, candidate.name, candidate.score
        self.hostapi = hostapi
        self.rate, self.channels = int(rate), int(channels)
        self.resampler = Resampler(self.rate, target_rate)
        self.stream = None
        self.opened_at = time.monotonic()
        #: Monotonic time of the newest block (written by PortAudio's thread).
        self.last_block_at: float | None = None
        #: ISO time of the first block: the microphone is connected from then.
        self.connected_since: str | None = None


class AudioEngine:
    """Background microphone capture plus windowed acoustic classification.

    ``capture=False`` builds the classifier only (no microphone thread): the
    replay tools use it to classify a video's own soundtrack (:class:`FileAudio`).
    Capture runs whatever the sound model (``off`` included: the recorder and
    the clip buffer still get the room's sound, nothing is classified).
    ``available`` is True only while an open microphone is delivering.
    """

    def __init__(self, capture: bool = True) -> None:
        self.cfg = settings.windows
        self.available = False
        self._result = AudioResult()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: (samples at the device's rate, end_at, wall, session) per block.
        self._blocks: queue.Queue = queue.Queue(maxsize=32)
        self._watcher: threading.Thread | None = None
        self.microphone: str | None = None
        # The microphone (see the module notes). ``_mic_lock`` guards the
        # session swap and the flags read by status(); it is never held while
        # PortAudio is called, and is taken after _PA_LOCK when both are.
        self._session: _MicSession | None = None
        self._mic_lock = threading.Lock()
        self._closing: list[threading.Thread] = []     # close helpers still running
        self._fingerprint: str | None = None          # at the last device-list rebuild
        self._scanned_at = 0.0
        self._rescan_due = False                      # a connected microphone was lost
        self._settle = 0
        self._better_until = 0.0                      # retry better inputs until then
        self._known: set[str] | None = None           # device names at the first look
        self._candidates: list[MicCandidate] = []
        self._warned: set[tuple[str, str]] = set()
        self._last_error: str | None = None
        self._absent_logged = False
        self._block_count = 0
        self._dropped = 0
        self._overflows = 0

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
        #: Classify captured windows; with the model ``off`` capture only feeds listeners.
        self._classifying = choice != "off"
        if choice in ("auto", "panns"):
            self._load_tagger()
        if choice == "cnn" or (choice == "auto" and self.tagger is None):
            self._load()
        if capture:
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
    def _start_capture(self, watch: bool = True) -> None:
        """One persistent ``InputStream`` on the best microphone there is (no
        gaps between hops, unlike a fresh ``sd.rec`` per hop); its callback
        only queues the block with its capture time, and a worker thread
        resamples and classifies. The watcher thread (``watch``) reconnects
        after an unplug and moves to a better microphone plugged in later, so
        a USB microphone works in any port, at any time, without a restart."""
        if not _SD:
            log.warning("sounddevice not installed — microphone disabled "
                        "(on a Pi: sudo apt install libportaudio2)")
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="sentrix-audio")
        self._thread.start()
        fingerprint = devices_fingerprint()
        self._fingerprint = fingerprint
        self._scanned_at = time.monotonic()
        # PortAudio listed its devices when sounddevice was imported; a USB
        # microphone still enumerating at boot may have arrived since.
        self._connect(refresh=fingerprint != _listed_fingerprint, fingerprint=fingerprint)
        if watch:
            self._watcher = threading.Thread(target=self._watch, daemon=True,
                                             name="sentrix-mic-watch")
            self._watcher.start()

    def _watch(self) -> None:
        """Look every ``mic_retry_seconds`` (see :meth:`_watch_once`)."""
        retry = max(0.5, settings.audio.mic_retry_seconds)
        while not self._stop.wait(retry):
            try:
                self._watch_once()
            except Exception as exc:                # the watcher outlives one bad look
                self._warn_once("microphone watcher", f"{type(exc).__name__}: {exc}")

    def _watch_once(self, now: float | None = None) -> None:
        """One look: is the open microphone still delivering (else it was
        unplugged: the stream stops, or sends nothing for
        ``mic_stall_seconds``), did the system's device list change (a better
        microphone may be there), is there a microphone to open."""
        now = time.monotonic() if now is None else now
        cfg = settings.audio
        stall = max(1.0, cfg.mic_stall_seconds)
        fingerprint = devices_fingerprint()
        changed = fingerprint != self._fingerprint
        session = self._session
        if session is not None:
            heard_at = session.last_block_at or session.opened_at
            if not getattr(session.stream, "active", True):
                self._lose("the stream stopped")
            elif now - heard_at > stall:
                self._lose(f"no sound for {stall:.0f} s")
            elif changed:
                # A better microphone may have been plugged in, but PortAudio
                # sees it only after re-initialising, which closes every stream.
                log.info("audio devices changed: listing them again (%s pauses)", session.name)
                if not self._close_stream(None):
                    return
            else:
                if self._better_until > now:
                    self._upgrade()
                return
        rescan_every = max(cfg.mic_retry_seconds, cfg.mic_rescan_seconds)
        refresh = (changed or self._rescan_due or self._settle > 0
                   or now - self._scanned_at >= rescan_every)
        self._connect(refresh=refresh, fingerprint=fingerprint, changed=changed, now=now)

    def _connect(self, *, refresh: bool, fingerprint: str | None, changed: bool = False,
                 now: float | None = None) -> bool:
        """Open the best microphone in PortAudio's list, rebuilding the list
        first when ``refresh``; True when one is open (it counts as connected
        from its first block)."""
        now = time.monotonic() if now is None else now
        session, candidates = None, []
        try:
            with _portaudio():
                if refresh and self._refresh(fingerprint):
                    # Every attempt counts, failed or not: a failing PortAudio
                    # is not re-initialised (and the log muted) every retry.
                    self._fingerprint, self._scanned_at = fingerprint, now
                    self._rescan_due = False
                    self._settle = _SETTLE_SCANS if changed else max(0, self._settle - 1)
                candidates = self._scan()
                for candidate in candidates:
                    if self._stop.is_set():
                        break
                    session = self._open(candidate)
                    if session is not None:
                        break
        except PortAudioBusy as exc:
            self._warn_once("PortAudio", str(exc))
            return False
        if session is None:
            self._report_absent(candidates)
            return False
        self._install(session, candidates)
        return True

    def _refresh(self, fingerprint: str | None) -> bool:
        """Rebuild PortAudio's device list (the caller holds the lock).
        ``_terminate`` runs only while PortAudio is initialised, and its
        failure never skips ``_initialize``: one failed start must not leave
        the microphone down for good. False when the rebuild must wait: a
        stream is still closing (re-initialising would free it under the
        closing thread)."""
        global _listed_fingerprint
        if self._stop.is_set() or self._pending_closes():
            return False
        if getattr(sd, "_initialized", 1) > 0:
            try:
                sd._terminate()
            except Exception as exc:
                log.debug("PortAudio terminate failed: %s", exc)
        try:
            sd._initialize()
        except Exception as exc:
            self._warn_once("PortAudio",
                            f"failed to initialise ({exc}); no microphone until it does")
        else:
            _listed_fingerprint = fingerprint
        return True

    def _pending_closes(self) -> bool:
        with self._mic_lock:
            self._closing = [t for t in self._closing if t.is_alive()]
            pending = bool(self._closing)
        if pending:
            self._warn_once("PortAudio", "an old microphone stream is still closing; "
                            "devices are listed again once it has")
        return pending

    def _scan(self) -> list[MicCandidate]:
        """The candidates in PortAudio's current list, best first (the caller
        holds the lock). The first list read is what "plugged in while
        running" is measured against."""
        try:
            devices = [dict(d) for d in sd.query_devices()]
        except Exception as exc:
            log.debug("PortAudio device list unavailable: %s", exc)
            devices = []
        try:
            hostapi = int(sd.default.hostapi)
        except Exception:
            hostapi = 0
        try:
            default_input = int(sd.query_hostapis(hostapi)["default_input_device"])
        except Exception:
            default_input = -1
        wanted = settings.audio.mic_device
        candidates = rank_inputs(devices, hostapi=hostapi, wanted=wanted, known=self._known,
                                 default_input=default_input if default_input >= 0 else None)
        if self._known is None:
            self._known = {str(d.get("name", "")) for d in devices}
        if wanted.strip() and not any("asked for" in c.reasons for c in candidates):
            self._warn_once("SENTRIX_MIC_DEVICE",
                            f"{wanted!r} matches no input now; choosing one automatically")
        self._candidates = candidates
        return candidates

    def _open(self, candidate: MicCandidate) -> _MicSession | None:
        """Open ``candidate`` at the first format it takes (the caller holds
        the lock): the engine's own rate, then the rates USB microphones use,
        then its default; mono, then all its channels (downmixed).

        On ``plughw`` (Linux) the device's default comes first and 16 kHz
        last: plughw accepts every rate, and for one the hardware lacks it
        converts with ALSA's linear converter (no low-pass filter when the
        speexrate plugin is missing, as on the demo Pi), folding 8-24 kHz
        into the band the sound model and the recordings hear. PortAudio
        probes an ALSA device's default with ALSA's resampling off, so it is
        a rate the hardware has; :class:`Resampler` filters it to 16 kHz."""
        target = int(self.cfg.audio_sample_rate)
        order = ((candidate.default_rate, *_RATES, target) if _PLUGHW
                 else (target, *_RATES, candidate.default_rate))
        rates = [r for r in dict.fromkeys(order) if r > 0]
        layouts = [1] + ([candidate.channels] if candidate.channels > 1 else [])
        try:
            hostapi = str(sd.query_hostapis(candidate.hostapi)["name"])
        except Exception:
            hostapi = str(candidate.hostapi)
        error: Exception | None = None
        for channels in layouts:
            for rate in rates:
                try:
                    # Opens and closes the device on ALSA: a busy or vanished
                    # device fails here, before a stream exists.
                    sd.check_input_settings(device=candidate.index, channels=channels,
                                            dtype="float32", samplerate=rate)
                except Exception as exc:
                    error = exc
                    continue
                session = _MicSession(candidate, rate, channels, hostapi, target)
                stream = None
                try:
                    stream = sd.InputStream(
                        device=candidate.index, samplerate=rate, channels=channels,
                        dtype="float32", blocksize=int(rate * self.cfg.audio_hop_seconds),
                        callback=functools.partial(self._on_block, session=session))
                    stream.start()
                except Exception as exc:
                    error = exc
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:
                            pass
                    continue
                session.stream = stream
                return session
        self._note_error(candidate.name, error)
        return None

    def _install(self, session: _MicSession, candidates: list[MicCandidate]) -> None:
        """Make ``session`` the microphone; connected once its first block
        arrives (:meth:`_announce`)."""
        with self._mic_lock:
            stopping = self._stop.is_set()
            if not stopping:
                self._session = session
                self.available = False
        if stopping:
            self._close_quietly(session.stream, session.name)
            return
        self._settle = 0
        cfg = settings.audio
        # A better input that failed to open (busy for a moment) is tried
        # again on each look for a while.
        better = any(c.score > session.score for c in candidates)
        self._better_until = (time.monotonic() + max(cfg.mic_retry_seconds, cfg.mic_rescan_seconds)
                              if better else 0.0)
        log.debug("microphone opened: %s at %d Hz, %d channel(s); waiting for sound",
                  session.name, session.rate, session.channels)

    def _upgrade(self) -> None:
        """Retry the inputs ranked above the open one, which failed to open
        when it was chosen (a sound server still holding a new USB
        microphone): switch as soon as one opens."""
        current = self._session
        if current is None:
            return
        session, candidates = None, []
        try:
            with _portaudio():
                candidates = self._scan()
                for candidate in candidates:
                    if candidate.score <= current.score:
                        break
                    session = self._open(candidate)
                    if session is not None:
                        break
        except PortAudioBusy:
            return
        if session is None:
            return
        log.info("switching microphone: %s -> %s", current.name, session.name)
        self._close_stream(None)
        self._install(session, candidates)

    def _announce(self, session: _MicSession) -> None:
        """The first block of a newly opened stream (worker thread): the
        microphone is connected now, not when it opened."""
        with self._mic_lock:
            if session is not self._session or session.connected_since is not None:
                return
            session.connected_since = datetime.now(timezone.utc).isoformat()
            self.available = True
            self.microphone = session.name
            self._last_error = None
        self._absent_logged = False
        self._ring = np.zeros(0, dtype=np.float32)    # no window across devices or a gap
        target = int(self.cfg.audio_sample_rate)
        log.info("microphone connected: %s at %d Hz%s", session.name, session.rate,
                 "" if session.rate == target else f" (resampled to {target} Hz)")

    def _lose(self, reason: str) -> None:
        """The open microphone stopped delivering: close it; if it had been
        connected, list the devices again on the next try (it was probably
        unplugged, and may come back under another number)."""
        session = self._session
        if session is not None and session.connected_since is not None:
            self._rescan_due = True
        self._close_stream(reason)

    def _close_stream(self, reason: str | None) -> bool:
        """Forget the open stream and close it off this thread. ``reason``
        (None: a planned close) is logged as a state change: "lost" for a
        connected microphone, once per device and reason for one that never
        delivered. False when the close hung and was abandoned."""
        with self._mic_lock:
            session, self._session = self._session, None
            self.available = False
            self.microphone = None
        if session is None:
            return True
        if reason is not None:
            if session.connected_since is not None:
                self._last_error = f"{session.name}: {reason}"
                log.info("microphone lost: %s (%s) - looking for one again", session.name, reason)
            else:
                self._note_error(session.name, f"opened but {reason}")
        return self._close_quietly(session.stream, session.name)

    def _close_quietly(self, stream, name: str) -> bool:
        """``abort()`` then ``close()`` on a helper thread that holds the
        PortAudio lock, waited for at most ``_CLOSE_TIMEOUT`` s: a stream on a
        device that vanished can hang in its driver, and the watcher must not
        hang with it. Never call this holding the lock (the helper needs it);
        no device list is rebuilt while the helper runs."""
        if stream is None:
            return True

        def close() -> None:
            with _PA_LOCK:
                for step in ("abort", "close"):
                    try:
                        getattr(stream, step)()
                    except Exception as exc:
                        log.debug("microphone stream %s failed: %s", step, exc)

        helper = threading.Thread(target=close, daemon=True, name="sentrix-mic-close")
        with self._mic_lock:
            self._closing.append(helper)
        helper.start()
        helper.join(_CLOSE_TIMEOUT)
        if helper.is_alive():
            self._warn_once(f"microphone {name!r}",
                            f"its stream did not close within {_CLOSE_TIMEOUT:.0f} s; abandoned")
            return False
        return True

    def _report_absent(self, candidates: list[MicCandidate]) -> None:
        if not candidates:
            self._last_error = "no input device found"
        if not self._absent_logged:
            self._absent_logged = True
            log.info("no microphone (%s): plug a USB microphone into any port; "
                     "looking again every %.0f s", self._last_error or "none opened",
                     settings.audio.mic_retry_seconds)

    def _note_error(self, device: str, error) -> None:
        text = (str(error) or type(error).__name__) if error is not None else "failed to open"
        self._last_error = f"{device}: {text}"
        self._warn_once(f"microphone {device!r}", text)

    def _warn_once(self, what: str, text: str) -> None:
        """A WARNING the first time ``what`` fails with ``text``: a retry
        every few seconds must not flood the journal."""
        key = (what, text)
        if key in self._warned:
            return
        if len(self._warned) >= 256:
            self._warned.clear()
        self._warned.add(key)
        log.warning("%s: %s", what, text)

    def _on_block(self, indata, frames, time_info, status, session=None) -> None:
        """PortAudio callback: downmix, copy, stamp, hand over. Never blocks.
        ``session`` (bound per stream) says which device and rate the block
        was captured at; resampling happens on the worker."""
        now = time.monotonic()
        self._block_count += 1
        if session is not None:
            session.last_block_at = now
        if status is not None and getattr(status, "input_overflow", False):
            self._overflows += 1
        rate = session.rate if session is not None else self.cfg.audio_sample_rate
        data = np.asarray(indata, dtype=np.float32)
        if data.ndim > 1 and data.shape[1] > 1:
            samples = data.mean(axis=1, dtype=np.float32)
        else:                                 # PortAudio reuses its buffer: copy
            samples = np.array(data.reshape(-1), dtype=np.float32, copy=True)
        item = (samples, block_end_monotonic(time_info, frames, rate, now), time.time(), session)
        try:
            self._blocks.put_nowait(item)
        except queue.Full:                    # the worker fell behind: drop the oldest
            self._dropped += 1
            try:
                self._blocks.get_nowait()
                self._blocks.put_nowait(item)
            except (queue.Empty, queue.Full):
                pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                samples, end_at, wall, session = self._blocks.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if session is not None:
                    current = self._session
                    if current is not None and session is not current:
                        continue              # one microphone at a time while switching
                    self._announce(session)
                    samples, delay = session.resampler(samples)
                    if not samples.size:
                        continue
                    end_at, wall = end_at - delay, wall - delay
                self.process_block(samples, end_at, wall)
            except Exception as exc:
                log.debug("audio block failed: %s", exc)

    def process_block(self, samples: np.ndarray, end_at: float,
                      wall: float | None = None) -> AudioResult | None:
        """One captured hop: listeners (the clip buffer), the ring buffer and,
        once a full window is held, a classification stamped with that
        window (D15). With the sound model ``off`` only the listeners hear it.
        Public so it can be driven without a microphone."""
        window = self.cfg.audio_window_seconds
        window_samples = int(self.cfg.audio_sample_rate * window)
        samples = np.nan_to_num(np.asarray(samples, dtype=np.float32).reshape(-1))
        self._notify_samples(samples, end_at)
        if not self._classifying:
            return None
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
            # The device's own siren: not the room.
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

    def status(self) -> dict:
        """The microphone for the dashboard, ``/api/health`` and the recorder:
        which device, the rate and channels its stream opened at (before
        resampling to 16 kHz), since when, how much has arrived and the last
        error. Cheap and safe from any thread. ``available`` is True only
        while the open microphone delivers; ``candidates`` are the inputs of
        the last look, best first."""
        with self._mic_lock:
            session = self._session
            available = bool(self.available)
            last_error = self._last_error
        heard_at = session.last_block_at if session is not None else None
        return {
            "available": available,
            "device": session.name if session else None,
            "index": session.index if session else None,
            "hostapi": session.hostapi if session else None,
            "rate": session.rate if session else None,
            "channels": session.channels if session else None,
            "connected_since": session.connected_since if session else None,
            "last_error": last_error,
            "blocks": self._block_count,
            "dropped": self._dropped,
            "overflows": self._overflows,
            "last_block_age_s": (None if heard_at is None
                                 else round(max(0.0, time.monotonic() - heard_at), 3)),
            "candidates": [c.name for c in self._candidates],
        }

    def stop(self) -> None:
        """Stop looking, close the microphone (never waiting on a hung driver
        for more than ``_CLOSE_TIMEOUT``) and stop the worker."""
        self._stop.set()
        watcher = getattr(self, "_watcher", None)
        if watcher is not None:
            watcher.join(timeout=2.0)
        self._close_stream(None)
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
           "FileAudio", "MicCandidate", "PortAudioBusy", "Resampler", "THREAT_WEIGHTS",
           "alignment", "audio_votes", "block_end_monotonic", "decode_audio_file",
           "devices_fingerprint", "is_usb", "neutral_reason", "rank_inputs"]
