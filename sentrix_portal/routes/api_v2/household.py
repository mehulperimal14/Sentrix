"""Household (API v2): the profile and address, members, emergency contacts
and the household's own audit trail. Owners change; members read. Every rule
is ``services.household_flows`` (the HTML pages call the same functions).
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from sentrix_portal.db import DB
from sentrix_portal.models import EmergencyContact, Household, HouseholdMember
from sentrix_portal.routes.api_v2.common import (HOUSEHOLD, OWNER, ApiError, Ok, form_values,
                                                 household_of, ip_of, raise_for, user_of, z,
                                                 clean_detail)
from sentrix_portal.services import accounts
from sentrix_portal.services import household_flows as flows

router = APIRouter(tags=["household"])
AgeBand = Literal["adult", "minor", "senior"]


# ── models ────────────────────────────────────────────────────────────────────
class AddressFields(BaseModel):
    house: str | None = None
    building: str | None = None
    street: str | None = None
    locality: str | None = None
    landmark: str | None = None
    city: str | None = None
    district: str | None = None
    state: str | None = None
    pincode: str | None = None
    floor: str | None = None
    lat: float | None = None
    lon: float | None = None
    entry_note: str | None = None
    police_name: str | None = None
    police_phone: str | None = None
    fire_name: str | None = None
    fire_phone: str | None = None
    hospital_name: str | None = None
    hospital_phone: str | None = None


class HouseholdDetails(BaseModel):
    adults: int | None = None
    children: int | None = None
    seniors: int | None = None
    pets: str | None = None
    #: Medical or accessibility notes (stored only with their own consent).
    medical: str | None = None
    medical_consent: bool = False
    include_medical: bool = False


class HouseholdIn(AddressFields, HouseholdDetails):
    pass


class Option(BaseModel):
    value: str
    label: str


class DeviceSummary(BaseModel):
    id: str
    serial: str
    name: str | None
    state: str


class HouseholdOut(BaseModel):
    id: str
    name: str | None
    customer_code: str | None
    editable: bool
    values: HouseholdIn
    states: list[str]
    nearest_kinds: list[Option]
    members_count: int
    contacts_count: int
    devices: list[DeviceSummary]
    erasure_due: str | None


class MemberOut(BaseModel):
    id: str
    name: str
    relation: str | None
    age_band: str | None
    #: A Portal account was invited for this member.
    invited: bool


class MembersOut(BaseModel):
    editable: bool
    members: list[MemberOut]
    max_members: int
    age_bands: list[str]


class MemberIn(BaseModel):
    member_name: str | None = None
    member_relation: str | None = None
    age_band: AgeBand | None = None
    #: Adults only: an invitation to a Portal member account.
    invite_email: str | None = None


class MemberUpdateIn(BaseModel):
    member_relation: str | None = None
    age_band: AgeBand | None = None


class MemberSavedOut(BaseModel):
    member: MemberOut
    message: str
    #: The invited account's username (its activation code goes by e-mail).
    invited_username: str | None = None


class ContactOut(BaseModel):
    id: str
    name: str
    relation: str | None
    priority: int | None
    can_reach_30min: bool
    status: Literal["PENDING", "CONFIRMED", "DECLINED", "REVOKED"]
    confirmed_at: str | None
    phone_masked: str | None
    email: str | None
    telegram: bool
    #: Neither Telegram nor e-mail: unreachable while the device is offline.
    unreachable_offline: bool


class ContactsOut(BaseModel):
    editable: bool
    contacts: list[ContactOut]
    max_contacts: int
    can_add: bool
    free_priorities: list[int]
    declined_days: int | None = None


class ContactIn(BaseModel):
    name: str | None = None
    relation: str | None = None
    phone: str | None = None
    email: str | None = None
    priority: int | None = None
    can_reach_30min: bool = False


class ContactSavedOut(BaseModel):
    contact: ContactOut
    #: The contact's personal confirmation link, shown once (share it yourself).
    share_link: str | None
    emailed: bool


class AuditRow(BaseModel):
    id: int
    at: str | None
    event: str
    actor: str
    target_type: str | None
    detail: dict[str, Any]


class HouseholdAuditOut(BaseModel):
    rows: list[AuditRow]
    #: Pass as ``cursor`` for the next (older) page; null at the end.
    next_cursor: str | None


# ── converters ────────────────────────────────────────────────────────────────
def member_out(member: HouseholdMember) -> MemberOut:
    return MemberOut(id=member.id, name=member.name, relation=member.relation,
                     age_band=member.age_band, invited=bool(member.user_id))


def contact_out(contact: EmergencyContact, state: dict[str, Any]) -> ContactOut:
    return ContactOut(id=contact.id, name=contact.name, relation=contact.relation,
                      priority=contact.priority, can_reach_30min=bool(contact.can_reach_30min),
                      status=contact.consent_status, confirmed_at=z(contact.confirmed_at),
                      phone_masked=state["phones"].get(contact.id) or None,
                      email=contact.email, telegram=contact.id in state["telegram"],
                      unreachable_offline=contact.id in state["unreachable"])


def contacts_out(state: dict[str, Any], *, editable: bool) -> ContactsOut:
    return ContactsOut(editable=editable,
                       contacts=[contact_out(c, state) for c in state["contacts"]],
                       max_contacts=state["max_contacts"], can_add=state["can_add"],
                       free_priorities=state["free_priorities"],
                       declined_days=state.get("declined_days"))


def _household_out(db: Session, principal, household: Household) -> HouseholdOut:
    values = flows.household_values(db, household)
    return HouseholdOut(
        id=household.id, name=household.display_name, customer_code=household.customer_code,
        editable=principal.role == "owner", values=HouseholdIn(**values),
        states=list(accounts.INDIAN_STATES),
        nearest_kinds=[Option(value=k, label=label) for k, label in accounts.NEAREST_KINDS],
        members_count=len(accounts.household_members(db, household.id)),
        contacts_count=len(accounts.active_contacts(db, household.id)),
        devices=[DeviceSummary(id=d.id, serial=d.serial, name=d.name, state=d.state)
                 for d in accounts.household_devices(db, household.id)],
        erasure_due=z(accounts.erasure_due_at(household)))


# ── the household ─────────────────────────────────────────────────────────────
@router.get("/household", response_model=HouseholdOut)
def get_household(db: Session = DB, principal=Depends(HOUSEHOLD)) -> HouseholdOut:
    return _household_out(db, principal, household_of(db, principal))


@router.put("/household", response_model=HouseholdOut)
async def update_household(body: HouseholdIn, request: Request, db: Session = DB,
                           principal=Depends(OWNER)) -> HouseholdOut:
    """The address and who lives here (one form, as on the household page)."""
    household = household_of(db, principal)
    outcome = await flows.update_household(db, user_of(db, principal), household,
                                           form_values(body), ip=ip_of(request))
    raise_for(outcome, db)
    return _household_out(db, principal, household)


# ── members ───────────────────────────────────────────────────────────────────
def _members(db: Session, principal, household: Household) -> MembersOut:
    return MembersOut(editable=principal.role == "owner",
                      members=[member_out(m) for m in
                               accounts.household_members(db, household.id)],
                      max_members=accounts.MAX_MEMBERS, age_bands=list(accounts.AGE_BANDS))


def _member(db: Session, household: Household, member_id: str) -> HouseholdMember:
    member = flows.member_of(db, household, member_id)
    if member is None:
        raise ApiError("not_found", "That member was not found.")
    return member


@router.get("/household/members", response_model=MembersOut)
def members(db: Session = DB, principal=Depends(HOUSEHOLD)) -> MembersOut:
    return _members(db, principal, household_of(db, principal))


@router.post("/household/members", response_model=MemberSavedOut, status_code=201)
async def add_member(body: MemberIn, request: Request, db: Session = DB,
                     principal=Depends(OWNER)) -> MemberSavedOut:
    """A member; with an e-mail (adults only) also an invited Portal account."""
    household = household_of(db, principal)
    outcome = await flows.add_member(db, user_of(db, principal), household,
                                     form_values(body), ip=ip_of(request))
    raise_for(outcome, db)
    invited = outcome.data["invited"]
    return MemberSavedOut(member=member_out(outcome.data["member"]), message=outcome.message,
                          invited_username=invited.username if invited else None)


@router.patch("/household/members/{member_id}", response_model=MemberSavedOut)
async def update_member(member_id: str, body: MemberUpdateIn, request: Request,
                        db: Session = DB, principal=Depends(OWNER)) -> MemberSavedOut:
    household = household_of(db, principal)
    member = _member(db, household, member_id)
    outcome = await flows.update_member(db, user_of(db, principal), household, member,
                                        form_values(body), ip=ip_of(request))
    raise_for(outcome, db)
    return MemberSavedOut(member=member_out(member), message=outcome.message)


@router.delete("/household/members/{member_id}", response_model=Ok)
async def remove_member(member_id: str, request: Request, db: Session = DB,
                        principal=Depends(OWNER)) -> Ok:
    """Their Portal account (if any) is closed and signed out."""
    household = household_of(db, principal)
    member = _member(db, household, member_id)
    outcome = await flows.remove_member(db, user_of(db, principal), household, member,
                                        ip=ip_of(request))
    return Ok(message=outcome.message)


# ── emergency contacts ────────────────────────────────────────────────────────
def _contact(db: Session, household: Household, contact_id: str) -> EmergencyContact:
    contact = flows.contact_of(db, household, contact_id)
    if contact is None:
        raise ApiError("not_found", "That contact was not found.")
    return contact


async def contact_change(db: Session, request: Request, principal, action: str,
                         body: BaseModel | None,
                         contact_id: str | None = None) -> tuple[Household, Any]:
    household = household_of(db, principal)
    contact = _contact(db, household, contact_id) if contact_id else None
    outcome = await flows.contact_action(db, user_of(db, principal), household, action,
                                         form_values(body), contact, ip=ip_of(request))
    raise_for(outcome, db)
    return household, outcome


def _saved(db: Session, household: Household, outcome) -> ContactSavedOut:
    state = flows.household_contacts(db, household)
    return ContactSavedOut(contact=contact_out(outcome.data["contact"], state),
                           share_link=outcome.data["share_link"],
                           emailed=bool(outcome.data["emailed"]))


@router.get("/household/contacts", response_model=ContactsOut)
def contacts(db: Session = DB, principal=Depends(HOUSEHOLD)) -> ContactsOut:
    household = household_of(db, principal)
    return contacts_out(flows.household_contacts(db, household),
                        editable=principal.role == "owner")


@router.post("/household/contacts", response_model=ContactSavedOut, status_code=201)
async def add_contact(body: ContactIn, request: Request, db: Session = DB,
                      principal=Depends(OWNER)) -> ContactSavedOut:
    """A PENDING contact; they get a confirmation link by e-mail (and the
    owner gets it once here, to share). Nothing reaches them until they
    confirm."""
    household, outcome = await contact_change(db, request, principal, "add_contact", body)
    return _saved(db, household, outcome)


@router.patch("/household/contacts/{contact_id}", response_model=ContactSavedOut)
async def update_contact(contact_id: str, body: ContactIn, request: Request,
                         db: Session = DB, principal=Depends(OWNER)) -> ContactSavedOut:
    """A new number or e-mail needs a new confirmation (a new link)."""
    household, outcome = await contact_change(db, request, principal, "update_contact", body,
                                              contact_id)
    return _saved(db, household, outcome)


@router.delete("/household/contacts/{contact_id}", response_model=Ok)
async def remove_contact(contact_id: str, request: Request, db: Session = DB,
                         principal=Depends(OWNER)) -> Ok:
    _, outcome = await contact_change(db, request, principal, "remove_contact", None,
                                      contact_id)
    return Ok(message=outcome.message)


@router.post("/household/contacts/{contact_id}/resend", response_model=ContactSavedOut)
async def resend_contact(contact_id: str, request: Request, db: Session = DB,
                         principal=Depends(OWNER)) -> ContactSavedOut:
    """A new confirmation link for a contact who has not answered yet."""
    household, outcome = await contact_change(db, request, principal, "resend_contact", None,
                                              contact_id)
    return _saved(db, household, outcome)


# ── the household's audit trail ───────────────────────────────────────────────
@router.get("/household/audit", response_model=HouseholdAuditOut)
def audit(limit: int = Query(50, ge=1, le=200), cursor: str | None = None,
          db: Session = DB, principal=Depends(HOUSEHOLD)) -> HouseholdAuditOut:
    """Who did what, newest first (restricted SOS rows never appear)."""
    household = household_of(db, principal)
    before = int(cursor) if cursor and cursor.isdigit() else None
    rows = accounts.household_audit(db, household.id, limit=limit + 1, before_id=before)
    more = len(rows) > limit
    rows = rows[:limit]
    return HouseholdAuditOut(rows=[AuditRow(id=r["id"], at=z(r["at"]), event=r["event"],
                                   actor=r["actor"], target_type=r["target_type"],
                                   detail=clean_detail(r["detail"])) for r in rows],
                    next_cursor=str(rows[-1]["id"]) if more and rows else None)


__all__ = ["router", "AddressFields", "HouseholdDetails", "MemberOut", "ContactOut",
           "ContactsOut", "ContactSavedOut", "member_out", "contact_out", "contacts_out",
           "contact_change", "Option", "DeviceSummary"]
