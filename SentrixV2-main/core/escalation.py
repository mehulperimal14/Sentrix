# core/escalation.py
#
# ARCHITECTURE: Receives a TCI level integer (1-5) and returns a structured
# EscalationAction dataclass. Downstream modules (system_engine) read this
# plan to decide which actions to execute — snapshot, SMS, siren, evidence,
# dispatch form, voice call. Fully declarative; no side-effects here.

from dataclasses import dataclass


@dataclass
class EscalationAction:
    log:      bool   # Always True — every event is logged
    snapshot: bool   # Save annotated frame to disk
    sms:      bool   # Send Twilio SMS alert
    siren:    bool   # Activate local siren hardware
    evidence: bool   # Encrypt and save AES-256-GCM evidence bundle
    form:     bool   # Pre-populate emergency dispatch form
    call:     bool   # Make automated Twilio voice call


class EscalationEngine:
    """Maps TCI level (1-5) to a concrete escalation action plan."""

    def evaluate(self, level: int) -> EscalationAction:
        from core.instrumentation import log_instrumentation
        log_instrumentation("EscalationEngine", "evaluate", {"input_level": level})
        if level == 1:
            return EscalationAction(
                log=True, snapshot=False, sms=False,
                siren=False, evidence=False, form=False, call=False,
            )
        if level == 2:
            return EscalationAction(
                log=True, snapshot=True, sms=True,
                siren=False, evidence=False, form=False, call=False,
            )
        if level == 3:
            return EscalationAction(
                log=True, snapshot=True, sms=True,
                siren=True, evidence=True, form=False, call=False,
            )
        if level == 4:
            return EscalationAction(
                log=True, snapshot=True, sms=True,
                siren=True, evidence=True, form=True, call=False,
            )
        if level == 5:
            return EscalationAction(
                log=True, snapshot=True, sms=True,
                siren=True, evidence=True, form=True, call=True,
            )
        # Default safety fallback for any out-of-range value
        return EscalationAction(
            log=True, snapshot=False, sms=False,
            siren=False, evidence=False, form=False, call=False,
        )