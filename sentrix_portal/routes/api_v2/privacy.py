"""Privacy (API v2): the consents (one per purpose), the privacy notice,
the consent history, the data export (a JSON download, fresh password) and
the account's erasure (scheduled, cancellable). Owners change; members read.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from sentrix_portal.db import DB
from sentrix_portal.routes.api_v2.common import (ANY, ERROR_RESPONSES, HOUSEHOLD, OWNER,
                                                 ApiError, form_values, household_of,
                                                 ip_of, raise_for, user_of, z)
from sentrix_portal.routes.api_v2.onboarding import ConsentsIn, Purpose, purposes_out
from sentrix_portal.services import accounts
from sentrix_portal.services import household_flows as flows

router = APIRouter(tags=["privacy"])


class NoticeOut(BaseModel):
    notice_version: str
    purposes: list[Purpose]
    grievance: str
    grace_days: int
    #: The retention caps the notice states (days; ``event_log_max_gb``).
    retention: dict[str, float]


class ConsentRecord(BaseModel):
    purpose: str
    granted_at: str | None
    withdrawn_at: str | None
    notice_version: str | None
    method: str | None


class PrivacyOut(BaseModel):
    editable: bool
    stepped_up: bool
    consents: dict[str, bool]
    history: list[ConsentRecord]
    erasure_due: str | None
    grace_days: int
    notice: NoticeOut


class EraseIn(BaseModel):
    #: Type ERASE in capitals to confirm.
    confirm: str | None = None


class ErasureOut(BaseModel):
    ok: bool = True
    message: str
    erasure_due: str | None


def notice_out() -> NoticeOut:
    context = accounts.notice_context()
    caps = context["caps"]
    retention = {name: float(getattr(caps, name))
                 for name in getattr(caps, "__dataclass_fields__", {})
                 if isinstance(getattr(caps, name), (int, float))}
    return NoticeOut(notice_version=context["notice_version"], purposes=purposes_out(),
                     grievance=context["grievance"], grace_days=context["grace_days"],
                     retention=retention)


def _privacy(db: Session, principal) -> PrivacyOut:
    household = household_of(db, principal)
    return PrivacyOut(
        editable=principal.role == "owner", stepped_up=principal.stepped_up(),
        consents=accounts.consent_states(db, household.id),
        history=[ConsentRecord(purpose=c.purpose, granted_at=z(c.granted_at),
                               withdrawn_at=z(c.withdrawn_at), notice_version=c.notice_version,
                               method=c.method)
                 for c in accounts.consent_history(db, household.id)[:50]],
        erasure_due=z(accounts.erasure_due_at(household)),
        grace_days=accounts.ERASURE_GRACE_DAYS, notice=notice_out())


@router.get("/privacy", response_model=PrivacyOut)
def privacy(db: Session = DB, principal=Depends(HOUSEHOLD)) -> PrivacyOut:
    return _privacy(db, principal)


@router.get("/privacy/notice", response_model=NoticeOut)
def notice(principal=Depends(ANY)) -> NoticeOut:
    return notice_out()


@router.put("/privacy/consents", response_model=PrivacyOut)
async def save_consents(body: ConsentsIn, request: Request, db: Session = DB,
                        principal=Depends(OWNER)) -> PrivacyOut:
    """Withdrawing is as easy as granting; ``account_sos`` is the service
    itself (close the account to withdraw it)."""
    household = household_of(db, principal)
    outcome = await flows.save_consents(db, user_of(db, principal), household,
                                        form_values(body), ip=ip_of(request))
    raise_for(outcome, db)
    return _privacy(db, principal)


@router.post("/privacy/export", response_class=Response,
             responses={200: {"content": {"application/json": {}},
                              "description": "sentrix-export-<code>.json (a download)"},
                        **ERROR_RESPONSES})
def export(request: Request, db: Session = DB, principal=Depends(OWNER)) -> Response:
    """Everything the Portal holds about the household, as a JSON download
    (needs a fresh password: the file holds addresses and numbers)."""
    from sentrix_portal.models import Household
    household = db.get(Household, principal.household_id) if principal.household_id else None
    outcome = flows.export_data(db, principal, household, ip=ip_of(request))
    if outcome.code == "not_found":
        raise ApiError("not_found", "There is no household on this account.")
    raise_for(outcome, db)
    from sentrix_portal.routes.privacy import export_response
    return export_response(outcome)


@router.post("/privacy/erase", response_model=ErasureOut)
def schedule_erasure(body: EraseIn, request: Request, db: Session = DB,
                     principal=Depends(OWNER)) -> ErasureOut:
    """Close the account: erased after the grace period, cancellable until
    then (fresh password + typing ERASE)."""
    household = household_of(db, principal)
    outcome = flows.schedule_erasure(db, principal, user_of(db, principal), household,
                                     body.confirm, ip=ip_of(request))
    raise_for(outcome, db)
    return ErasureOut(message=outcome.message, erasure_due=z(outcome.data["due"]))


@router.post("/privacy/erase/cancel", response_model=ErasureOut)
def cancel_erasure(request: Request, db: Session = DB, principal=Depends(OWNER)) -> ErasureOut:
    household = household_of(db, principal)
    outcome = flows.cancel_erasure(db, user_of(db, principal), household, ip=ip_of(request))
    return ErasureOut(message=outcome.message, erasure_due=None)


__all__ = ["router", "notice_out", "NoticeOut"]
