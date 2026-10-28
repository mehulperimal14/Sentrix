"""/household: profile and address, /household/members, /household/contacts
(with the consent confirmation flow) and /household/audit (the household's own
audit trail). Owners edit; members only view (docs/7 §10). Every change bumps
the profile version and is pushed to the claimed devices.

Owner: track T-ACCT. Tenant isolation: every query is filtered on the
signed-in user's household; an id from another household answers 404. The
rules live in ``services.household_flows`` (shared with ``/api/v2``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from sentrix_portal import security
from sentrix_portal.db import DB
from sentrix_portal.routes.auth import household_of, render, see_other, user_of
from sentrix_portal.routes.onboarding import contact_action
from sentrix_portal.services import accounts
from sentrix_portal.services import household_flows as flows

router = APIRouter()

VIEW = security.require_role("owner", "member")
OWNER = security.require_role("owner")
AUDIT_PAGE_SIZE = 50


def _household(db: Session, principal):
    household = household_of(db, principal)
    if household is None:
        raise HTTPException(404, "no household")
    return household


def _household_page(request, db, principal, household, *, values=None, errors=None,
                    notice=None, status_code=200):
    return render(request, "household/household.html", {
        "household": household, "values": values or flows.household_values(db, household),
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
    outcome = await flows.update_household(db, user, household, form,
                                           ip=security.client_ip(request))
    if not outcome.ok:
        return _household_page(request, db, principal, household, values=dict(form),
                               errors=outcome.errors, status_code=outcome.status)
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
    if action == "add_member":
        outcome = await flows.add_member(db, user, household, form, ip=ip)
        if not outcome.ok:
            return _members_page(request, db, principal, household, errors=outcome.errors,
                                 member_form=dict(form), status_code=outcome.status)
    elif action in ("update_member", "remove_member"):
        member = flows.member_of(db, household, form.get("member_id"))
        if member is None:
            raise HTTPException(404, "no such member")
        if action == "remove_member":
            outcome = await flows.remove_member(db, user, household, member, ip=ip)
        else:
            outcome = await flows.update_member(db, user, household, member, form, ip=ip)
            if not outcome.ok:
                return _members_page(request, db, principal, household,
                                     errors=outcome.errors, status_code=outcome.status)
    else:
        raise HTTPException(400, "unknown action")
    return _members_page(request, db, principal, household, notice=outcome.message)


# ── emergency contacts ────────────────────────────────────────────────────────
def _contacts_page(request, db, principal, household, *, errors=None, status_code=200,
                   **extra):
    return render(request, "household/contacts.html", {
        "household": household, **flows.household_contacts(db, household),
        "editable": principal.role == "owner", "errors": errors or {}, **extra,
    }, status_code=status_code, active_page="household")


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
