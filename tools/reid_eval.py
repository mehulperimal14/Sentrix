"""How well body embeddings tell people apart, on the custom videos.

Runs the person detector with ByteTrack over the nine train + val videos
(``wa02``, the test video, is refused: CLAUDE.md, never select on it) and
embeds every tracked person crop with each body backend: one or more OSNet
checkpoints (``--weights``; default the shipped one, ``sentrix/perception/
osnet.py``) and the colour histogram ``ReIDEngine`` used before OSNet. With
several checkpoints the report names the one the D24 rule chooses: the most
same-person cases accepted at its own bar (the smallest 0.01 step that
accepts <= 0.05 % of different-people cases).

Pairs, with no identity labels needed:

* **same person** - one ByteTrack track at two moments >= ``--min-gap`` s
  apart (a tracker id switch makes a few of these different people, so the
  "same" rate is a lower bound);
* **different people** - two tracks in the same frame whose boxes do not
  overlap (IoU < 0.3), so they cannot be one person.

Reported per backend: the similarity distribution of both, AUROC, and the
threshold that accepts 1 % / 0.1 % of different-people pairs with the share of
same-person pairs it accepts there. Thresholds chosen from this report are
selected on train + val only.

Usage::

    python tools/reid_eval.py [--videos train,val] [--hz 6] [--tag osnet]

Writes ``training/runs/reid/<tag>/report.json`` and ``REPORT.md``. Reads video
files only; opens no camera or microphone and sends nothing.
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

from sentrix.evaluation.custom_dataset import videos_for  # noqa: E402
from sentrix.paths import MODELS_DIR  # noqa: E402

REFUSED = {"wa02"}


def iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def histogram(crop: np.ndarray) -> np.ndarray:
    """The colour-histogram embedding of ``ReIDEngine`` before OSNet."""
    import cv2
    hist = cv2.calcHist([crop], [0, 1, 2], None, [8, 8, 8], [0, 256, 0, 256, 0, 256])
    vec = hist.flatten().astype(np.float32)
    return vec / max(float(np.linalg.norm(vec)), 1e-8)


def collect(video, hz: float, min_height: int, embedders: dict) -> list[dict]:
    """Tracked person crops of one video, embedded by every backend: one key
    per OSNet checkpoint (``embedders``) and ``histogram``."""
    import cv2
    from sentrix.perception.detector import Detector
    from sentrix.perception.osnet import body_crop

    detector = Detector()
    detector.reset_tracks()
    cap = cv2.VideoCapture(str(video.path()))
    fps = cap.get(cv2.CAP_PROP_FPS) or video.fps
    step = max(1, round(fps / hz))
    rows, index = [], 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if index % step == 0:
            at = index / fps
            persons = [p for p in detector.detect(frame).persons
                       if p.track_id is not None and p.bbox[3] - p.bbox[1] >= min_height]
            crops = [body_crop(frame, p.bbox) for p in persons]
            keep = [(p, c) for p, c in zip(persons, crops) if c is not None]
            if keep:
                deep = {name: e.embed([c for _, c in keep]) for name, e in embedders.items()}
                for k, (person, crop) in enumerate(keep):
                    row = {"video": video.video_id, "t": round(at, 3),
                           "frame": index, "track": person.track_id,
                           "bbox": [int(v) for v in person.bbox],
                           "histogram": histogram(crop)}
                    row.update({name: vectors[k] for name, vectors in deep.items()})
                    rows.append(row)
        index += 1
    cap.release()
    return rows


def pairs(rows: list[dict], min_gap: float, per_track: int, rng) -> tuple[list, list]:
    same, different = [], []
    by_track, by_frame = defaultdict(list), defaultdict(list)
    for i, row in enumerate(rows):
        by_track[(row["video"], row["track"])].append(i)
        by_frame[(row["video"], row["frame"])].append(i)
    for members in by_track.values():
        candidates = [(a, b) for k, a in enumerate(members) for b in members[k + 1:]
                      if abs(rows[a]["t"] - rows[b]["t"]) >= min_gap]
        if len(candidates) > per_track:
            chosen = rng.choice(len(candidates), per_track, replace=False)
            candidates = [candidates[c] for c in chosen]
        same.extend(candidates)
    for members in by_frame.values():
        for k, a in enumerate(members):
            for b in members[k + 1:]:
                if rows[a]["track"] != rows[b]["track"] and \
                        iou(rows[a]["bbox"], rows[b]["bbox"]) < 0.3:
                    different.append((a, b))
    return same, different


def auroc(pos: np.ndarray, neg: np.ndarray) -> float:
    scores = np.concatenate([pos, neg])
    ranks = scores.argsort().argsort() + 1.0
    return float((ranks[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2)
                 / (len(pos) * len(neg)))


def summarise(rows, same, different, backend: str) -> dict:
    sim = lambda a, b: float(np.dot(rows[a][backend], rows[b][backend]))  # noqa: E731
    pos = np.array([sim(a, b) for a, b in same])
    neg = np.array([sim(a, b) for a, b in different])
    q = lambda x: {p: round(float(np.quantile(x, p / 100)), 4)  # noqa: E731
                   for p in (1, 5, 25, 50, 75, 95, 99)}
    out = {"same_pairs": int(pos.size), "different_pairs": int(neg.size),
           "same_quantiles": q(pos), "different_quantiles": q(neg),
           "auroc": round(auroc(pos, neg), 4), "operating_points": []}
    for far in (0.05, 0.01, 0.001):
        threshold = float(np.quantile(neg, 1 - far))
        out["operating_points"].append({
            "different_accepted": far, "threshold": round(threshold, 4),
            "same_accepted": round(float((pos > threshold).mean()), 4)})
    out["at_0.50_intrusionsentry"] = {
        "same_accepted": round(float((pos >= 0.5).mean()), 4),
        "different_accepted": round(float((neg >= 0.5).mean()), 4)}
    return out


def _spread(items: list, cap: int) -> list:
    if len(items) <= cap:
        return items
    return [items[i] for i in np.linspace(0, len(items) - 1, cap).round().astype(int)]


def rule_scores(rows: list[dict], key: str, looks_cap: int, check_gap: float,
                min_gap: float) -> tuple[np.ndarray, np.ndarray]:
    """The identity rule of ``TrackIdentity`` (D24) on tracks: a resident's
    look set (up to ``looks_cap`` crops of one track) against two probes of a
    track ``check_gap`` s apart; the rule passes when both probes' best match
    over the looks clears the bar, so each score is the smaller of the two.
    Same person: one track's first half as looks, its crops >= ``min_gap`` s
    after the last look as probes. Different people: two tracks seen in one
    frame without overlap."""
    tracks: dict = defaultdict(list)
    frames: dict = defaultdict(list)
    for i, row in enumerate(rows):
        tracks[(row["video"], row["track"])].append(i)
        frames[(row["video"], row["frame"])].append(i)
    for members in tracks.values():
        members.sort(key=lambda i: rows[i]["t"])
    apart = set()
    for members in frames.values():
        for k, a in enumerate(members):
            for b in members[k + 1:]:
                ka = (rows[a]["video"], rows[a]["track"])
                kb = (rows[b]["video"], rows[b]["track"])
                if ka != kb and iou(rows[a]["bbox"], rows[b]["bbox"]) < 0.3:
                    apart |= {(ka, kb), (kb, ka)}

    def two_checks(looks: list[int], probes: list[int]) -> list[float]:
        if not looks or len(probes) < 2:
            return []
        bank = np.stack([rows[i][key] for i in looks])
        best = {i: float((bank @ rows[i][key]).max()) for i in probes}
        out, j = [], 0
        for k, a in enumerate(probes):
            while j < len(probes) and rows[probes[j]]["t"] - rows[a]["t"] < check_gap:
                j += 1
            if j >= len(probes):
                break
            out.append(min(best[a], best[probes[j]]))
        return out

    same, different = [], []
    for members in tracks.values():
        span = rows[members[-1]]["t"] - rows[members[0]]["t"]
        if span < 2 * min_gap:
            continue
        half = rows[members[0]]["t"] + span / 2
        looks = [i for i in members if rows[i]["t"] <= half]
        last = rows[looks[-1]]["t"]
        same += two_checks(_spread(looks, looks_cap),
                           [i for i in members if rows[i]["t"] >= last + min_gap])
    for ka, kb in apart:
        different += two_checks(_spread(tracks[ka], looks_cap), tracks[kb])
    return np.array(same), np.array(different)


def rule_summary(same: np.ndarray, different: np.ndarray) -> dict:
    out = {"same_cases": int(same.size), "different_cases": int(different.size),
           "auroc": round(auroc(same, different), 4), "operating_points": []}
    for far in (0.01, 0.005, 0.001):
        threshold = float(np.quantile(different, 1 - far))
        out["operating_points"].append({
            "different_accepted": far, "threshold": round(threshold, 4),
            "same_accepted": round(float((same > threshold).mean()), 4)})
    for threshold in (0.80, 0.85, 0.88, 0.90):
        out[f"at_{threshold:.2f}"] = {
            "same_accepted": round(float((same >= threshold).mean()), 4),
            "different_accepted": round(float((different >= threshold).mean()), 5)}
    # The D24 bar, fixed before any comparison: the smallest 0.01 step at which
    # the rule accepts <= MAX_DIFFERENT of the different-people cases.
    for step in range(0, 101):
        bar = round(step / 100, 2)
        if (different >= bar).mean() <= MAX_DIFFERENT:
            out["bar"] = {"threshold": bar,
                          "same_accepted": round(float((same >= bar).mean()), 4),
                          "different_accepted": round(float((different >= bar).mean()), 5)}
            break
    return out


#: The identity bar's ceiling on accepted different-people cases (D24).
MAX_DIFFERENT = 0.0005


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--videos", default="train,val",
                        help="train,val (default), a split, or video ids; wa02 is refused")
    parser.add_argument("--weights", type=Path, action="append",
                        help="an OSNet checkpoint to compare (repeatable); "
                             "default: the shipped models/reid/ weights")
    parser.add_argument("--hz", type=float, default=6.0, help="sampling rate (pipeline: 6)")
    parser.add_argument("--min-height", type=int, default=80, help="smallest box, px")
    parser.add_argument("--min-gap", type=float, default=2.0, help="same-track gap, s")
    parser.add_argument("--per-track", type=int, default=200, help="same pairs per track")
    parser.add_argument("--looks", type=int, default=20,
                        help="looks kept per resident (settings.reid.identity_looks)")
    parser.add_argument("--check-gap", type=float, default=1.0,
                        help="time between two face checks, s (recheck_seconds)")
    parser.add_argument("--tag", default="osnet")
    args = parser.parse_args(argv)

    selected = []
    for part in args.videos.split(","):
        selected += videos_for(part.strip())
    refused = [v.video_id for v in selected if v.video_id in REFUSED or v.split == "test"]
    if refused:
        print(f"refused: {refused} (test video; never select on it)")
        return 2

    from sentrix.config import settings
    from sentrix.perception.osnet import WEIGHTS_NAME, BodyEmbedder
    weights = args.weights or [MODELS_DIR / "reid" / WEIGHTS_NAME]
    embedders = {}
    for path in weights:
        embedder = BodyEmbedder(path, threads=settings.reid.threads)
        if not embedder.available:
            print(f"OSNet weights missing or unreadable: {path}")
            return 1
        embedders[path.stem] = embedder

    started = time.time()
    rows: list[dict] = []
    per_video = {}
    for video in selected:
        got = collect(video, args.hz, args.min_height, embedders)
        per_video[video.video_id] = {"crops": len(got),
                                     "tracks": len({r["track"] for r in got})}
        print(f"{video.video_id:6s} {len(got):5d} crops, {per_video[video.video_id]['tracks']} tracks")
        rows += got
    rng = np.random.default_rng(0)
    same, different = pairs(rows, args.min_gap, args.per_track, rng)
    rules = {}
    for name in embedders:
        rule_same, rule_different = rule_scores(rows, name, args.looks, args.check_gap,
                                                args.min_gap)
        rules[name] = rule_summary(rule_same, rule_different)
    # Fixed before the comparison: the most same-person cases accepted at
    # each checkpoint's own bar; then pairwise AUROC.
    chosen = max(rules, key=lambda n: (rules[n].get("bar", {}).get("same_accepted", 0.0),
                                       rules[n]["auroc"]))
    report = {"videos": [v.video_id for v in selected], "hz": args.hz,
              "min_height": args.min_height, "min_gap_s": args.min_gap,
              "per_video": per_video, "seconds": round(time.time() - started, 1),
              "weights": {name: {"file": e.weights, "architecture": e.name}
                          for name, e in embedders.items()},
              "backends": {b: summarise(rows, same, different, b)
                           for b in [*embedders, "histogram"]},
              "identity_rule": {"looks": args.looks, "check_gap_s": args.check_gap,
                                "max_different": MAX_DIFFERENT, "by_weights": rules},
              "chosen": chosen}
    out_dir = ROOT / "training" / "runs" / "reid" / args.tag
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    lines = [f"# Body re-id on the custom videos ({args.tag})", "",
             f"Videos: {', '.join(report['videos'])} (train + val; wa02 never used). "
             f"Sampled at {args.hz:g} Hz, person boxes >= {args.min_height} px, "
             f"same-person pairs >= {args.min_gap:g} s apart on one track.", "",
             "| Backend | same / different pairs | AUROC | median same / different "
             "| thr @ 1 % different | same accepted | thr @ 0.1 % | same accepted |",
             "|---|---|---|---|---|---|---|---|"]
    for name, b in report["backends"].items():
        p1, p01 = b["operating_points"][1], b["operating_points"][2]
        lines.append(f"| {name} | {b['same_pairs']} / {b['different_pairs']} | {b['auroc']} | "
                     f"{b['same_quantiles'][50]} / {b['different_quantiles'][50]} | "
                     f"{p1['threshold']} | {p1['same_accepted']} | "
                     f"{p01['threshold']} | {p01['same_accepted']} |")
    lines += [""]
    for name in embedders:
        osn = report["backends"][name]["at_0.50_intrusionsentry"]
        lines.append(f"IntrusionSentry's threshold 0.50 on {name}: accepts "
                     f"{osn['same_accepted']:.1%} of same-person and "
                     f"{osn['different_accepted']:.1%} of different-people pairs.")
    lines += ["", f"## Identity rule (body looks, TrackIdentity): {args.looks} looks, two checks "
              f"{args.check_gap:g} s apart", "",
              f"Bar: the smallest 0.01 step accepting <= {MAX_DIFFERENT:.2%} of "
              "different-people cases; chosen: the most same-person cases at its "
              "bar (fixed before the comparison).", "",
              "| weights | cases same / different | AUROC | bar | same accepted "
              "| different accepted | 0.85: same / different |",
              "|---|---|---|---|---|---|---|"]
    for name, rule in rules.items():
        bar = rule.get("bar", {"threshold": None, "same_accepted": 0, "different_accepted": 0})
        mark = " **(chosen)**" if name == chosen else ""
        lines.append(f"| {name}{mark} | {rule['same_cases']} / {rule['different_cases']} | "
                     f"{rule['auroc']} | {bar['threshold']} | {bar['same_accepted']:.1%} | "
                     f"{bar['different_accepted']:.3%} | "
                     f"{rule['at_0.85']['same_accepted']:.1%} / "
                     f"{rule['at_0.85']['different_accepted']:.3%} |")
    (out_dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
