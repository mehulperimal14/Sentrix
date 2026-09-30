"""Level to action-plan mapping, driven entirely by ``AlertConfig``.

The previous ``EscalationEngine`` hardcoded a ladder that contradicted every
document describing it. Documented behaviour vs. what actually shipped:

=======  ============================  ==================================
Level    Documented (docs/1, Table 4)  Previously implemented
=======  ============================  ==================================
2        UI status only                snapshot + **SMS** + DB row
3        snapshot + evidence           snapshot + SMS + **siren** + evidence
4        + SMS                         + dispatch package
5        + call + siren                + voice call
=======  ============================  ==================================

SMS at level 2 and a siren at level 3 are two full steps more aggressive than
specified, and level 2 was the system's permanent idle state because of the
fusion calibration bug. That combination is what produced the alert storm.

Thresholds now live in configuration rather than in branching code, so the
ladder is auditable and adjustable without touching logic.
"""

from __future__ import annotations

from dataclasses import dataclass

from sentrix.config import settings


@dataclass(frozen=True)
class ActionPlan:
    """What a given level authorises. Consumed by the pipeline."""

    level: int
    log: bool = True
    snapshot: bool = False
    evidence: bool = False
    sms: bool = False
    siren: bool = False
    dispatch: bool = False
    call: bool = False

    @property
    def any_side_effect(self) -> bool:
        return any((self.snapshot, self.evidence, self.sms, self.siren,
                    self.dispatch, self.call))


def plan_for(level: int) -> ActionPlan:
    """Build the action plan for a committed threat level."""
    alert = settings.alert
    evidence = settings.evidence
    level = max(1, min(int(level), 5))
    return ActionPlan(
        level=level,
        log=True,
        # A snapshot is cheap and unencrypted; capture from level 2 so an
        # operator has visual context for anything worth a database row.
        snapshot=level >= 2,
        evidence=level >= evidence.min_level,
        sms=level >= alert.sms_min_level,
        siren=level >= alert.siren_min_level,
        dispatch=level >= alert.dispatch_min_level,
        call=level >= alert.call_min_level,
    )


__all__ = ["ActionPlan", "plan_for"]
