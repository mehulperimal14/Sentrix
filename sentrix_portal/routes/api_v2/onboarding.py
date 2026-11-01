"""Onboarding (API v2): the wizard's state, each step's data and its save,
claiming a device (step 5 and the label's QR), the SOS preview. Owner only;
a later step opens only after the earlier ones (409 ``conflict`` names the
step to finish). Members and contacts of step 3 and 4 use
``/household/members`` and ``/household/contacts``; the Telegram code of
step 6 is ``POST /telegram/link-code``.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from sentrix_portal.db import DB
from sentrix_portal.models import Household, User
from sentrix_portal.routes.api_v2.common import (OWNER, ApiError, form_values, household_of,
                                                 ip_of, raise_for, user_of)
from sentrix_portal.routes.api_v2.household import (AddressFields, ContactsOut,
                                                    HouseholdDetails, MemberOut, Option,
                                                    contacts_out, member_out)
from sentrix_portal.routes.api_v2.settings import (Channels, PinState, SosValues, channels_in,
                                                   pin_state_out, sos_values_out)
from sentrix_portal.services import accounts
from sentrix_portal.services import household_flows as flows
from sentrix_portal.services.outcome import Outcome

router = APIRouter(tags=["onboarding"])


# ── the wizard ────────────────────────────────────────────────────────────────
class StepState(BaseModel):
    number: int
    title: str
    done: bool
    reachable: bool


class WizardOut(BaseModel):
    #: The furthest step reached (where an unfinished setup resumes).
    current_step: int
    completed: bool
    reachable_upto: int
    steps: list[StepState]


class StepSavedOut(BaseModel):
    #: Where the old wizard went next (``/onboarding/<n>``, ``/home?welcome=1``...).
    next: str
    wizard: WizardOut


def wizard_out(db: Session, user: User, household: Household) -> WizardOut:
    state = flows.wizard_state(db, user, household, 1)
    return WizardOut(
        current_step=min(int(household.onboarding_step or 1), accounts.LAST_STEP),
        completed=state["completed"], reachable_upto=state["reachable_upto"],
        steps=[StepState(number=n, title=title, done=bool(state["done"][n]),
                         reachable=flows.step_reachable(household, n))
               for n, title in accounts.ONBOARDING_STEPS.items()])


def _load(db: Session, principal, step: int | None = None) -> tuple[User, Household]:
    household = household_of(db, principal)
    if step is not None and not flows.step_reachable(household, step):
        current = int(household.onboarding_step or 1)
        raise ApiError("conflict", f"Finish step {current} "
                                   f"({accounts.ONBOARDING_STEPS[current]}) first.")
    return user_of(db, principal), household


def _saved(db: Session, user: User, household: Household, outcome: Outcome) -> StepSavedOut:
    raise_for(outcome, db)
    return StepSavedOut(next=outcome.data["next"], wizard=wizard_out(db, user, household))


@router.get("/onboarding", response_model=WizardOut)
def wizard(db: Session = DB, principal=Depends(OWNER)) -> WizardOut:
    user, household = _load(db, principal)
    return wizard_out(db, user, household)


# ── step 1: about you ─────────────────────────────────────────────────────────
class AboutYou(BaseModel):
    display_name: str | None = None
    phone: str | None = None
    email: str | None = None
    language: str | None = "en"
    #: "I am 18 or older and set up SENTRIX for my household."
    adult: bool = False


class AboutYouOut(BaseModel):
    wizard: WizardOut
    values: AboutYou
    languages: list[Option]


@router.get("/onboarding/1", response_model=AboutYouOut)
def step1(db: Session = DB, principal=Depends(OWNER)) -> AboutYouOut:
    user, household = _load(db, principal, 1)
    values = flows.step_values(db, user, household, 1)
    return AboutYouOut(wizard=wizard_out(db, user, household),
                       values=AboutYou(**{**values, "adult": bool(values["adult"])}),
                       languages=[Option(value=k, label=v)
                                  for k, v in accounts.LANGUAGES.items()])


@router.put("/onboarding/1", response_model=StepSavedOut)
async def save_step1(body: AboutYou, request: Request, db: Session = DB,
                     principal=Depends(OWNER)) -> StepSavedOut:
    user, household = _load(db, principal, 1)
    outcome = await flows.save_about_you(db, user, household, form_values(body),
                                         ip=ip_of(request), stepped_up=principal.stepped_up())
    return _saved(db, user, household, outcome)


# ── step 2: home address ──────────────────────────────────────────────────────
class AddressOut(BaseModel):
    wizard: WizardOut
    values: AddressFields
    states: list[str]
    nearest_kinds: list[Option]


@router.get("/onboarding/2", response_model=AddressOut)
def step2(db: Session = DB, principal=Depends(OWNER)) -> AddressOut:
    user, household = _load(db, principal, 2)
    return AddressOut(wizard=wizard_out(db, user, household),
                      values=AddressFields(**flows.step_values(db, user, household, 2)),
                      states=list(accounts.INDIAN_STATES),
                      nearest_kinds=[Option(value=k, label=label)
                                     for k, label in accounts.NEAREST_KINDS])


@router.put("/onboarding/2", response_model=StepSavedOut)
async def save_step2(body: AddressFields, request: Request, db: Session = DB,
                     principal=Depends(OWNER)) -> StepSavedOut:
    user, household = _load(db, principal, 2)
    outcome = await flows.save_address_step(db, user, household, form_values(body),
                                            ip=ip_of(request))
    return _saved(db, user, household, outcome)


# ── step 3: household ─────────────────────────────────────────────────────────
class HouseholdStepOut(BaseModel):
    wizard: WizardOut
    values: HouseholdDetails
    members: list[MemberOut]
    age_bands: list[str]
    max_members: int


@router.get("/onboarding/3", response_model=HouseholdStepOut)
def step3(db: Session = DB, principal=Depends(OWNER)) -> HouseholdStepOut:
    user, household = _load(db, principal, 3)
    return HouseholdStepOut(
        wizard=wizard_out(db, user, household),
        values=HouseholdDetails(**flows.step_values(db, user, household, 3)),
        members=[member_out(m) for m in accounts.household_members(db, household.id)],
        age_bands=list(accounts.AGE_BANDS), max_members=accounts.MAX_MEMBERS)


@router.put("/onboarding/3", response_model=StepSavedOut)
async def save_step3(body: HouseholdDetails, request: Request, db: Session = DB,
                     principal=Depends(OWNER)) -> StepSavedOut:
    user, household = _load(db, principal, 3)
    outcome = await flows.save_household_step(db, user, household, form_values(body),
                                              ip=ip_of(request))
    return _saved(db, user, household, outcome)


# ── step 4: emergency contacts ────────────────────────────────────────────────
class ContactsStepOut(BaseModel):
    wizard: WizardOut
    contacts: ContactsOut


@router.get("/onboarding/4", response_model=ContactsStepOut)
def step4(db: Session = DB, principal=Depends(OWNER)) -> ContactsStepOut:
    user, household = _load(db, principal, 4)
    return ContactsStepOut(wizard=wizard_out(db, user, household),
                           contacts=contacts_out(flows.wizard_contacts(db, household),
                                                 editable=True))


@router.post("/onboarding/4", response_model=StepSavedOut)
async def save_step4(db: Session = DB, principal=Depends(OWNER)) -> StepSavedOut:
    """Continue: at least one contact (pending or confirmed)."""
    user, household = _load(db, principal, 4)
    return _saved(db, user, household, await flows.continue_contacts_step(db, household))


# ── step 5: the device ────────────────────────────────────────────────────────
class AssignedDevice(BaseModel):
    id: str
    serial: str


class ClaimedDevice(BaseModel):
    id: str
    serial: str
    name: str | None
    room_label: str | None
    floor: str | None
    online: bool


class DeviceStepOut(BaseModel):
    wizard: WizardOut
    #: The serial from a scanned label (``?sn=``), normalised.
    serial: str
    assigned: list[AssignedDevice]
    claimed: list[ClaimedDevice]


class ClaimIn(BaseModel):
    serial: str | None = None
    claim_code: str | None = None


class ClaimOut(BaseModel):
    device: AssignedDevice
    message: str


class DeviceSetupIn(BaseModel):
    name: str | None = None
    room_label: str | None = None
    floor: str | None = None
    mode: Literal["home", "armed"] | None = "home"
    #: "I have checked the view."
    view_checked: bool = False


@router.get("/onboarding/5", response_model=DeviceStepOut)
def step5(sn: str = "", db: Session = DB, principal=Depends(OWNER)) -> DeviceStepOut:
    user, household = _load(db, principal, 5)
    extra = flows.step_extra(db, principal, user, household, 5)
    return DeviceStepOut(
        wizard=wizard_out(db, user, household),
        serial=flows.step_values(db, user, household, 5, sn)["serial"],
        assigned=[AssignedDevice(id=d.id, serial=d.serial) for d in extra["assigned"]],
        claimed=[ClaimedDevice(id=row["device"].id, serial=row["device"].serial,
                               name=row["device"].name,
                               room_label=row["camera"].room_label if row["camera"] else None,
                               floor=row["camera"].floor if row["camera"] else None,
                               online=row["online"]) for row in extra["claimed"]])


async def claim_device(db: Session, request: Request, principal, body: ClaimIn) -> ClaimOut:
    user = user_of(db, principal)
    outcome, status = await flows.claim(db, user, serial=body.serial, code=body.claim_code,
                                        ip=ip_of(request))
    if not outcome.ok:
        db.commit()                                   # the attempt counts (rate limit)
        if status == 429:
            raise ApiError("rate_limited", outcome.error, retry_after=outcome.retry_after,
                           fields={"claim_code": outcome.error})
        raise ApiError("validation", outcome.error, fields={"claim_code": outcome.error})
    return ClaimOut(device=AssignedDevice(id=outcome.device.id, serial=outcome.device.serial),
                    message=flows.claimed_message(outcome.device))


@router.post("/onboarding/5/claim", response_model=ClaimOut)
async def step5_claim(body: ClaimIn, request: Request, db: Session = DB,
                      principal=Depends(OWNER)) -> ClaimOut:
    """Serial + claim code from the label or the letter (5 wrong tries lock
    claiming for 15 minutes)."""
    _load(db, principal, 5)
    return await claim_device(db, request, principal, body)


@router.put("/onboarding/5/devices/{device_id}", response_model=StepSavedOut)
async def save_step5(device_id: str, body: DeviceSetupIn, request: Request, db: Session = DB,
                     principal=Depends(OWNER)) -> StepSavedOut:
    """Name, room label, floor, default mode and "I have checked the view"."""
    user, household = _load(db, principal, 5)
    device = flows.claimed_device_of(db, household, device_id)
    if device is None:
        raise ApiError("not_found", "That device is not claimed by your household.")
    outcome = await flows.save_device_step(db, principal, user, household, device,
                                           form_values(body), ip=ip_of(request))
    return _saved(db, user, household, outcome)


# ── step 6: alerts and SOS ────────────────────────────────────────────────────
class SosStepOut(BaseModel):
    wizard: WizardOut
    values: SosValues
    window_choices: list[int]
    pins: PinState
    telegram_linked: bool
    bot_configured: bool
    stepped_up: bool


class SosStepIn(BaseModel):
    window: int | None = None
    safety_defaults: bool = False
    #: Needed to switch the safety defaults off.
    safety_off_ack: bool = False
    channels: Channels = Channels()
    sos_pin: str | None = None
    sos_pin_confirm: str | None = None
    duress_pin: str | None = None
    duress_pin_confirm: str | None = None


@router.get("/onboarding/6", response_model=SosStepOut)
def step6(db: Session = DB, principal=Depends(OWNER)) -> SosStepOut:
    user, household = _load(db, principal, 6)
    extra = flows.step_extra(db, principal, user, household, 6)
    return SosStepOut(wizard=wizard_out(db, user, household),
                      values=sos_values_out(flows.sos_values(household)),
                      window_choices=list(extra["window_choices"]),
                      pins=pin_state_out(extra), telegram_linked=extra["telegram_linked"],
                      bot_configured=extra["bot_configured"], stepped_up=extra["stepped_up"])


@router.put("/onboarding/6", response_model=StepSavedOut)
async def save_step6(body: SosStepIn, request: Request, db: Session = DB,
                     principal=Depends(OWNER)) -> StepSavedOut:
    """Window, safety defaults, channels and the PINs (changing a PIN that
    is set, or the safety settings after setup, needs a fresh password)."""
    user, household = _load(db, principal, 6)
    values = form_values(body.model_dump(exclude={"channels"}))
    values.update(channels_in(body.channels))
    outcome = await flows.save_sos_step(db, principal, user, household, values,
                                        ip=ip_of(request))
    return _saved(db, user, household, outcome)


# ── step 7: consent and review ────────────────────────────────────────────────
class Purpose(BaseModel):
    key: str
    title: str
    text: str


class Preview(BaseModel):
    full: str
    sms: str


class ReviewOut(BaseModel):
    wizard: WizardOut
    notice_version: str
    purposes: list[Purpose]
    consents: dict[str, bool]
    preview: Preview
    confirmed_contacts: list[str]
    pending_contacts: list[str]


class ConsentsIn(BaseModel):
    consent_account_sos: bool = False
    consent_face_recognition: bool = False
    consent_medical_notes: bool = False
    consent_sos_snapshot: bool = False
    consent_remote_view: bool = False
    consent_desk_live_view: bool = False
    consent_model_improvement: bool = False


class FinishedOut(StepSavedOut):
    #: The first finish completes the setup.
    first: bool


def purposes_out() -> list[Purpose]:
    return [Purpose(key=k, title=v["title"], text=v["text"]) for k, v in accounts.PURPOSES.items()]


@router.get("/onboarding/7", response_model=ReviewOut)
def step7(db: Session = DB, principal=Depends(OWNER)) -> ReviewOut:
    user, household = _load(db, principal, 7)
    review = flows.review_state(db, household)
    return ReviewOut(wizard=wizard_out(db, user, household),
                     notice_version=review["notice_version"], purposes=purposes_out(),
                     consents=review["consents"], preview=Preview(**review["preview"]),
                     confirmed_contacts=[c.name for c in review["confirmed_contacts"]],
                     pending_contacts=[c.name for c in review["pending_contacts"]])


@router.put("/onboarding/7", response_model=FinishedOut)
async def save_step7(body: ConsentsIn, request: Request, db: Session = DB,
                     principal=Depends(OWNER)) -> FinishedOut:
    user, household = _load(db, principal, 7)
    outcome = await flows.finish_onboarding(db, user, household, form_values(body),
                                            ip=ip_of(request))
    raise_for(outcome, db)
    return FinishedOut(next=outcome.data["next"], first=outcome.data["first"],
                       wizard=wizard_out(db, user, household))


@router.get("/onboarding/preview", response_model=Preview)
def preview(db: Session = DB, principal=Depends(OWNER)) -> Preview:
    """The SOS as it would be sent, from what the household entered."""
    household = household_of(db, principal)
    return Preview(**accounts.render_sos_preview(accounts.preview_profile(db, household)))


# ── /claim (the label's QR, after setup) ──────────────────────────────────────
@router.post("/claim", response_model=ClaimOut)
async def claim(body: ClaimIn, request: Request, db: Session = DB,
                principal=Depends(OWNER)) -> ClaimOut:
    household_of(db, principal)
    return await claim_device(db, request, principal, body)


__all__ = ["router", "WizardOut", "wizard_out", "Purpose", "purposes_out", "ConsentsIn",
           "Preview", "ClaimIn", "ClaimOut", "claim_device"]
