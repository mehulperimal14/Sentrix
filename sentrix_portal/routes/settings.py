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
from starlette.concurrency import run_in_threadpool

from sentrix.actor import Actor
from sentrix_portal import security
from sentrix_portal.db import get_db
from sentrix_portal.models import Camera, Device, TelegramLink
from sentrix_portal.routes.auth import household_of, render, see_other, user_of
from sentrix_portal.services import accounts

router = APIRouter()

VIEW = security.require_role("owner", "member")
OWNER = security.require_role("owner")

SAVED = {"sos": "Saved. Safety settings apply to future requests.",
         "pins": "PINs changed. The device gets them with the next profile.",
         "device": "Device saved.",
         "unpaired": "Device unpaired.", "claimed": "Device claimed.",
         "telegram_unlinked": "Telegram unlinked."}
STEP_UP_NOTES = {"failed": "That password was not right.",
                 "wait": "Too many attempts; wait a minute."}


def _household(db: Session, principal):
    household = household_of(db, principal)
    if household is None:
        raise HTTPException(404, "no household")
    return household


def _page(request, db, principal, household, *, errors=None, values=None, notice=None,
          status_code=200, **extra) -> HTMLResponse:
    from sentrix_portal.services import hub
    user = user_of(db, principal)
    devices = accounts.household_devices(db, household.id)
    return render(request, "settings/settings.html", {
        "household": household, "editable": principal.role == "owner",
        "stepped_up": principal.stepped_up(),
        "step_up_seconds": accounts.portal_config().step_up_seconds,
        "values": values or {"window": accounts.clamp_window(household.sos_window_s),
                             "safety_defaults": bool(household.safety_defaults),
                             "channels": accounts.household_channels(household)},
        "window_choices": accounts.window_choices(), "errors": errors or {},
        "notice": notice, "pin_set": bool(household.sos_pin_hash),
        "duress_set": bool(household.duress_pin_hash),
        "contacts_exist": bool(accounts.active_contacts(db, household.id)),
        "pin_length": accounts.sos_config().pin_length,
        "duress_digits": accounts.sos_config().duress_min_digit_difference,
        "telegram": accounts.telegram_links(db, "user", user.id),
        "bot_configured": bool(accounts.portal_config().telegram_bot_name),
        "devices": [{"device": d, "camera": (accounts.device_cameras(db, d.id) or [None])[0],
                     "online": d.state == "CLAIMED" and bool(hub.is_online(d.id))}
                    for d in devices],
        **extra,
    }, status_code=status_code, active_page="settings")


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: str = "", stepup: str = "",
                  db: Session = Depends(get_db), principal=Depends(VIEW)):
    household = _household(db, principal)
    notice = SAVED.get(saved) or STEP_UP_NOTES.get(stepup)
    return _page(request, db, principal, household, notice=notice)


def _step_up_error(errors: accounts.FormErrors, principal, field: str) -> bool:
    if principal.stepped_up():
        return False
    errors.add(field, "Confirm your password above first (it stays confirmed for "
                      f"{accounts.portal_config().step_up_seconds // 60} minutes).")
    return True


@router.post("/settings", response_class=HTMLResponse)
async def settings_submit(request: Request, db: Session = Depends(get_db),
                          principal=Depends(OWNER)):
    household = _household(db, principal)
    user = user_of(db, principal)
    form = await request.form()
    section = form.get("section")
    ip = security.client_ip(request)
    errors = accounts.FormErrors()

    if section == "sos":
        values = accounts.clean_sos_settings(errors, form,
                                             safety_was_on=bool(household.safety_defaults))
        changes = accounts.sos_settings_change(household, values)
        if not errors and {"window", "safety_defaults"} & set(changes):
            _step_up_error(errors, principal, "window")
        if errors:
            return _page(request, db, principal, household, errors=errors, values=values,
                         status_code=403 if "window" in errors and not principal.stepped_up()
                         else 400)
        if changes:
            accounts.apply_sos_settings(household, values)
            accounts.profile_changed(db, household.id, actor_id=user.id, ip=ip,
                                     what="sos_settings", detail={"changed": changes})
        return await _saved(db, household, section)

    if section == "pins":
        if _step_up_error(errors, principal, "sos_pin"):
            return _page(request, db, principal, household, errors=errors, status_code=403)
        sos_pin, duress_pin = accounts.check_pins(
            errors, form.get("sos_pin") or "", form.get("sos_pin_confirm") or "",
            form.get("duress_pin") or "", form.get("duress_pin_confirm") or "",
            phone=accounts.user_phone(user), pincode=household.pincode,
            contacts_exist=bool(accounts.active_contacts(db, household.id)))
        if errors:
            return _page(request, db, principal, household, errors=errors, status_code=400)
        sos_hash = await run_in_threadpool(security.hash_pin, sos_pin)
        duress_hash = (await run_in_threadpool(security.hash_pin, duress_pin)
                       if duress_pin else None)
        accounts.apply_pins(db, household, sos_hash, duress_hash, user=user, ip=ip)
        accounts.profile_changed(db, household.id, actor_id=user.id, what="pins", ip=ip)
        return await _saved(db, household, "pins")

    if section == "telegram":
        code = accounts.mint_telegram_link(db, subject_type="user", subject_id=user.id,
                                           household_id=household.id)
        return _page(request, db, principal, household, telegram_code=code,
                     telegram_link=accounts.telegram_deep_link(code))

    if section == "telegram_unlink":
        link = db.get(TelegramLink, form.get("link_id") or "")
        if link is None or link.subject_type != "user" or link.subject_id != user.id:
            raise HTTPException(404, "no such link")
        link.status = "revoked"
        security.audit(db, "telegram.unlinked", actor_type="user", actor_id=user.id,
                       household_id=household.id, target_type="telegram_link",
                       target_id=link.id, ip=ip)
        accounts.profile_changed(db, household.id, actor_id=user.id, what="channels", ip=ip)
        return await _saved(db, household, "telegram_unlinked")

    if section in ("device", "unpair"):
        device = db.get(Device, form.get("device_id") or "")
        if device is None or device.household_id != household.id or device.state != "CLAIMED":
            raise HTTPException(404, "no such device")
        if section == "device":
            return await _save_device(request, db, principal, household, user, device, form)
        if _step_up_error(errors, principal, f"unpair_{device.id}"):
            return _page(request, db, principal, household, errors=errors, status_code=403)
        return await _unpair(request, db, principal, household, user, device)

    raise HTTPException(400, "unknown section")


async def _saved(db, household, what: str):
    db.commit()
    await accounts.push_profile(db, household.id)
    return see_other(f"/settings?saved={what}")


async def _save_device(request, db, principal, household, user, device, form):
    errors = accounts.FormErrors()
    name = accounts.clean_text(errors, "name", form.get("name"), label="Device name",
                               max_len=60, required=True)
    room = accounts.clean_text(errors, "room_label", form.get("room_label"),
                               label="Room label", max_len=40, required=True)
    floor = accounts.clean_text(errors, "floor", form.get("floor"), label="Floor", max_len=20)
    if errors:
        return _page(request, db, principal, household, errors=errors, status_code=400)
    device.name = name
    camera = db.query(Camera).filter_by(device_id=device.id).order_by(Camera.camera_id).first()
    if camera is None:
        camera = Camera(device_id=device.id, camera_id="CAM_0")
        db.add(camera)
    camera.room_label, camera.floor = room, floor
    accounts.profile_changed(db, household.id, actor_id=user.id, what="device",
                             ip=security.client_ip(request), detail={"device_id": device.id})
    return await _saved(db, household, "device")


async def _unpair(request, db, principal, household, user, device):
    """Owner + step-up: the device wipes its token and profile (queued while
    it is offline); it stays assigned to the household and needs a new claim
    code from SENTRIX to be claimed again."""
    from sentrix_portal.services import hub
    stepped = principal.step_up_at.timestamp() if principal.step_up_at else None
    actor = Actor("owner", id=user.id, name=principal.display_name, step_up_at=stepped)
    try:
        await hub.unpair(db, device.id, actor.to_dict())
    except Exception as exc:                          # the Portal side still unpairs
        accounts.log.error("unpair of %s failed: %s", device.id, exc)
    device.state = "ASSIGNED"
    device.token_hash = device.claim_code_hash = None
    device.claimed_at = device.claimed_by = None
    db.query(Camera).filter_by(device_id=device.id).delete(synchronize_session=False)
    security.audit(db, "device.unpaired_by_owner", actor_type="user", actor_id=user.id,
                   household_id=household.id, target_type="device", target_id=device.id,
                   ip=security.client_ip(request), detail={"serial": device.serial})
    accounts.profile_changed(db, household.id, actor_id=user.id, what="device_unpaired",
                             ip=security.client_ip(request))
    db.commit()
    return see_other("/settings?saved=unpaired")


__all__ = ["router"]
