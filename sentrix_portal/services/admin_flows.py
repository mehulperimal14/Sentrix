"""Company admin flows, shared by ``routes/admin.py`` (HTML) and
``routes/api_v2/admin.py`` (JSON): customers, welcome letters, the device
fleet and its labels, staff accounts and the audit log.

The admin never sees a customer credential, video, faces or evidence
(docs/7 §10, D14): an activation code exists in clear only in the letter's
own response, claim codes and the bootstrap secret only on the label
printed at provisioning (or a reprint / transfer, which replaces the claim
code); the Portal keeps only their HMAC digests.

Owner: track T-ACCT.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Callable, Mapping

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from sentrix.actor import Actor
from sentrix.utils.redact import mask_email, mask_phone
from sentrix_portal import security
from sentrix_portal.models import AuditLog, Device, Household, User
from sentrix_portal.services import accounts
from sentrix_portal.services.outcome import Outcome, done, failed, invalid

Form = Mapping[str, Any]
AUDIT_PAGE_SIZE = 100
DEVICE_STATES = ("UNASSIGNED", "ASSIGNED", "CLAIMED", "RETIRED")
STAFF_ACTIONS = ("disable", "enable", "reset_2fa", "resend")
NOTICES = {"created": "Customer created. Print the welcome letter next.",
           "assigned": "Device assigned.", "deactivated": "Customer deactivated.",
           "reactivated": "Customer reactivated.", "resent": "E-mail queued.",
           "not_queued": "The e-mail could not be queued (no e-mail channel). Try again "
                         "once the e-mail channel runs.",
           "staff_updated": "Saved.", "retired": "Device retired."}
NO_LETTER = ("This customer already chose a password: a letter would need a credential the "
             "company must never see. Use \"Send reset e-mail\" instead.")
CANNOT_SEND = "This customer has no active account with an e-mail address."
NO_SUCH_CUSTOMER = "Check the customer code (SX-0001)."


def cloud_actor(principal: security.Principal) -> dict:
    """Commands from company staff reach the device as the Portal itself."""
    return Actor("cloud", id=principal.user_id, name="SENTRIX admin").to_dict()


def _online(online: Callable[[str], bool] | None) -> Callable[[str], bool]:
    if online is not None:
        return online
    from sentrix_portal.services import hub
    return hub.is_online


# ── overview ──────────────────────────────────────────────────────────────────
def overview(db: Session, online: Callable[[str], bool] | None = None) -> dict[str, Any]:
    online = _online(online)
    states = dict(db.query(Device.state, func.count(Device.id)).group_by(Device.state).all())
    claimed = db.query(Device).filter_by(state="CLAIMED").all()
    return {"customers": db.query(Household).count(),
            "pending": db.query(User).filter_by(role="owner",
                                                status="pending_activation").count(),
            "device_states": states, "online": sum(1 for d in claimed if online(d.id)),
            "claimed": len(claimed),
            "operators": db.query(User).filter_by(role="operator").count(),
            "chain": security.verify_audit_chain(db)}


# ── customers ─────────────────────────────────────────────────────────────────
def customers(db: Session, q: str = "") -> list[dict[str, Any]]:
    query = db.query(Household).order_by(Household.created_at.desc())
    if q:
        like = f"%{q.strip()[:40]}%"
        owners = db.query(User.household_id).filter(User.username.ilike(like))
        query = query.filter(or_(Household.customer_code.ilike(like),
                                 Household.display_name.ilike(like),
                                 Household.city.ilike(like), Household.id.in_(owners)))
    rows = []
    for household in query.limit(200):
        owner = accounts.household_owner(db, household.id)
        rows.append({"household": household, "owner": owner,
                     "devices": len(accounts.household_devices(db, household.id))})
    return rows


def unassigned_devices(db: Session) -> list[Device]:
    return db.query(Device).filter_by(state="UNASSIGNED").order_by(Device.serial).all()


def customer_of(db: Session, household_id: str) -> tuple[Household | None, User | None]:
    household = db.get(Household, household_id)
    if household is None:
        return None, None
    return household, accounts.household_owner(db, household.id)


def create_customer(db: Session, principal: security.Principal, form: Form) -> Outcome:
    """A household and its owner (``pending_activation``), optionally with an
    unassigned device pre-assigned. ``data["household"]``."""
    errors = accounts.FormErrors()
    name = accounts.clean_text(errors, "name", form.get("name"), label="Customer name",
                               max_len=80, required=True, min_len=2)
    username = accounts.clean_username(errors, "username", form.get("username"))
    email = accounts.clean_email(errors, "email", form.get("email"), required=True)
    phone = accounts.clean_phone(errors, "phone", form.get("phone"), label="Mobile",
                                 required=False)
    city = accounts.clean_text(errors, "city", form.get("city"), label="City", max_len=80)
    serial = accounts.normalize_serial(form.get("serial"))
    device = db.query(Device).filter_by(serial=serial).first() if serial else None
    if username and accounts.username_taken(db, username):
        errors.add("username", "That username is taken.")
    if serial and (device is None or device.state != "UNASSIGNED"):
        errors.add("serial", "Choose an unassigned device (or none).")
    if errors:
        return invalid(errors)
    household, _ = accounts.create_customer(db, name=name, username=username, email=email,
                                            phone=phone, city=city,
                                            created_by=principal.user_id)
    if device is not None:
        accounts.assign_device(db, device, household, actor_id=principal.user_id)
    return done(NOTICES["created"], household=household)


def customer_detail(db: Session, household: Household, owner: User | None,
                    online: Callable[[str], bool] | None = None) -> dict[str, Any]:
    online = _online(online)
    devices = accounts.household_devices(db, household.id)
    events = (db.query(AuditLog).filter_by(household_id=household.id)
              .filter(AuditLog.event.like("admin.%") | AuditLog.event.like("auth.%")
                      | AuditLog.event.like("device.%"))
              .order_by(AuditLog.id.desc()).limit(30).all())
    return {"household": household, "owner": owner,
            "masked_email": mask_email(owner.email) if owner else "",
            "masked_phone": mask_phone(accounts.user_phone(owner)) if owner else "",
            "devices": [{"device": d, "online": d.state == "CLAIMED" and bool(online(d.id))}
                        for d in devices],
            "unassigned": unassigned_devices(db), "events": events,
            "members": db.query(User).filter_by(household_id=household.id,
                                                role="member").count()}


def resend_activation(db: Session, principal: security.Principal, household: Household,
                      owner: User | None, *, ip: str | None) -> Outcome:
    """"Reset": a new code by e-mail to the customer only (D14). For an
    account that was never activated it replaces the letter's code."""
    if owner is None or owner.status not in ("pending_activation", "active") or not owner.email:
        return failed("conflict", CANNOT_SEND, status=409)
    purpose = "activate" if owner.status == "pending_activation" else "reset"
    queued = accounts.send_activation_email(db, owner, purpose=purpose,
                                            created_by=principal.user_id)
    security.audit(db, "admin.activation_resent", actor_type="admin",
                   actor_id=principal.user_id, household_id=household.id,
                   target_type="user", target_id=owner.id, ip=ip,
                   detail={"purpose": purpose, "queued": bool(queued)})
    key = "resent" if queued else "not_queued"
    return done(NOTICES[key], done=key, queued=bool(queued), purpose=purpose)


def set_customer_status(db: Session, principal: security.Principal, household: Household,
                        action: Any, *, ip: str | None) -> Outcome:
    if action not in ("deactivate", "reactivate") or household.status not in ("active",
                                                                              "disabled"):
        return failed("conflict", "not possible in this state", status=409)
    accounts.set_household_status(db, household,
                                  "disabled" if action == "deactivate" else "active",
                                  actor_id=principal.user_id, ip=ip)
    return done(NOTICES[f"{action}d"], done=f"{action}d")


def assign_device(db: Session, principal: security.Principal, device: Device,
                  household: Household) -> Outcome:
    """Pre-assignment at the point of sale (from either the customer's or
    the device's page)."""
    try:
        accounts.assign_device(db, device, household, actor_id=principal.user_id)
    except ValueError as exc:
        return failed("conflict", str(exc), status=409)
    return done(NOTICES["assigned"], done="assigned")


def device_by_serial(db: Session, serial: Any) -> Device | None:
    return db.query(Device).filter_by(serial=accounts.normalize_serial(serial)).first()


def customer_by_code(db: Session, code: Any) -> Household | None:
    code = (code if isinstance(code, str) else "").strip().upper()
    return db.query(Household).filter_by(customer_code=code).first() if code else None


def letter_state(db: Session, household: Household, owner: User | None) -> dict[str, Any]:
    devices = [d for d in accounts.household_devices(db, household.id) if d.state == "ASSIGNED"]
    return {"household": household, "owner": owner, "devices": devices,
            "printable": owner is not None and owner.status == "pending_activation"
            and household.status == "active"}


def print_letter(db: Session, principal: security.Principal, household: Household,
                 owner: User | None, *, new_claim_codes: bool, ip: str | None) -> Outcome:
    """The printable welcome letter: Portal URL, username, a fresh single-use
    activation code, and the assigned devices' serials with a claim code
    (new unless the label's code is kept). ``data``: ``code``,
    ``activation_url``, ``base_url``, ``ttl_hours`` and ``devices``
    (``serial``, ``claim_code`` or None, ``claim_url``, ``qr_url``: what
    the claim QR encodes)."""
    if owner is None or owner.status != "pending_activation" or household.status != "active":
        return failed("conflict", NO_LETTER, status=409)
    code, _ = accounts.issue_activation_code(db, owner, purpose="activate",
                                             created_by=principal.user_id)
    base = accounts.portal_config().base_url
    devices = []
    for device in accounts.household_devices(db, household.id):
        if device.state != "ASSIGNED":
            continue
        claim_code = (accounts.rotate_claim_code(db, device, actor_id=principal.user_id)
                      if new_claim_codes else None)
        claim_url = f"{base}/claim?sn={device.serial}"
        devices.append({"serial": device.serial, "claim_code": claim_code,
                        "claim_url": claim_url,
                        "qr_url": claim_url + (f"#code={claim_code}" if claim_code else "")})
    security.audit(db, "admin.letter_printed", actor_type="admin", actor_id=principal.user_id,
                   household_id=household.id, target_type="user", target_id=owner.id, ip=ip,
                   detail={"devices": [d["serial"] for d in devices],
                           "new_claim_codes": bool(new_claim_codes)})
    return done(code=code, base_url=base, activation_url=accounts.activation_url(code),
                devices=devices,
                ttl_hours=accounts.portal_config().activation_ttl_seconds // 3600)


# ── devices ───────────────────────────────────────────────────────────────────
def devices(db: Session, state: str = "",
            online: Callable[[str], bool] | None = None) -> list[dict[str, Any]]:
    online = _online(online)
    query = db.query(Device).order_by(Device.serial)
    if state in DEVICE_STATES:
        query = query.filter_by(state=state)
    rows = []
    for device in query.limit(500):
        household = db.get(Household, device.household_id) if device.household_id else None
        heartbeat = device.last_heartbeat or {}
        rows.append({"device": device, "household": household,
                     "online": device.state == "CLAIMED" and bool(online(device.id)),
                     "ai_hz": heartbeat.get("ai_hz"),
                     "profile_behind": bool(household and device.state == "CLAIMED" and
                                            (device.profile_version_acked or 0)
                                            < (household.profile_version or 0))})
    return rows


def label(device: Device, *, claim_code: str | None, setup_code: str | None = None,
          identity: dict | None = None, title: str = "Device label") -> dict[str, Any]:
    """What a printed label shows (codes only when just made)."""
    base = accounts.portal_config().base_url
    claim_url = f"{base}/claim?sn={device.serial}"
    identity_json = json.dumps(identity, indent=2) if identity else None
    return {"device": device, "claim_code": claim_code, "setup_code": setup_code,
            "claim_url": claim_url, "title": title,
            "qr_url": claim_url + (f"#code={claim_code}" if claim_code else ""),
            "identity": identity, "identity_json": identity_json,
            "identity_b64": (base64.b64encode(identity_json.encode()).decode()
                             if identity_json else None)}


def provision(db: Session, principal: security.Principal, form: Form) -> Outcome:
    """A new UNASSIGNED device; ``data["label"]`` with the claim code, the
    Wi-Fi setup code and ``identity.json`` (shown once)."""
    errors = accounts.FormErrors()
    name = accounts.clean_text(errors, "name", form.get("name"), label="Name", max_len=60)
    if errors:
        return invalid(errors)
    try:
        provisioned = accounts.provision_device(db, serial=form.get("serial") or None,
                                                name=name, created_by=principal.user_id)
    except ValueError as exc:
        errors.add("serial", str(exc))
        return invalid(errors)
    return done(label=label(provisioned.device, claim_code=provisioned.claim_code,
                            setup_code=provisioned.setup_code, identity=provisioned.identity,
                            title="Device provisioned"))


def reprint_label(db: Session, principal: security.Principal, device: Device) -> Outcome:
    try:
        code = accounts.rotate_claim_code(db, device, actor_id=principal.user_id)
    except ValueError as exc:
        return failed("conflict", str(exc), status=409)
    return done(label=label(device, claim_code=code, title="Label reprinted"))


async def _unpair_if_claimed(db: Session, device: Device, principal) -> None:
    if device.state != "CLAIMED":
        return
    from sentrix_portal.services import hub
    try:
        await hub.unpair(db, device.id, cloud_actor(principal))
    except Exception as exc:                          # the token is revoked below anyway
        accounts.log.error("unpair of %s failed: %s", device.id, exc)


async def transfer(db: Session, principal: security.Principal, device: Device) -> Outcome:
    """Back to UNASSIGNED with a new claim code (a claimed device is unpaired
    first: it wipes its token and profile)."""
    if device.state == "RETIRED":
        return failed("conflict", "retired", status=409)
    previous = device.household_id
    await _unpair_if_claimed(db, device, principal)
    code = accounts.release_device(db, device, actor_id=principal.user_id)
    if previous and db.get(Household, previous) is not None:
        accounts.profile_changed(db, previous, actor_id=principal.user_id, actor_type="admin",
                                 what="device_transferred")
    return done(label=label(device, claim_code=code, title="Device transferred"))


async def retire(db: Session, principal: security.Principal, device: Device) -> Outcome:
    await _unpair_if_claimed(db, device, principal)
    accounts.release_device(db, device, actor_id=principal.user_id, retire=True)
    return done(NOTICES["retired"], done="retired")


# ── staff ─────────────────────────────────────────────────────────────────────
def staff(db: Session) -> list[User]:
    return (db.query(User).filter(User.role.in_(accounts.STAFF_ROLES))
            .order_by(User.role, User.username).all())


def create_staff(db: Session, principal: security.Principal, form: Form) -> Outcome:
    """A Desk operator (or another admin). The activation code is shown once
    to hand over in person, and e-mailed when an address is given; TOTP is
    set up at the first sign-in. ``data``: ``user``, ``code``,
    ``activation_url``, ``ttl_hours``."""
    errors = accounts.FormErrors()
    role = accounts.clean_choice(errors, "role", form.get("role") or "operator",
                                 accounts.STAFF_ROLES, label="Role")
    username = accounts.clean_username(errors, "username", form.get("username"))
    name = accounts.clean_text(errors, "display_name", form.get("display_name"),
                               label="Name", max_len=80, required=True, min_len=2)
    email = accounts.clean_email(errors, "email", form.get("email"))
    if username and accounts.username_taken(db, username):
        errors.add("username", "That username is taken.")
    if errors:
        return invalid(errors)
    user = accounts.create_staff_user(db, role=role, username=username, display_name=name,
                                      email=email, created_by=principal.user_id)
    code, row = accounts.issue_activation_code(db, user, created_by=principal.user_id)
    if email:
        subject, text = accounts.activation_text(user, code, purpose="activate")
        accounts.email_user(db, user, template="account.activation",
                            step=f"activation:{row.id}", subject=subject, text=text)
    return done(user=user, code=code, activation_url=accounts.activation_url(code),
                ttl_hours=accounts.portal_config().activation_ttl_seconds // 3600)


def staff_of(db: Session, user_id: str) -> User | None:
    user = db.get(User, user_id)
    return user if user is not None and user.role in accounts.STAFF_ROLES else None


def update_staff(db: Session, principal: security.Principal, user: User, action: Any, *,
                 ip: str | None) -> Outcome:
    if user.id == principal.user_id:
        return failed("conflict", "use your own account page", status=409)
    if action == "disable" and user.status in ("active", "pending_activation"):
        user.status = "disabled"
        security.revoke_user_sessions(db, user.id)
    elif action == "enable" and user.status == "disabled":
        user.status = "active" if user.password_hash else "pending_activation"
    elif action == "reset_2fa":
        user.totp_enabled, user.totp_secret_enc, user.recovery_codes = False, None, []
        security.revoke_user_sessions(db, user.id)
    elif action == "resend" and user.email and user.status in ("pending_activation", "active"):
        accounts.send_activation_email(
            db, user, purpose="activate" if user.status == "pending_activation" else "reset",
            created_by=principal.user_id)
    else:
        return failed("conflict", "not possible for this account", status=409)
    security.audit(db, f"admin.staff_{action}", actor_type="admin", actor_id=principal.user_id,
                   target_type="user", target_id=user.id, ip=ip)
    return done(NOTICES["staff_updated"], done="staff_updated")


# ── audit ─────────────────────────────────────────────────────────────────────
def audit_rows(db: Session, *, event: str = "", household: str = "", offset: int = 0,
               limit: int = AUDIT_PAGE_SIZE, before_id: int | None = None) -> list[AuditLog]:
    """Newest first; ``limit + 1`` rows tell whether there are more."""
    query = db.query(AuditLog).order_by(AuditLog.id.desc())
    if event:
        query = query.filter(AuditLog.event.like(f"{event.strip()[:40]}%"))
    if household:
        query = query.filter(AuditLog.household_id == household.strip()[:36])
    if before_id is not None:
        query = query.filter(AuditLog.id < before_id)
    return query.offset(offset).limit(limit).all()


__all__ = ["AUDIT_PAGE_SIZE", "DEVICE_STATES", "STAFF_ACTIONS", "NOTICES", "NO_LETTER",
           "CANNOT_SEND", "NO_SUCH_CUSTOMER", "cloud_actor", "overview", "customers",
           "unassigned_devices", "customer_of", "create_customer", "customer_detail",
           "resend_activation", "set_customer_status", "assign_device", "device_by_serial",
           "customer_by_code", "letter_state", "print_letter", "devices", "label",
           "provision", "reprint_label", "transfer", "retire", "staff", "create_staff",
           "staff_of", "update_staff", "audit_rows"]
