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
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from sentrix.actor import Actor
from sentrix.utils.redact import mask_phone
from sentrix_portal import security
from sentrix_portal.db import get_db
from sentrix_portal.models import Camera, Device, EmergencyContact, HouseholdMember, utcnow
from sentrix_portal.routes.auth import household_of, render, see_other, user_of
from sentrix_portal.services import accounts

router = APIRouter()

OWNER = security.require_role("owner")


def _actor(principal: security.Principal) -> dict[str, Any]:
    stepped = principal.step_up_at.timestamp() if principal.step_up_at else None
    return Actor("owner", id=principal.user_id, name=principal.display_name,
                 step_up_at=stepped).to_dict()


def _load(db: Session, principal):
    household = household_of(db, principal)
    if household is None:
        raise HTTPException(404, "no household")
    return user_of(db, principal), household


def _step_context(db: Session, principal, user, household, step: int) -> dict[str, Any]:
    done = accounts.onboarding_done(db, household, user)
    reachable = (household.onboarding_completed_at is not None
                 or step <= int(household.onboarding_step or 1))
    return {"step": step, "steps": accounts.ONBOARDING_STEPS, "done": done,
            "reachable_upto": (accounts.LAST_STEP if household.onboarding_completed_at
                               else int(household.onboarding_step or 1)),
            "reachable": reachable, "household": household, "user": user,
            "completed": household.onboarding_completed_at is not None}


def _values(db: Session, user, household, step: int, sn: str = "") -> dict[str, Any]:
    if step == 1:
        return {"display_name": user.display_name or "", "phone": accounts.user_phone(user) or "",
                "email": user.email or "", "language": household.language or "en",
                "adult": accounts.onboarding_done(db, household, user)[1]}
    if step == 2:
        values = {name: getattr(household, name) for name in accounts.ADDRESS_FIELDS}
        for kind, _ in accounts.NEAREST_KINDS:
            place = (household.nearest or {}).get(kind) or {}
            values[f"{kind}_name"], values[f"{kind}_phone"] = place.get("name"), place.get("phone")
        return values
    if step == 3:
        occupants = household.occupants or {}
        consents = accounts.consent_states(db, household.id)
        return {"adults": occupants.get("adults", 1), "children": occupants.get("children", 0),
                "seniors": occupants.get("seniors", 0), "pets": household.pets or "",
                "medical": accounts.medical_notes(household) or "",
                "medical_consent": consents["medical_notes"],
                "include_medical": bool(household.include_medical)}
    if step == 5:
        return {"serial": accounts.normalize_serial(sn)}
    if step == 6:
        return {"window": accounts.clamp_window(household.sos_window_s),
                "safety_defaults": bool(household.safety_defaults),
                "channels": accounts.household_channels(household)}
    return {}


def _extra(db: Session, principal, user, household, step: int) -> dict[str, Any]:
    """Lists and side panels each step shows next to its form."""
    if step == 2:
        return {"states_options": [(s, s) for s in accounts.INDIAN_STATES],
                "nearest_kinds": accounts.NEAREST_KINDS}
    if step == 3:
        return {"members": accounts.household_members(db, household.id),
                "age_bands": accounts.AGE_BANDS}
    if step == 4:
        contacts = (db.query(EmergencyContact).filter_by(household_id=household.id)
                    .order_by(EmergencyContact.priority).all())
        active = [c for c in contacts if c.consent_status in ("PENDING", "CONFIRMED")]
        return {"contacts": contacts,
                "phones": {c.id: mask_phone(accounts.contact_phone(c)) for c in contacts},
                "unreachable": {c.id for c in active
                                if accounts.contact_unreachable_offline(db, c)},
                "telegram": {c.id for c in active if accounts.contact_telegram_linked(db, c)},
                "max_contacts": accounts.MAX_CONTACTS,
                "can_add": len(accounts.active_contacts(db, household.id)) < accounts.MAX_CONTACTS,
                "free_priorities": _free_priorities(db, household.id)}
    if step == 5:
        from sentrix_portal.services import hub
        devices = accounts.household_devices(db, household.id)
        return {"assigned": [d for d in devices if d.state == "ASSIGNED"],
                "claimed": [{"device": d, "camera": (accounts.device_cameras(db, d.id) or [None])[0],
                             "online": bool(hub.is_online(d.id))}
                            for d in devices if d.state == "CLAIMED"]}
    if step == 6:
        links = accounts.telegram_links(db, "user", user.id)
        return {"window_choices": accounts.window_choices(),
                "pin_set": bool(household.sos_pin_hash),
                "duress_set": bool(household.duress_pin_hash),
                "contacts_exist": bool(accounts.active_contacts(db, household.id)),
                "telegram_linked": bool(links), "stepped_up": principal.stepped_up(),
                "pin_length": accounts.sos_config().pin_length,
                "duress_digits": accounts.sos_config().duress_min_digit_difference,
                "bot_configured": bool(accounts.portal_config().telegram_bot_name)}
    if step == 7:
        consents = accounts.consent_states(db, household.id)
        if not any(consents.values()):
            consents = dict(accounts.DEFAULT_CONSENTS)
        consents["medical_notes"] = consents["medical_notes"] or bool(household.medical_enc)
        profile = accounts.preview_profile(db, household)
        pending = [c for c in accounts.active_contacts(db, household.id)
                   if c.consent_status == "PENDING"]
        return {**accounts.notice_context(), "consents": consents,
                "preview": accounts.render_sos_preview(profile),
                "pending_contacts": pending,
                "confirmed_contacts": accounts.confirmed_contacts(db, household.id)}
    return {}


def _free_priorities(db: Session, household_id: str) -> list[int]:
    taken = {c.priority for c in accounts.active_contacts(db, household_id)}
    return [p for p in range(1, accounts.MAX_CONTACTS + 1) if p not in taken]


def _page(request: Request, db: Session, principal, user, household, step: int, *,
          values: dict | None = None, errors: dict | None = None, notice: str | None = None,
          status_code: int = 200, **extra) -> HTMLResponse:
    context = _step_context(db, principal, user, household, step)
    context.update(_extra(db, principal, user, household, step))
    context.update(values=values if values is not None else _values(db, user, household, step),
                   errors=errors or {}, notice=notice, **extra)
    return render(request, f"onboarding/step{step}.html", context, status_code=status_code,
                  active_page="household")


def _after_save(household, step: int) -> str:
    accounts.advance_onboarding(household, step)
    return f"/onboarding/{step + 1}" if step < accounts.LAST_STEP else "/home"


@router.get("/onboarding")
def onboarding_start(request: Request, db: Session = Depends(get_db),
                     principal=Depends(security.require_role("owner", "member"))):
    if principal.role != "owner":
        return see_other("/home")
    _, household = _load(db, principal)
    return see_other(f"/onboarding/{min(int(household.onboarding_step or 1), accounts.LAST_STEP)}")


@router.get("/onboarding/{step}", response_class=HTMLResponse)
def onboarding_page(request: Request, step: int, sn: str = "",
                    db: Session = Depends(get_db), principal=Depends(OWNER)):
    if step not in accounts.ONBOARDING_STEPS:
        raise HTTPException(404, "no such step")
    user, household = _load(db, principal)
    if not _step_context(db, principal, user, household, step)["reachable"]:
        return see_other(f"/onboarding/{int(household.onboarding_step or 1)}")
    values = _values(db, user, household, step, sn)
    return _page(request, db, principal, user, household, step, values=values)


@router.post("/onboarding/{step}", response_class=HTMLResponse)
async def onboarding_submit(request: Request, step: int, db: Session = Depends(get_db),
                            principal=Depends(OWNER)):
    if step not in accounts.ONBOARDING_STEPS:
        raise HTTPException(404, "no such step")
    user, household = _load(db, principal)
    if not _step_context(db, principal, user, household, step)["reachable"]:
        return see_other(f"/onboarding/{int(household.onboarding_step or 1)}")
    form = await request.form()
    handler = _HANDLERS[step]
    return await handler(request, db, principal, user, household, form)


# ── step handlers ─────────────────────────────────────────────────────────────
async def _saved(request, db, household, target: str):
    db.commit()
    await accounts.push_profile(db, household.id)
    return see_other(target)


async def _step1(request, db, principal, user, household, form):
    errors = accounts.FormErrors()
    name = accounts.clean_text(errors, "display_name", form.get("display_name"),
                               label="Full name", max_len=80, required=True, min_len=2)
    phone = accounts.clean_phone(errors, "phone", form.get("phone"), label="Mobile")
    email = accounts.clean_email(errors, "email", form.get("email"), required=True)
    language = accounts.clean_choice(errors, "language", form.get("language") or "en",
                                     accounts.LANGUAGES, label="Language")
    if not form.get("adult"):
        errors.add("adult", "Only an adult (18 or older) can set up the household.")
    if errors:
        return _page(request, db, principal, user, household, 1, values=dict(form),
                     errors=errors, status_code=400)
    ip = security.client_ip(request)
    if phone != accounts.user_phone(user):
        user.phone_enc, user.phone_verified_at = security.seal(phone), None
    if email != user.email:
        user.email, user.email_verified_at = email, None
        accounts.send_email_verification(db, user)
    user.display_name, user.language = name, language
    household.language = language
    household.display_name = household.display_name or name
    accounts.profile_changed(db, household.id, actor_id=user.id, what="about_you", ip=ip,
                             detail={"adult_attested": True})
    return await _saved(request, db, household, _after_save(household, 1))


async def _step2(request, db, principal, user, household, form):
    errors = accounts.FormErrors()
    values = accounts.clean_address(errors, form)
    if errors:
        return _page(request, db, principal, user, household, 2, values=dict(form),
                     errors=errors, status_code=400)
    accounts.apply_address(household, values)
    accounts.profile_changed(db, household.id, actor_id=user.id, what="address",
                             ip=security.client_ip(request))
    return await _saved(request, db, household, _after_save(household, 2))


async def _step3(request, db, principal, user, household, form):
    action = form.get("action") or "save"
    ip = security.client_ip(request)
    errors = accounts.FormErrors()
    if action == "add_member":
        values = accounts.clean_member(errors, form)
        if len(accounts.household_members(db, household.id)) >= accounts.MAX_MEMBERS:
            errors.add("member_name", f"At most {accounts.MAX_MEMBERS} members.")
        if errors:
            return _page(request, db, principal, user, household, 3, errors=errors,
                         member_form=dict(form), status_code=400)
        _, invited = accounts.add_member(db, household, values, actor=user, ip=ip)
        db.commit()
        await accounts.push_profile(db, household.id)
        notice = (f"Added. An invitation was e-mailed; their username is {invited.username}."
                  if invited else "Added.")
        return _page(request, db, principal, user, household, 3, notice=notice)
    if action == "remove_member":
        member = db.get(HouseholdMember, form.get("member_id") or "")
        if member is None or member.household_id != household.id:
            raise HTTPException(404, "no such member")
        accounts.remove_member(db, member, actor=user, ip=ip)
        db.commit()
        await accounts.push_profile(db, household.id)
        return _page(request, db, principal, user, household, 3, notice="Removed.")
    values = accounts.clean_household_details(errors, form)
    if errors:
        return _page(request, db, principal, user, household, 3, values=dict(form),
                     errors=errors, status_code=400)
    accounts.apply_household_details(db, household, values, user=user, method="wizard", ip=ip)
    accounts.profile_changed(db, household.id, actor_id=user.id, what="household", ip=ip)
    return await _saved(request, db, household, _after_save(household, 3))


async def contact_action(request, db, user, household, form) -> tuple[dict, dict, int]:
    """The contact sub-forms of step 4 and /household/contacts: add, edit,
    remove, new confirmation link. Returns (errors, page extras, status)."""
    action = form.get("action")
    ip = security.client_ip(request)
    errors = accounts.FormErrors()
    owner_phone = accounts.user_phone(accounts.household_owner(db, household.id))
    contact = None
    if action in ("update_contact", "remove_contact", "resend_contact"):
        contact = db.get(EmergencyContact, form.get("contact_id") or "")
        if contact is None or contact.household_id != household.id:
            raise HTTPException(404, "no such contact")
    others = [c for c in accounts.active_contacts(db, household.id) if c is not contact]
    if action in ("add_contact", "update_contact"):
        values = accounts.clean_contact(
            errors, form, owner_phone=owner_phone,
            taken_priorities={c.priority for c in others},
            taken_phones={accounts.contact_phone(c) for c in others})
        if action == "add_contact" and len(others) >= accounts.MAX_CONTACTS:
            errors.add("name", f"At most {accounts.MAX_CONTACTS} emergency contacts.")
        if errors:
            return errors, {"contact_form": dict(form)}, 400
        if action == "add_contact":
            contact, link = accounts.add_contact(db, household, values, actor=user, ip=ip)
        else:
            link = accounts.update_contact(db, contact, values, actor=user, ip=ip)
        db.commit()
        await accounts.push_profile(db, household.id)
        return errors, {"share_link": link, "share_for": contact.name,
                        "emailed": bool(contact.email and link)}, 200
    if action == "remove_contact":
        accounts.remove_contact(db, contact, actor=user, ip=ip)
        db.commit()
        await accounts.push_profile(db, household.id)
        return errors, {"notice": "Contact removed."}, 200
    if action == "resend_contact":
        if contact.consent_status != "PENDING":
            return errors, {"notice": "Only a contact awaiting confirmation gets a new link."}, 409
        link, _ = accounts.request_contact_consent(
            db, contact, owner_name=user.display_name or "A resident")
        return errors, {"share_link": link, "share_for": contact.name,
                        "emailed": bool(contact.email)}, 200
    raise HTTPException(400, "unknown action")


async def _step4(request, db, principal, user, household, form):
    if (form.get("action") or "continue") == "continue":
        if not accounts.active_contacts(db, household.id):
            errors = accounts.FormErrors()
            errors.add("contacts", "Add at least one emergency contact.")
            return _page(request, db, principal, user, household, 4, errors=errors,
                         status_code=400)
        return await _saved(request, db, household, _after_save(household, 4))
    errors, extra, status = await contact_action(request, db, user, household, form)
    notice = extra.pop("notice", None)
    return _page(request, db, principal, user, household, 4, errors=errors, notice=notice,
                 status_code=status, **extra)


async def claim(request, db, principal, user, form):
    """Claim by serial + code; on success the hub issues the device token
    and the profile is pushed. Returns (outcome, status code)."""
    outcome = accounts.claim_device(db, user=user, serial=form.get("serial"),
                                    code=form.get("claim_code"),
                                    ip=security.client_ip(request))
    if not outcome.ok:
        return outcome, 429 if outcome.retry_after else 400
    db.commit()
    from sentrix_portal.services import hub
    try:
        await hub.issue_token(db, outcome.device.id)
    except Exception as exc:                          # the claim itself stands
        accounts.log.error("token issue for %s failed: %s", outcome.device.id, exc)
    await accounts.push_profile(db, user.household_id)
    return outcome, 200


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
                     notice=f"Device {outcome.device.serial} is now yours. Name it below.")
    errors = accounts.FormErrors()
    device = db.get(Device, form.get("device_id") or "")
    if device is None or device.household_id != household.id or device.state != "CLAIMED":
        raise HTTPException(404, "no such device")
    name = accounts.clean_text(errors, "name", form.get("name"), label="Device name",
                               max_len=60, required=True)
    room = accounts.clean_text(errors, "room_label", form.get("room_label"),
                               label="Room label", max_len=40, required=True)
    floor = accounts.clean_text(errors, "floor", form.get("floor"), label="Floor", max_len=20)
    mode = accounts.clean_choice(errors, "mode", form.get("mode") or "home",
                                 ("home", "armed"), label="Default mode")
    if not form.get("view_checked"):
        errors.add("view_checked", "Open the live view and tick that the camera sees the "
                                   "room as intended.")
    if errors:
        return _page(request, db, principal, user, household, 5, errors=errors,
                     device_form=dict(form), status_code=400)
    ip = security.client_ip(request)
    device.name = name
    camera = db.query(Camera).filter_by(device_id=device.id).order_by(Camera.camera_id).first()
    if camera is None:
        camera = Camera(device_id=device.id, camera_id="CAM_0")
        db.add(camera)
    camera.room_label, camera.floor = room, floor
    accounts.profile_changed(db, household.id, actor_id=user.id, what="device", ip=ip,
                             detail={"device_id": device.id, "view_checked": True,
                                     "default_mode": mode})
    target = _after_save(household, 5)
    db.commit()
    if mode == "armed":
        # Arming needs no PIN (docs/7 §5.6); queued while the device is offline.
        from sentrix_portal.services import hub
        await hub.send_command(device.id, "mode.set", {"armed": True}, _actor(principal))
    await accounts.push_profile(db, household.id)
    return see_other(target)


async def _step6(request, db, principal, user, household, form):
    if form.get("action") == "telegram":
        code = accounts.mint_telegram_link(db, subject_type="user", subject_id=user.id,
                                           household_id=household.id)
        return _page(request, db, principal, user, household, 6,
                     telegram_code=code, telegram_link=accounts.telegram_deep_link(code))
    errors = accounts.FormErrors()
    values = accounts.clean_sos_settings(errors, form,
                                         safety_was_on=bool(household.safety_defaults))
    sos_pin, duress_pin = (form.get("sos_pin") or "").strip(), (form.get("duress_pin") or "").strip()
    pins = None
    changing_pins = bool(sos_pin or duress_pin or not household.sos_pin_hash)
    if changing_pins:
        if household.sos_pin_hash and not principal.stepped_up():
            errors.add("sos_pin", "Confirm your password first to change the PINs.")
        else:
            pins = accounts.check_pins(
                errors, sos_pin, form.get("sos_pin_confirm") or "", duress_pin,
                form.get("duress_pin_confirm") or "", phone=accounts.user_phone(user),
                pincode=household.pincode,
                contacts_exist=bool(accounts.active_contacts(db, household.id)))
    if not errors and household.onboarding_completed_at is not None and \
            {"window", "safety_defaults"} & set(accounts.sos_settings_change(household, values)) \
            and not principal.stepped_up():
        errors.add("window", "Confirm your password first to change the safety settings.")
    if errors:
        return _page(request, db, principal, user, household, 6, values={
            "window": values["window"], "safety_defaults": values["safety_defaults"],
            "channels": values["channels"]}, errors=errors, status_code=400)
    ip = security.client_ip(request)
    accounts.apply_sos_settings(household, values)
    if pins is not None and pins[0]:
        sos_hash = await run_in_threadpool(security.hash_pin, pins[0])
        duress_hash = (await run_in_threadpool(security.hash_pin, pins[1])) if pins[1] else None
        accounts.apply_pins(db, household, sos_hash, duress_hash, user=user, ip=ip)
    accounts.profile_changed(db, household.id, actor_id=user.id, what="sos_settings", ip=ip)
    return await _saved(request, db, household, _after_save(household, 6))


async def _step7(request, db, principal, user, household, form):
    errors = accounts.FormErrors()
    done = accounts.onboarding_done(db, household, user)
    missing = [n for n in range(1, accounts.LAST_STEP) if not done[n]]
    if missing:
        errors.add("steps", "Finish these steps first: " + ", ".join(
            f"{n}. {accounts.ONBOARDING_STEPS[n]}" for n in missing) + ".")
    if not form.get("consent_account_sos"):
        errors.add("consent_account_sos", "SENTRIX cannot protect your home without the "
                                          "account and SOS purpose.")
    if errors:
        return _page(request, db, principal, user, household, 7, errors=errors,
                     status_code=400)
    ip = security.client_ip(request)
    for purpose in accounts.PURPOSES:
        accounts.set_consent(db, household, purpose, bool(form.get(f"consent_{purpose}")),
                             user=user, method="wizard", ip=ip)
    first = household.onboarding_completed_at is None
    household.onboarding_completed_at = household.onboarding_completed_at or utcnow()
    household.onboarding_step = accounts.LAST_STEP
    if first:
        security.audit(db, "onboarding.completed", actor_type="user", actor_id=user.id,
                       household_id=household.id, target_type="household",
                       target_id=household.id, ip=ip)
    accounts.profile_changed(db, household.id, actor_id=user.id, what="consents", ip=ip)
    return await _saved(request, db, household, "/home?welcome=1" if first else "/household")


_HANDLERS = {1: _step1, 2: _step2, 3: _step3, 4: _step4, 5: _step5, 6: _step6, 7: _step7}


# ── /claim?sn=... (the label's QR) ────────────────────────────────────────────
@router.get("/claim", response_class=HTMLResponse)
def claim_page(request: Request, sn: str = "", db: Session = Depends(get_db),
               principal=Depends(OWNER)):
    _, household = _load(db, principal)
    serial = accounts.normalize_serial(sn)
    if household.onboarding_completed_at is None:
        return see_other(f"/onboarding/5?sn={quote(serial)}" if serial else "/onboarding/5")
    return render(request, "onboarding/claim.html",
                  {"values": {"serial": serial}, "errors": {}}, active_page="home")


@router.post("/claim", response_class=HTMLResponse)
async def claim_submit(request: Request, db: Session = Depends(get_db),
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
