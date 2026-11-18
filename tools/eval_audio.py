"""Sound-model metrics: cue AUROC and the deployed cue thresholds.

Runs the product's own path, ``AudioEngine(capture=False).classify_window`` on
16 kHz mono windows (no microphone is opened), for whichever sound model
``SENTRIX_AUDIO_MODEL`` selects (``auto`` = PANNs CNN10, ``cnn`` = the 4-class
fallback):

* **AudioSet clips** (``audio_data/audioset_clips/{fire,scream,siren,
  speech_normal}``): 4 s windows, 2 s hop; a clip's cue is its highest window.
  Per cue, AUROC of its target class against the other classes, and recall /
  false-alarm rate at ``settings.audio.cue_thresholds``: aggression (scream
  vs the rest and vs speech), siren (siren), fire_sound (fire), calm_talk
  (speech vs scream). **In-sample for both models**: PANNs was trained on
  AudioSet and the 4-class CNN on these very clips - read these as an upper
  bound, never as room performance.
* **Custom video soundtracks** (the ``segments.csv`` videos with audio):
  4 s windows, 1 s hop, labelled by the segment at the window's centre; per
  video, each cue's highest value in threat segments and in normal ones.

Usage::

    python tools/eval_audio.py --tag panns
    SENTRIX_AUDIO_MODEL=cnn python tools/eval_audio.py --tag cnn

Writes ``training/runs/eval_audio/<tag>/report.json``.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import wave
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RATE = 16000
CUES = ("aggression", "weapon_sound", "fire_sound", "benign_social", "calm_talk",
        "background", "siren")


def auroc(pos, neg) -> float | None:
    pos, neg = np.asarray(pos, float), np.asarray(neg, float)
    if pos.size == 0 or neg.size == 0:
        return None
    scores = np.concatenate([pos, neg])
    ranks = scores.argsort().argsort() + 1.0
    for v in np.unique(scores):
        ranks[scores == v] = ranks[scores == v].mean()
    return round(float((ranks[:pos.size].sum() - pos.size * (pos.size + 1) / 2)
                       / (pos.size * neg.size)), 4)


def read_wav(path: Path) -> np.ndarray | None:
    from scipy.signal import resample_poly
    try:
        with wave.open(str(path)) as w:
            raw = w.readframes(w.getnframes())
            width, channels, rate = w.getsampwidth(), w.getnchannels(), w.getframerate()
    except Exception:
        return None
    dtype = {1: np.uint8, 2: np.int16, 4: np.int32}.get(width)
    if dtype is None:
        return None
    x = np.frombuffer(raw, dtype).astype(np.float32)
    x = (x - 128.0) / 128.0 if width == 1 else x / float(np.iinfo(dtype).max)
    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1)
    if rate != RATE:
        from math import gcd
        g = gcd(rate, RATE)
        x = resample_poly(x, RATE // g, rate // g).astype(np.float32)
    return x


def decode(path: Path) -> np.ndarray | None:
    """A video's soundtrack as 16 kHz mono float (ffmpeg)."""
    try:
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar",
                              str(RATE), "-f", "f32le", "-"], capture_output=True,
                             timeout=120).stdout
    except Exception:
        return None
    return np.frombuffer(raw, np.float32) if raw else None


def windows(x: np.ndarray, seconds: float, hop: float) -> list[tuple[float, np.ndarray]]:
    n, h = int(seconds * RATE), int(hop * RATE)
    if len(x) < n:
        return [(len(x) / RATE / 2, np.pad(x, (0, n - len(x))))]
    return [((i + n / 2) / RATE, x[i:i + n]) for i in range(0, len(x) - n + 1, h)]


def cues_of(engine, w: np.ndarray) -> dict:
    result = engine.classify_window(w)
    cues = dict(result.cues or {})
    return {c: float(cues.get(c, 0.0)) for c in CUES} | {"_label": result.label,
                                                           "_model": result.model}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data", type=Path, default=Path(os.getenv("SENTRIX_DATA_DIR",
                                                                     "G:/Capstone/data")))
    parser.add_argument("--tag", required=True)
    args = parser.parse_args(argv)

    from sentrix.config import settings
    from sentrix.evaluation.custom_dataset import METADATA_DIR, load_videos
    from sentrix.perception.audio import AudioEngine
    engine = AudioEngine(capture=False)
    thresholds = dict(settings.audio.cue_thresholds)
    report = {"tag": args.tag, "model": engine.model_name, "cue_thresholds": thresholds,
              "audioset_in_sample": True}

    clips = defaultdict(list)
    root = args.data / "audio_data" / "audioset_clips"
    for cls_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for f in sorted(cls_dir.glob("*.wav")):
            x = read_wav(f)
            if x is None or len(x) < RATE // 2:
                continue
            per = [cues_of(engine, w) for _, w in windows(x, 4.0, 2.0)]
            clips[cls_dir.name].append({c: max(p[c] for p in per) for c in CUES})
    targets = {"aggression": "scream", "siren": "siren", "fire_sound": "fire",
               "calm_talk": "speech_normal"}
    report["audioset"] = {"clips": {c: len(v) for c, v in clips.items()}, "cues": {}}
    for cue, target in targets.items():
        pos = [c[cue] for c in clips[target]]
        neg = [c[cue] for k, v in clips.items() if k != target for c in v]
        thr = thresholds.get(cue, 0.5)
        entry = {"target": target, "auroc_vs_rest": auroc(pos, neg),
                 "recall_at_threshold": round(float(np.mean(np.array(pos) >= thr)), 4) if pos else None,
                 "false_alarm_at_threshold": round(float(np.mean(np.array(neg) >= thr)), 4) if neg else None}
        if neg and pos:
            bar = float(np.quantile(neg, 0.99))
            entry["bar_at_1pct_false_alarm"] = round(bar, 4)
            entry["recall_at_that_bar"] = round(float(np.mean(np.array(pos) > bar)), 4)
        if cue == "aggression":
            entry["auroc_vs_speech"] = auroc(pos, [c[cue] for c in clips["speech_normal"]])
        if cue == "calm_talk":
            entry["auroc_vs_scream"] = auroc(pos, [c[cue] for c in clips["scream"]])
        report["audioset"]["cues"][cue] = entry

    with open(METADATA_DIR / "segments.csv", newline="", encoding="utf-8") as fh:
        segments = defaultdict(list)
        for r in csv.DictReader(fh):
            segments[r["video_id"]].append(r)
    report["videos"] = {}
    for vid, info in load_videos().items():
        x = decode(info.path())
        if x is None or np.sqrt(np.mean(x ** 2)) < 1e-4:
            report["videos"][vid] = {"audio": "none"}
            continue
        best = defaultdict(lambda: defaultdict(float))
        for centre, w in windows(x, 4.0, 1.0):
            seg = next((s for s in segments[vid] if float(s["start_time"]) <= centre
                        < float(s["end_time"])), None)
            if seg is None or float(seg["confidence"]) < 0.7:
                continue
            group = "threat" if seg["label"] != "normal" else "normal"
            c = cues_of(engine, w)
            for cue in CUES:
                best[f"{seg['label']}" if group == "threat" else "normal"][cue] = max(
                    best[seg["label"] if group == "threat" else "normal"][cue], c[cue])
        report["videos"][vid] = {k: {c: round(v, 3) for c, v in d.items()}
                                 for k, d in best.items()}
    out = ROOT / "training" / "runs" / "eval_audio" / args.tag
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
