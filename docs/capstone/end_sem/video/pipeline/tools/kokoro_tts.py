"""Speaks each line "path<TAB>text" of a UTF-8 file with Kokoro (offline, onnxruntime).

    kokoro-venv/python kokoro_tts.py jobs.tsv MODEL_DIR VOICE SPEED
"""
import sys
from pathlib import Path

import soundfile as sf
from kokoro_onnx import Kokoro

jobs, model_dir, voice, speed = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], float(sys.argv[4])
k = Kokoro(str(model_dir / "kokoro-v1.0.onnx"), str(model_dir / "voices-v1.0.bin"))
n = 0
for line in jobs.read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    path, text = line.split("\t", 1)
    samples, sr = k.create(text, voice=voice, speed=speed, lang="en-us" if voice.startswith("a") else "en-gb")
    sf.write(path, samples, sr, subtype="PCM_16")
    n += 1
print(f"done {n}")
