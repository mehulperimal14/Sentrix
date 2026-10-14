"""Emergency dispatch packages.

Prototype only: there is no integration with any real police or fire authority
API, and the report should say so plainly. What this does is assemble a
pre-populated, human-reviewable incident package and route it on explicit
operator confirmation — which is the correct design regardless, because
automatically dispatching emergency services on an ML score is neither legal nor
responsible.

Two kinds of package exist (docs/7 §6.1, §7.4):

* **One per assistance request** (SOS service on): opened with the request at
  the L3 commit, filled from the household profile (resident, address,
  camera room), carrying the request id. Its life follows the request: the
  authority chosen at approval, SENT when a target accepted the SOS
  (``delivered``), CANCELLED on a decline or a FALSE verdict while prompting,
  EXPIRED when the request lapsed. "Send now" on it approves the request
  (``sos.approve_package``), never :meth:`DispatchService.send`.
* **One per L4 incident** (SOS off: the pre-portal behaviour), from the
  ``SENTRIX_USER_*`` variables, sent only by :meth:`DispatchService.send`.
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
    #: The assistance request this package belongs to (None: legacy L4 package).
    request_id: str | None = None


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

    # ── packages of assistance requests ──────────────────────────────────────
    def create_for_request(self, request: dict, profile: dict | None, *,
                           tci: float = 0.0, reason: str = "",
                           sos_text: str | None = None) -> DispatchPackage | None:
        """The one package of an assistance request, filled from the profile.

        ``request`` is a ``storage.requests.request_dict``; the package
        carries its id, type, level, evidence and window (``expires_at``).
        Idempotent: an existing package of the request is returned as is.
        """
        from sentrix.sos.policy import recommended_authority
        from sentrix.sos.templates import clean
        from sentrix.storage import database

        try:
            existing = self.package_for_request(request["id"])
            if existing is not None:
                return existing
            profile = profile or {}
            owner = profile.get("owner") or {}
            address = profile.get("address") or {}
            camera = next((c for c in profile.get("cameras") or []
                           if isinstance(c, dict)
                           and c.get("camera_id") == request.get("camera_id")), None) \
                or next(iter(profile.get("cameras") or []), {}) or {}
            parts = [address.get(k) for k in ("house", "building", "street", "locality",
                                              "city", "state", "pincode")]
            landmark = clean(address.get("landmark"), 60)
            user_address = ", ".join(p for p in (clean(x, 80) for x in parts) if p)
            if landmark:
                user_address = f"{user_address} (near {landmark})" if user_address else landmark
            room = ", ".join(p for p in (clean(camera.get("room_label"), 40),
                                         clean(camera.get("floor"), 30)) if p)
            payload = {
                "request_id": request["id"], "short_id": request.get("short_id"),
                "incident_type": request.get("incident_type"),
                "level": request.get("peak_level"), "tci": round(float(tci or 0.0), 4),
                "reason": reason, "evidence_state": request.get("evidence_state"),
                "map_url": address.get("map_url"), "sos_text": sos_text,
                "detected_at": request.get("created_at"),
                "test_mode": bool(request.get("test_mode", True)),
            }
            package = DispatchPackage(
                id=str(uuid.uuid4()), created_at=datetime.now(timezone.utc).isoformat(),
                incident_id=request.get("incident_id"),
                incident_type=str(request.get("incident_type") or "unknown"),
                level=int(request.get("peak_level") or request.get("trigger_level") or 3),
                tci=round(float(tci or 0.0), 4),
                user_name=clean(owner.get("name"), 100) or "Resident",
                user_address=user_address or "Address not set",
                user_phone=str(owner.get("phone") or "")[:24],
                camera_location=room or "Camera",
                evidence_bundle=request.get("evidence_bundle"),
                evidence_hash=request.get("evidence_hash"),
                authority=recommended_authority(str(request.get("incident_type") or "")),
                status="PENDING", payload_json=json.dumps(payload, default=str),
                request_id=request["id"])
            database.save_dispatch(package)
            expires = request.get("expires_at")
            if expires:
                database.update_dispatch_status(package.id, "PENDING",
                                                expires_at=datetime.fromisoformat(expires),
                                                request_id=request["id"])
            log.info("dispatch package %s created for request %s",
                     package.id[:8], request.get("short_id"))
            return package
        except Exception as exc:
            log.error("dispatch package for request failed: %s", exc)
            return None

    @staticmethod
    def package_for_request(request_id: str) -> DispatchPackage | None:
        from sentrix.storage import database
        from sentrix.storage.models import DispatchRecord
        try:
            with database.session_scope() as session:
                row = (session.query(DispatchRecord).filter_by(request_id=request_id)
                       .order_by(DispatchRecord.created_at).first())
                if row is None:
                    return None
                return DispatchPackage(
                    id=row.id, created_at=str(row.created_at), incident_id=row.incident_id,
                    incident_type=row.incident_type, level=row.level, tci=row.tci,
                    user_name=row.user_name, user_address=row.user_address,
                    user_phone=row.user_phone, camera_location=row.camera_location,
                    evidence_bundle=row.evidence_bundle, evidence_hash=row.evidence_hash,
                    authority=row.authority, status=row.status,
                    payload_json=row.payload_json or "{}", request_id=row.request_id)
        except Exception as exc:
            log.error("package lookup failed: %s", exc)
            return None

    def apply_request_change(self, request: dict, change: str, *, by: str | None = None,
                             authority: str | None = None,
                             sent_to: list | None = None) -> str | None:
        """Follow the request: ``authority`` (approved: the chosen authority,
        still PENDING), ``sent`` (a target accepted it), ``cancelled``,
        ``expired``, ``evidence`` (the bundle sealed after the request
        opened). Returns the package's new status, or None (no package, or a
        final package left alone)."""
        from sentrix.storage import database

        package = self.package_for_request(request["id"])
        if package is None:
            return None
        now = datetime.now(timezone.utc)
        if change == "authority":
            if package.status not in SENDABLE_STATUSES:
                return None
            database.update_dispatch_status(package.id, package.status,
                                            chosen_authority=authority, sent_by=by)
            return package.status
        if change == "sent":
            if package.status == "SENT":
                return "SENT"
            if package.status not in SENDABLE_STATUSES:
                return None
            database.update_dispatch_status(
                package.id, "SENT", chosen_authority=authority or request.get("authority"),
                sent_at=now, sent_by=by or request.get("responded_by"),
                sent_to=sent_to or [], error=None)
            return "SENT"
        if change in ("cancelled", "expired"):
            if package.status not in SENDABLE_STATUSES:
                return None
            status = "CANCELLED" if change == "cancelled" else "EXPIRED"
            database.update_dispatch_status(package.id, status)
            return status
        if change == "evidence":
            bundle, digest = request.get("evidence_bundle"), request.get("evidence_hash")
            if not bundle or package.evidence_bundle:
                return None
            from sentrix.storage.models import DispatchRecord
            with database.session_scope() as session:
                row = session.query(DispatchRecord).filter_by(id=package.id).first()
                if row is not None:
                    row.evidence_bundle, row.evidence_hash = bundle, digest
                    row.updated_at = now
            return package.status
        raise ValueError(f"unknown package change {change!r}")

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


def owner_view(package: dict, request: dict | None = None) -> dict:
    """A package as owners, members and the LC may see it.

    A duress approval looks like a decline everywhere the household looks
    (docs/7 §7.8), so the package of a duress request shows as CANCELLED with
    nothing about where it went. ``request`` is the package's request
    (looked up by ``request_id`` when omitted)."""
    if not package or not package.get("request_id"):
        return package
    if request is None:
        from sentrix.storage import requests
        request = requests.get_request(package["request_id"])
    if not request or not (request.get("flags") or {}).get("duress"):
        return package
    hidden = dict(package)
    hidden.update(status="CANCELLED", chosen_authority=None, sent_at=None, sent_by=None,
                  sent_to=[], error=None)
    return hidden


__all__ = ["DispatchPackage", "DispatchService", "SendOutcome", "dispatch_service",
           "AUTHORITIES", "SENDABLE_STATUSES", "owner_view"]
