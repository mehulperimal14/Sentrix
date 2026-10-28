"""Company admin console (API v2, company_admin + TOTP): the overview,
customers (create, deactivate, re-send activation, assign a device, the
welcome letter as data), the device fleet (provision, label as data,
assign, transfer, retire), staff accounts and the audit chain.

The admin never sees a customer credential, video, faces or evidence: an
activation code is returned only by the letter that makes it (and only for
an account never activated), claim codes and the device's ``identity.json``
only by the provisioning / reprint / transfer that makes them. The app
draws QR codes from the URLs returned (``qr_url``, ``activation_url``).
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from sentrix.utils.redact import mask_email, mask_phone
from sentrix_portal import security
from sentrix_portal.db import DB
from sentrix_portal.models import Device, Household, User
from sentrix_portal.routes.api_v2.common import (ADMIN, ApiError, Ok, clean_detail,
                                                 form_values, ip_of, raise_for, z)
from sentrix_portal.services import accounts
from sentrix_portal.services import admin_flows as flows

router = APIRouter(prefix="/admin", tags=["admin"])
StaffRole = Literal["operator", "company_admin"]


# ── models ────────────────────────────────────────────────────────────────────
class ChainOut(BaseModel):
    ok: bool
    entries: int
    problems: list[int]
    retention_gaps: list[dict[str, Any]] = []


class OverviewOut(BaseModel):
    customers: int
    #: Owners who have not activated yet.
    pending: int
    device_states: dict[str, int]
    online: int
    claimed: int
    operators: int
    chain: ChainOut


class DeviceRef(BaseModel):
    id: str
    serial: str


class CustomerRow(BaseModel):
    id: str
    customer_code: str | None
    name: str | None
    city: str | None
    status: str | None
    owner_username: str | None
    owner_status: str | None
    devices: int
    setup_done: bool
    setup_step: int


class CustomersOut(BaseModel):
    rows: list[CustomerRow]
    unassigned: list[DeviceRef]


class CustomerIn(BaseModel):
    name: str | None = None
    username: str | None = None
    email: str | None = None
    phone: str | None = None
    city: str | None = None
    #: An unassigned device to pre-assign (optional).
    serial: str | None = None


class OwnerOut(BaseModel):
    id: str
    username: str
    display_name: str | None
    status: str | None
    email_masked: str | None
    phone_masked: str | None
    last_login_at: str | None


class CustomerDevice(BaseModel):
    id: str
    serial: str
    name: str | None
    state: str
    online: bool


class AccountEvent(BaseModel):
    id: int
    at: str | None
    event: str
    actor_type: str | None


class CustomerOut(BaseModel):
    id: str
    customer_code: str | None
    name: str | None
    city: str | None
    status: str | None
    setup_done: bool
    setup_step: int
    owner: OwnerOut | None
    members: int
    devices: list[CustomerDevice]
    unassigned: list[DeviceRef]
    events: list[AccountEvent]
    message: str | None = None


class StatusIn(BaseModel):
    action: Literal["deactivate", "reactivate"] | None = None


class SerialIn(BaseModel):
    serial: str | None = None


class LetterStateOut(BaseModel):
    printable: bool
    username: str | None
    devices: list[DeviceRef]


class LetterIn(BaseModel):
    #: Print a new claim code for each assigned device (replaces the label's).
    new_claim_codes: bool = False


class LetterDevice(BaseModel):
    serial: str
    #: Only when a new one was made for this letter.
    claim_code: str | None
    claim_url: str
    #: What the claim QR encodes (the claim URL, with ``#code=`` when new).
    qr_url: str


class LetterOut(BaseModel):
    customer_code: str | None
    customer_name: str | None
    owner_name: str | None
    username: str
    portal_url: str
    #: Single use, valid ``ttl_hours``; shown only in this response.
    activation_code: str
    activation_url: str
    ttl_hours: int
    devices: list[LetterDevice]


class DeviceRow(BaseModel):
    id: str
    serial: str
    name: str | None
    state: str
    household_id: str | None
    customer_code: str | None
    online: bool
    ai_hz: float | None
    last_seen_at: str | None
    firmware: str | None
    #: Claimed but has not acknowledged the household's latest profile.
    profile_behind: bool


class DevicesOut(BaseModel):
    rows: list[DeviceRow]
    next_serial: str


class ProvisionIn(BaseModel):
    serial: str | None = None
    name: str | None = None


class LabelOut(BaseModel):
    title: str
    device_id: str
    serial: str
    #: Shown once: the Portal keeps only digests of these codes.
    claim_code: str | None
    setup_code: str | None
    claim_url: str
    qr_url: str
    #: ``identity.json`` for the SD card (provisioning only, shown once).
    identity: dict[str, Any] | None


class LabelStateOut(BaseModel):
    serial: str
    state: str
    reprintable: bool


class CustomerCodeIn(BaseModel):
    customer_code: str | None = None


class StaffRow(BaseModel):
    id: str
    username: str
    display_name: str | None
    role: StaffRole
    status: str | None
    totp_enabled: bool
    last_login_at: str | None
    me: bool


class StaffOut(BaseModel):
    staff: list[StaffRow]
    roles: list[str]


class StaffIn(BaseModel):
    role: StaffRole | None = "operator"
    username: str | None = None
    display_name: str | None = None
    email: str | None = None


class StaffCodeOut(BaseModel):
    user: StaffRow
    #: Hand it over in person (also e-mailed when an address was given).
    activation_code: str
    activation_url: str
    ttl_hours: int


class StaffActionIn(BaseModel):
    action: Literal["disable", "enable", "reset_2fa", "resend"]


class AuditEntry(BaseModel):
    id: int
    at: str | None
    event: str
    actor_type: str | None
    actor_id: str | None
    target_type: str | None
    target_id: str | None
    household_id: str | None
    detail: dict[str, Any]


class AdminAuditOut(BaseModel):
    rows: list[AuditEntry]
    next_cursor: str | None
    chain: ChainOut


# ── converters ────────────────────────────────────────────────────────────────
def _online(request: Request):
    """The flows' own default (``hub.is_online``), as the old pages used."""
    return None


def _chain(db: Session) -> ChainOut:
    return ChainOut(**security.verify_audit_chain(db))


def _refs(devices: list[Device]) -> list[DeviceRef]:
    return [DeviceRef(id=d.id, serial=d.serial) for d in devices]


def _customer(db: Session, household_id: str) -> tuple[Household, User | None]:
    household, owner = flows.customer_of(db, household_id)
    if household is None:
        raise ApiError("not_found", "No such customer.")
    return household, owner


def _customer_out(db: Session, request: Request, household: Household, owner: User | None,
                  message: str | None = None) -> CustomerOut:
    detail = flows.customer_detail(db, household, owner, online=_online(request))
    return CustomerOut(
        id=household.id, customer_code=household.customer_code, name=household.display_name,
        city=household.city, status=household.status,
        setup_done=household.onboarding_completed_at is not None,
        setup_step=int(household.onboarding_step or 1),
        owner=None if owner is None else OwnerOut(
            id=owner.id, username=owner.username, display_name=owner.display_name,
            status=owner.status, email_masked=mask_email(owner.email) or None,
            phone_masked=mask_phone(accounts.user_phone(owner)) or None,
            last_login_at=z(owner.last_login_at)),
        members=detail["members"],
        devices=[CustomerDevice(id=r["device"].id, serial=r["device"].serial,
                                name=r["device"].name, state=r["device"].state,
                                online=r["online"]) for r in detail["devices"]],
        unassigned=_refs(detail["unassigned"]),
        events=[AccountEvent(id=e.id, at=z(e.at), event=e.event, actor_type=e.actor_type)
                for e in detail["events"]],
        message=message)


def _device(db: Session, device_id: str) -> Device:
    device = db.get(Device, device_id)
    if device is None:
        raise ApiError("not_found", "No such device.")
    return device


def _device_row(row: dict[str, Any]) -> DeviceRow:
    device, household = row["device"], row["household"]
    return DeviceRow(id=device.id, serial=device.serial, name=device.name, state=device.state,
                     household_id=device.household_id,
                     customer_code=household.customer_code if household else None,
                     online=row["online"], ai_hz=row["ai_hz"],
                     last_seen_at=z(device.last_seen_at), firmware=device.firmware,
                     profile_behind=row["profile_behind"])


def _label_out(label: dict[str, Any]) -> LabelOut:
    device = label["device"]
    return LabelOut(title=label["title"], device_id=device.id, serial=device.serial,
                    claim_code=label["claim_code"], setup_code=label["setup_code"],
                    claim_url=label["claim_url"], qr_url=label["qr_url"],
                    identity=label["identity"])


def _staff_row(user: User, principal) -> StaffRow:
    return StaffRow(id=user.id, username=user.username, display_name=user.display_name,
                    role=user.role, status=user.status, totp_enabled=bool(user.totp_enabled),
                    last_login_at=z(user.last_login_at), me=user.id == principal.user_id)


# ── overview ──────────────────────────────────────────────────────────────────
@router.get("/overview", response_model=OverviewOut)
def overview(request: Request, db: Session = DB, principal=Depends(ADMIN)) -> OverviewOut:
    data = flows.overview(db, online=_online(request))
    return OverviewOut(**{**data, "chain": ChainOut(**data["chain"])})


# ── customers ─────────────────────────────────────────────────────────────────
@router.get("/customers", response_model=CustomersOut)
def customers(q: str = "", db: Session = DB, principal=Depends(ADMIN)) -> CustomersOut:
    rows = []
    for row in flows.customers(db, q):
        household, owner = row["household"], row["owner"]
        rows.append(CustomerRow(
            id=household.id, customer_code=household.customer_code,
            name=household.display_name, city=household.city, status=household.status,
            owner_username=owner.username if owner else None,
            owner_status=owner.status if owner else None, devices=row["devices"],
            setup_done=household.onboarding_completed_at is not None,
            setup_step=int(household.onboarding_step or 1)))
    return CustomersOut(rows=rows, unassigned=_refs(flows.unassigned_devices(db)))


@router.post("/customers", response_model=CustomerOut, status_code=201)
def create_customer(body: CustomerIn, request: Request, db: Session = DB,
                    principal=Depends(ADMIN)) -> CustomerOut:
    """A household and its owner (pending activation: no password until the
    customer chooses one with the letter's code)."""
    outcome = flows.create_customer(db, principal, form_values(body))
    raise_for(outcome, db)
    household = outcome.data["household"]
    return _customer_out(db, request, household, accounts.household_owner(db, household.id),
                         outcome.message)


@router.get("/customers/{household_id}", response_model=CustomerOut)
def customer(household_id: str, request: Request, db: Session = DB,
             principal=Depends(ADMIN)) -> CustomerOut:
    household, owner = _customer(db, household_id)
    return _customer_out(db, request, household, owner)


@router.post("/customers/{household_id}/resend", response_model=Ok)
def resend(household_id: str, request: Request, db: Session = DB,
           principal=Depends(ADMIN)) -> Ok:
    """A new activation (or reset) code by e-mail to the customer only."""
    household, owner = _customer(db, household_id)
    outcome = flows.resend_activation(db, principal, household, owner, ip=ip_of(request))
    raise_for(outcome, db)
    return Ok(message=outcome.message)


@router.post("/customers/{household_id}/status", response_model=CustomerOut)
def customer_status(household_id: str, body: StatusIn, request: Request, db: Session = DB,
                    principal=Depends(ADMIN)) -> CustomerOut:
    household, owner = _customer(db, household_id)
    outcome = flows.set_customer_status(db, principal, household, body.action,
                                        ip=ip_of(request))
    raise_for(outcome, db)
    return _customer_out(db, request, household, owner, outcome.message)


@router.post("/customers/{household_id}/assign", response_model=CustomerOut)
def customer_assign(household_id: str, body: SerialIn, request: Request, db: Session = DB,
                    principal=Depends(ADMIN)) -> CustomerOut:
    household, owner = _customer(db, household_id)
    device = flows.device_by_serial(db, body.serial)
    if device is None:
        raise ApiError("not_found", "No such device.", fields={"serial": "No such device."})
    outcome = flows.assign_device(db, principal, device, household)
    raise_for(outcome, db)
    return _customer_out(db, request, household, owner, outcome.message)


@router.get("/customers/{household_id}/letter", response_model=LetterStateOut)
def letter_state(household_id: str, db: Session = DB,
                 principal=Depends(ADMIN)) -> LetterStateOut:
    household, owner = _customer(db, household_id)
    state = flows.letter_state(db, household, owner)
    return LetterStateOut(printable=state["printable"],
                          username=owner.username if owner else None,
                          devices=_refs(state["devices"]))


@router.post("/customers/{household_id}/letter", response_model=LetterOut)
def letter(household_id: str, body: LetterIn, request: Request, db: Session = DB,
           principal=Depends(ADMIN)) -> LetterOut:
    """The welcome letter as data: a fresh single-use activation code (the
    old one stops working) and the assigned devices' claim details. Like
    every API answer it is ``Cache-Control: no-store``."""
    household, owner = _customer(db, household_id)
    outcome = flows.print_letter(db, principal, household, owner,
                                 new_claim_codes=body.new_claim_codes, ip=ip_of(request))
    raise_for(outcome, db)
    data = outcome.data
    return LetterOut(customer_code=household.customer_code, customer_name=household.display_name,
                    owner_name=owner.display_name, username=owner.username,
                    portal_url=data["base_url"], activation_code=data["code"],
                    activation_url=data["activation_url"], ttl_hours=data["ttl_hours"],
                     devices=[LetterDevice(**d) for d in data["devices"]])


# ── devices ───────────────────────────────────────────────────────────────────
@router.get("/devices", response_model=DevicesOut)
def devices(request: Request, state: str = "", db: Session = DB,
            principal=Depends(ADMIN)) -> DevicesOut:
    return DevicesOut(rows=[_device_row(r) for r in flows.devices(db, state, _online(request))],
                      next_serial=accounts.next_serial(db))


@router.post("/devices", response_model=LabelOut, status_code=201)
def provision(body: ProvisionIn, db: Session = DB, principal=Depends(ADMIN)) -> LabelOut:
    """A new UNASSIGNED device: its label (claim code, Wi-Fi setup code) and
    ``identity.json`` (with the bootstrap secret), shown only here."""
    outcome = flows.provision(db, principal, form_values(body))
    raise_for(outcome, db)
    return _label_out(outcome.data["label"])


@router.get("/devices/{device_id}/label", response_model=LabelStateOut)
def label_state(device_id: str, db: Session = DB, principal=Depends(ADMIN)) -> LabelStateOut:
    device = _device(db, device_id)
    return LabelStateOut(serial=device.serial, state=device.state,
                         reprintable=device.state in ("UNASSIGNED", "ASSIGNED"))


@router.post("/devices/{device_id}/label", response_model=LabelOut)
def reprint_label(device_id: str, db: Session = DB, principal=Depends(ADMIN)) -> LabelOut:
    """A new claim code (the old one stops working) on a new label."""
    outcome = flows.reprint_label(db, principal, _device(db, device_id))
    raise_for(outcome, db)
    return _label_out(outcome.data["label"])


@router.post("/devices/{device_id}/assign", response_model=DeviceRow)
def device_assign(device_id: str, body: CustomerCodeIn, request: Request, db: Session = DB,
                  principal=Depends(ADMIN)) -> DeviceRow:
    device = _device(db, device_id)
    household = flows.customer_by_code(db, body.customer_code)
    if household is None:
        raise ApiError("not_found", flows.NO_SUCH_CUSTOMER,
                       fields={"customer_code": flows.NO_SUCH_CUSTOMER})
    outcome = flows.assign_device(db, principal, device, household)
    raise_for(outcome, db)
    row = next(r for r in flows.devices(db, "", _online(request)) if r["device"].id == device.id)
    return _device_row(row)


@router.post("/devices/{device_id}/transfer", response_model=LabelOut)
async def transfer(device_id: str, db: Session = DB, principal=Depends(ADMIN)) -> LabelOut:
    """Back to UNASSIGNED with a new claim code (a claimed device is unpaired
    first)."""
    outcome = await flows.transfer(db, principal, _device(db, device_id))
    raise_for(outcome, db)
    return _label_out(outcome.data["label"])


@router.post("/devices/{device_id}/retire", response_model=Ok)
async def retire(device_id: str, db: Session = DB, principal=Depends(ADMIN)) -> Ok:
    outcome = await flows.retire(db, principal, _device(db, device_id))
    return Ok(message=outcome.message)


# ── staff ─────────────────────────────────────────────────────────────────────
@router.get("/operators", response_model=StaffOut)
def staff(db: Session = DB, principal=Depends(ADMIN)) -> StaffOut:
    return StaffOut(staff=[_staff_row(u, principal) for u in flows.staff(db)],
                    roles=list(accounts.STAFF_ROLES))


@router.post("/operators", response_model=StaffCodeOut, status_code=201)
def create_staff(body: StaffIn, db: Session = DB, principal=Depends(ADMIN)) -> StaffCodeOut:
    """A Desk operator (or another admin); TOTP is set up at the first sign-in."""
    outcome = flows.create_staff(db, principal, form_values(body))
    raise_for(outcome, db)
    data = outcome.data
    return StaffCodeOut(user=_staff_row(data["user"], principal), activation_code=data["code"],
                        activation_url=data["activation_url"], ttl_hours=data["ttl_hours"])


@router.post("/operators/{user_id}", response_model=StaffRow)
def update_staff(user_id: str, body: StaffActionIn, request: Request, db: Session = DB,
                 principal=Depends(ADMIN)) -> StaffRow:
    user = flows.staff_of(db, user_id)
    if user is None:
        raise ApiError("not_found", "No such staff account.")
    outcome = flows.update_staff(db, principal, user, body.action, ip=ip_of(request))
    raise_for(outcome, db)
    return _staff_row(user, principal)


# ── audit ─────────────────────────────────────────────────────────────────────
@router.get("/audit", response_model=AdminAuditOut)
def audit(event: str = "", household: str = "", limit: int = Query(100, ge=1, le=500),
          cursor: str | None = None, db: Session = DB, principal=Depends(ADMIN)) -> AdminAuditOut:
    """The hash-chained audit log, newest first, and whether it verifies."""
    before = int(cursor) if cursor and cursor.isdigit() else None
    rows = flows.audit_rows(db, event=event, household=household, limit=limit + 1,
                            before_id=before)
    more = len(rows) > limit
    rows = rows[:limit]
    return AdminAuditOut(rows=[AuditEntry(id=r.id, at=z(r.at), event=r.event,
                                     actor_type=r.actor_type, actor_id=r.actor_id,
                                     target_type=r.target_type, target_id=r.target_id,
                                     household_id=r.household_id,
                                     detail=clean_detail(r.detail))
                          for r in rows],
                    next_cursor=str(rows[-1].id) if more and rows else None,
                    chain=_chain(db))


__all__ = ["router"]
