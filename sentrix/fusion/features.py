"""The TCI feature contract.

There were previously three mutually inconsistent definitions of "the fusion
inputs" in the repository:

===========================================  ==================================
Location                                     Features
===========================================  ==================================
Deployed booster / ``fusion_engine`` call    5: vision, audio, motion, identity, is_night
``FusionEngine.WEIGHTS`` (fallback only)     7: + behaviour, weapon, fire
Retraining notebook's intended schema        7: violence, anomaly, audio, weapon, fire, motion, identity
Mid-semester report, section 3.2             8: vision, audio, motion, behaviour, identity, weapon, fire, is_night
===========================================  ==================================

Training a model against any one of them produced something the serving path
could not consume. This module makes the contract explicit, versioned, and
importable by both the runtime and the training notebooks, so drift becomes a
test failure rather than a silent shape mismatch.

``V2`` is the canonical contract and matches the mid-semester report formula.
``V1`` is retained only so the already-shipped 5-feature booster keeps working
without retraining.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

#: Canonical contract — matches the published TCI formula.
CONTRACT_V2 = ("vision", "audio", "motion", "behaviour", "identity",
               "weapon", "fire", "is_night")

#: Legacy contract of the shipped ``tci_xgboost.json``.
CONTRACT_V1 = ("vision", "audio", "motion", "identity", "is_night")

CONTRACTS: dict[str, tuple[str, ...]] = {"v1": CONTRACT_V1, "v2": CONTRACT_V2}

#: Human-readable descriptions, surfaced in the dashboard and notebooks.
FEATURE_DOCS: dict[str, str] = {
    "vision": "Highest person-detection confidence in the frame.",
    "audio": "Acoustic threat score = class weight x classifier confidence.",
    "motion": "Normalised inter-frame difference energy.",
    "behaviour": "Kinematic/violence behaviour score for the dominant track.",
    "identity": "Unfamiliarity penalty: 0 when authorised, high when unknown.",
    "weapon": "Highest weapon-detection confidence.",
    "fire": "Highest fire/smoke detection confidence.",
    "is_night": "1.0 when scene luminance indicates night/low light.",
}


@dataclass
class FeatureVector:
    """One frame's fusion inputs, plus the context fusion needs for overrides.

    Only the fields named in a contract are fed to the model. The remainder are
    decision context (``authorized``, ``behaviour_label``) or diagnostics, and
    are carried alongside so an evidence sidecar records the complete state.
    """

    vision: float = 0.0
    audio: float = 0.0
    motion: float = 0.0
    behaviour: float = 0.0
    identity: float = 0.0
    weapon: float = 0.0
    fire: float = 0.0
    is_night: float = 0.0

    # Context — never fed to the model.
    authorized: bool = False
    person_present: bool = False
    behaviour_label: str = "normal"
    audio_label: str = "normal_ambient"
    person_count: int = 0
    identity_degraded: bool = False
    extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in CONTRACT_V2:
            value = getattr(self, name)
            setattr(self, name, float(min(max(float(value), 0.0), 1.0)))

    @property
    def intrusion(self) -> float:
        """Composite intrusion signal: an unknown person who is also moving."""
        return self.identity * 0.5 + self.motion * 0.5

    @property
    def trusted_authorization(self) -> bool:
        """True only when authorisation came from a working identity backend.

        Authorisation suppresses alarms, so a degraded identity check must
        never be able to grant it. Every consumer must test this rather than
        the raw ``authorized`` flag, or a failing face backend becomes an
        alarm-suppression bypass.
        """
        return bool(self.authorized) and not self.identity_degraded

    def to_array(self, contract: str = "v2") -> np.ndarray:
        order = CONTRACTS[contract]
        return np.array([[getattr(self, name) for name in order]],
                        dtype=np.float32)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["intrusion"] = round(self.intrusion, 4)
        return data

    def model_features(self, contract: str = "v2") -> dict[str, float]:
        return {name: getattr(self, name) for name in CONTRACTS[contract]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FeatureVector":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def contract_for(n_features: int) -> str:
    """Pick the contract matching a loaded booster's input width."""
    for name, order in CONTRACTS.items():
        if len(order) == n_features:
            return name
    raise ValueError(
        f"no feature contract with {n_features} inputs; "
        f"known: { {k: len(v) for k, v in CONTRACTS.items()} }")


__all__ = ["CONTRACT_V1", "CONTRACT_V2", "CONTRACTS", "FEATURE_DOCS",
           "FeatureVector", "contract_for"]
