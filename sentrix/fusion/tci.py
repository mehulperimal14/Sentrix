"""Threat Confidence Index computation.

Fixes relative to the previous ``FusionEngine``:

1. **No unfitted input transform.** See ``calibration.py``. Raw modality scores
   are fed to the booster, matching how it was fitted. An empty room now scores
   ~0.01 instead of 0.42.
2. **Contract-aware model loading.** The booster's input width selects the
   feature contract, so both the shipped 5-feature model and a future 8-feature
   one work without a code change.
3. **Frame-rate independent smoothing.** ``alpha`` is derived from real elapsed
   time and a configured time constant, instead of being a fixed per-frame
   constant that silently changed meaning whenever the inference rate changed.
4. **Overrides no longer corrupt the smoother.** Every early-return path used to
   skip updating ``previous_tci``, so once a weapon override fired, the EMA
   resumed from a stale value from before the incident.
5. **All eight modalities reach the score.** With a 5-feature booster, weapon,
   fire and behaviour previously contributed nothing except through hard
   overrides — a knife at confidence 0.5 and no knife at all produced an
   identical TCI. They are now folded in explicitly.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

from sentrix.config import settings
from sentrix.fusion import calibration
from sentrix.fusion.features import CONTRACTS, FeatureVector, contract_for
from sentrix.paths import MODELS_DIR
from sentrix.perception.behaviour import SUSPICIOUS_LABELS
from sentrix.utils.logging import get_logger, telemetry

log = get_logger("tci")


@dataclass
class TCIResult:
    tci: float
    raw_tci: float
    incident_type: str
    reason: str
    source: str                        # xgboost | linear | override
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


class TCIEngine:
    """Late-fusion threat scoring with hard overrides and temporal smoothing."""

    def __init__(self) -> None:
        self.cfg = settings.fusion
        self._booster = None
        self._contract = "v2"
        self._previous = 0.0
        self._last_update: float | None = None
        self._output_calibrator = calibration.load(
            MODELS_DIR / "tci_calibrator.json")
        self._load_booster()

        if isinstance(self._output_calibrator, calibration.IdentityCalibrator):
            log.info("no fitted output calibrator — TCI is a raw score, not a "
                     "probability (see notebooks/02_tci_calibration.ipynb)")

    # ── model ─────────────────────────────────────────────────────────────────
    def _load_booster(self) -> None:
        path = MODELS_DIR / self.cfg.model_filename
        if not path.exists():
            log.warning("no booster at %s — using transparent linear fusion",
                        path)
            return
        try:
            import xgboost as xgb
            booster = xgb.Booster()
            booster.load_model(str(path))
            n_features = int(booster.num_features())
            self._contract = contract_for(n_features)
            self._booster = booster
            log.info("TCI booster loaded: %s (%d features -> contract %s: %s)",
                     path.name, n_features, self._contract,
                     ", ".join(CONTRACTS[self._contract]))
            if self._contract == "v1":
                log.warning(
                    "booster uses the legacy 5-feature contract; weapon, fire "
                    "and behaviour are folded in outside the model. Retrain "
                    "against CONTRACT_V2 to use all eight inputs.")
        except ImportError:
            log.warning("xgboost not installed — using linear fusion")
        except Exception as exc:
            log.error("booster load failed (%s) — using linear fusion", exc)

    @property
    def model_source(self) -> str:
        return "xgboost" if self._booster is not None else "linear"

    # ── smoothing ─────────────────────────────────────────────────────────────
    def _smooth(self, raw: float, now: float) -> float:
        """Time-constant EMA: ``alpha = 1 - exp(-dt / tau)``.

        Identical behaviour at any inference rate. The old fixed ``alpha = 0.3``
        at ~5 Hz corresponded to tau = 0.56 s; the default tau here is 1.2 s.
        """
        if self._last_update is None:
            self._last_update, self._previous = now, raw
            return raw
        dt = max(now - self._last_update, 1e-3)
        self._last_update = now
        alpha = 1.0 - math.exp(-dt / max(self.cfg.ema_tau_seconds, 1e-3))
        self._previous += alpha * (raw - self._previous)
        return float(self._previous)

    def reset(self) -> None:
        self._previous, self._last_update = 0.0, None

    # ── scoring ───────────────────────────────────────────────────────────────
    def _linear(self, features: FeatureVector) -> float:
        weights = self.cfg.fallback_weights
        return sum(getattr(features, name, 0.0) * weight
                   for name, weight in weights.items())

    def _predict(self, features: FeatureVector) -> tuple[float, str]:
        if self._booster is None:
            return self._linear(features), "linear"
        try:
            import xgboost as xgb
            matrix = xgb.DMatrix(features.to_array(self._contract),
                                 feature_names=list(CONTRACTS[self._contract]))
            score = float(self._booster.predict(matrix)[0])
        except Exception as exc:
            log.debug("booster inference failed (%s) — linear fallback", exc)
            return self._linear(features), "linear"

        if self._contract == "v1":
            # The legacy booster is blind to weapon, fire and behaviour, so a
            # visible knife below the override threshold would contribute
            # nothing at all. Add the missing modalities explicitly, using the
            # published fallback weights.
            #
            # Taking max(booster, linear) instead does not work: the booster
            # term dominates and the residual modalities still vanish. Retrain
            # against CONTRACT_V2 to remove this seam entirely.
            missing = set(CONTRACTS["v2"]) - set(CONTRACTS["v1"])
            score += sum(getattr(features, name, 0.0) * weight
                         for name, weight in self.cfg.fallback_weights.items()
                         if name in missing)
        return score, "xgboost"

    @staticmethod
    def _explain(features: FeatureVector, weights: dict) -> list[dict]:
        factors = [
            {"name": name,
             "score": round(getattr(features, name, 0.0), 3),
             "weight": round(weight, 3),
             "contribution": round(getattr(features, name, 0.0) * weight, 4)}
            for name, weight in weights.items()
        ]
        factors.sort(key=lambda f: f["contribution"], reverse=True)
        return factors[:3]

    @staticmethod
    def _uncertainty(features: FeatureVector, tci: float) -> tuple[float, tuple[float, float]]:
        """Disagreement between modalities, as a crude confidence proxy."""
        import numpy as np
        values = [features.vision, features.audio, features.motion,
                  features.behaviour, features.identity, features.weapon,
                  features.fire]
        peak = max(max(values), 0.01)
        uncertainty = float(min(np.std(values) / peak, 1.0))
        band = uncertainty * 0.2
        return round(uncertainty, 4), (round(max(0.0, tci - band), 4),
                                       round(min(1.0, tci + band), 4))

    def compute(self, features: FeatureVector,
                now: float | None = None) -> TCIResult:
        """Score one frame. Never raises."""
        now = time.monotonic() if now is None else now
        cfg = self.cfg

        def finish(raw: float, incident: str, reason: str, source: str) -> TCIResult:
            # Every path smooths, including overrides. Skipping this was what
            # left the EMA holding a stale pre-incident value.
            tci = self._smooth(min(max(raw, 0.0), 1.0), now)
            tci = self._output_calibrator(tci)
            uncertainty, band = self._uncertainty(features, tci)
            result = TCIResult(
                tci=round(tci, 4), raw_tci=round(raw, 4), incident_type=incident,
                reason=reason, source=source, features=features,
                uncertainty=uncertainty, confidence_band=band,
                top_factors=self._explain(features, cfg.fallback_weights),
            )
            telemetry.record("tci", "score", {
                "tci": result.tci, "raw": result.raw_tci, "source": source,
                "incident": incident,
            })
            return result

        # ── hard overrides ────────────────────────────────────────────────────
        if features.fire >= cfg.fire_override:
            return finish(0.95, "fire", "Fire or smoke confirmed by detector",
                          "override")
        if features.weapon >= cfg.weapon_override_critical:
            return finish(0.92, "weapon", "Weapon confirmed at high confidence",
                          "override")
        if features.weapon >= cfg.weapon_override_high:
            return finish(0.78, "weapon", "Probable weapon detected", "override")
        if features.intrusion >= cfg.intrusion_override:
            return finish(0.78, "intrusion",
                          "Unrecognised person with sustained motion", "override")

        # Authorised residents floor the score — but only when unarmed, and
        # only when identity is not running degraded.
        if (features.trusted_authorization
                and features.weapon < cfg.weapon_override_high):
            return finish(cfg.authorised_floor, "normal",
                          "Authorised resident recognised", "override")

        # ── model ─────────────────────────────────────────────────────────────
        base, source = self._predict(features)

        if features.person_present and not features.trusted_authorization:
            base += cfg.unknown_person_boost
        if features.behaviour_label in SUSPICIOUS_LABELS:
            base += cfg.suspicious_behaviour_boost

        incident = self._incident_type(features, base)
        return finish(base, incident, self._reason(incident, features), source)

    @staticmethod
    def _incident_type(features: FeatureVector, score: float) -> str:
        if features.weapon > 0.3:
            return "weapon"
        if features.fire > 0.3:
            return "fire"
        if features.behaviour_label == "fighting":
            return "assault"
        if (features.person_present and not features.trusted_authorization
                and score > 0.4):
            return "intrusion"
        return "normal"

    @staticmethod
    def _reason(incident: str, features: FeatureVector) -> str:
        return {
            "weapon": "Possible weapon in frame",
            "fire": "Possible combustion detected",
            "assault": f"Violent behaviour ({features.behaviour_label})",
            "intrusion": "Unrecognised person on premises",
            "normal": "Routine activity",
        }.get(incident, "Routine activity")


__all__ = ["TCIEngine", "TCIResult"]
