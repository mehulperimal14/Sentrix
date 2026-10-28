"""Settings (API v2): the SOS window, safety defaults and channels, the PINs,
Telegram linking, and the household's devices (name, room, floor, unpair).
Owners change them; members read the SOS and device sections. Safety
settings, PINs and unpairing need a fresh password (``step_up_required``).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from sentrix_portal.db import DB
from sentrix_portal.models import Device, Household
from sentrix_portal.routes.api_v2.common import (HOUSEHOLD, OWNER, ApiError, Ok, form_values,
                                                 household_of, ip_of, raise_for, user_of, z)
from sentrix_portal.services import accounts
from sentrix_portal.services import household_flows as flows

router = APIRouter(tags=["settings"])


class Channels(BaseModel):
    email: bool = False
    telegram: bool = False
    whatsapp: bool = False
    sms: bool = False


class SosValues(BaseModel):
    #: The answer window in seconds (one of ``window_choices``).
    window: int | None
    safety_defaults: bool
    channels: Channels


class PinState(BaseModel):
    pin_set: bool
    duress_set: bool
    contacts_exist: bool
    pin_length: int
    duress_digits: int


class TelegramLinkOut(BaseModel):
    id: str
    linked_at: str | None


class TelegramOut(BaseModel):
    bot_configured: bool
    links: list[TelegramLinkOut]


class DeviceOut(BaseModel):
    id: str
    serial: str
    name: str | None
    state: str
    claimed: bool
    online: bool
    room_label: str | None
    floor: str | None


class SettingsOut(BaseModel):
    editable: bool
    stepped_up: bool
    step_up_seconds: int
    sos: SosValues
    window_choices: list[int]
    #: Owners only (members see the SOS and device sections).
    pins: PinState | None
    telegram: TelegramOut | None
    devices: list[DeviceOut]


class SosSettingsIn(BaseModel):
    window: int | None = None
    safety_defaults: bool = False
    #: Needed to switch the safety defaults off.
    safety_off_ack: bool = False
    channels: Channels = Channels()


class PinsIn(BaseModel):
    sos_pin: str | None = None
    sos_pin_confirm: str | None = None
    duress_pin: str | None = None
    duress_pin_confirm: str | None = None


class DeviceIn(BaseModel):
    name: str | None = None
    room_label: str | None = None
    floor: str | None = None


class TelegramCodeOut(BaseModel):
    #: Single use, 24 h: send the bot ``/start <code>``.
    code: str
    #: ``https://t.me/<bot>?start=<code>``; null when no bot is configured.
    link: str | None


def channels_in(channels: Channels) -> dict[str, str]:
    """The form fields ``channel_<key>`` the flows read."""
    return {f"channel_{key}": "on" for key, on in channels.model_dump().items() if on}


def sos_values_out(values: dict[str, Any]) -> SosValues:
    return SosValues(window=values["window"], safety_defaults=bool(values["safety_defaults"]),
                     channels=Channels(**values["channels"]))


def pin_state_out(state: dict[str, Any]) -> PinState:
    return PinState(**{k: state[k] for k in PinState.model_fields})


def device_out(row: dict[str, Any]) -> DeviceOut:
    device, camera = row["device"], row["camera"]
    return DeviceOut(id=device.id, serial=device.serial, name=device.name, state=device.state,
                     claimed=device.state == "CLAIMED", online=row["online"],
                     room_label=camera.room_label if camera else None,
                     floor=camera.floor if camera else None)


def _settings(db: Session, principal, household: Household) -> SettingsOut:
    owner = principal.role == "owner"
    user = user_of(db, principal)
    return SettingsOut(
        editable=owner, stepped_up=principal.stepped_up(),
        step_up_seconds=accounts.portal_config().step_up_seconds,
        sos=sos_values_out(flows.sos_values(household)),
        window_choices=list(accounts.window_choices()),
        pins=pin_state_out(flows.pin_state(db, household)) if owner else None,
        telegram=TelegramOut(
            bot_configured=bool(accounts.portal_config().telegram_bot_name),
            links=[TelegramLinkOut(id=link.id, linked_at=z(link.linked_at))
                   for link in accounts.telegram_links(db, "user", user.id)])
        if owner else None,
        devices=[device_out(row) for row in flows.device_rows(db, household)])


@router.get("/settings", response_model=SettingsOut)
def get_settings(db: Session = DB, principal=Depends(HOUSEHOLD)) -> SettingsOut:
    return _settings(db, principal, household_of(db, principal))


@router.put("/settings/sos", response_model=SettingsOut)
async def save_sos(body: SosSettingsIn, request: Request, db: Session = DB,
                   principal=Depends(OWNER)) -> SettingsOut:
    """Changes apply to future requests only (each request freezes its policy)."""
    household = household_of(db, principal)
    values = form_values(body.model_dump(exclude={"channels"}))
    values.update(channels_in(body.channels))
    outcome = await flows.save_sos_settings(db, principal, user_of(db, principal), household,
                                            values, ip=ip_of(request))
    raise_for(outcome, db)
    return _settings(db, principal, household)


@router.put("/settings/pins", response_model=SettingsOut)
async def save_pins(body: PinsIn, request: Request, db: Session = DB,
                    principal=Depends(OWNER)) -> SettingsOut:
    """Both PINs together; the device gets them with the next profile."""
    household = household_of(db, principal)
    outcome = await flows.save_pins(db, principal, user_of(db, principal), household,
                                    form_values(body), ip=ip_of(request))
    raise_for(outcome, db)
    return _settings(db, principal, household)


# ── Telegram ──────────────────────────────────────────────────────────────────
@router.post("/telegram/link-code", response_model=TelegramCodeOut, status_code=201)
def telegram_link_code(db: Session = DB, principal=Depends(OWNER)) -> TelegramCodeOut:
    """A single-use code to link the owner's own Telegram chat."""
    household = household_of(db, principal)
    return TelegramCodeOut(**flows.telegram_code(db, user_of(db, principal), household))


@router.delete("/telegram/links/{link_id}", response_model=Ok)
async def telegram_unlink(link_id: str, request: Request, db: Session = DB,
                          principal=Depends(OWNER)) -> Ok:
    household = household_of(db, principal)
    user = user_of(db, principal)
    link = flows.telegram_link_of(db, user, link_id)
    if link is None:
        raise ApiError("not_found", "That Telegram chat is not linked.")
    outcome = await flows.unlink_telegram(db, user, household, link, ip=ip_of(request))
    return Ok(message=outcome.message)


# ── devices ───────────────────────────────────────────────────────────────────
def claimed_device(db: Session, household: Household, device_id: str) -> Device:
    device = flows.claimed_device_of(db, household, device_id)
    if device is None:
        raise ApiError("not_found", "That device is not claimed by your household.")
    return device


@router.patch("/devices/{device_id}", response_model=DeviceOut)
async def save_device(device_id: str, body: DeviceIn, request: Request, db: Session = DB,
                      principal=Depends(OWNER)) -> DeviceOut:
    """Name, room label (what the SOS says under WHERE) and floor."""
    household = household_of(db, principal)
    device = claimed_device(db, household, device_id)
    outcome = await flows.save_device(db, user_of(db, principal), household, device,
                                      form_values(body), ip=ip_of(request))
    raise_for(outcome, db)
    row = next(r for r in flows.device_rows(db, household) if r["device"].id == device.id)
    return device_out(row)


@router.post("/devices/{device_id}/unpair", response_model=Ok)
async def unpair_device(device_id: str, request: Request, db: Session = DB,
                        principal=Depends(OWNER)) -> Ok:
    """Owner + fresh password: the device stops sending alerts to the Portal
    until SENTRIX gives a new claim code."""
    household = household_of(db, principal)
    device = claimed_device(db, household, device_id)
    outcome = await flows.unpair_device(db, principal, user_of(db, principal), household,
                                        device, ip=ip_of(request))
    raise_for(outcome, db)
    return Ok(message=outcome.message)


__all__ = ["router", "Channels", "SosValues", "PinState", "channels_in", "sos_values_out",
           "pin_state_out", "DeviceOut", "device_out", "claimed_device"]
