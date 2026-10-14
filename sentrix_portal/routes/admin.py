"""Company admin console (company_admin + TOTP): /admin (overview),
/admin/customers (create, deactivate, re-send activation, welcome letter
print view, device assignment), /admin/devices (provision, label with QR,
assign, transfer, retire, fleet status from the link hub), /admin/operators
(Desk operators and admins), /admin/audit (the hash-chained audit log).

Owner: track T-ACCT. The admin never sees a customer credential, video,
faces or evidence (docs/7 §10, D14):

* a customer account has no password until the customer chooses one; the
  welcome letter carries a single-use activation code that exists in clear
  only in the letter's own response (``Cache-Control: no-store``), and only
  while the account was never activated;
* after activation the admin can only send a reset link to the customer's
  e-mail; the code goes to the outbox, never to the admin's screen;
* claim codes and bootstrap secrets are shown once, on the label printed at
  provisioning (or a reprint, which replaces the claim code); the Portal
  keeps only their HMAC digests;
* no page here links to ``/d/{device_id}`` (the tunnel refuses company_admin
  anyway) or shows incident media.
"""

from __future__ import annotations

import base64
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from sentrix.actor import Actor
from sentrix.utils.redact import mask_email, mask_phone
from sentrix_portal import security
from sentrix_portal.db import get_db
from sentrix_portal.models import AuditLog, Device, Household, User
from sentrix_portal.routes.auth import message_page, qr_svg, render, see_other
from sentrix_portal.services import accounts

router = APIRouter(prefix="/admin")

ADMIN = security.require_role("company_admin")
NO_STORE = {"Cache-Control": "no-store"}
AUDIT_PAGE_SIZE = 100
NOTICES = {"created": "Customer created. Print the welcome letter next.",
           "assigned": "Device assigned.", "deactivated": "Customer deactivated.",
           "reactivated": "Customer reactivated.", "resent": "E-mail queued.",
           "not_queued": "The e-mail could not be queued (no e-mail channel). Try again "
                         "once the e-mail channel runs.",
           "staff_updated": "Saved.", "retired": "Device retired."}


def _cloud_actor(principal) -> dict:
    """Commands from company staff reach the device as the Portal itself."""
    return Actor("cloud", id=principal.user_id, name="SENTRIX admin").to_dict()


def _customer(db: Session, household_id: str) -> tuple[Household, User | None]:
    household = db.get(Household, household_id)
    if household is None:
        raise HTTPException(404, "no such customer")
    return household, accounts.household_owner(db, household.id)


def _device(db: Session, device_id: str) -> Device:
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(404, "no such device")
    return device


# ── overview ──────────────────────────────────────────────────────────────────
@router.get("", response_class=HTMLResponse)
def admin_index(request: Request, db: Session = Depends(get_db), principal=Depends(ADMIN)):
    from sentrix_portal.services import hub
    states = dict(db.query(Device.state, func.count(Device.id)).group_by(Device.state).all())
    claimed = db.query(Device).filter_by(state="CLAIMED").all()
    return render(request, "admin/index.html", {
        "customers": db.query(Household).count(),
        "pending": db.query(User).filter_by(role="owner", status="pending_activation").count(),
        "device_states": states, "online": sum(1 for d in claimed if hub.is_online(d.id)),
        "claimed": len(claimed),
        "operators": db.query(User).filter_by(role="operator").count(),
        "chain": security.verify_audit_chain(db),
    }, active_page="admin")


# ── customers ─────────────────────────────────────────────────────────────────
def _customers_page(request, db, *, q: str = "", errors=None, values=None,
                    status_code=200, notice=None):
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
    return render(request, "admin/customers.html", {
        "rows": rows, "q": q, "errors": errors or {}, "values": values or {},
        "notice": notice,
        "unassigned": db.query(Device).filter_by(state="UNASSIGNED").order_by(Device.serial).all(),
    }, status_code=status_code, active_page="admin")


@router.get("/customers", response_class=HTMLResponse)
def customers_page(request: Request, q: str = "", db: Session = Depends(get_db),
                   principal=Depends(ADMIN)):
    return _customers_page(request, db, q=q)


@router.post("/customers", response_class=HTMLResponse)
async def customers_create(request: Request, db: Session = Depends(get_db),
                           principal=Depends(ADMIN)):
    form = await request.form()
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
        return _customers_page(request, db, errors=errors, values=dict(form), status_code=400)
    household, _ = accounts.create_customer(db, name=name, username=username, email=email,
                                            phone=phone, city=city,
                                            created_by=principal.user_id)
    if device is not None:
        accounts.assign_device(db, device, household, actor_id=principal.user_id)
    return see_other(f"/admin/customers/{household.id}?done=created")


@router.get("/customers/{household_id}", response_class=HTMLResponse)
def customer_page(request: Request, household_id: str, done: str = "",
                  db: Session = Depends(get_db), principal=Depends(ADMIN)):
    from sentrix_portal.services import hub
    household, owner = _customer(db, household_id)
    devices = accounts.household_devices(db, household.id)
    events = (db.query(AuditLog).filter_by(household_id=household.id)
              .filter(AuditLog.event.like("admin.%") | AuditLog.event.like("auth.%")
                      | AuditLog.event.like("device.%"))
              .order_by(AuditLog.id.desc()).limit(30).all())
    return render(request, "admin/customer.html", {
        "household": household, "owner": owner, "notice": NOTICES.get(done),
        "masked_email": mask_email(owner.email) if owner else "",
        "masked_phone": mask_phone(accounts.user_phone(owner)) if owner else "",
        "devices": [{"device": d, "online": d.state == "CLAIMED" and bool(hub.is_online(d.id))}
                    for d in devices],
        "unassigned": db.query(Device).filter_by(state="UNASSIGNED").order_by(Device.serial).all(),
        "events": events, "members": db.query(User).filter_by(household_id=household.id,
                                                             role="member").count(),
    }, active_page="admin")


@router.post("/customers/{household_id}/resend")
def customer_resend(request: Request, household_id: str, db: Session = Depends(get_db),
                    principal=Depends(ADMIN)):
    """"Reset": a new code by e-mail to the customer only (D14). For an
    account that was never activated it replaces the letter's code."""
    household, owner = _customer(db, household_id)
    if owner is None or owner.status not in ("pending_activation", "active") or not owner.email:
        return message_page(request, "Cannot send", "This customer has no active account "
                            "with an e-mail address.", status_code=409,
                            link=(f"/admin/customers/{household.id}", "Back"),
                            active_page="admin")
    purpose = "activate" if owner.status == "pending_activation" else "reset"
    queued = accounts.send_activation_email(db, owner, purpose=purpose,
                                            created_by=principal.user_id)
    security.audit(db, "admin.activation_resent", actor_type="admin",
                   actor_id=principal.user_id, household_id=household.id,
                   target_type="user", target_id=owner.id, ip=security.client_ip(request),
                   detail={"purpose": purpose, "queued": bool(queued)})
    return see_other(f"/admin/customers/{household.id}?done={'resent' if queued else 'not_queued'}")


@router.post("/customers/{household_id}/status")
async def customer_status(request: Request, household_id: str, db: Session = Depends(get_db),
                          principal=Depends(ADMIN)):
    household, _ = _customer(db, household_id)
    form = await request.form()
    action = form.get("action")
    if action not in ("deactivate", "reactivate") or household.status not in ("active",
                                                                              "disabled"):
        raise HTTPException(409, "not possible in this state")
    accounts.set_household_status(db, household,
                                  "disabled" if action == "deactivate" else "active",
                                  actor_id=principal.user_id, ip=security.client_ip(request))
    return see_other(f"/admin/customers/{household.id}?done={action}d")


@router.post("/customers/{household_id}/assign")
async def customer_assign(request: Request, household_id: str, db: Session = Depends(get_db),
                          principal=Depends(ADMIN)):
    household, _ = _customer(db, household_id)
    form = await request.form()
    device = db.query(Device).filter_by(serial=accounts.normalize_serial(form.get("serial"))).first()
    if device is None:
        raise HTTPException(404, "no such device")
    try:
        accounts.assign_device(db, device, household, actor_id=principal.user_id)
    except ValueError as exc:
        return message_page(request, "Cannot assign", str(exc), status_code=409,
                            link=(f"/admin/customers/{household.id}", "Back"),
                            active_page="admin")
    return see_other(f"/admin/customers/{household.id}?done=assigned")


@router.get("/customers/{household_id}/letter", response_class=HTMLResponse)
def letter_confirm(request: Request, household_id: str, db: Session = Depends(get_db),
                   principal=Depends(ADMIN)):
    household, owner = _customer(db, household_id)
    devices = [d for d in accounts.household_devices(db, household.id) if d.state == "ASSIGNED"]
    return render(request, "admin/letter_confirm.html", {
        "household": household, "owner": owner, "devices": devices,
        "printable": owner is not None and owner.status == "pending_activation"
        and household.status == "active"}, active_page="admin")


@router.post("/customers/{household_id}/letter", response_class=HTMLResponse)
async def letter_print(request: Request, household_id: str, db: Session = Depends(get_db),
                       principal=Depends(ADMIN)):
    """The printable welcome letter: Portal URL, username, a fresh single-use
    activation code with its QR, and the assigned devices' serials with a
    claim code (new unless the label's code is kept) and the claim QR."""
    household, owner = _customer(db, household_id)
    if owner is None or owner.status != "pending_activation" or household.status != "active":
        return message_page(request, "No letter for an activated account",
                            "This customer already chose a password: a letter would need a "
                            "credential the company must never see. Use \"Send reset "
                            "e-mail\" instead.", status_code=409,
                            link=(f"/admin/customers/{household.id}", "Back"),
                            active_page="admin")
    form = await request.form()
    code, _ = accounts.issue_activation_code(db, owner, purpose="activate",
                                             created_by=principal.user_id)
    base = accounts.portal_config().base_url
    devices = []
    for device in accounts.household_devices(db, household.id):
        if device.state != "ASSIGNED":
            continue
        claim_code = (accounts.rotate_claim_code(db, device, actor_id=principal.user_id)
                      if form.get("new_claim_codes") else None)
        claim_url = f"{base}/claim?sn={device.serial}"
        devices.append({"serial": device.serial, "claim_code": claim_code,
                        "qr": qr_svg(claim_url + (f"#code={claim_code}" if claim_code else "")),
                        "claim_url": claim_url})
    security.audit(db, "admin.letter_printed", actor_type="admin", actor_id=principal.user_id,
                   household_id=household.id, target_type="user", target_id=owner.id,
                   ip=security.client_ip(request),
                   detail={"devices": [d["serial"] for d in devices],
                           "new_claim_codes": bool(form.get("new_claim_codes"))})
    return render(request, "admin/letter.html", {
        "household": household, "owner": owner, "code": code, "base_url": base,
        "activation_url": accounts.activation_url(code),
        "activation_qr": qr_svg(accounts.activation_url(code)), "devices": devices,
        "ttl_hours": accounts.portal_config().activation_ttl_seconds // 3600,
    }, headers=NO_STORE, active_page="admin")


# ── devices ───────────────────────────────────────────────────────────────────
def _devices_page(request, db, *, errors=None, values=None, status_code=200, notice=None,
                  state: str = ""):
    from sentrix_portal.services import hub
    query = db.query(Device).order_by(Device.serial)
    if state in ("UNASSIGNED", "ASSIGNED", "CLAIMED", "RETIRED"):
        query = query.filter_by(state=state)
    rows = []
    for device in query.limit(500):
        household = db.get(Household, device.household_id) if device.household_id else None
        heartbeat = device.last_heartbeat or {}
        rows.append({"device": device, "household": household,
                     "online": device.state == "CLAIMED" and bool(hub.is_online(device.id)),
                     "ai_hz": heartbeat.get("ai_hz"),
                     "profile_behind": bool(household and device.state == "CLAIMED" and
                                            (device.profile_version_acked or 0)
                                            < (household.profile_version or 0))})
    return render(request, "admin/devices.html", {
        "rows": rows, "errors": errors or {}, "values": values or {}, "notice": notice,
        "state": state, "next_serial": accounts.next_serial(db),
    }, status_code=status_code, active_page="admin")


@router.get("/devices", response_class=HTMLResponse)
def devices_page(request: Request, state: str = "", done: str = "",
                 db: Session = Depends(get_db), principal=Depends(ADMIN)):
    return _devices_page(request, db, state=state, notice=NOTICES.get(done))


def _label(request, *, device: Device, claim_code: str | None,
           setup_code: str | None = None, identity: dict | None = None,
           title: str = "Device label") -> HTMLResponse:
    base = accounts.portal_config().base_url
    claim_url = f"{base}/claim?sn={device.serial}"
    identity_json = json.dumps(identity, indent=2) if identity else None
    return render(request, "admin/label.html", {
        "device": device, "claim_code": claim_code, "setup_code": setup_code,
        "claim_url": claim_url, "title": title,
        "qr": qr_svg(claim_url + (f"#code={claim_code}" if claim_code else "")),
        "identity_json": identity_json,
        "identity_b64": base64.b64encode(identity_json.encode()).decode() if identity_json else None,
    }, headers=NO_STORE, active_page="admin")


@router.post("/devices", response_class=HTMLResponse)
async def devices_provision(request: Request, db: Session = Depends(get_db),
                            principal=Depends(ADMIN)):
    form = await request.form()
    errors = accounts.FormErrors()
    name = accounts.clean_text(errors, "name", form.get("name"), label="Name", max_len=60)
    if errors:
        return _devices_page(request, db, errors=errors, values=dict(form), status_code=400)
    try:
        provisioned = accounts.provision_device(db, serial=form.get("serial") or None,
                                                name=name, created_by=principal.user_id)
    except ValueError as exc:
        errors.add("serial", str(exc))
        return _devices_page(request, db, errors=errors, values=dict(form), status_code=400)
    return _label(request, device=provisioned.device, claim_code=provisioned.claim_code,
                  setup_code=provisioned.setup_code, identity=provisioned.identity,
                  title="Device provisioned")


@router.get("/devices/{device_id}/label", response_class=HTMLResponse)
def label_confirm(request: Request, device_id: str, db: Session = Depends(get_db),
                  principal=Depends(ADMIN)):
    device = _device(db, device_id)
    return render(request, "admin/label_confirm.html", {
        "device": device, "reprintable": device.state in ("UNASSIGNED", "ASSIGNED")},
        active_page="admin")


@router.post("/devices/{device_id}/label", response_class=HTMLResponse)
def label_reprint(request: Request, device_id: str, db: Session = Depends(get_db),
                  principal=Depends(ADMIN)):
    device = _device(db, device_id)
    try:
        code = accounts.rotate_claim_code(db, device, actor_id=principal.user_id)
    except ValueError as exc:
        return message_page(request, "Cannot reprint", str(exc), status_code=409,
                            link=("/admin/devices", "Back"), active_page="admin")
    return _label(request, device=device, claim_code=code, title="Label reprinted")


@router.post("/devices/{device_id}/assign")
async def device_assign(request: Request, device_id: str, db: Session = Depends(get_db),
                        principal=Depends(ADMIN)):
    device = _device(db, device_id)
    form = await request.form()
    code = (form.get("customer_code") or "").strip().upper()
    household = db.query(Household).filter_by(customer_code=code).first() if code else None
    if household is None:
        return message_page(request, "No such customer", "Check the customer code (SX-0001).",
                            status_code=404, link=("/admin/devices", "Back"),
                            active_page="admin")
    try:
        accounts.assign_device(db, device, household, actor_id=principal.user_id)
    except ValueError as exc:
        return message_page(request, "Cannot assign", str(exc), status_code=409,
                            link=("/admin/devices", "Back"), active_page="admin")
    return see_other("/admin/devices?done=assigned")


async def _unpair_if_claimed(db: Session, device: Device, principal) -> None:
    if device.state != "CLAIMED":
        return
    from sentrix_portal.services import hub
    try:
        await hub.unpair(db, device.id, _cloud_actor(principal))
    except Exception as exc:                          # the token is revoked below anyway
        accounts.log.error("unpair of %s failed: %s", device.id, exc)


@router.post("/devices/{device_id}/transfer", response_class=HTMLResponse)
async def device_transfer(request: Request, device_id: str, db: Session = Depends(get_db),
                          principal=Depends(ADMIN)):
    """Back to UNASSIGNED with a new claim code (a claimed device is unpaired
    first: it wipes its token and profile)."""
    device = _device(db, device_id)
    if device.state == "RETIRED":
        raise HTTPException(409, "retired")
    previous = device.household_id
    await _unpair_if_claimed(db, device, principal)
    code = accounts.release_device(db, device, actor_id=principal.user_id)
    if previous and db.get(Household, previous) is not None:
        accounts.profile_changed(db, previous, actor_id=principal.user_id, actor_type="admin",
                                 what="device_transferred")
    return _label(request, device=device, claim_code=code, title="Device transferred")


@router.post("/devices/{device_id}/retire")
async def device_retire(request: Request, device_id: str, db: Session = Depends(get_db),
                        principal=Depends(ADMIN)):
    device = _device(db, device_id)
    await _unpair_if_claimed(db, device, principal)
    accounts.release_device(db, device, actor_id=principal.user_id, retire=True)
    return see_other("/admin/devices?done=retired")


# ── operators and admins ──────────────────────────────────────────────────────
def _staff_page(request, db, principal, *, errors=None, values=None, status_code=200,
                notice=None):
    staff = (db.query(User).filter(User.role.in_(accounts.STAFF_ROLES))
             .order_by(User.role, User.username).all())
    return render(request, "admin/operators.html", {
        "staff": staff, "errors": errors or {}, "values": values or {}, "notice": notice,
        "me": principal.user_id, "roles": accounts.STAFF_ROLES,
    }, status_code=status_code, active_page="admin")


@router.get("/operators", response_class=HTMLResponse)
def operators_page(request: Request, done: str = "", db: Session = Depends(get_db),
                   principal=Depends(ADMIN)):
    return _staff_page(request, db, principal, notice=NOTICES.get(done))


@router.post("/operators", response_class=HTMLResponse)
async def operators_create(request: Request, db: Session = Depends(get_db),
                           principal=Depends(ADMIN)):
    """A Desk operator (or another admin). Company staff: the activation code
    is shown once to hand over in person, and e-mailed when an address is
    given; TOTP is set up at the first sign-in."""
    form = await request.form()
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
        return _staff_page(request, db, principal, errors=errors, values=dict(form),
                           status_code=400)
    user = accounts.create_staff_user(db, role=role, username=username, display_name=name,
                                      email=email, created_by=principal.user_id)
    code, row = accounts.issue_activation_code(db, user, created_by=principal.user_id)
    if email:
        subject, text = accounts.activation_text(user, code, purpose="activate")
        accounts.email_user(db, user, template="account.activation",
                            step=f"activation:{row.id}", subject=subject, text=text)
    return render(request, "admin/staff_code.html", {
        "user": user, "code": code, "activation_url": accounts.activation_url(code),
        "ttl_hours": accounts.portal_config().activation_ttl_seconds // 3600,
    }, headers=NO_STORE, active_page="admin")


@router.post("/operators/{user_id}")
async def operators_update(request: Request, user_id: str, db: Session = Depends(get_db),
                           principal=Depends(ADMIN)):
    user = db.get(User, user_id)
    if user is None or user.role not in accounts.STAFF_ROLES:
        raise HTTPException(404, "no such staff account")
    if user.id == principal.user_id:
        raise HTTPException(409, "use your own account page")
    form = await request.form()
    action = form.get("action")
    ip = security.client_ip(request)
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
        raise HTTPException(409, "not possible for this account")
    security.audit(db, f"admin.staff_{action}", actor_type="admin", actor_id=principal.user_id,
                   target_type="user", target_id=user.id, ip=ip)
    return see_other("/admin/operators?done=staff_updated")


# ── audit ─────────────────────────────────────────────────────────────────────
@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, page: int = 1, event: str = "", household: str = "",
               db: Session = Depends(get_db), principal=Depends(ADMIN)):
    page = max(1, min(page, 100_000))
    query = db.query(AuditLog).order_by(AuditLog.id.desc())
    if event:
        query = query.filter(AuditLog.event.like(f"{event.strip()[:40]}%"))
    if household:
        query = query.filter(AuditLog.household_id == household.strip()[:36])
    rows = query.offset((page - 1) * AUDIT_PAGE_SIZE).limit(AUDIT_PAGE_SIZE + 1).all()
    return render(request, "admin/audit.html", {
        "rows": rows[:AUDIT_PAGE_SIZE], "more": len(rows) > AUDIT_PAGE_SIZE, "page": page,
        "event": event, "household": household, "chain": security.verify_audit_chain(db),
    }, active_page="audit")


__all__ = ["router"]
