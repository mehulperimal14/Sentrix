# core/dispatch_service.py
#
# ARCHITECTURE: Creates pre-populated emergency dispatch packages at Level 4/5.
# Packages are stored in the SQLite DB. dispatch() sends via Twilio SMS.
# This is a prototype — no real police or fire API integration.
# create_package() and dispatch() never raise; both fall back gracefully.

import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

import db.database as database
from core.alert_service import alert_service

load_dotenv()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DispatchPackage:
    id:                    str
    created_at:            str
    incident_type:         str
    level:                 int
    tci:                   float
    user_name:             str
    user_address:          str
    user_phone:            str
    camera_location:       str
    snapshot_url:          str
    evidence_hash:         str
    recommended_authority: str   # "POLICE" | "FIRE"
    status:                str   # "PENDING" | "SENT" | "CANCELLED"
    payload_json:          str   # Full JSON for audit trail


class DispatchService:
    """Creates and dispatches Level 4/5 emergency packages."""

    def create_package(
        self,
        result,
        snapshot_url: Optional[str] = None,
        evidence_meta=None,
    ) -> Optional[DispatchPackage]:
        try:
            authority = "FIRE" if result.incident_type == "fire" else "POLICE"
            pkg_id    = str(uuid.uuid4())
            created   = _iso_now()

            # Build a dict first so we can JSON-serialise it for payload_json
            pkg_dict = {
                "id":                    pkg_id,
                "created_at":            created,
                "incident_type":         result.incident_type,
                "level":                 result.level,
                "tci":                   round(result.tci, 4),
                "user_name":             os.getenv("SENTRIX_USER_NAME", "Unknown User"),
                "user_address":          os.getenv("SENTRIX_USER_ADDRESS", "Address not set"),
                "user_phone":            os.getenv("SENTRIX_USER_PHONE", ""),
                "camera_location":       os.getenv("SENTRIX_CAMERA_LOCATION", "Main Entrance"),
                "snapshot_url":          snapshot_url or "",
                "evidence_hash":         evidence_meta.sha256 if evidence_meta else "",
                "recommended_authority": authority,
                "status":                "PENDING",
                "payload_json":          "",
            }
            pkg_dict["payload_json"] = json.dumps(pkg_dict)

            pkg = DispatchPackage(**pkg_dict)
            if os.getenv("SENTRIX_EVAL_MODE") == "1":
                print(f"[DispatchService-EVAL] Mock Package created: {pkg.id}")
                return pkg

            database.save_dispatch_package(pkg)
            print(f"[DispatchService] Package created: {pkg.id} | Authority: {authority}")
            return pkg

        except Exception as e:
            print(f"[DispatchService] create_package error: {e}")
            return None

    def dispatch(self, package_id: str, authority: str) -> bool:
        """Send dispatch package via SMS and update status to SENT."""
        try:
            pkg_dict = database.get_dispatch_package(package_id)
            if pkg_dict is None:
                print(f"[DispatchService] Package {package_id} not found")
                return False

            # Accept both ORM row and plain dict
            if hasattr(pkg_dict, "__dict__"):
                d = {c.name: getattr(pkg_dict, c.name)
                     for c in pkg_dict.__table__.columns}
            else:
                d = pkg_dict

            evidence_hash = d.get("evidence_hash", "") or ""
            hash_preview  = (evidence_hash[:16] + "...") if len(evidence_hash) >= 16 else evidence_hash

            message = (
                f"SENTRIX EMERGENCY DISPATCH\n"
                f"Authority : {authority}\n"
                f"Incident  : {str(d.get('incident_type', '')).upper()}\n"
                f"TCI Level : {d.get('level', '?')} ({d.get('tci', 0):.2f})\n"
                f"Location  : {d.get('camera_location', '')}\n"
                f"Address   : {d.get('user_address', '')}\n"
                f"Time      : {d.get('created_at', '')}\n"
                f"Snapshot  : {d.get('snapshot_url', '')}\n"
                f"Evidence  : {hash_preview}\n"
                f"Contact   : {d.get('user_phone', '')}\n"
                f"This is an automated SENTRIX prototype alert."
            )

            alert_service.send_sms_raw(message)
            database.update_dispatch_status(package_id, "SENT")
            print(f"[DispatchService] Dispatched to {authority}: {package_id}")
            return True

        except Exception as e:
            print(f"[DispatchService] dispatch error: {e}")
            return False


# Module-level singleton
dispatch_service = DispatchService()
