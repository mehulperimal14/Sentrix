"""/household: profile and address, /household/members, /household/contacts
(with the consent confirmation flow) and /household/audit (the household's own
audit trail). Owners edit; members only view (docs/7 §10). Every change bumps
the profile version and is pushed to the claimed devices.

Owner: track T-ACCT. Tenant isolation: every query is filtered on the
signed-in user's household; an id from another household answers 404.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from sentrix.utils.redact import mask_phone
from sentrix_portal import security
from sentrix_portal.db import DB
from sentrix_portal.models import EmergencyContact, HouseholdMember
from sentrix_portal.routes.auth import household_of, render, see_other, user_of
from sentrix_portal.routes.onboarding import contact_action
from sentrix_portal.services import accounts

router = APIRouter()

VIEW = security.require_role("owner", "member")
OWNER = security.require_role("owner")
AUDIT_PAGE_SIZE = 50


def _household(db: Session, principal):
    household = household_of(db, principal)
    if household is None:
        raise HTTPException(404, "no household")
    return household


def _profile_values(db: Session, household) -> dict:
    values = {name: getattr(household, name) for name in accounts.ADDRESS_FIELDS}
    for kind, _ in accounts.NEAREST_KINDS:
        place = (household.nearest or {}).get(kind) or {}
        values[f"{kind}_name"], values[f"{kind}_phone"] = place.get("name"), place.get("phone")
    occupants = household.occupants or {}
    consents = accounts.consent_states(db, household.id)
    values.update(adults=occupants.get("adults", 1), children=occupants.get("children", 0),
                  seniors=occupants.get("seniors", 0), pets=household.pets or "",
                  medical=accounts.medical_notes(household) or "",
                  medical_consent=consents["medical_notes"],
                  include_medical=bool(household.include_medical))
    return values


def _household_page(request, db, principal, household, *, values=None, errors=None,
                    notice=None, status_code=200):
    return render(request, "household/household.html", {
        "household": household, "values": values or _profile_values(db, household),
        "errors": errors or {}, "notice": notice, "editable": principal.role == "owner",
        "states_options": [(s, s) for s in accounts.INDIAN_STATES],
        "nearest_kinds": accounts.NEAREST_KINDS,
        "members": accounts.household_members(db, household.id),
        "contacts": accounts.active_contacts(db, household.id),
        "devices": accounts.household_devices(db, household.id),
        "erasure_due": accounts.iso(accounts.erasure_due_at(household)),
    }, status_code=status_code, active_page="household")


@router.get("/household", response_class=HTMLResponse)
def household_page(request: Request, saved: str = "", db: Session = DB,
                   principal=Depends(VIEW)):
    household = _household(db, principal)
    return _household_page(request, db, principal, household,
                           notice="Saved." if saved else None)


@router.post("/household", response_class=HTMLResponse)
async def household_update(request: Request, db: Session = DB,
                           principal=Depends(OWNER)):
    household = _household(db, principal)
    user = user_of(db, principal)
    form = await request.form()
    errors = accounts.FormErrors()
    address = accounts.clean_address(errors, form)
    details = accounts.clean_household_details(errors, form)
    if errors:
        return _household_page(request, db, principal, household, values=dict(form),
                               errors=errors, status_code=400)
    ip = security.client_ip(request)
    accounts.apply_address(household, address)
    accounts.apply_household_details(db, household, details, user=user, method="household",
                                     ip=ip)
    accounts.profile_changed(db, household.id, actor_id=user.id, what="household", ip=ip)
    db.commit()
    await accounts.push_profile(db, household.id)
    return see_other("/household?saved=1")


# ── members ───────────────────────────────────────────────────────────────────
def _members_page(request, db, principal, household, *, errors=None, notice=None,
                  member_form=None, status_code=200):
    return render(request, "household/members.html", {
        "household": household, "members": accounts.household_members(db, household.id),
        "errors": errors or {}, "notice": notice, "member_form": member_form or {},
        "editable": principal.role == "owner", "age_bands": accounts.AGE_BANDS,
    }, status_code=status_code, active_page="household")


@router.get("/household/members", response_class=HTMLResponse)
def members_page(request: Request, db: Session = DB, principal=Depends(VIEW)):
    return _members_page(request, db, principal, _household(db, principal))


@router.post("/household/members", response_class=HTMLResponse)
async def members_submit(request: Request, db: Session = DB,
                         principal=Depends(OWNER)):
    household = _household(db, principal)
    user = user_of(db, principal)
    form = await request.form()
    ip = security.client_ip(request)
    action = form.get("action")
    errors = accounts.FormErrors()
    if action == "add_member":
        values = accounts.clean_member(errors, form)
        if len(accounts.household_members(db, household.id)) >= accounts.MAX_MEMBERS:
            errors.add("member_name", f"At most {accounts.MAX_MEMBERS} members.")
        if errors:
            return _members_page(request, db, principal, household, errors=errors,
                                 member_form=dict(form), status_code=400)
        _, invited = accounts.add_member(db, household, values, actor=user, ip=ip)
        notice = (f"Added. An invitation was e-mailed; their username is {invited.username}."
                  if invited else "Added.")
    elif action in ("update_member", "remove_member"):
        member = db.get(HouseholdMember, form.get("member_id") or "")
        if member is None or member.household_id != household.id:
            raise HTTPException(404, "no such member")
        if action == "remove_member":
            accounts.remove_member(db, member, actor=user, ip=ip)
            notice = "Removed."
        else:
            relation = accounts.clean_text(errors, "member_relation",
                                           form.get("member_relation"), label="Relation",
                                           max_len=40)
            band = accounts.clean_choice(errors, "age_band", form.get("age_band"),
                                         accounts.AGE_BANDS, label="Age band")
            if band == "minor" and member.user_id:
                errors.add("age_band", "A member with a Portal account cannot be a minor.")
            if errors:
                return _members_page(request, db, principal, household, errors=errors,
                                     status_code=400)
            member.relation, member.age_band = relation, band
            accounts.profile_changed(db, household.id, actor_id=user.id, what="members", ip=ip)
            notice = "Saved."
    else:
        raise HTTPException(400, "unknown action")
    db.commit()
    await accounts.push_profile(db, household.id)
    return _members_page(request, db, principal, household, notice=notice)


# ── emergency contacts ────────────────────────────────────────────────────────
def _contacts_page(request, db, principal, household, *, errors=None, status_code=200,
                   **extra):
    contacts = (db.query(EmergencyContact).filter_by(household_id=household.id)
                .order_by(EmergencyContact.priority, EmergencyContact.created_at).all())
    taken = {c.priority for c in contacts if c.consent_status in ("PENDING", "CONFIRMED")}
    return render(request, "household/contacts.html", {
        "household": household, "contacts": contacts,
        "phones": {c.id: mask_phone(accounts.contact_phone(c)) for c in contacts},
        "unreachable": {c.id for c in contacts if c.consent_status in ("PENDING", "CONFIRMED")
                        and accounts.contact_unreachable_offline(db, c)},
        "telegram": {c.id for c in contacts if accounts.contact_telegram_linked(db, c)},
        "max_contacts": accounts.MAX_CONTACTS,
        "can_add": len(taken) < accounts.MAX_CONTACTS,
        "free_priorities": [p for p in range(1, accounts.MAX_CONTACTS + 1) if p not in taken],
        "editable": principal.role == "owner", "errors": errors or {},
        "declined_days": _declined_days(), **extra,
    }, status_code=status_code, active_page="household")


def _declined_days() -> int:
    from sentrix.config import settings
    return settings.retention.declined_contact_days


@router.get("/household/contacts", response_class=HTMLResponse)
def contacts_page(request: Request, db: Session = DB, principal=Depends(VIEW)):
    return _contacts_page(request, db, principal, _household(db, principal))


@router.post("/household/contacts", response_class=HTMLResponse)
async def contacts_submit(request: Request, db: Session = DB,
                          principal=Depends(OWNER)):
    household = _household(db, principal)
    form = await request.form()
    errors, extra, status = await contact_action(request, db, user_of(db, principal),
                                                 household, form)
    return _contacts_page(request, db, principal, household, errors=errors,
                          status_code=status, **extra)


# ── the household's audit trail ───────────────────────────────────────────────
@router.get("/household/audit", response_class=HTMLResponse)
def audit_page(request: Request, page: int = 1, db: Session = DB,
               principal=Depends(VIEW)):
    household = _household(db, principal)
    page = max(1, min(page, 10_000))
    rows = accounts.household_audit(db, household.id, limit=AUDIT_PAGE_SIZE + 1,
                                    offset=(page - 1) * AUDIT_PAGE_SIZE)
    return render(request, "household/audit.html", {
        "rows": rows[:AUDIT_PAGE_SIZE], "page": page,
        "more": len(rows) > AUDIT_PAGE_SIZE}, active_page="household")


__all__ = ["router"]
