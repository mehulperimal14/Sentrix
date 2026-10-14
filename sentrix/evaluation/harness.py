"""Drive the real ``Pipeline`` for evaluation, with every side effect removed.

There is deliberately no second inference path here: stills and videos both go
through ``Pipeline._observe`` / ``Pipeline.process``, the code the live camera
runs. What the harness changes is *only* what must not happen during a
measurement:

* **No side effects.** ``_act`` and ``_submit`` are replaced with no-ops, so a
  replay can never write evidence, save incident rows, sound the siren or send
  an SMS — ``.env`` in a developer checkout carries live Twilio credentials.
* **Identity is neutral.** Nobody in the dataset footage is enrolled, and the
  dataset has no authorisation labels. A face match against whoever happens to
  be enrolled on this machine would floor the TCI and hide the threat signal
  being measured, so every person is scored as a non-degraded unknown.
* **No microphone.** Live room audio is uncorrelated with a replayed clip.
* **No SOS, link or channels.** The pipeline keeps ``NullSosService`` and the
  harness refuses to run while any background service is running
  (``sentrix.services.assert_measurement_safe``; decision D13).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from sentrix.config import settings
from sentrix.evaluation.custom_dataset import incident_to_class
from sentrix.fusion.features import FeatureVector
from sentrix.fusion.tci import TCIResult
from sentrix.io.sources import FrameSource
from sentrix.perception.identity import IdentityResult
from sentrix.services import assert_measurement_safe
from sentrix.sos import NullSosService


class _NullSource(FrameSource):
    """A source that never yields a frame, so no camera is ever opened."""

    name = "eval:null"

    def read(self):
        return False, None

    @property
    def online(self) -> bool:
        return False

    def release(self) -> None:
        pass


class _UnknownIdentity:
    """Everyone is an unrecognised, non-degraded person."""

    backend = "eval-unknown"
    available = True

    def check(self, frame, now=None) -> IdentityResult:
        return IdentityResult(authorized=False, name=None, similarity=0.0,
                              degraded=False)

    def match_faces(self, frame) -> list:
        return []                   # no face is ever recognised

    def score(self, result: IdentityResult, person_present: bool) -> float:
        return settings.identity.unknown_person_score if person_present else 0.0


def _noop(*_args, **_kwargs) -> None:
    return None


def build_eval_pipeline():
    """A ``Pipeline`` that measures but cannot act."""
    from sentrix.pipeline.engine import Pipeline

    pipeline = Pipeline(source=_NullSource(), enable_audio=False,
                        event_source="eval")
    pipeline.identity = _UnknownIdentity()
    pipeline._act = _noop            # snapshots, evidence, SMS, siren, dispatch, call
    pipeline._submit = _noop         # background writes, incl. incident DB rows
    pipeline._save_open_incident = _noop     # the shutdown save as well
    pipeline.sos = NullSosService()          # never opens an assistance request
    assert_measurement_safe(pipeline)
    return pipeline


@dataclass
class StillResult:
    features: FeatureVector
    result: TCIResult

    @property
    def predicted_class(self) -> str:
        return incident_to_class(self.result.incident_type)


def score_still(pipeline, frame: np.ndarray) -> StillResult:
    """Score one image as a static clip, through the product perception path.

    The image is observed ``max(confirm_n, confirm_k + 1)`` times, one violence
    hop apart, so every source (including the violence model, which re-scores
    once per hop) makes ``confirm_k`` fresh evaluations. Identical frames make
    the k-of-n rule trivially met, so a still measures the per-frame decision
    plus the person gate, not temporal confirmation; that is measured on video
    (``tools/replay_custom_videos.py``). All temporal state is reset first, so
    images are independent.
    """
    pipeline.reset()
    hop = settings.windows.violence_hop_seconds
    threat = settings.threat
    ticks = max(threat.confirm_n, threat.confirm_k + 1)
    features, now = None, 0.0
    for i in range(ticks):
        now = i * hop
        features = pipeline._observe(frame, now)
    result = pipeline.tci.compute(features, now)
    return StillResult(features, result)


def still_record(still: StillResult) -> dict[str, Any]:
    """Flat, JSON-safe summary of one scored image."""
    f = still.features
    extras = f.extras or {}
    record = {
        "pred": still.predicted_class,
        "incident_type": still.result.incident_type,
        "raw_tci": still.result.raw_tci,
        "weapon": round(f.weapon, 4),
        "fire": round(f.fire, 4),
        "behaviour": round(f.behaviour, 4),
        "behaviour_label": f.behaviour_label,
        "violence_score": extras.get("violence_score"),
        "persons": f.person_count,
    }
    scene = extras.get("scene")
    if scene:
        record["scene"] = scene
    return record


#: Raw per-source values recorded on every replayed row (gate G1 AUROC).
SOURCE_COLUMNS = ("scene_weapon", "scene_fire", "scene_assault", "scene_robbery",
                  "detector_weapon", "detector_fire", "violence_assault")


def replay_video(pipeline, path, fps: float | None = None,
                 on_frame: Callable[[float, dict], dict] | None = None,
                 max_seconds: float | None = None,
                 period: float | None = None) -> list[dict]:
    """Replay one video at the pipeline inference rate with true timestamps.

    Frames between inference ticks are decoded and discarded, exactly as the
    live loop discards frames that arrive faster than ``inference_fps``.
    ``period`` overrides the inference period (multi-rate replay: the first
    live test ran at 0.5 Hz, not 6).
    """
    import cv2

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise FileNotFoundError(f"cannot open video {path}")
    native = fps or float(cap.get(cv2.CAP_PROP_FPS) or 25.0)
    period = period or settings.pipeline.inference_period

    pipeline.reset()
    rows: list[dict] = []
    index, next_tick = 0, 0.0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            t = index / native
            index += 1
            if max_seconds is not None and t > max_seconds:
                break
            if t + 1e-9 < next_tick:
                continue
            next_tick = t + period
            out = pipeline.process(frame, now=t)
            row = {"t": round(t, 3), "tci": round(out["tci"], 4),
                   "level": out["level"], "incident_type": out["incident_type"],
                   "pred": incident_to_class(out["incident_type"]),
                   "weapon": round(out["features"].get("weapon", 0.0), 4),
                   "fire": round(out["features"].get("fire", 0.0), 4),
                   "behaviour": round(out["features"].get("behaviour", 0.0), 4),
                   "latency_ms": round(out["latency"] * 1000, 1)}
            for source, per_class in (out.get("sources") or {}).items():
                for cls, vote in per_class.items():
                    column = f"{source}_{cls}"
                    if column in SOURCE_COLUMNS:
                        row[column] = round(float(vote[0]), 4)
            if on_frame is not None:
                row = on_frame(t, row) or row
            rows.append(row)
    finally:
        cap.release()
    return rows


__all__ = ["build_eval_pipeline", "score_still", "still_record", "StillResult",
           "replay_video"]
