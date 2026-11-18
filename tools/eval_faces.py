"""Face-recognition metrics for the deployed InsightFace packs.

The match bar (``SENTRIX_FACE_THRESHOLD`` 0.45, a cosine *distance*: a face
matches at cosine >= 0.55) was set for ``buffalo_l`` on the laptop; the Pi
runs ``buffalo_s`` at detection size 320 (``deploy/pi5/pi.env``) and was never
checked (ROADMAP §2.4). No identity labels are needed for a first check -
the same construction as ``tools/reid_eval.py``:

* faces are attached to ByteTrack person tracks (the product's ``_owner``
  rule) on frames sampled at 2 Hz from the custom videos (train + val; the
  test video ``wa02`` is refused);
* **same person**: two faces of one track >= 2 s apart (a tracker id switch
  makes a few of these two people, so the rate is a lower bound);
* **different people**: faces of two tracks in one frame.

Per configuration: faces found per person box, the similarity distributions,
AUROC, the share of same-person and different-people pairs accepted at the
deployed bar, and the bar that accepts 0.1 % of different-people pairs.

Usage::

    python tools/eval_faces.py --tag 2026-11-18

Reads video files only; opens no camera.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CONFIGS = (("buffalo_l", 640), ("buffalo_s", 320), ("buffalo_s", 640))


def auroc(pos, neg) -> float | None:
    pos, neg = np.asarray(pos, float), np.asarray(neg, float)
    if pos.size == 0 or neg.size == 0:
        return None
    scores = np.concatenate([pos, neg])
    ranks = scores.argsort().argsort() + 1.0
    return round(float((ranks[:pos.size].sum() - pos.size * (pos.size + 1) / 2)
                       / (pos.size * neg.size)), 4)


def iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def app_for(pack: str, size: int):
    from insightface.app import FaceAnalysis
    app = FaceAnalysis(name=pack, allowed_modules=["detection", "recognition"],
                       providers=["CPUExecutionProvider"])
    app.prepare(ctx_id=-1, det_size=(size, size))
    return app


def sample(video, hz: float) -> list[tuple[float, np.ndarray, list]]:
    """(t, frame, [(track, box)]) at ``hz`` with the product's person detector."""
    import cv2
    from sentrix.perception.detector import Detector
    detector = Detector()
    detector.reset_tracks()
    cap = cv2.VideoCapture(str(video.path()))
    fps = cap.get(cv2.CAP_PROP_FPS) or video.fps
    # Track at 6 Hz (the pipeline's rate), keep a frame at ``hz``.
    track_step = max(1, round(fps / 6.0))
    step = max(track_step, round(fps / hz) // track_step * track_step)
    index, out = 0, []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if index % track_step == 0:
            persons = [(p.track_id, tuple(p.bbox)) for p in detector.detect(frame).persons
                       if p.track_id is not None]
            if index % step == 0 and persons:
                out.append((index / fps, frame, persons))
        index += 1
    cap.release()
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--tag", required=True)
    parser.add_argument("--hz", type=float, default=2.0)
    parser.add_argument("--min-gap", type=float, default=2.0)
    args = parser.parse_args(argv)

    from sentrix.config import settings
    from sentrix.evaluation.custom_dataset import videos_for
    from sentrix.perception.identity import _owner
    videos = [v for v in videos_for("train") + videos_for("val")]
    if any(v.video_id == "wa02" or v.split == "test" for v in videos):
        raise SystemExit("the test video is never used")
    bar = 1.0 - settings.identity.match_threshold
    apps = {f"{p}@{s}": app_for(p, s) for p, s in CONFIGS}
    faces = {k: [] for k in apps}             # (video, t, track, embedding)
    persons_seen = 0
    started = time.time()
    for video in videos:
        frames = sample(video, args.hz)
        persons_seen += sum(len(p) for _, _, p in frames)
        for key, app in apps.items():
            for t, frame, persons in frames:
                for face in app.get(frame):
                    track = _owner(tuple(face.bbox), tuple(persons))
                    if track is not None:
                        faces[key].append((video.video_id, round(t, 2), track,
                                           face.normed_embedding.astype(np.float32),
                                           next(b for tr, b in persons if tr == track)))
        print(f"{video.video_id:9s} {len(frames)} frames", flush=True)
    report = {"tag": args.tag, "bar_cosine": round(bar, 4), "videos": [v.video_id for v in videos],
              "person_boxes_sampled": persons_seen, "configs": {}}
    for key, rows in faces.items():
        by_track, by_frame = defaultdict(list), defaultdict(list)
        for i, (v, t, tr, _, _) in enumerate(rows):
            by_track[(v, tr)].append(i)
            by_frame[(v, t)].append(i)
        same = [float(rows[a][3] @ rows[b][3]) for m in by_track.values()
                for k, a in enumerate(m) for b in m[k + 1:]
                if abs(rows[a][1] - rows[b][1]) >= args.min_gap]
        diff = [float(rows[a][3] @ rows[b][3]) for m in by_frame.values()
                for k, a in enumerate(m) for b in m[k + 1:]
                if rows[a][2] != rows[b][2] and iou(rows[a][4], rows[b][4]) < 0.3]
        same, diff = np.array(same), np.array(diff)
        entry = {"faces_on_person_boxes": len(rows),
                 "faces_per_person_box": round(len(rows) / max(persons_seen, 1), 3),
                 "same_pairs": int(same.size), "different_pairs": int(diff.size),
                 "auroc": auroc(same, diff)}
        if same.size and diff.size:
            entry.update({
                "same_median": round(float(np.median(same)), 4),
                "different_median": round(float(np.median(diff)), 4),
                "same_accepted_at_bar": round(float((same >= bar).mean()), 4),
                "different_accepted_at_bar": round(float((diff >= bar).mean()), 5),
                "bar_for_0.1pct_different": round(float(np.quantile(diff, 0.999)), 4),
                "different_max": round(float(diff.max()), 4)})
        report["configs"][key] = entry
    report["seconds"] = round(time.time() - started, 1)
    out = ROOT / "training" / "runs" / "eval_faces" / args.tag
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
