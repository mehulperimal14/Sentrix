"""The 7-step onboarding wizard /onboarding/{1..7} and the device claim by code
or QR (/claim?sn=...). Owner only (members cannot edit the profile).

Owner: track T-ACCT. Steps (design §4.1 as amended by the reviews and
decisions D8/D9/D14):

1. About you: name, mobile (+91), e-mail (verified by link), language,
   "I am 18 or older".
2. Home: address with landmark, 6-digit PIN code (first digit 1-8), state
   from the list, optional map pin, entry note (never door codes), nearest
   police / fire / hospital.
3. Household: occupants, members (age band; minors get no account and their
   faces are never enrolled), pets, medical notes behind a separate consent.
4. Emergency contacts (1-3): each confirms by link; only CONFIRMED contacts
   ever receive an SOS; contacts without Telegram or e-mail are unreachable
   while the device is offline (warned).
5. Device: claim by serial + claim code (or the label's QR), name, room
   label, default mode, "I have checked the view" (the tunnelled live view).
6. Alerts and SOS: window 30/45/60 min, safety defaults (on), channels,
   Telegram link, 6-digit SOS PIN (weak PINs refused) and, with contacts,
   a duress PIN differing in at least 2 digits.
7. Consent and review: the notice, one toggle per purpose, and the SOS
   preview rendered with the household's own data.

A step can be revisited; a later step opens only after the earlier ones.
Every saved step bumps the profile version (pushed to claimed devices).
The rules live in ``services.household_flows`` (shared with ``/api/v2``).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from sentrix_portal import security
from sentrix_portal.db import DB
from sentrix_portal.routes.auth import household_of, render, see_other, user_of
from sentrix_portal.services import accounts
from sentrix_portal.services import household_flows as flows

router = APIRouter()

OWNER = security.require_role("owner")


def _actor(principal: security.Principal) -> dict[str, Any]:
    return flows.owner_actor(principal)


def _load(db: Session, principal):
    household = household_of(db, principal)
    if household is None:
        raise HTTPException(404, "no household")
    return user_of(db, principal), household


def _page(request: Request, db: Session, principal, user, household, step: int, *,
          values: dict | None = None, errors: dict | None = None, notice: str | None = None,
          status_code: int = 200, **extra) -> HTMLResponse:
    context = flows.wizard_state(db, user, household, step)
    context.update(flows.step_extra(db, principal, user, household, step))
    context.update(values=values if values is not None
                   else flows.step_values(db, user, household, step),
                   errors=errors or {}, notice=notice, **extra)
    return render(request, f"onboarding/step{step}.html", context, status_code=status_code,
                  active_page="household")


@router.get("/onboarding")
def onboarding_start(request: Request, db: Session = DB,
                     principal=Depends(security.require_role("owner", "member"))):
    if principal.role != "owner":
        return see_other("/home")
    _, household = _load(db, principal)
    return see_other(f"/onboarding/{min(int(household.onboarding_step or 1), accounts.LAST_STEP)}")


#: The serial of a label QR scanned before step 5 was reachable.
CLAIM_SN_COOKIE = "portal_claim_sn"


@router.get("/onboarding/{step}", response_class=HTMLResponse)
def onboarding_page(request: Request, step: int, sn: str = "",
                    db: Session = DB, principal=Depends(OWNER)):
    if step not in accounts.ONBOARDING_STEPS:
        raise HTTPException(404, "no such step")
    user, household = _load(db, principal)
    if not flows.step_reachable(household, step):
        response = see_other(f"/onboarding/{int(household.onboarding_step or 1)}")
        serial = accounts.normalize_serial(sn)[:40] if sn else ""
        if serial:
            # A label QR scanned before step 5: keep the serial for step 5.
            response.set_cookie(CLAIM_SN_COOKIE, serial, max_age=3600, httponly=True,
                                samesite="lax", path="/onboarding")
        return response
    remembered = request.cookies.get(CLAIM_SN_COOKIE, "") if step == 5 else ""
    if not sn and remembered:
        sn = remembered
    values = flows.step_values(db, user, household, step, sn)
    page = _page(request, db, principal, user, household, step, values=values)
    if remembered:
        page.delete_cookie(CLAIM_SN_COOKIE, path="/onboarding")
    return page


@router.post("/onboarding/{step}", response_class=HTMLResponse)
async def onboarding_submit(request: Request, step: int, db: Session = DB,
                            principal=Depends(OWNER)):
    if step not in accounts.ONBOARDING_STEPS:
        raise HTTPException(404, "no such step")
    user, household = _load(db, principal)
    if not flows.step_reachable(household, step):
        return see_other(f"/onboarding/{int(household.onboarding_step or 1)}")
    form = await request.form()
    handler = _HANDLERS[step]
    return await handler(request, db, principal, user, household, form)


# ── step handlers ─────────────────────────────────────────────────────────────
def _form_page(request, db, principal, user, household, step, outcome, form):
    """A step saved (go on) or refused (the form again with its errors)."""
    if outcome.ok:
        return see_other(outcome.data["next"])
    return _page(request, db, principal, user, household, step, values=dict(form),
                 errors=outcome.errors, status_code=outcome.status)


async def _step1(request, db, principal, user, household, form):
    outcome = await flows.save_about_you(db, user, household, form,
                                         ip=security.client_ip(request))
    return _form_page(request, db, principal, user, household, 1, outcome, form)


async def _step2(request, db, principal, user, household, form):
    outcome = await flows.save_address_step(db, user, household, form,
                                            ip=security.client_ip(request))
    return _form_page(request, db, principal, user, household, 2, outcome, form)


async def _step3(request, db, principal, user, household, form):
    action = form.get("action") or "save"
    ip = security.client_ip(request)
    if action == "add_member":
        outcome = await flows.add_member(db, user, household, form, ip=ip)
        if not outcome.ok:
            return _page(request, db, principal, user, household, 3, errors=outcome.errors,
                         member_form=dict(form), status_code=outcome.status)
        return _page(request, db, principal, user, household, 3, notice=outcome.message)
    if action == "remove_member":
        member = flows.member_of(db, household, form.get("member_id"))
        if member is None:
            raise HTTPException(404, "no such member")
        outcome = await flows.remove_member(db, user, household, member, ip=ip)
        return _page(request, db, principal, user, household, 3, notice=outcome.message)
    outcome = await flows.save_household_step(db, user, household, form, ip=ip)
    return _form_page(request, db, principal, user, household, 3, outcome, form)


async def contact_action(request, db, user, household, form) -> tuple[dict, dict, int]:
    """The contact sub-forms of step 4 and /household/contacts: add, edit,
    remove, new confirmation link. Returns (errors, page extras, status)."""
    action = form.get("action")
    contact = None
    if action in ("update_contact", "remove_contact", "resend_contact"):
        contact = flows.contact_of(db, household, form.get("contact_id"))
        if contact is None:
            raise HTTPException(404, "no such contact")
    if action not in flows.CONTACT_ACTIONS:
        raise HTTPException(400, "unknown action")
    outcome = await flows.contact_action(db, user, household, action, form, contact,
                                         ip=security.client_ip(request))
    if outcome.code == "validation":
        return outcome.errors, {"contact_form": dict(form)}, outcome.status
    if "share_link" in outcome.data:
        return outcome.errors, {"share_link": outcome.data["share_link"],
                                "share_for": outcome.data["share_for"],
                                "emailed": outcome.data["emailed"]}, 200
    return outcome.errors, {"notice": outcome.message}, outcome.status


async def _step4(request, db, principal, user, household, form):
    if (form.get("action") or "continue") == "continue":
        outcome = await flows.continue_contacts_step(db, household)
        if not outcome.ok:
            return _page(request, db, principal, user, household, 4, errors=outcome.errors,
                         status_code=outcome.status)
        return see_other(outcome.data["next"])
    errors, extra, status = await contact_action(request, db, user, household, form)
    notice = extra.pop("notice", None)
    return _page(request, db, principal, user, household, 4, errors=errors, notice=notice,
                 status_code=status, **extra)


async def claim(request, db, principal, user, form):
    """Claim by serial + code; on success the hub issues the device token
    and the profile is pushed. Returns (outcome, status code)."""
    return await flows.claim(db, user, serial=form.get("serial"),
                             code=form.get("claim_code"), ip=security.client_ip(request))


async def _step5(request, db, principal, user, household, form):
    action = form.get("action") or "device"
    if action == "claim":
        outcome, status = await claim(request, db, principal, user, form)
        if not outcome.ok:
            errors = accounts.FormErrors()
            errors.add("claim_code", outcome.error)
            return _page(request, db, principal, user, household, 5, errors=errors,
                         values={"serial": accounts.normalize_serial(form.get("serial"))},
                         status_code=status)
        return _page(request, db, principal, user, household, 5,
                     notice=flows.claimed_message(outcome.device))
    device = flows.claimed_device_of(db, household, form.get("device_id"))
    if device is None:
        raise HTTPException(404, "no such device")
    outcome = await flows.save_device_step(db, principal, user, household, device, form,
                                           ip=security.client_ip(request))
    if not outcome.ok:
        return _page(request, db, principal, user, household, 5, errors=outcome.errors,
                     device_form=dict(form), status_code=outcome.status)
    return see_other(outcome.data["next"])


async def _step6(request, db, principal, user, household, form):
    if form.get("action") == "telegram":
        code = flows.telegram_code(db, user, household)
        return _page(request, db, principal, user, household, 6,
                     telegram_code=code["code"], telegram_link=code["link"])
    outcome = await flows.save_sos_step(db, principal, user, household, form,
                                        ip=security.client_ip(request))
    if not outcome.ok:
        return _page(request, db, principal, user, household, 6,
                     values=outcome.data["values"], errors=outcome.errors,
                     status_code=outcome.status)
    return see_other(outcome.data["next"])


async def _step7(request, db, principal, user, household, form):
    outcome = await flows.finish_onboarding(db, user, household, form,
                                            ip=security.client_ip(request))
    if not outcome.ok:
        return _page(request, db, principal, user, household, 7, errors=outcome.errors,
                     status_code=outcome.status)
    return see_other(outcome.data["next"])


_HANDLERS = {1: _step1, 2: _step2, 3: _step3, 4: _step4, 5: _step5, 6: _step6, 7: _step7}


# ── /claim?sn=... (the label's QR) ────────────────────────────────────────────
@router.get("/claim", response_class=HTMLResponse)
def claim_page(request: Request, sn: str = "", db: Session = DB,
               principal=Depends(OWNER)):
    _, household = _load(db, principal)
    serial = accounts.normalize_serial(sn)
    if household.onboarding_completed_at is None:
        return see_other(f"/onboarding/5?sn={quote(serial)}" if serial else "/onboarding/5")
    return render(request, "onboarding/claim.html",
                  {"values": {"serial": serial}, "errors": {}}, active_page="home")


@router.post("/claim", response_class=HTMLResponse)
async def claim_submit(request: Request, db: Session = DB,
                       principal=Depends(OWNER)):
    user, _ = _load(db, principal)
    form = await request.form()
    outcome, status = await claim(request, db, principal, user, form)
    if not outcome.ok:
        errors = accounts.FormErrors()
        errors.add("claim_code", outcome.error)
        return render(request, "onboarding/claim.html",
                      {"values": {"serial": accounts.normalize_serial(form.get("serial"))},
                       "errors": errors}, status_code=status, active_page="home")
    return see_other("/settings?saved=claimed#devices")


__all__ = ["router", "claim", "contact_action"]
