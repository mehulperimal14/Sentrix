"""Contact consent pages: ``GET/POST /consent/{token}`` (confirm or opt out of
being an emergency contact; token purpose ``contact_consent``). SOS contact
pages are ``/c/{token}`` (T-LINK, ``routes/contacts.py``); e-mail
verification is ``/account/verify-email/{token}`` (``routes/auth.py``).

Owner: track T-ACCT. The contact has no login:

* the page opens from the e-mailed link or the link the owner shared; a GET
  never changes anything (mail scanners open links) and every open is
  counted; the answer is a POST with the double-submit CSRF cookie;
* only a CONFIRMED contact ever receives an SOS; opting out erases the
  contact's number and e-mail at once and the rest within
  ``declined_contact_days`` (7);
* confirming shows a personal opt-out link (valid for a year) and, when the
  Telegram bot is configured, a deep link to get SOS alerts on Telegram;
* the page shows the owner's name and the relation only: never the address,
  other contacts or anything about the home.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from sentrix_portal import security
from sentrix_portal.db import get_db
from sentrix_portal.routes.auth import message_page, render
from sentrix_portal.services import accounts

router = APIRouter()

NOT_VALID = ("This link is not valid any more: it was used, replaced by a newer one, or "
             "has expired. Ask the person who added you to send a new one.")


def _context(db: Session, contact) -> dict:
    owner = accounts.household_owner(db, contact.household_id)
    from sentrix.config import settings
    return {"contact": contact,
            "owner_name": (owner.display_name or "A SENTRIX customer") if owner else
            "A SENTRIX customer",
            "purposes": accounts.PURPOSES,
            "notice_version": accounts.portal_config().notice_version,
            "declined_days": settings.retention.declined_contact_days}


@router.get("/consent/{token}", response_class=HTMLResponse)
def consent_page(request: Request, token: str, db: Session = Depends(get_db)):
    found = accounts.consent_page_target(db, token)
    if found is None:
        return message_page(request, "Link not valid", NOT_VALID, status_code=404)
    row, contact = found
    row.opened_count = (row.opened_count or 0) + 1
    csrf = security.login_csrf_token(request)
    response = render(request, "consent/consent.html",
                      {**_context(db, contact), "csrf_token": csrf})
    security.set_login_csrf(response, csrf)
    return response


@router.post("/consent/{token}", response_class=HTMLResponse)
async def consent_submit(request: Request, token: str, db: Session = Depends(get_db)):
    form = await request.form()
    if not security.verify_login_csrf(request, form.get("csrf_token")):
        return message_page(request, "Form expired", "Open the link again and answer.",
                            status_code=403)
    found = accounts.consent_page_target(db, token)
    if found is None:
        return message_page(request, "Link not valid", NOT_VALID, status_code=404)
    row, contact = found
    decision = form.get("decision")
    if decision not in ("confirm", "decline"):
        return message_page(request, "Choose an answer", "Confirm or opt out.",
                            status_code=400)
    context = _context(db, contact)
    opt_out = accounts.decide_contact_consent(db, row, contact, decision,
                                              ip=security.client_ip(request))
    telegram = None
    if decision == "confirm" and accounts.portal_config().telegram_bot_name:
        telegram = accounts.telegram_deep_link(accounts.mint_telegram_link(
            db, subject_type="contact", subject_id=contact.id,
            household_id=contact.household_id))
    db.commit()
    await accounts.push_profile(db, contact.household_id)
    return render(request, "consent/done.html", {
        **context, "decision": decision, "telegram_link": telegram,
        "opt_out_link": accounts.consent_url(opt_out) if opt_out else None})


__all__ = ["router"]
