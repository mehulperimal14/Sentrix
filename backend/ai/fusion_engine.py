# ai/fusion_engine.py
#
# ARCHITECTURE: Central aggregation point. Receives normalised scores from all
# engines. Applies hard override rules first (fire/weapon bypass), then
# weighted fusion across 7 dimensions, then contextual boosters, then maps
# the resulting TCI value to one of 5 discrete threat levels.
# The FusionEngine is stateless — call compute() for every frame.
#
# ENHANCEMENTS:
#   - TCIResult now includes uncertainty (0=confident, 1=uncertain),
#     top_factors (ranked contributing signals), and confidence_band.
#   - All existing hard overrides and level mappings are UNCHANGED.

import os
import numpy as np
from dataclasses import dataclass, field
from typing import Dict, List, Tuple
from collections import deque
from pathlib import Path
from core.paths import MODELS_DIR


@dataclass
class TCIResult:
    tci:             float             # 0.0 to 1.0
    level:           int               # 1 to 5
    status:          str               # NORMAL | SUSPICIOUS | ELEVATED | HIGH | CRITICAL
    reason:          str               # Human-readable explanation
    incident_type:   str               # normal | intrusion | weapon | fire | assault
    scores:          Dict[str, float]  = field(default_factory=dict)
    # Explainability fields (new — backward-compatible defaults)
    uncertainty:     float             = 0.0   # 0=very confident, 1=very uncertain
    top_factors:     List[dict]        = field(default_factory=list)  # [{name, weight}]
    confidence_band: Tuple[float, float] = (0.0, 0.0)  # (tci_low, tci_high)


class FusionEngine:
    """
    Stateless multi-modal threat fusion with 5-level output using XGBoost.
    Includes Exponential Moving Average (EMA) for TCI smoothing.
    Provides uncertainty estimation and explainability signals.
    """

    WEIGHTS = {
        "vision":    0.20,
        "audio":     0.15,
        "motion":    0.15,
        "behaviour": 0.15,
        "identity":  0.15,
        "weapon":    0.15,
        "fire":      0.05,
    }

    def __init__(self):
        self.xgb_model = None
        self.model_path = str(MODELS_DIR / "tci_xgboost.json")
        self.previous_tci = 0.0
        self.alpha = 0.3

        try:
            import xgboost as xgb
            if os.path.exists(self.model_path):
                self.xgb_model = xgb.Booster()
                self.xgb_model.load_model(self.model_path)
                print("[FusionEngine] Loaded XGBoost Late Fusion Model.")
            else:
                print(f"[FusionEngine] XGBoost model not found at {self.model_path}. Fallback to naive weights.")
        except ImportError:
            print("[FusionEngine] xgboost not installed. Fallback to naive weights.")
        except Exception as e:
            print(f"[FusionEngine] Failed to load XGBoost: {e}")

    def calibrate_score(self, raw_score, A=-1.5, B=0.5):
        """Platt scaling (dummy params for capstone demonstration)"""
        return 1.0 / (1.0 + np.exp(A * raw_score + B))

    def apply_temporal_smoothing(self, raw_tci: float) -> float:
        """EMA Formula to prevent flickering."""
        smoothed_tci = (self.alpha * raw_tci) + ((1.0 - self.alpha) * self.previous_tci)
        self.previous_tci = smoothed_tci
        return float(smoothed_tci)

    def _compute_uncertainty(self, scores: dict, tci: float) -> Tuple[float, Tuple[float, float]]:
        """
        Estimate uncertainty as the normalised spread of input signals.
        Low spread (all signals agree) → low uncertainty.
        High spread (signals disagree) → high uncertainty.
        Returns (uncertainty_score, (tci_low, tci_high)).
        """
        signal_values = [
            float(scores.get(k, 0.0))
            for k in self.WEIGHTS
            if isinstance(scores.get(k, 0.0), (int, float))
        ]
        if not signal_values or len(signal_values) < 2:
            return 0.0, (tci, tci)

        spread = float(np.std(signal_values))
        max_val = max(max(signal_values), 0.01)
        uncertainty = min(spread / max_val, 1.0)

        # Confidence band: ±uncertainty * 0.2 range around TCI
        band = uncertainty * 0.2
        tci_low  = max(0.0, tci - band)
        tci_high = min(1.0, tci + band)
        return round(uncertainty, 4), (round(tci_low, 4), round(tci_high, 4))

    def _compute_top_factors(self, scores: dict, base_tci: float) -> List[dict]:
        """
        Return the top 3 contributing factors ranked by weighted contribution.
        Each factor: {"name": str, "weight": float, "score": float}
        """
        factors = []
        for key, weight in self.WEIGHTS.items():
            score = float(scores.get(key, 0.0))
            contribution = score * weight
            factors.append({
                "name":         key,
                "score":        round(score, 3),
                "weight":       round(weight, 3),
                "contribution": round(contribution, 4),
            })

        # Sort by contribution descending, return top 3
        factors.sort(key=lambda f: f["contribution"], reverse=True)
        return factors[:3]

    def compute(self, scores: dict) -> TCIResult:
        fire_score   = float(scores.get("fire", 0.0))
        weapon_score = float(scores.get("weapon", 0.0))
        intrusion    = float(scores.get("intrusion", 0.0))
        unauthorized = bool(scores.get("unauthorized", False))
        authorized   = bool(scores.get("authorized", False))
        behaviour    = str(scores.get("behaviour_label", "normal"))

        vision_score   = float(scores.get("vision", 0.0))
        audio_score    = float(scores.get("audio", 0.0))
        motion_score   = float(scores.get("motion", 0.0))
        identity_score = float(scores.get("identity", 0.0))
        is_night       = 1.0 if scores.get("is_night", False) else 0.0

        # STEP 1: Hard overrides — bypass all fusion
        if fire_score >= 0.70:
            return TCIResult(
                tci=0.95, level=5, status="CRITICAL",
                reason="Fire confirmed by cloud engine",
                incident_type="fire", scores=scores,
                uncertainty=0.0, top_factors=[{"name": "fire", "score": fire_score, "weight": 1.0, "contribution": fire_score}],
                confidence_band=(0.90, 1.0),
            )

        if weapon_score >= 0.70:
            return TCIResult(
                tci=0.90, level=5, status="CRITICAL",
                reason="Weapon confirmed by cloud engine",
                incident_type="weapon", scores=scores,
                uncertainty=0.0, top_factors=[{"name": "weapon", "score": weapon_score, "weight": 1.0, "contribution": weapon_score}],
                confidence_band=(0.85, 1.0),
            )

        if weapon_score >= 0.50 or intrusion >= 0.75:
            return TCIResult(
                tci=0.78, level=4, status="HIGH",
                reason="Confirmed threat indicator: weapon or intrusion",
                incident_type="intrusion", scores=scores,
                uncertainty=0.05, top_factors=[{"name": "weapon", "score": weapon_score, "weight": 1.0, "contribution": weapon_score}],
                confidence_band=(0.70, 0.85),
            )

        # STEP 1.5: Authorized User Override
        if authorized and weapon_score < 0.50:
            return TCIResult(
                tci=0.15,
                level=1,
                status="NORMAL",
                reason="Authorized resident recognized",
                incident_type="normal",
                scores=scores,
                uncertainty=0.0,
                top_factors=[{"name": "identity", "score": 1.0, "weight": 1.0, "contribution": 1.0}],
                confidence_band=(0.10, 0.20),
            )

        # STEP 2: ML Fusion via XGBoost
        if self.xgb_model:
            import xgboost as xgb
            # Calibrate inputs
            cal_vision = self.calibrate_score(vision_score)
            cal_audio  = self.calibrate_score(audio_score)
            cal_motion = self.calibrate_score(motion_score)
            cal_identity = self.calibrate_score(identity_score)

            # Predict
            x_input = xgb.DMatrix([[cal_vision, cal_audio, cal_motion, cal_identity, is_night]])
            base = float(self.xgb_model.predict(x_input)[0])
        else:
            # Fallback naive weighted fusion
            from core.instrumentation import log_instrumentation
            log_instrumentation("FusionEngine", "fallback_activation", {"type": "xgboost_to_weighted"})
            base = sum(float(scores.get(key, 0.0)) * weight for key, weight in self.WEIGHTS.items())

        # STEP 3: Contextual boosters
        if unauthorized:
            base += 0.18  # Raised from 0.12 — ensures EMA convergence into L2

        if behaviour in ("running", "crawling", "loitering"):
            base += 0.12  # Raised from 0.10

        raw_tci = max(0.0, min(1.0, base))

        # STEP 4: Temporal Smoothing
        tci = self.apply_temporal_smoothing(raw_tci)

        # STEP 5: Level mapping
        if tci <= 0.25:
            level, status, reason = 1, "NORMAL",    "Routine activity"
        elif tci <= 0.50:
            level, status, reason = 2, "SUSPICIOUS", "Unusual activity detected"
        elif tci <= 0.70:
            level, status, reason = 3, "ELEVATED",  "Multiple risk factors converging"
        elif tci <= 0.85:
            level, status, reason = 4, "HIGH",      "Confirmed threat indicators"
        else:
            level, status, reason = 5, "CRITICAL",  "Critical threat confirmed"

        # STEP 6: Derive incident type
        if weapon_score > 0.3:
            incident_type = "weapon"
        elif fire_score > 0.3:
            incident_type = "fire"
        elif unauthorized and tci > 0.4:
            incident_type = "intrusion"
        else:
            incident_type = "normal"

        # STEP 7: Compute explainability fields
        uncertainty, confidence_band = self._compute_uncertainty(scores, tci)
        top_factors = self._compute_top_factors(scores, tci)

        from core.instrumentation import log_instrumentation
        log_instrumentation("FusionEngine", "inference", {
            "tci": tci, "level": level, "status": status, "incident_type": incident_type,
            "uncertainty": uncertainty, "is_xgboost": self.xgb_model is not None
        })

        return TCIResult(
            tci=round(tci, 4),
            level=level,
            status=status,
            reason=reason,
            incident_type=incident_type,
            scores=scores,
            uncertainty=uncertainty,
            top_factors=top_factors,
            confidence_band=confidence_band,
        )