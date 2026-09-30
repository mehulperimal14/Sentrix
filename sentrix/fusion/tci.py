"""Threat Confidence Index computation — transparent evidence fusion.

What the TCI is now
-------------------
``raw_tci`` is the strongest per-class threat evidence (:mod:`sentrix.fusion.
evidence`): each class's confirmed, corroboration-capped score, maximised over
classes. Anyone can re-derive it by hand from the votes recorded in an event row.
The level policy consumes ``raw_tci``; ``tci`` is the same value after the
optional fitted output calibrator (none is fitted yet, so they are equal). There
is no display smoothing: the evidence is already confirmed over several
observations, and an EMA made the gauge lag the committed level at every onset.

What it replaced, and why (CLAUDE.md §5, D2, D6)
------------------------------------------------
* **The XGBoost booster** was fitted to a synthetic formula in which
  ``identity`` meant *familiarity*, while the runtime feeds *unfamiliarity*. It
  scored an authorised resident ~5x higher than a stranger, and on labelled
  replay it ranked threats worse than the scene probability it was computed
  from (AUROC 0.793 vs 0.848). Retired until a model can be fitted on real
  session logs (M8).
* **Hard overrides** turned one frame over a threshold into TCI 0.78-0.95.
  Temporal confirmation and corroboration caps now live in the evidence layer.
* **Presence boosts** (+0.15 for any unrecognised person, the intrusion
  override on one saturated motion frame) made every unenrolled teammate an
  L2 incident with a snapshot. Presence is context: in home mode it is logged
  only; in armed mode (``SENTRIX_ARMED=1``) a person who stays unrecognised for
  ``intrusion_dwell_seconds`` raises an intrusion at the L3 band.

Callers that build a :class:`FeatureVector` by hand (tests, notebooks) without an
``extras["evidence"]`` block are treated as already speaking the TCI scale:
``weapon`` and ``fire`` are taken as class evidence directly, and a ``fighting``
or ``robbery`` behaviour label contributes experimental evidence capped at L2.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from sentrix.config import settings
from sentrix.fusion import calibration
from sentrix.fusion.evidence import CLASS_ORDER, strongest
from sentrix.fusion.features import FeatureVector
from sentrix.paths import MODELS_DIR
from sentrix.utils.logging import get_logger, telemetry

log = get_logger("tci")

_BEHAVIOUR_CLASS = {"fighting": "assault", "robbery": "robbery"}

_DEFAULT_REASONS = {
    "weapon": "Possible weapon",
    "fire": "Possible fire",
    "assault": "Possible assault (experimental)",
    "robbery": "Possible theft (experimental)",
    "intrusion": "Unrecognised person (armed mode)",
    "normal": "Routine activity",
}


@dataclass
class TCIResult:
    tci: float
    raw_tci: float
    incident_type: str
    reason: str
    source: str                        # evidence | intrusion | none
    features: FeatureVector
    uncertainty: float = 0.0
    confidence_band: tuple[float, float] = (0.0, 0.0)
    top_factors: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tci": round(self.tci, 4),
            "raw_tci": round(self.raw_tci, 4),
            "incident_type": self.incident_type,
            "reason": self.reason,
            "source": self.source,
            "uncertainty": round(self.uncertainty, 4),
            "confidence_band": [round(v, 4) for v in self.confidence_band],
            "top_factors": self.top_factors,
            "features": self.features.to_dict(),
        }


def class_scores(features: FeatureVector) -> tuple[dict[str, float], dict[str, str]]:
    """Per-class TCI-scale scores and reasons for one feature vector."""
    evidence = (features.extras or {}).get("evidence")
    if evidence:
        return ({c: float(e["score"]) for c, e in evidence.items()},
                {c: str(e.get("reason", "")) for c, e in evidence.items()})
    threat = settings.threat
    scores = {"weapon": features.weapon, "fire": features.fire}
    cls = _BEHAVIOUR_CLASS.get(features.behaviour_label)
    if cls:
        scores[cls] = min(features.behaviour, threat.experimental_cap)
    return scores, {}


class TCIEngine:
    """Transparent evidence fusion with an optional armed-mode intrusion rule."""

    def __init__(self) -> None:
        self.cfg = settings.fusion
        self.threat = settings.threat
        self._unknown_since: float | None = None
        self._unknown_last = 0.0
        self._output_calibrator = calibration.load(MODELS_DIR / "tci_calibrator.json")
        if isinstance(self._output_calibrator, calibration.IdentityCalibrator):
            log.info("TCI = strongest confirmed class evidence (transparent); no "
                     "fitted output calibrator yet (CLAUDE.md M8)")

    @property
    def model_source(self) -> str:
        return "evidence"

    def reset(self) -> None:
        self._unknown_since, self._unknown_last = None, 0.0

    # ── scoring ───────────────────────────────────────────────────────────────
    def _intrusion(self, features: FeatureVector, now: float) -> float | None:
        """Seconds an unrecognised person has been present, in armed mode."""
        unknown = features.person_present and not features.trusted_authorization
        if unknown:
            self._unknown_last = now
            if self._unknown_since is None:
                self._unknown_since = now
        elif (self._unknown_since is None or features.trusted_authorization
              or now - self._unknown_last > self.threat.person_memory_seconds):
            # Recognised, or gone for longer than a missed detection explains.
            self._unknown_since = None
            return None
        if not self.threat.armed:
            return None
        held = now - self._unknown_since
        return held if held >= self.threat.intrusion_dwell_seconds else None

    def compute(self, features: FeatureVector, now: float | None = None) -> TCIResult:
        """Score one frame. Never raises."""
        now = time.monotonic() if now is None else now
        scores, reasons = class_scores(features)
        cls, raw = strongest(scores)
        typing_bar = settings.policy.thresholds[0]

        if cls is not None and raw >= typing_bar:
            incident, source = cls, "evidence"
            reason = reasons.get(cls) or _DEFAULT_REASONS[cls]
        else:
            incident, source, reason = "normal", "none", _DEFAULT_REASONS["normal"]

        present_for = self._intrusion(features, now)
        if present_for is not None and raw < self.threat.intrusion_level:
            raw = self.threat.intrusion_level
            incident, source = "intrusion", "intrusion"
            reason = f"Unrecognised person present for {present_for:.0f} s (armed mode)"

        raw = min(max(raw, 0.0), 1.0)
        tci = self._output_calibrator(raw)
        factors = sorted(({"name": c, "score": round(s, 3), "weight": 1.0,
                           "contribution": round(s, 4)} for c, s in scores.items()),
                         key=lambda f: (-f["contribution"], CLASS_ORDER.index(f["name"])
                                        if f["name"] in CLASS_ORDER else 99))[:3]
        result = TCIResult(
            tci=round(tci, 4), raw_tci=round(raw, 4), incident_type=incident,
            reason=reason, source=source, features=features,
            uncertainty=0.0, confidence_band=(round(tci, 4), round(tci, 4)),
            top_factors=factors)
        telemetry.record("tci", "score", {"tci": result.tci, "raw": result.raw_tci,
                                          "source": source, "incident": incident})
        return result


__all__ = ["TCIEngine", "TCIResult", "class_scores"]
