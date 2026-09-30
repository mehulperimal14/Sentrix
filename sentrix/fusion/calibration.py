"""Score calibration.

The previous ``FusionEngine.calibrate_score`` was, by its own docstring, a
"Platt scaling (dummy params for capstone demonstration)":

    calibrate(x) = 1 / (1 + exp(-1.5 * x + 0.5))

Two things were wrong with applying it in the serving path.

**It was never fitted.** ``A = -1.5, B = 0.5`` are placeholders. Platt scaling
means fitting a logistic to held-out scores against ground-truth labels; these
constants encode no data.

**It broke the train/serve contract.** The booster in ``models/tci_xgboost.json``
was fitted on raw scores spanning ``[0, 1]``. The function above maps ``[0, 1]``
onto ``[0.378, 0.731]``, so at serving time the model never saw an input
resembling its training distribution. Measured against the shipped booster:

======================================  ==============  ==================
Scene                                   raw features    calibrated (served)
======================================  ==============  ==================
Empty room, daytime                     0.012           0.425
Empty room, night                       0.054           0.466
High threat                             1.003           0.843
======================================  ==============  ==================

An empty room therefore scored 0.425 — level 2, permanently — while genuine
threats were compressed downward. The full dynamic range of TCI collapsed to
roughly ``[0.40, 0.84]``.

The fix is to stop transforming inputs (``FusionConfig.calibrate_inputs`` is
False by default). Calibration is still the right idea; it just has to be
*fitted*, on held-out data, and applied to the fusion **output** where it has a
probabilistic meaning. ``IsotonicCalibrator`` below is the vehicle for that, and
``notebooks/02_tci_calibration.ipynb`` fits and evaluates it. No fitted
calibrator ships, so the runtime uses ``IdentityCalibrator`` and says so.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from sentrix.utils.logging import get_logger

log = get_logger("calibration")


class Calibrator:
    """Maps a raw score to a calibrated probability."""

    name = "base"

    def __call__(self, score: float) -> float:      # pragma: no cover
        raise NotImplementedError

    def transform(self, scores: np.ndarray) -> np.ndarray:
        return np.array([self(float(s)) for s in scores], dtype=np.float64)


class IdentityCalibrator(Calibrator):
    """No-op. The honest default when nothing has been fitted."""

    name = "identity"

    def __call__(self, score: float) -> float:
        return float(min(max(score, 0.0), 1.0))


class PlattCalibrator(Calibrator):
    """Logistic calibration, ``p = sigmoid(a * s + b)``.

    Construct via :meth:`fit`. The constructor is not a place to invent
    coefficients — that is precisely the bug this module documents.
    """

    name = "platt"

    def __init__(self, a: float, b: float) -> None:
        self.a, self.b = float(a), float(b)

    def __call__(self, score: float) -> float:
        return float(1.0 / (1.0 + np.exp(-(self.a * score + self.b))))

    @classmethod
    def fit(cls, scores: np.ndarray, labels: np.ndarray) -> "PlattCalibrator":
        from sklearn.linear_model import LogisticRegression
        model = LogisticRegression(C=1e6, solver="lbfgs")
        model.fit(np.asarray(scores).reshape(-1, 1), np.asarray(labels))
        return cls(float(model.coef_[0][0]), float(model.intercept_[0]))

    def to_dict(self) -> dict:
        return {"kind": "platt", "a": self.a, "b": self.b}


class IsotonicCalibrator(Calibrator):
    """Non-parametric monotone calibration.

    Preferred over Platt for tree ensembles, whose score distributions are not
    logistic. Needs more held-out data — roughly 1,000+ labelled frames.
    """

    name = "isotonic"

    def __init__(self, x: np.ndarray, y: np.ndarray) -> None:
        self.x = np.asarray(x, dtype=np.float64)
        self.y = np.asarray(y, dtype=np.float64)

    def __call__(self, score: float) -> float:
        return float(np.interp(score, self.x, self.y, left=self.y[0],
                               right=self.y[-1]))

    @classmethod
    def fit(cls, scores: np.ndarray, labels: np.ndarray) -> "IsotonicCalibrator":
        from sklearn.isotonic import IsotonicRegression
        model = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        scores = np.asarray(scores, dtype=np.float64)
        model.fit(scores, np.asarray(labels, dtype=np.float64))
        grid = np.linspace(scores.min(), scores.max(), 256)
        return cls(grid, model.predict(grid))

    def to_dict(self) -> dict:
        return {"kind": "isotonic", "x": self.x.tolist(), "y": self.y.tolist()}


def save(calibrator: Calibrator, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(calibrator.to_dict(), indent=2), encoding="utf-8")
    log.info("calibrator saved: %s (%s)", path, calibrator.name)


def load(path: Path) -> Calibrator:
    """Load a fitted calibrator, or fall back to identity if absent."""
    if not path.exists():
        return IdentityCalibrator()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("kind") == "platt":
            return PlattCalibrator(data["a"], data["b"])
        if data.get("kind") == "isotonic":
            return IsotonicCalibrator(np.array(data["x"]), np.array(data["y"]))
    except Exception as exc:
        log.warning("could not load calibrator %s (%s) — using identity",
                    path, exc)
    return IdentityCalibrator()


def expected_calibration_error(probs: np.ndarray, labels: np.ndarray,
                               bins: int = 10) -> float:
    """ECE — mean gap between confidence and accuracy across equal-width bins.

    The headline calibration metric for ``notebooks/02_tci_calibration.ipynb``.
    Below 0.05 is generally considered well calibrated.
    """
    probs = np.asarray(probs, dtype=np.float64)
    labels = np.asarray(labels, dtype=np.float64)
    edges = np.linspace(0.0, 1.0, bins + 1)
    error = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (probs > lo) & (probs <= hi)
        if not mask.any():
            continue
        error += mask.mean() * abs(labels[mask].mean() - probs[mask].mean())
    return float(error)


__all__ = ["Calibrator", "IdentityCalibrator", "PlattCalibrator",
           "IsotonicCalibrator", "save", "load", "expected_calibration_error"]
