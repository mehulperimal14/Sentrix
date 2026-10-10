"""The SENTRIX custom frame dataset: classes, splits and a reviewed timeline.

765 hand-reviewed frames from 10 source videos, five single-label classes::

    0 normal   1 fire   2 weapon   3 assault   4 robbery

The images (80 MB) live outside the repository. Their ground truth does not:
``training/datasets/sentrix_custom/`` vendors ``labels.csv``, ``frames.csv``
and the rest of the metadata, so every metric in this repository is pinned to
the commit that produced it even if the dataset folder is later edited.

Two views of the ground truth are exposed:

* :func:`load_frames` — the 765 kept frames, for per-image scoring.
* :class:`Timeline` — a per-video label timeline for scoring video replay. It
  is built from *every* sampled frame in ``frames.csv`` (3-8 fps), kept or
  dropped. Kept frames carry the reviewed label. A dropped frame was dropped as
  a near-duplicate (pHash distance <= 2) of a kept frame; it inherits that
  twin's reviewed label when the twin is within one second, otherwise keeps its
  own segment label if the segment was confident (>= 0.7), otherwise it is
  left unscored. The reviewed labels matter: they corrected several
  low-confidence segments (e.g. ``wa02`` 41.3-47.6 s is a weapon, not the
  ``normal (0.45)`` segment it sits inside).

The frames and the ten source videos ship with the repository
(``training/datasets/sentrix_custom/frames`` and ``.../videos``), so every
evaluation runs on a fresh clone. ``SENTRIX_CUSTOM_DATASET`` /
``SENTRIX_CUSTOM_VIDEOS`` point elsewhere when set.
"""

from __future__ import annotations

import csv
import os
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from sentrix.paths import ROOT_DIR

CLASSES: tuple[str, ...] = ("normal", "fire", "weapon", "assault", "robbery")
CLASS_TO_IDX: dict[str, int] = {c: i for i, c in enumerate(CLASSES)}
THREAT_CLASSES: tuple[str, ...] = CLASSES[1:]

#: Vendored ground truth. Authoritative for every metric in this repository.
METADATA_DIR = ROOT_DIR / "training" / "datasets" / "sentrix_custom"

#: The frame dataset (labels.csv + train/val/test) and its source videos.
DEFAULT_DATASET_DIR = METADATA_DIR / "frames"
DEFAULT_VIDEOS_DIR = METADATA_DIR / "videos"

#: TCI incident type -> dataset class. ``intrusion`` (an unrecognised person)
#: is not one of the five dataset classes: nobody in this footage is enrolled,
#: so it would fire on every frame with a person in it and says nothing about
#: whether the *threat* was recognised.
INCIDENT_TO_CLASS: dict[str, str] = {
    "fire": "fire",
    "weapon": "weapon",
    "assault": "assault",
    "robbery": "robbery",
}


def incident_to_class(incident_type: str | None) -> str:
    return INCIDENT_TO_CLASS.get(str(incident_type or "normal"), "normal")


def dataset_dir(override: str | Path | None = None) -> Path:
    return Path(override or os.getenv("SENTRIX_CUSTOM_DATASET")
                or DEFAULT_DATASET_DIR)


def videos_dir(override: str | Path | None = None) -> Path:
    return Path(override or os.getenv("SENTRIX_CUSTOM_VIDEOS")
                or DEFAULT_VIDEOS_DIR)


@dataclass(frozen=True)
class Frame:
    """One labelled image."""

    path: Path
    label: str
    split: str
    video: str                      # video id, e.g. "wa02"
    timestamp: float
    confidence: float

    @property
    def class_id(self) -> int:
        return CLASS_TO_IDX[self.label]


def _video_id(source_video: str) -> str:
    return Path(source_video).stem


def load_frames(splits: tuple[str, ...] | list[str] | None = None,
                root: str | Path | None = None) -> list[Frame]:
    """Kept frames from the vendored ``labels.csv``, resolved under ``root``."""
    base = dataset_dir(root)
    wanted = set(splits) if splits else None
    frames: list[Frame] = []
    with open(METADATA_DIR / "labels.csv", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if wanted is not None and row["split"] not in wanted:
                continue
            frames.append(Frame(
                path=base / row["filepath"],
                label=row["class_name"],
                split=row["split"],
                video=_video_id(row["source_video"]),
                timestamp=float(row["timestamp_sec"]),
                confidence=float(row["label_confidence"]),
            ))
    return frames


@dataclass(frozen=True)
class VideoInfo:
    video_id: str
    file: str
    split: str
    fps: float

    def path(self, root: str | Path | None = None) -> Path:
        return videos_dir(root) / self.file


def load_videos() -> dict[str, VideoInfo]:
    with open(METADATA_DIR / "videos.csv", newline="", encoding="utf-8") as fh:
        return {row["video_id"]: VideoInfo(row["video_id"], row["file"],
                                           row["split"], float(row["fps"]))
                for row in csv.DictReader(fh)}


def videos_for(selection: str) -> list[VideoInfo]:
    """``heldout`` (val + test), ``all``, a split name, or ``a,b,c`` ids."""
    videos = load_videos()
    if selection == "all":
        return list(videos.values())
    if selection == "heldout":
        return [v for v in videos.values() if v.split in ("val", "test")]
    if selection in ("train", "val", "test"):
        return [v for v in videos.values() if v.split == selection]
    ids = [s.strip() for s in selection.split(",") if s.strip()]
    missing = [i for i in ids if i not in videos]
    if missing:
        raise KeyError(f"unknown video id(s) {missing}; known: {sorted(videos)}")
    return [videos[i] for i in ids]


class Timeline:
    """Reviewed per-video labels over time, for scoring video replay."""

    def __init__(self, frames_csv: Path | None = None) -> None:
        path = frames_csv or METADATA_DIR / "frames.csv"
        with open(path, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))

        kept = {Path(r["image_path"]).name: (float(r["timestamp"]), r["class"])
                for r in rows if r["status"] == "kept"}
        points: dict[str, list[tuple[float, str | None]]] = defaultdict(list)
        for r in rows:
            t = float(r["timestamp"])
            label: str | None = r["class"]
            if r["status"] != "kept":
                label = self._dropped_label(r, t, kept)
            points[r["source_video_id"]].append((t, label))

        self._times: dict[str, list[float]] = {}
        self._labels: dict[str, list[str]] = {}
        for video, pts in points.items():
            pts.sort()
            self._times[video] = [t for t, _ in pts]
            self._labels[video] = [lab for _, lab in pts]

    #: A near-duplicate only inherits its kept twin's label when the two are
    #: this close in time. pHash is a global-structure hash: a small flame or
    #: a stick barely moves it, so a "duplicate" seconds away can differ in
    #: exactly the object that decides the label.
    TWIN_MAX_GAP = 1.0
    #: Otherwise the frame keeps its own segment label only if the segment
    #: was confident — the same 0.7 bar the dataset build used.
    MIN_SEGMENT_CONFIDENCE = 0.7

    @classmethod
    def _dropped_label(cls, row: dict, t: float,
                       kept: dict[str, tuple[float, str]]) -> str | None:
        twin = kept.get(row["duplicate_status"].removeprefix("duplicate_of:"))
        if twin is not None and abs(twin[0] - t) <= cls.TWIN_MAX_GAP:
            return twin[1]
        if float(row["label_confidence"] or 0.0) >= cls.MIN_SEGMENT_CONFIDENCE:
            return row["class"]
        return None

    @property
    def videos(self) -> list[str]:
        return sorted(self._times)

    def label_at(self, video: str, t: float, tolerance: float = 0.25) -> str | None:
        """Label of the nearest sampled frame within ``tolerance`` seconds.

        ``None`` means unscored: nothing was sampled close enough to ``t``, or
        the nearest sample is ambiguous.
        """
        times = self._times.get(video)
        if not times:
            return None
        i = bisect_left(times, t)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(times):
                gap = abs(times[j] - t)
                if gap <= tolerance and (best is None or gap < best[0]):
                    best = (gap, j)
        return None if best is None else self._labels[video][best[1]]

    def episodes(self, video: str, max_gap: float = 0.6) -> list[tuple[str, float, float]]:
        """Contiguous runs of one label, as ``(label, start, end)``."""
        out: list[tuple[str, float, float]] = []
        for t, lab in zip(self._times.get(video, []), self._labels.get(video, [])):
            if lab is None:
                continue
            if out and out[-1][0] == lab and t - out[-1][2] <= max_gap:
                out[-1] = (lab, out[-1][1], t)
            else:
                out.append((lab, t, t))
        return out


__all__ = [
    "CLASSES", "CLASS_TO_IDX", "THREAT_CLASSES", "METADATA_DIR",
    "INCIDENT_TO_CLASS", "incident_to_class", "dataset_dir", "videos_dir",
    "Frame", "load_frames", "VideoInfo", "load_videos", "videos_for", "Timeline",
]
