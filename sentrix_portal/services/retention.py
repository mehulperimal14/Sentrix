"""Retention caps on the Portal (decisions, data protection; docs/7 §3.8, §6.2).

Owner: track T-ACCT. A background module (``start(app) -> stop``) that prunes
once a minute after start-up and then daily. The caps
(``sentrix.config.settings.retention``) override every exemption:

* SOS snapshots after ``sos_snapshot_days`` (30) or their own expiry;
* incident mirrors: unreviewed (and FALSE) after ``unreviewed_days`` (180),
  reviewed FALSE below L4 after ``reviewed_low_days``, TRUE after
  ``true_days`` (1 year); dispatch / evidence / snapshot mirrors with them;
* closed assistance requests and their events after ``audit_days`` (1 year);
* the notification log after :data:`NOTIFICATION_DAYS` (90, design §4.4);
* audit rows after ``audit_days``: the oldest prefix goes behind a signed
  boundary row, so ``verify_audit_chain`` reports a retention gap, not
  tampering (``security.prune_audit_before``);
* declined / revoked emergency contacts ``declined_contact_days`` (7) after
  they answered (their number and e-mail were already erased then);
* households whose erasure was requested, once the grace period has passed
  (``accounts.erase_household``; claimed devices are unpaired over the link
  first);
* housekeeping: expired sessions, old sign-in and claim attempts, used or
  expired codes and tokens.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select

from sentrix.actor import Actor
from sentrix.sos import states as sos_states
from sentrix_portal import security
from sentrix_portal.models import (ActionToken, ActivationCode, AssistanceRequest,
                                   ClaimAttempt, DispatchMirror, EmergencyContact,
                                   EvidenceMetaMirror, Household, IncidentMirror,
                                   LoginAttempt, Notification, RequestEvent, SnapshotMirror,
                                   SosSnapshot, UserSession, utcnow)
from sentrix_portal.services import accounts

log = logging.getLogger("sentrix_portal.retention")

NOTIFICATION_DAYS = 90
#: Expired or used codes, tokens, sessions and attempt rows kept this long.
HOUSEKEEPING_DAYS = 30
PRUNE_INTERVAL_SECONDS = 24 * 3600
FIRST_RUN_DELAY_SECONDS = 60


def _caps():
    from sentrix.config import settings
    return settings.retention


def _delete(counts: dict[str, int], name: str, query) -> None:
    removed = query.delete(synchronize_session=False)
    if removed:
        counts[name] = counts.get(name, 0) + removed


def due_erasures(db, now: datetime | None = None) -> list[Household]:
    now = now or utcnow()
    return [h for h in db.query(Household).filter_by(status="erasing")
            if (accounts.erasure_due_at(h) or now) <= now]


def prune(db, now=None) -> dict[str, Any]:
    """One pass; returns counts per table (only what was removed)."""
    now = now or utcnow()
    caps = _caps()
    counts: dict[str, int] = {}
    day = timedelta(days=1)

    for household in due_erasures(db, now):
        for name, value in accounts.erase_household(db, household.id).items():
            counts[f"erased.{name}"] = counts.get(f"erased.{name}", 0) + value

    _delete(counts, "sos_snapshots", db.query(SosSnapshot).filter(or_(
        SosSnapshot.expires_at <= now,
        SosSnapshot.created_at <= now - caps.sos_snapshot_days * day)))

    _delete(counts, "incidents_mirror", db.query(IncidentMirror).filter(or_(
        IncidentMirror.started_at <= now - caps.true_days * day,
        (IncidentMirror.verdict.is_(None) | (IncidentMirror.verdict != "TRUE"))
        & (IncidentMirror.started_at <= now - caps.unreviewed_days * day),
        (IncidentMirror.verdict == "FALSE") & (IncidentMirror.peak_level < 4)
        & (IncidentMirror.started_at <= now - caps.reviewed_low_days * day))))
    kept = select(IncidentMirror.id)
    for model, name in ((DispatchMirror, "dispatch_mirror"),
                        (EvidenceMetaMirror, "evidence_meta_mirror"),
                        (SnapshotMirror, "snapshot_mirror")):
        # Orphans only once they are old enough that their incident is not
        # merely late (outbox kinds arrive in any order).
        _delete(counts, name, db.query(model).filter(or_(
            model.created_at <= now - caps.true_days * day,
            model.incident_id.notin_(kept)
            & (model.created_at <= now - caps.reviewed_low_days * day))))

    old_requests = [r for (r,) in db.query(AssistanceRequest.id).filter(
        AssistanceRequest.state.in_(tuple(sos_states.TERMINAL_STATES)),
        AssistanceRequest.created_at <= now - caps.audit_days * day)]
    if old_requests:
        _delete(counts, "request_events", db.query(RequestEvent)
                .filter(RequestEvent.request_id.in_(old_requests)))
        _delete(counts, "sos_snapshots", db.query(SosSnapshot)
                .filter(SosSnapshot.request_id.in_(old_requests)))
        _delete(counts, "assistance_requests", db.query(AssistanceRequest)
                .filter(AssistanceRequest.id.in_(old_requests)))

    _delete(counts, "notifications", db.query(Notification)
            .filter(Notification.created_at <= now - NOTIFICATION_DAYS * day))

    for contact in db.query(EmergencyContact).filter(
            EmergencyContact.consent_status.in_(("DECLINED", "REVOKED"))).all():
        if accounts.contact_decided_at(db, contact) <= now - caps.declined_contact_days * day:
            _delete(counts, "action_tokens", db.query(ActionToken).filter_by(
                subject_type="contact", subject_id=contact.id))
            db.delete(contact)
            counts["emergency_contacts"] = counts.get("emergency_contacts", 0) + 1

    pruned = security.prune_audit_before(db, now - caps.audit_days * day)
    if pruned:
        counts["audit_log"] = pruned

    stale = now - HOUSEKEEPING_DAYS * day
    _delete(counts, "sessions", db.query(UserSession).filter(or_(
        UserSession.expires_at <= now - day, UserSession.revoked_at <= stale)))
    _delete(counts, "login_attempts", db.query(LoginAttempt).filter(LoginAttempt.at <= stale))
    _delete(counts, "claim_attempts", db.query(ClaimAttempt).filter(ClaimAttempt.at <= stale))
    _delete(counts, "activation_codes", db.query(ActivationCode).filter(or_(
        ActivationCode.used_at <= stale, ActivationCode.expires_at <= stale)))
    _delete(counts, "action_tokens", db.query(ActionToken).filter(or_(
        ActionToken.expires_at <= stale, ActionToken.used_at <= stale,
        ActionToken.revoked_at <= stale)))
    db.flush()
    if counts:
        log.info("retention pruned %s", counts)
    return counts


async def run_once(app) -> dict[str, Any]:
    """Unpair the claimed devices of households about to be erased (over the
    link, best effort), then prune in a worker thread."""
    from sentrix_portal.services import hub
    database = app.state.db

    def devices_to_unpair() -> list[str]:
        with database.session() as db:
            return [d.id for h in due_erasures(db) for d in accounts.claimed_devices(db, h.id)]

    for device_id in await asyncio.to_thread(devices_to_unpair):
        try:
            with database.session() as db:
                await hub.unpair(db, device_id, Actor.system().to_dict())
        except Exception as exc:
            log.error("unpair of %s before erasure failed: %s", device_id, exc)

    def prune_in_session() -> dict[str, Any]:
        with database.session() as db:
            return prune(db)

    return await asyncio.to_thread(prune_in_session)


def start(app):
    async def loop() -> None:
        await asyncio.sleep(FIRST_RUN_DELAY_SECONDS)
        while True:
            try:
                await run_once(app)
            except Exception as exc:                  # keep the worker alive
                log.error("retention pass failed: %s", exc)
            await asyncio.sleep(PRUNE_INTERVAL_SECONDS)

    task = asyncio.get_running_loop().create_task(loop(), name="portal-retention")

    async def stop() -> None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    return stop


__all__ = ["prune", "start", "run_once", "due_erasures", "NOTIFICATION_DAYS"]
