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
    authority: str            # POLICE | FIRE
    status: str               # PENDING | SENT | CANCELLED
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

    def send(self, package_id: str, authority: str) -> bool:
        """Send a package. Operator-confirmed; never called automatically."""
        from sentrix.storage import database

        authority = authority.upper()
        if authority not in ("POLICE", "FIRE"):
            log.error("invalid authority: %s", authority)
            return False
        package = database.get_dispatch(package_id)
        if package is None:
            log.error("dispatch %s not found", package_id)
            return False

        body = (f"[SENTRIX EMERGENCY - {authority}]\n"
                f"{package['incident_type'].upper()} at "
                f"{package['camera_location']}\n"
                f"Level {package['level']} (TCI {package['tci']:.2f})\n"
                f"Resident: {package['user_name']}\n"
                f"Address: {package['user_address']}\n"
                f"Contact: {package['user_phone']}\n"
                f"Evidence: {package.get('evidence_bundle') or 'n/a'}")
        sent = notifier.send_raw(body)
        database.update_dispatch_status(package_id, "SENT" if sent else "PENDING")
        return sent

    @staticmethod
    def to_dict(package: DispatchPackage) -> dict:
        return asdict(package)


dispatch_service = DispatchService()

__all__ = ["DispatchPackage", "DispatchService", "dispatch_service"]
