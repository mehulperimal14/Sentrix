"""Emergency dispatch packages.

Prototype only: there is no integration with any real police or fire authority
API, and the report should say so plainly. What this does is assemble a
pre-populated, human-reviewable incident package and route it on explicit
operator confirmation — which is the correct design regardless, because
automatically dispatching emergency services on an ML score is neither legal nor
responsible.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from sentrix.alerting.notifier import notifier
from sentrix.utils.logging import get_logger

log = get_logger("dispatch")

FIRE_INCIDENTS = {"fire"}
#: Authorities a package can be sent to (``sentrix.sos.states.AUTHORITIES``).
AUTHORITIES = ("POLICE", "FIRE", "MEDICAL")
#: Package statuses that may still be sent (docs/7 §6.1). SENT, CANCELLED and
#: EXPIRED are final: sending one again answers 409 ``conflict``.
SENDABLE_STATUSES = ("PENDING", "FAILED")


@dataclass(frozen=True)
class SendOutcome:
    """Result of :meth:`DispatchService.send`. Truthy only when sent, so
    ``if dispatch_service.send(...)`` keeps working. ``code`` is the docs/7
    §13 code a route maps to its status: sent -> ok (200), failed -> ok with
    ``status: "failed"`` (the guard suppressed it or the provider failed; the
    package stays sendable), conflict -> 409, not_found -> 404, invalid -> 400."""

    status: str                 # sent | failed | conflict | not_found | invalid
    message: str = ""

    def __bool__(self) -> bool:
        return self.status == "sent"

    @property
    def code(self) -> str:
        return {"sent": "ok", "failed": "ok", "conflict": "conflict",
                "not_found": "not_found", "invalid": "invalid_args"}[self.status]


@dataclass
class DispatchPackage:
    id: str
    created_at: str
    incident_id: str | None
    incident_type: str
    level: int
    tci: float
    user_name: str
    user_address: str
    user_phone: str
    camera_location: str
    evidence_bundle: str | None
    evidence_hash: str | None
    authority: str            # POLICE | FIRE (recommended)
    status: str               # database.DISPATCH_STATUSES
    payload_json: str


class DispatchService:
    """Builds packages and sends them once an operator confirms."""

    def create(self, result, level: int, incident_id: str | None = None,
               evidence=None) -> DispatchPackage | None:
        try:
            authority = "FIRE" if result.incident_type in FIRE_INCIDENTS else "POLICE"
            payload = {
                "incident_type": result.incident_type,
                "level": level,
                "tci": round(result.tci, 4),
                "reason": result.reason,
                "features": result.features.model_features(),
                "top_factors": result.top_factors,
                "detected_at": datetime.now(timezone.utc).isoformat(),
            }
            package = DispatchPackage(
                id=str(uuid.uuid4()),
                created_at=datetime.now(timezone.utc).isoformat(),
                incident_id=incident_id,
                incident_type=result.incident_type,
                level=level,
                tci=round(result.tci, 4),
                user_name=os.getenv("SENTRIX_USER_NAME", "Unknown"),
                user_address=os.getenv("SENTRIX_USER_ADDRESS", "Address not set"),
                user_phone=os.getenv("SENTRIX_USER_PHONE", ""),
                camera_location=os.getenv("SENTRIX_CAMERA_LOCATION", "Unknown"),
                evidence_bundle=getattr(evidence, "bundle_id", None),
                evidence_hash=getattr(evidence, "sha256", None),
                authority=authority,
                status="PENDING",
                payload_json=json.dumps(payload, default=str),
            )

            from sentrix.storage import database
            database.save_dispatch(package)
            log.info("dispatch package %s created (%s, level %d)",
                     package.id[:8], authority, level)
            return package
        except Exception as exc:
            log.error("dispatch creation failed: %s", exc)
            return None

    def send(self, package_id: str, authority: str, *,
             by: str = "lc:device_admin") -> SendOutcome:
        """Send a package. Operator-confirmed; never called automatically.

        With the SOS service on, a package linked to a request is sent by the
        SOS engine (``sos.approve_package``), not here.
        """
        from sentrix.storage import database

        authority = (authority or "").upper()
        if authority not in AUTHORITIES:
            log.error("invalid authority: %s", authority)
            return SendOutcome("invalid", f"authority must be one of {', '.join(AUTHORITIES)}")
        package = database.get_dispatch(package_id)
        if package is None:
            log.error("dispatch %s not found", package_id)
            return SendOutcome("not_found", "unknown package")
        if package["status"] not in SENDABLE_STATUSES:
            return SendOutcome("conflict", f"package is {package['status']}")

        body = (f"[SENTRIX EMERGENCY - {authority}]\n"
                f"{package['incident_type'].upper()} at "
                f"{package['camera_location']}\n"
                f"Level {package['level']} (TCI {package['tci']:.2f})\n"
                f"Resident: {package['user_name']}\n"
                f"Address: {package['user_address']}\n"
                f"Contact: {package['user_phone']}\n"
                f"Evidence: {package.get('evidence_bundle') or 'n/a'}")
        sent = notifier.send_raw(body)
        if sent:
            database.update_dispatch_status(
                package_id, "SENT", chosen_authority=authority, sent_by=by,
                sent_at=datetime.now(timezone.utc), error=None)
            return SendOutcome("sent")
        # Stays PENDING (the dashboard's dispatch panel lists PENDING packages,
        # and a dry run is the default): the attempt is recorded in ``error``.
        status = package["status"] if package["status"] in SENDABLE_STATUSES else "PENDING"
        database.update_dispatch_status(package_id, status, chosen_authority=authority,
                                        error="not sent (send guard or provider)")
        return SendOutcome("failed", "not sent: the send guard refused it (dry run "
                                     "by default) or the provider failed")

    @staticmethod
    def to_dict(package: DispatchPackage) -> dict:
        return asdict(package)


dispatch_service = DispatchService()

__all__ = ["DispatchPackage", "DispatchService", "SendOutcome", "dispatch_service",
           "AUTHORITIES", "SENDABLE_STATUSES"]
