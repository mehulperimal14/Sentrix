"""OSNet-AIN cost on the Pi as the product runs it (2-thread onnxruntime session).

Run on the Pi from stdin with the service stopped. Person boxes from the 320 px
person head on vid07 (four people); crops as ReIDEngine makes them. Nothing is
written.
"""
import os
import statistics
import sys
import time

sys.path.insert(0, os.getcwd())

import cv2

from sentrix.config import settings
from sentrix.evaluation.custom_dataset import videos_for
from sentrix.paths import MODELS_DIR
from sentrix.perception.detector import _PERSON_WEIGHTS, _resolve
from sentrix.perception.osnet import WEIGHTS_NAME, BodyEmbedder, body_crop
from sentrix.runtime import build_head

person = build_head(_resolve(_PERSON_WEIGHTS), 320, "onnx", 4)
cap = cv2.VideoCapture(str(videos_for("vid07")[0].path()))
crops, frames_read = [], 0
while len(crops) < 120 and frames_read < 900:
    ok, frame = cap.read()
    if not ok:
        break
    frames_read += 1
    if frames_read % 10:
        continue
    frame = cv2.resize(frame, (1280, 720))
    for box in person.predict(frame, conf=0.4):
        if box.label == "person":
            crop = body_crop(frame, box.bbox)
            if crop is not None:
                crops.append(crop)
cap.release()
embedder = BodyEmbedder(MODELS_DIR / "reid" / WEIGHTS_NAME, threads=settings.reid.threads)
print(f"crops {len(crops)}; embedder {embedder.name}, threads {settings.reid.threads}", flush=True)
embedder.embed(crops[:4])
for batch in (1, 2, 4):
    times = []
    for i in range(0, min(len(crops), 60) - batch + 1, batch):
        start = time.perf_counter()
        embedder.embed(crops[i:i + batch])
        times.append((time.perf_counter() - start) * 1000)
    p50 = statistics.median(times)
    print(f"batch {batch}: p50 {p50:.1f} ms a call, {p50 / batch:.1f} ms a crop; "
          f"p95 {sorted(times)[int(0.95 * (len(times) - 1))]:.1f} ms ({len(times)} calls)", flush=True)
