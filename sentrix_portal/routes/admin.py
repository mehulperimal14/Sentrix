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

The rules live in ``services.admin_flows`` (shared with ``/api/v2``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from sentrix_portal import security
from sentrix_portal.db import DB
from sentrix_portal.models import Device, Household, User
from sentrix_portal.routes.auth import message_page, qr_svg, render, see_other
from sentrix_portal.services import accounts
from sentrix_portal.services import admin_flows as flows

router = APIRouter(prefix="/admin")

ADMIN = security.require_role("company_admin")
NO_STORE = {"Cache-Control": "no-store"}
AUDIT_PAGE_SIZE = flows.AUDIT_PAGE_SIZE
NOTICES = flows.NOTICES
_cloud_actor = flows.cloud_actor


def _customer(db: Session, household_id: str) -> tuple[Household, User | None]:
    household, owner = flows.customer_of(db, household_id)
    if household is None:
        raise HTTPException(404, "no such customer")
    return household, owner


def _device(db: Session, device_id: str) -> Device:
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(404, "no such device")
    return device


# ── overview ──────────────────────────────────────────────────────────────────
@router.get("", response_class=HTMLResponse)
def admin_index(request: Request, db: Session = DB, principal=Depends(ADMIN)):
    return render(request, "admin/index.html", flows.overview(db), active_page="admin")


# ── customers ─────────────────────────────────────────────────────────────────
def _customers_page(request, db, *, q: str = "", errors=None, values=None,
                    status_code=200, notice=None):
    return render(request, "admin/customers.html", {
        "rows": flows.customers(db, q), "q": q, "errors": errors or {},
        "values": values or {}, "notice": notice,
        "unassigned": flows.unassigned_devices(db),
    }, status_code=status_code, active_page="admin")


@router.get("/customers", response_class=HTMLResponse)
def customers_page(request: Request, q: str = "", db: Session = DB,
                   principal=Depends(ADMIN)):
    return _customers_page(request, db, q=q)


@router.post("/customers", response_class=HTMLResponse)
async def customers_create(request: Request, db: Session = DB,
                           principal=Depends(ADMIN)):
    form = await request.form()
    outcome = flows.create_customer(db, principal, form)
    if not outcome.ok:
        return _customers_page(request, db, errors=outcome.errors, values=dict(form),
                               status_code=outcome.status)
    return see_other(f"/admin/customers/{outcome.data['household'].id}?done=created")


@router.get("/customers/{household_id}", response_class=HTMLResponse)
def customer_page(request: Request, household_id: str, done: str = "",
                  db: Session = DB, principal=Depends(ADMIN)):
    household, owner = _customer(db, household_id)
    return render(request, "admin/customer.html", {
        **flows.customer_detail(db, household, owner), "notice": NOTICES.get(done),
    }, active_page="admin")


@router.post("/customers/{household_id}/resend")
def customer_resend(request: Request, household_id: str, db: Session = DB,
                    principal=Depends(ADMIN)):
    """"Reset": a new code by e-mail to the customer only (D14). For an
    account that was never activated it replaces the letter's code."""
    household, owner = _customer(db, household_id)
    outcome = flows.resend_activation(db, principal, household, owner,
                                      ip=security.client_ip(request))
    if not outcome.ok:
        return message_page(request, "Cannot send", outcome.message, status_code=409,
                            link=(f"/admin/customers/{household.id}", "Back"),
                            active_page="admin")
    return see_other(f"/admin/customers/{household.id}?done={outcome.data['done']}")


@router.post("/customers/{household_id}/status")
async def customer_status(request: Request, household_id: str, db: Session = DB,
                          principal=Depends(ADMIN)):
    household, _ = _customer(db, household_id)
    form = await request.form()
    outcome = flows.set_customer_status(db, principal, household, form.get("action"),
                                        ip=security.client_ip(request))
    if not outcome.ok:
        raise HTTPException(409, outcome.message)
    return see_other(f"/admin/customers/{household.id}?done={outcome.data['done']}")


@router.post("/customers/{household_id}/assign")
async def customer_assign(request: Request, household_id: str, db: Session = DB,
                          principal=Depends(ADMIN)):
    household, _ = _customer(db, household_id)
    form = await request.form()
    device = flows.device_by_serial(db, form.get("serial"))
    if device is None:
        raise HTTPException(404, "no such device")
    outcome = flows.assign_device(db, principal, device, household)
    if not outcome.ok:
        return message_page(request, "Cannot assign", outcome.message, status_code=409,
                            link=(f"/admin/customers/{household.id}", "Back"),
                            active_page="admin")
    return see_other(f"/admin/customers/{household.id}?done=assigned")


@router.get("/customers/{household_id}/letter", response_class=HTMLResponse)
def letter_confirm(request: Request, household_id: str, db: Session = DB,
                   principal=Depends(ADMIN)):
    household, owner = _customer(db, household_id)
    return render(request, "admin/letter_confirm.html",
                  flows.letter_state(db, household, owner), active_page="admin")


@router.post("/customers/{household_id}/letter", response_class=HTMLResponse)
async def letter_print(request: Request, household_id: str, db: Session = DB,
                       principal=Depends(ADMIN)):
    """The printable welcome letter: Portal URL, username, a fresh single-use
    activation code with its QR, and the assigned devices' serials with a
    claim code (new unless the label's code is kept) and the claim QR."""
    household, owner = _customer(db, household_id)
    if owner is None or owner.status != "pending_activation" or household.status != "active":
        return message_page(request, "No letter for an activated account", flows.NO_LETTER,
                            status_code=409,
                            link=(f"/admin/customers/{household.id}", "Back"),
                            active_page="admin")
    form = await request.form()
    outcome = flows.print_letter(db, principal, household, owner,
                                 new_claim_codes=bool(form.get("new_claim_codes")),
                                 ip=security.client_ip(request))
    letter = outcome.data
    devices = [{**d, "qr": qr_svg(d["qr_url"])} for d in letter["devices"]]
    return render(request, "admin/letter.html", {
        "household": household, "owner": owner, "code": letter["code"],
        "base_url": letter["base_url"], "activation_url": letter["activation_url"],
        "activation_qr": qr_svg(letter["activation_url"]), "devices": devices,
        "ttl_hours": letter["ttl_hours"],
    }, headers=NO_STORE, active_page="admin")


# ── devices ───────────────────────────────────────────────────────────────────
def _devices_page(request, db, *, errors=None, values=None, status_code=200, notice=None,
                  state: str = ""):
    return render(request, "admin/devices.html", {
        "rows": flows.devices(db, state), "errors": errors or {}, "values": values or {},
        "notice": notice, "state": state, "next_serial": accounts.next_serial(db),
    }, status_code=status_code, active_page="admin")


@router.get("/devices", response_class=HTMLResponse)
def devices_page(request: Request, state: str = "", done: str = "",
                 db: Session = DB, principal=Depends(ADMIN)):
    return _devices_page(request, db, state=state, notice=NOTICES.get(done))


def _label(request, label: dict) -> HTMLResponse:
    return render(request, "admin/label.html", {**label, "qr": qr_svg(label["qr_url"])},
                  headers=NO_STORE, active_page="admin")


@router.post("/devices", response_class=HTMLResponse)
async def devices_provision(request: Request, db: Session = DB,
                            principal=Depends(ADMIN)):
    form = await request.form()
    outcome = flows.provision(db, principal, form)
    if not outcome.ok:
        return _devices_page(request, db, errors=outcome.errors, values=dict(form),
                             status_code=outcome.status)
    return _label(request, outcome.data["label"])


@router.get("/devices/{device_id}/label", response_class=HTMLResponse)
def label_confirm(request: Request, device_id: str, db: Session = DB,
                  principal=Depends(ADMIN)):
    device = _device(db, device_id)
    return render(request, "admin/label_confirm.html", {
        "device": device, "reprintable": device.state in ("UNASSIGNED", "ASSIGNED")},
        active_page="admin")


@router.post("/devices/{device_id}/label", response_class=HTMLResponse)
def label_reprint(request: Request, device_id: str, db: Session = DB,
                  principal=Depends(ADMIN)):
    device = _device(db, device_id)
    outcome = flows.reprint_label(db, principal, device)
    if not outcome.ok:
        return message_page(request, "Cannot reprint", outcome.message, status_code=409,
                            link=("/admin/devices", "Back"), active_page="admin")
    return _label(request, outcome.data["label"])


@router.post("/devices/{device_id}/assign")
async def device_assign(request: Request, device_id: str, db: Session = DB,
                        principal=Depends(ADMIN)):
    device = _device(db, device_id)
    form = await request.form()
    household = flows.customer_by_code(db, form.get("customer_code"))
    if household is None:
        return message_page(request, "No such customer", flows.NO_SUCH_CUSTOMER,
                            status_code=404, link=("/admin/devices", "Back"),
                            active_page="admin")
    outcome = flows.assign_device(db, principal, device, household)
    if not outcome.ok:
        return message_page(request, "Cannot assign", outcome.message, status_code=409,
                            link=("/admin/devices", "Back"), active_page="admin")
    return see_other("/admin/devices?done=assigned")


@router.post("/devices/{device_id}/transfer", response_class=HTMLResponse)
async def device_transfer(request: Request, device_id: str, db: Session = DB,
                          principal=Depends(ADMIN)):
    """Back to UNASSIGNED with a new claim code (a claimed device is unpaired
    first: it wipes its token and profile)."""
    device = _device(db, device_id)
    outcome = await flows.transfer(db, principal, device)
    if not outcome.ok:
        raise HTTPException(409, outcome.message)
    return _label(request, outcome.data["label"])


@router.post("/devices/{device_id}/retire")
async def device_retire(request: Request, device_id: str, db: Session = DB,
                        principal=Depends(ADMIN)):
    device = _device(db, device_id)
    await flows.retire(db, principal, device)
    return see_other("/admin/devices?done=retired")


# ── operators and admins ──────────────────────────────────────────────────────
def _staff_page(request, db, principal, *, errors=None, values=None, status_code=200,
                notice=None):
    return render(request, "admin/operators.html", {
        "staff": flows.staff(db), "errors": errors or {}, "values": values or {},
        "notice": notice, "me": principal.user_id, "roles": accounts.STAFF_ROLES,
    }, status_code=status_code, active_page="admin")


@router.get("/operators", response_class=HTMLResponse)
def operators_page(request: Request, done: str = "", db: Session = DB,
                   principal=Depends(ADMIN)):
    return _staff_page(request, db, principal, notice=NOTICES.get(done))


@router.post("/operators", response_class=HTMLResponse)
async def operators_create(request: Request, db: Session = DB,
                           principal=Depends(ADMIN)):
    """A Desk operator (or another admin). Company staff: the activation code
    is shown once to hand over in person, and e-mailed when an address is
    given; TOTP is set up at the first sign-in."""
    form = await request.form()
    outcome = flows.create_staff(db, principal, form)
    if not outcome.ok:
        return _staff_page(request, db, principal, errors=outcome.errors, values=dict(form),
                           status_code=outcome.status)
    return render(request, "admin/staff_code.html", outcome.data, headers=NO_STORE,
                  active_page="admin")


@router.post("/operators/{user_id}")
async def operators_update(request: Request, user_id: str, db: Session = DB,
                           principal=Depends(ADMIN)):
    user = flows.staff_of(db, user_id)
    if user is None:
        raise HTTPException(404, "no such staff account")
    if user.id == principal.user_id:
        raise HTTPException(409, "use your own account page")
    form = await request.form()
    outcome = flows.update_staff(db, principal, user, form.get("action"),
                                 ip=security.client_ip(request))
    if not outcome.ok:
        raise HTTPException(409, outcome.message)
    return see_other("/admin/operators?done=staff_updated")


# ── audit ─────────────────────────────────────────────────────────────────────
@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, page: int = 1, event: str = "", household: str = "",
               db: Session = DB, principal=Depends(ADMIN)):
    page = max(1, min(page, 100_000))
    rows = flows.audit_rows(db, event=event, household=household,
                            offset=(page - 1) * AUDIT_PAGE_SIZE, limit=AUDIT_PAGE_SIZE + 1)
    return render(request, "admin/audit.html", {
        "rows": rows[:AUDIT_PAGE_SIZE], "more": len(rows) > AUDIT_PAGE_SIZE, "page": page,
        "event": event, "household": household, "chain": security.verify_audit_chain(db),
    }, active_page="audit")


__all__ = ["router"]
