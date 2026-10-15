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
  L2 incident with a snapshot. Presence is context. A person who stays
  unrecognised (``intrusion_dwell_seconds`` in armed mode,
  ``intrusion_home_dwell_seconds`` at home), or - armed only - a PIR trigger
  or door change with nobody recognised in view, raises an "unrecognised
  person" at ``intrusion_level``: the **L2 band in both modes** (D15). Levels
  3-5 need a real threat (weapon, fire, a corroborated fight); L2 is log and
  dashboard only (D4), so no snapshot and no SOS request (D8 as amended).

Every result carries ``contributions``: for the leading class, what each
input did - video detector, scene/action model, audio (or why it was
unavailable), motion, people count, identity, sensors - so the dashboard's
Threat Analysis and the event log show how the level was reached.

Callers that build a :class:`FeatureVector` by hand (tests, notebooks) without an
``extras["evidence"]`` block are treated as already speaking the TCI scale:
``weapon`` and ``fire`` are taken as class evidence directly, and a ``fighting``
or ``robbery`` behaviour label contributes experimental evidence capped at L2.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from sentrix import mode as runtime_mode
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
    "intrusion": "Unrecognised person",
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
    #: What each input did for the leading class (D15; :func:`contributions`).
    contributions: list[dict] = field(default_factory=list)

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
            "contributions": self.contributions,
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


def _vote_effect(vote: dict) -> str:
    if vote.get("gated"):
        return "gated"
    if not vote.get("confirmed"):
        return "below confirmation"
    # Banter never lowers a fight below L2 (review F3): it holds it there.
    return "holds at L2" if vote.get("role") == "against" else "raises"


def contributions(features: FeatureVector, cls: str | None,
                  intrusion: dict | None = None) -> list[dict]:
    """Per-input contributions for the leading class ``cls`` (D15).

    One row per input: ``{"input", "value", "detail", "effect"}`` with effect
    one of ``raises``, ``lowers``, ``gated``, ``below confirmation``,
    ``gate ok`` / ``gate failed`` (a precondition), ``context`` (shown,
    changes nothing) or ``unavailable``.
    """
    extras = features.extras or {}
    evidence = (extras.get("evidence") or {}).get(cls or "") or {}
    rows: list[dict] = []
    names = {"detector": "video detector", "fastlane": "video detector (fast lane)",
             "scene": "scene model", "violence": "violence model",
             "audio": "audio", "audio_aggression": "audio (aggression)",
             "audio_benign": "audio (banter)"}
    for vote in evidence.get("votes") or []:
        source = vote.get("source", "?")
        rows.append({"input": names.get(source, source),
                     "value": round(float(vote.get("value", 0.0)), 3),
                     "detail": (f"{vote.get('value', 0):.2f} vs {vote.get('threshold', 0):.2f}"
                                f", {vote.get('hits', 0)}/{vote.get('seen', 0)}"
                                + (f" [{vote['gated']}]" if vote.get("gated") else "")),
                     "effect": _vote_effect(vote)})
    audio = extras.get("audio") or {}
    if not any(r["input"].startswith("audio") for r in rows):
        status = audio.get("status") or "disabled"
        lag = audio.get("lag_s")
        detail = status if lag is None else f"{status} (window ended {lag:.1f} s before the frame)"
        if status == "aligned" and audio.get("neutral"):
            detail = f"aligned, neutral: {audio['neutral']}"
        rows.append({"input": "audio", "value": None, "detail": detail,
                     "effect": "unavailable" if status != "aligned" else "context"})
    threat = settings.threat
    context = extras.get("fusion_context") or {}
    # The decision used fusion_context's people count; None there means the
    # person detector could not say (the gate did not apply) - show that, not
    # this tick's count (review F12). Hand-built vectors have no context.
    persons = (context.get("persons_recent") if "persons_recent" in context
               else features.person_count)
    shown = "unknown" if persons is None else persons
    motion = float(context.get("motion", features.motion) or 0.0)
    if cls == "weapon":
        gate = extras.get("person_recent")
        rows.append({"input": "people", "value": persons,
                     "detail": f"{shown} in view (a weapon needs a person)",
                     "effect": "context" if gate is None else ("gate ok" if gate else "gate failed")})
        rows.append({"input": "motion", "value": round(motion, 3), "detail": "context",
                     "effect": "context"})
    elif cls == "assault":
        need = threat.fight_min_persons
        rows.append({"input": "people", "value": persons,
                     "detail": f"{shown} in view (a fight needs {need}+)",
                     "effect": ("context (count unknown: gate not applied)" if persons is None
                                else "gate ok" if persons >= need else "gate failed")})
        rows.append({"input": "motion", "value": round(motion, 3),
                     "detail": f"floor {threat.fight_motion_floor:.2f} to leave L2",
                     "effect": "gate ok" if motion >= threat.fight_motion_floor else "gate failed"})
    else:
        rows.append({"input": "people", "value": persons, "detail": f"{shown} in view",
                     "effect": "context"})
        rows.append({"input": "motion", "value": round(motion, 3), "detail": "context",
                     "effect": "context"})
    if features.identity_degraded:
        who = "face check unavailable (fail closed)"
    elif features.trusted_authorization:
        who = f"authorised ({extras.get('identity_name') or 'resident'})"
    elif features.person_present:
        who = "unrecognised person in view"
    else:
        who = "nobody in view"
    raising = bool(intrusion and intrusion.get("cause") in ("person", "sensor"))
    rows.append({"input": "identity", "value": features.identity, "detail": who,
                 "effect": "raises (L2 only)" if raising and intrusion.get("cause") == "person"
                 else "context (never lowers a threat)"})
    sensors = extras.get("sensors") or {}
    if sensors:
        detail = ", ".join(f"{k} {v:.0f} s ago" for k, v in sorted(sensors.items()))
        rows.append({"input": "sensors", "value": min(sensors.values()), "detail": detail,
                     "effect": "raises (L2 only)" if raising and intrusion.get("cause") == "sensor"
                     else "context"})
    else:
        rows.append({"input": "sensors", "value": None, "detail": "no PIR/door event",
                     "effect": "context"})
    return rows


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
    def _armed(self) -> bool:
        # A runtime HOME/ARMED toggle (sentrix/mode.py) overrides SENTRIX_ARMED.
        override = runtime_mode.armed_override()
        return bool(self.threat.armed if override is None else override)

    def _intrusion(self, features: FeatureVector, now: float) -> dict | None:
        """An unrecognised person (both modes) or, armed, sensor activity with
        nobody recognised: ``{"cause", "seconds"}`` once the dwell is met."""
        armed = self._armed()
        unknown = features.person_present and not features.trusted_authorization
        if unknown:
            self._unknown_last = now
            if self._unknown_since is None:
                self._unknown_since = now
        elif (self._unknown_since is None or features.trusted_authorization
              or now - self._unknown_last > self.threat.person_memory_seconds):
            # Recognised, or gone for longer than a missed detection explains.
            self._unknown_since = None
        if self._unknown_since is not None:
            dwell = (self.threat.intrusion_dwell_seconds if armed
                     else self.threat.intrusion_home_dwell_seconds)
            held = now - self._unknown_since
            if (armed or self.threat.unknown_alert_home) and held >= dwell:
                return {"cause": "person", "seconds": held, "armed": armed}
        if armed and not features.trusted_authorization:
            sensors = (features.extras or {}).get("sensors") or {}
            recent = [age for age in sensors.values()
                      if 0.0 <= age <= self.threat.sensor_memory_seconds]
            if recent:
                return {"cause": "sensor", "seconds": min(recent), "armed": True,
                        "kinds": sorted(k for k, a in sensors.items()
                                        if 0.0 <= a <= self.threat.sensor_memory_seconds)}
        return None

    def _intrusion_level(self) -> float:
        """``intrusion_level``, held under the L3 threshold whatever the config."""
        l3 = settings.policy.thresholds[1]
        return min(self.threat.intrusion_level, l3 - 0.01)

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

        intrusion = self._intrusion(features, now)
        level = self._intrusion_level()
        if intrusion is not None and raw < level:
            raw = level
            incident, source = "intrusion", "intrusion"
            mode = "armed mode" if intrusion["armed"] else "home mode"
            if intrusion["cause"] == "person":
                reason = (f"Unrecognised person present for {intrusion['seconds']:.0f} s "
                          f"({mode}; L2 only)")
            else:
                reason = (f"Sensor activity ({', '.join(intrusion['kinds'])}) with nobody "
                          f"recognised ({mode}; L2 only)")

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
            top_factors=factors,
            contributions=contributions(
                features, cls if incident not in ("normal", "intrusion") else (
                    cls if cls is not None and incident == "normal" else None),
                intrusion if incident == "intrusion" else None))
        telemetry.record("tci", "score", {"tci": result.tci, "raw": result.raw_tci,
                                          "source": source, "incident": incident})
        return result


__all__ = ["TCIEngine", "TCIResult", "class_scores", "contributions"]
