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

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from sentrix_portal import security
from sentrix_portal.db import DB
from sentrix_portal.routes.auth import message_page, render
from sentrix_portal.services import accounts
from sentrix_portal.services import household_flows as flows

router = APIRouter()

NOT_VALID = flows.CONSENT_LINK_NOT_VALID


@router.get("/consent/{token}", response_class=HTMLResponse)
def consent_page(request: Request, token: str, db: Session = DB):
    found = accounts.consent_page_target(db, token)
    if found is None:
        return message_page(request, "Link not valid", NOT_VALID, status_code=404)
    row, contact = found
    flows.consent_opened(row)
    csrf = security.login_csrf_token(request)
    response = render(request, "consent/consent.html",
                      {**flows.consent_view(db, contact), "csrf_token": csrf})
    security.set_login_csrf(response, csrf)
    return response


@router.post("/consent/{token}", response_class=HTMLResponse)
async def consent_submit(request: Request, token: str, db: Session = DB):
    form = await request.form()
    if not security.verify_login_csrf(request, form.get("csrf_token")):
        return message_page(request, "Form expired", "Open the link again and answer.",
                            status_code=403)
    found = accounts.consent_page_target(db, token)
    if found is None:
        return message_page(request, "Link not valid", NOT_VALID, status_code=404)
    row, contact = found
    context = flows.consent_view(db, contact)
    outcome = await flows.decide_consent(db, row, contact, form.get("decision"),
                                         ip=security.client_ip(request))
    if not outcome.ok:
        return message_page(request, "Choose an answer", outcome.message, status_code=400)
    return render(request, "consent/done.html", {**context, **outcome.data})


__all__ = ["router"]
