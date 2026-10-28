"""/privacy: the standalone privacy notice (with its version), one consent
toggle per purpose (withdrawing is as easy as granting), the data export
(JSON) and account erasure (scheduled, cancellable for a grace period), and
the grievance contact. /privacy/notice is the notice on its own page.

Owner: track T-ACCT (DPDP Act 2023 / Rules 2025 and the IT Act SPDI Rules;
design §4.3, decisions on data protection). Owners change consents, export
and erase (export and erasure need a fresh password); members read the
notice and see the household's consents.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from sentrix_portal import security
from sentrix_portal.db import DB
from sentrix_portal.routes.auth import household_of, render, see_other, user_of
from sentrix_portal.services import accounts
from sentrix_portal.services import household_flows as flows

router = APIRouter()

VIEW = security.require_role("owner", "member")
OWNER = security.require_role("owner")

SAVED = flows.PRIVACY_SAVED


def _household(db: Session, principal):
    household = household_of(db, principal)
    if household is None:
        raise HTTPException(404, "no household")
    return household


def _page(request, db, principal, household, *, notice=None, errors=None, status_code=200):
    return render(request, "privacy/privacy.html", {
        **accounts.notice_context(), "household": household,
        "consents": accounts.consent_states(db, household.id),
        "history": accounts.consent_history(db, household.id)[:50],
        "editable": principal.role == "owner", "stepped_up": principal.stepped_up(),
        "erasure_due": accounts.iso(accounts.erasure_due_at(household)),
        "notice": notice, "errors": errors or {},
    }, status_code=status_code, active_page="privacy")


@router.get("/privacy", response_class=HTMLResponse)
def privacy_page(request: Request, saved: str = "", db: Session = DB,
                 principal=Depends(VIEW)):
    return _page(request, db, principal, _household(db, principal), notice=SAVED.get(saved))


@router.get("/privacy/notice", response_class=HTMLResponse)
def notice_page(request: Request, principal=Depends(security.require_role())):
    return render(request, "privacy/notice.html", accounts.notice_context(),
                  active_page="privacy")


@router.post("/privacy/consents", response_class=HTMLResponse)
async def consents_submit(request: Request, db: Session = DB,
                          principal=Depends(OWNER)):
    household = _household(db, principal)
    user = user_of(db, principal)
    form = await request.form()
    outcome = await flows.save_consents(db, user, household, form,
                                        ip=security.client_ip(request))
    if not outcome.ok:
        return _page(request, db, principal, household, errors=outcome.errors,
                     status_code=outcome.status)
    return see_other("/privacy?saved=consents")


@router.get("/privacy/export", response_class=HTMLResponse)
def export_page(request: Request, stepup: str = "", db: Session = DB,
                principal=Depends(OWNER)):
    return render(request, "privacy/export.html",
                  {"stepped_up": principal.stepped_up(), "stepup": stepup},
                  active_page="privacy")


@router.post("/privacy/export")
def export_download(request: Request, db: Session = DB,
                    principal=Depends(OWNER)):
    """Everything the Portal holds about the household, as JSON (right of
    access). Needs a fresh password: the file holds addresses and numbers."""
    outcome = flows.export_data(db, principal, household_of(db, principal),
                                ip=security.client_ip(request))
    if outcome.code == "step_up_required":
        return see_other("/privacy/export?stepup=required")
    if not outcome.ok:
        raise HTTPException(404, "no household")
    return export_response(outcome)


def export_response(outcome) -> Response:
    """The export as a download (also the JSON API's answer)."""
    filename = outcome.data["filename"]
    return Response(outcome.data["body"], media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"',
                             "Cache-Control": "no-store"})


def _erase_page(request, db, principal, household, *, errors=None, status_code=200):
    return render(request, "privacy/erase.html", {
        "household": household, "stepped_up": principal.stepped_up(),
        "erasure_due": accounts.iso(accounts.erasure_due_at(household)),
        "grace_days": accounts.ERASURE_GRACE_DAYS, "errors": errors or {}},
        status_code=status_code, active_page="privacy")


@router.get("/privacy/erase", response_class=HTMLResponse)
def erase_page(request: Request, db: Session = DB, principal=Depends(OWNER)):
    return _erase_page(request, db, principal, _household(db, principal))


@router.post("/privacy/erase", response_class=HTMLResponse)
async def erase_submit(request: Request, db: Session = DB,
                       principal=Depends(OWNER)):
    """``action=schedule`` (fresh password + typing ERASE) or ``cancel``."""
    household = _household(db, principal)
    user = user_of(db, principal)
    ip = security.client_ip(request)
    form = await request.form()
    if form.get("action") == "cancel":
        flows.cancel_erasure(db, user, household, ip=ip)
        return see_other("/privacy?saved=cancelled")
    outcome = flows.schedule_erasure(db, principal, user, household, form.get("confirm"),
                                     ip=ip)
    if not outcome.ok:
        return _erase_page(request, db, principal, household, errors=outcome.errors,
                           status_code=outcome.status)
    return see_other("/privacy?saved=erasure")


__all__ = ["router", "export_response"]
