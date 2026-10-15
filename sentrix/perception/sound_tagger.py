"""General sound tagging: a pretrained AudioSet model mapped to SENTRIX cues.

Why a general tagger
--------------------
The 4-class audio CNN (``models/v3_real``) was trained on 375 clips and knows
``speech_normal``, ``scream``, ``siren``... but nothing about laughter, music
or a television, so it cannot tell friendly banter from a fight - the question
the fusion asks of audio (CLAUDE.md D15). A model trained on AudioSet (527
classes, 2 M clips) can, out of the box.

The model is **PANNs CNN10** (Kong et al., 2020, "PANNs: Large-Scale Pretrained
Audio Neural Networks for Audio Pattern Recognition"; code MIT, weights from
Zenodo record 3987831): 5.2 M parameters, 25 MB, AudioSet mAP 0.380, 32 kHz
log-mel front end. It was chosen over AST (346 MB, needs ``transformers``, a
10 s input) and CNN14 (327 MB) because it installs with nothing but torch,
runs a 4 s window in tens of milliseconds on a laptop CPU and fits on a Pi.

The network below reproduces the published ``Cnn10`` module with the same
parameter names, so the checkpoint loads unchanged; the STFT and mel filter
bank are part of the checkpoint (``torchlibrosa`` stores them as tensors), so
no ``torchlibrosa`` or ``librosa`` is needed either. Weights:
``models/audio/panns_cnn10_audioset.pth`` (``tools/fetch_models.py`` downloads
and checks them if missing).

Cues
----
:data:`CUE_LABELS` maps AudioSet display names to five SENTRIX cues; a cue's
value is the highest sigmoid score among its labels:

* ``aggression`` - screaming, shouting, yelling, crying, slaps, breaking glass;
* ``weapon_sound`` - gunshots, machine guns, explosions;
* ``fire_sound`` - smoke and fire alarms, the crackle of fire;
* ``benign_social`` - laughter, music, television, radio, and calm talk
  (``Speech``/``Conversation`` scaled by ``1 - aggression``, so a shouted
  argument is not "calm talk");
* ``siren`` - emergency vehicles.

The mapping is a published rule, not a fitted model; its thresholds
(``settings.audio.cue_thresholds``) are untested on the deployment room
until the M6 recordings exist (ROADMAP.md).
"""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

from sentrix.utils.logging import get_logger

log = get_logger("sound")

#: The model's native rate; input at any other rate is resampled.
SAMPLE_RATE = 32000
WEIGHTS_NAME = "panns_cnn10_audioset.pth"
LABELS_NAME = "audioset_labels.csv"
#: SHA-256 of the Zenodo file ``Cnn10_mAP=0.380.pth`` (record 3987831).
WEIGHTS_SHA256 = "5240bdc47444e331bbeb54fd741d2b3f933a4526e84b0c17bf3593df94b13962"
WEIGHTS_URL = "https://zenodo.org/records/3987831/files/Cnn10_mAP=0.380.pth?download=1"

CUES = ("aggression", "weapon_sound", "fire_sound", "benign_social", "siren")

#: AudioSet display names behind each cue (exact names from the label file).
CUE_LABELS: dict[str, tuple[str, ...]] = {
    "aggression": ("Screaming", "Shout", "Yell", "Bellow", "Battle cry",
                   "Children shouting", "Crying, sobbing", "Wail, moan",
                   "Slap, smack", "Shatter", "Breaking", "Smash, crash"),
    "weapon_sound": ("Gunshot, gunfire", "Machine gun", "Fusillade", "Artillery fire",
                     "Cap gun", "Explosion", "Boom"),
    "fire_sound": ("Smoke detector, smoke alarm", "Fire alarm", "Fire", "Crackle"),
    "benign_social": ("Laughter", "Baby laughter", "Giggle", "Snicker", "Belly laugh",
                      "Chuckle, chortle", "Music", "Television", "Radio"),
    "siren": ("Siren", "Civil defense siren", "Emergency vehicle", "Police car (siren)",
              "Ambulance (siren)", "Fire engine, fire truck (siren)"),
}
#: Talk counts as benign only in proportion to how un-aggressive it sounds.
CALM_TALK_LABELS = ("Speech", "Conversation", "Narration, monologue")


def load_labels(path: Path) -> list[str]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [row["display_name"] for row in sorted(rows, key=lambda r: int(r["index"]))]


def _build_cnn10(classes: int = 527):
    """PANNs ``Cnn10`` with the published parameter names (STFT as convs)."""
    import torch
    import torch.nn as nn
    import torch.nn.functional as F

    class Stft(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.conv_real = nn.Conv1d(1, 513, 1024, stride=320, bias=False)
            self.conv_imag = nn.Conv1d(1, 513, 1024, stride=320, bias=False)

    class Spectrogram(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.stft = Stft()

        def forward(self, x):                      # (B, L) -> (B, 1, T, 513) power
            x = F.pad(x[:, None, :], (512, 512), mode="reflect")
            real, imag = self.stft.conv_real(x), self.stft.conv_imag(x)
            return (real ** 2 + imag ** 2).transpose(1, 2)[:, None, :, :]

    class LogMel(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.melW = nn.Parameter(torch.zeros(513, 64), requires_grad=False)

        def forward(self, x):
            mel = torch.matmul(x, self.melW)
            return 10.0 * torch.log10(torch.clamp(mel, min=1e-10))

    class ConvBlock(nn.Module):
        def __init__(self, cin: int, cout: int) -> None:
            super().__init__()
            self.conv1 = nn.Conv2d(cin, cout, 3, padding=1, bias=False)
            self.conv2 = nn.Conv2d(cout, cout, 3, padding=1, bias=False)
            self.bn1 = nn.BatchNorm2d(cout)
            self.bn2 = nn.BatchNorm2d(cout)

        def forward(self, x):
            x = F.relu(self.bn1(self.conv1(x)))
            x = F.relu(self.bn2(self.conv2(x)))
            return F.avg_pool2d(x, 2)

    class Cnn10(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.spectrogram_extractor = Spectrogram()
            self.logmel_extractor = LogMel()
            self.bn0 = nn.BatchNorm2d(64)
            self.conv_block1 = ConvBlock(1, 64)
            self.conv_block2 = ConvBlock(64, 128)
            self.conv_block3 = ConvBlock(128, 256)
            self.conv_block4 = ConvBlock(256, 512)
            self.fc1 = nn.Linear(512, 512)
            self.fc_audioset = nn.Linear(512, classes)

        def forward(self, wave):                    # (B, L) at 32 kHz -> (B, 527)
            x = self.logmel_extractor(self.spectrogram_extractor(wave))
            x = self.bn0(x.transpose(1, 3)).transpose(1, 3)
            for block in (self.conv_block1, self.conv_block2, self.conv_block3,
                          self.conv_block4):
                x = block(x)
            x = torch.mean(x, dim=3)
            x = torch.max(x, dim=2)[0] + torch.mean(x, dim=2)
            x = F.relu(self.fc1(x))
            return torch.sigmoid(self.fc_audioset(x))

    return Cnn10()


def resample(samples: np.ndarray, rate: int, target: int = SAMPLE_RATE) -> np.ndarray:
    if rate == target:
        return samples.astype(np.float32, copy=False)
    from math import gcd

    from scipy.signal import resample_poly
    g = gcd(int(rate), int(target))
    return resample_poly(samples, target // g, int(rate) // g).astype(np.float32)


def cues_from_scores(scores: np.ndarray, labels: list[str], *,
                     calm_talk: bool = True) -> dict[str, float]:
    """The five cue values (and the strongest label of each) from 527 scores."""
    index = {name: i for i, name in enumerate(labels)}
    out: dict[str, float] = {}
    for cue, names in CUE_LABELS.items():
        ids = [index[n] for n in names if n in index]
        out[cue] = float(max((scores[i] for i in ids), default=0.0))
    if calm_talk:
        talk = max((float(scores[index[n]]) for n in CALM_TALK_LABELS if n in index),
                   default=0.0)
        out["benign_social"] = max(out["benign_social"], talk * (1.0 - out["aggression"]))
    return out


class SoundTagger:
    """PANNs CNN10 on CPU (or CUDA if torch has it). ``available`` is False
    when the weights or labels are missing; nothing is ever downloaded here."""

    name = "panns_cnn10"

    def __init__(self, weights: Path, labels: Path, *, threads: int | None = None) -> None:
        self.available = False
        self.labels: list[str] = []
        self._model = None
        self._torch = None
        if not weights.exists() or not labels.exists():
            log.info("sound tagger weights not found (%s); run tools/fetch_models.py",
                     weights)
            return
        try:
            import torch
            if threads:
                torch.set_num_threads(max(1, int(threads)))
            checkpoint = torch.load(str(weights), map_location="cpu", weights_only=True)
            state = checkpoint.get("model", checkpoint)
            net = _build_cnn10(int(state["fc_audioset.weight"].shape[0]))
            net.load_state_dict(state)
            net.eval()
            self.labels = load_labels(labels)
            if len(self.labels) != int(state["fc_audioset.weight"].shape[0]):
                log.error("sound tagger: %d labels for %d outputs", len(self.labels),
                          int(state["fc_audioset.weight"].shape[0]))
                return
            self._model, self._torch = net, torch
            self.available = True
            log.info("sound tagger loaded: PANNs CNN10 (%d AudioSet classes)", len(self.labels))
        except Exception as exc:
            log.error("sound tagger failed to load: %s", exc)

    def scores(self, samples: np.ndarray, rate: int) -> np.ndarray:
        """527 sigmoid scores for one window of mono samples."""
        torch = self._torch
        wave = resample(np.asarray(samples, dtype=np.float32).reshape(-1), rate)
        with torch.no_grad():
            out = self._model(torch.from_numpy(wave)[None, :])
        return out[0].numpy()

    def tag(self, samples: np.ndarray, rate: int, *,
            calm_talk: bool = True) -> tuple[dict[str, float], str, float]:
        """(cues, top AudioSet label, its score) for one window."""
        scores = self.scores(samples, rate)
        top = int(np.argmax(scores))
        return (cues_from_scores(scores, self.labels, calm_talk=calm_talk),
                self.labels[top], float(scores[top]))


__all__ = ["CALM_TALK_LABELS", "CUES", "CUE_LABELS", "SAMPLE_RATE", "SoundTagger",
           "WEIGHTS_NAME", "WEIGHTS_SHA256", "WEIGHTS_URL", "LABELS_NAME",
           "cues_from_scores", "load_labels", "resample"]
