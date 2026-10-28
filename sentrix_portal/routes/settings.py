"""/settings: SOS window and safety defaults, SOS and duress PINs, the owner's
notification channels, Telegram linking (deep link code), and the devices
(name, room label, unpair). Owners change them; members view.

Owner: track T-ACCT. Rules:

* safety settings (window, safety defaults), PINs and unpairing need a fresh
  password (step-up, ``step_up_seconds``); a stranger holding an unlocked,
  signed-in phone cannot quietly switch the safety net off;
* changes apply to future requests only: each request freezes its policy
  when it opens (docs/7 §7.5);
* both PINs are entered together, so the duress rule (differs from the SOS
  PIN in at least 2 digits) is checked on the digits, never on a hash;
* the Telegram code is shown once (``accounts.mint_telegram_link``, single
  use, 24 h); T-CH's bot links the chat on ``/start <code>``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from sentrix_portal import security
from sentrix_portal.db import DB
from sentrix_portal.routes.auth import household_of, render, see_other, user_of
from sentrix_portal.services import accounts
from sentrix_portal.services import household_flows as flows

router = APIRouter()

VIEW = security.require_role("owner", "member")
OWNER = security.require_role("owner")

SAVED = flows.SETTINGS_SAVED
STEP_UP_NOTES = {"failed": "That password was not right.",
                 "wait": "Too many attempts; wait a minute."}


def _household(db: Session, principal):
    household = household_of(db, principal)
    if household is None:
        raise HTTPException(404, "no household")
    return household


def _page(request, db, principal, household, *, errors=None, values=None, notice=None,
          status_code=200, **extra) -> HTMLResponse:
    user = user_of(db, principal)
    return render(request, "settings/settings.html", {
        "household": household, "editable": principal.role == "owner",
        "stepped_up": principal.stepped_up(),
        "step_up_seconds": accounts.portal_config().step_up_seconds,
        "values": values or flows.sos_values(household),
        "window_choices": accounts.window_choices(), "errors": errors or {},
        "notice": notice, **flows.pin_state(db, household),
        "telegram": accounts.telegram_links(db, "user", user.id),
        "bot_configured": bool(accounts.portal_config().telegram_bot_name),
        "devices": flows.device_rows(db, household),
        **extra,
    }, status_code=status_code, active_page="settings")


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: str = "", stepup: str = "",
                  db: Session = DB, principal=Depends(VIEW)):
    household = _household(db, principal)
    notice = SAVED.get(saved) or STEP_UP_NOTES.get(stepup)
    return _page(request, db, principal, household, notice=notice)


@router.post("/settings", response_class=HTMLResponse)
async def settings_submit(request: Request, db: Session = DB,
                          principal=Depends(OWNER)):
    household = _household(db, principal)
    user = user_of(db, principal)
    form = await request.form()
    section = form.get("section")
    ip = security.client_ip(request)

    if section == "sos":
        outcome = await flows.save_sos_settings(db, principal, user, household, form, ip=ip)
        if not outcome.ok:
            return _page(request, db, principal, household, errors=outcome.errors,
                         values=outcome.data["values"], status_code=outcome.status)
        return see_other(f"/settings?saved={outcome.data['saved']}")

    if section == "pins":
        outcome = await flows.save_pins(db, principal, user, household, form, ip=ip)
        if not outcome.ok:
            return _page(request, db, principal, household, errors=outcome.errors,
                         status_code=outcome.status)
        return see_other(f"/settings?saved={outcome.data['saved']}")

    if section == "telegram":
        code = flows.telegram_code(db, user, household)
        return _page(request, db, principal, household, telegram_code=code["code"],
                     telegram_link=code["link"])

    if section == "telegram_unlink":
        link = flows.telegram_link_of(db, user, form.get("link_id"))
        if link is None:
            raise HTTPException(404, "no such link")
        outcome = await flows.unlink_telegram(db, user, household, link, ip=ip)
        return see_other(f"/settings?saved={outcome.data['saved']}")

    if section in ("device", "unpair"):
        device = flows.claimed_device_of(db, household, form.get("device_id"))
        if device is None:
            raise HTTPException(404, "no such device")
        if section == "device":
            outcome = await flows.save_device(db, user, household, device, form, ip=ip)
        else:
            outcome = await flows.unpair_device(db, principal, user, household, device, ip=ip)
        if not outcome.ok:
            return _page(request, db, principal, household, errors=outcome.errors,
                         status_code=outcome.status)
        return see_other(f"/settings?saved={outcome.data['saved']}")

    raise HTTPException(400, "unknown section")


__all__ = ["router"]
