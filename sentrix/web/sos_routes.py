"""/api/sos/* - the LC JSON API for assistance requests (docs/7 §9.2). Every
route requires the LC session or a verified tunnel identity; POST routes need
the CSRF header.

Owner: track T-SOS. Included by ``sentrix.web.extensions``. The pages that
use it (the SOS prompt and banner, ``/sos``) are T-WEB's.

These are thin wrappers: parse the input, build the actor from the
principal (``sentrix.web.auth.require_lc``), call one method of the SOS
service (``sentrix.sos.current_service()``) and map its ``SosResult`` to a
status. Roles, consents and rate limits of tunnelled requests are the link's
path table's (``sentrix/link/policy.py``); the business rules (the SOS PIN,
who may approve, 409 on a closed request) are the service's.

What every caller sees is the **owner view** (the duress flag and the help
path behind it hidden, ``states.owner_view``), except a Desk operator, who
gets the Desk view.

==========  ==========================================  ===========================
Method      Path                                        Body / query
==========  ==========================================  ===========================
GET         /api/sos                                    -> {current, recent}
GET         /api/sos/requests                           ?limit (<= 200) &offset
GET         /api/sos/requests/{id}                      -> {request, events, chain}
POST        /api/sos/requests/{id}/approve              {authority}
POST        /api/sos/requests/{id}/decline              {pin}  403 pin_invalid, 429 pin_locked
POST        /api/sos/requests/{id}/checking             -
POST        /api/sos/requests/{id}/safe                 {pin}
POST        /api/sos/requests/{id}/help-now             {authority}: a new linked request
GET         /api/sos/preview                            ?incident_type&level&evidence_state
POST        /api/sos/demo/close-incident                test mode only
POST        /api/sos/demo/trigger                       {incident_type}; test mode, device admin
==========  ==========================================  ===========================
"""

from __future__ import annotations

import asyncio
import hmac
import re
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import JSONResponse

from sentrix.config import settings
from sentrix.sos import current_service
from sentrix.web import auth

router = APIRouter()

_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_PREVIEW_TYPES = ("weapon", "fire", "assault", "intrusion", "duress")


def _principal(request: Request) -> auth.LcPrincipal:
    return request.state.lc_principal


def _require_csrf(request: Request) -> None:
    """LC sessions send their CSRF token back as ``X-CSRF-Token``; tunnelled
    requests were checked by the Portal before they were forwarded (§5.8)."""
    principal = _principal(request)
    if principal.kind != "lc":
        return
    supplied = request.headers.get(settings.web.csrf_header, "")
    if not (supplied and principal.csrf and hmac.compare_digest(supplied, principal.csrf)):
        error = auth.LcError("csrf", "missing or wrong CSRF token")
        error.status_code = 403
        raise error


def _check_id(request_id: str) -> str:
    if not _REQUEST_ID.match(request_id or ""):
        raise auth.LcError("not_found", "unknown request")
    return request_id


def _view(principal: auth.LcPrincipal) -> str:
    return "desk" if principal.role == "operator" else "owner"


def _project(request: dict | None, principal: auth.LcPrincipal) -> dict | None:
    service = current_service()
    project = getattr(service, "project", None)
    if project is not None:
        return project(request, _view(principal))
    from sentrix.sos.service import owner_projection
    return owner_projection(request)


def _visible(rows: list[dict], principal: auth.LcPrincipal) -> list[dict]:
    projected = (_project(r, principal) for r in rows)
    return [r for r in projected if r is not None]


def _result(result: dict, *, retry_after: float | None = None) -> JSONResponse:
    """A ``SosResult`` as JSON, or the §13 error (``{"detail", "code"}``)."""
    if not result["ok"]:
        headers = None
        if result["code"] == "pin_locked" and retry_after:
            headers = {"Retry-After": str(max(1, int(retry_after + 0.999)))}
        raise auth.LcError(result["code"], result["message"] or result["code"], headers=headers)
    return JSONResponse({"ok": True, "message": result["message"],
                         "request": result["request"]})


def _retry_after() -> float:
    service = current_service()
    limiter = getattr(service, "limiter", None)
    if limiter is None:
        from sentrix.sos import pins
        limiter = pins.LIMITER
    return limiter.retry_after()


async def _respond(request: Request, request_id: str, action: str, *,
                   authority: str | None = None, pin: str | None = None) -> JSONResponse:
    _require_csrf(request)
    principal = _principal(request)
    result = await asyncio.to_thread(
        current_service().respond, _check_id(request_id), action,
        actor=principal.to_actor(), channel=principal.channel,
        authority=(authority or None), pin=pin)
    return _result(result, retry_after=_retry_after() if result["code"] == "pin_locked"
                   else None)


# ── reads ─────────────────────────────────────────────────────────────────────
@router.get("/api/sos")
async def api_sos(principal: auth.LcPrincipal = Depends(auth.require_lc())) -> JSONResponse:
    """The open request (or null) and the most recent ones."""
    from sentrix.storage import requests

    def read() -> dict[str, Any]:
        service = current_service()
        current = service.current(_view(principal))
        recent = _visible(requests.list_requests(10), principal)
        return {"current": current, "recent": recent, "status": service.status()}

    return JSONResponse(await asyncio.to_thread(read))


@router.get("/api/sos/requests")
async def api_sos_requests(limit: int = Query(50, ge=1, le=200),
                           offset: int = Query(0, ge=0),
                           principal: auth.LcPrincipal = Depends(auth.require_lc())
                           ) -> JSONResponse:
    from sentrix.storage import requests

    def read() -> list:
        return _visible(requests.list_requests(limit, offset), principal)

    return JSONResponse({"requests": await asyncio.to_thread(read), "limit": limit,
                         "offset": offset})


@router.get("/api/sos/requests/{request_id}")
async def api_sos_request(request_id: str,
                          principal: auth.LcPrincipal = Depends(auth.require_lc())
                          ) -> JSONResponse:
    """One request, its audit trail and whether the chain verifies. Owners
    never see restricted rows (nor gaps that would hint at them)."""
    from sentrix.sos.service import owner_events
    from sentrix.storage import requests
    request_id = _check_id(request_id)

    def read() -> dict[str, Any] | None:
        row = requests.get_request(request_id)
        shown = _project(row, principal)
        if shown is None:
            return None              # unknown, or a silent request the household never saw
        view = _view(principal)
        if view == "owner":
            events = owner_events(requests.events(request_id, include_restricted=True))
        else:
            events = requests.events(request_id, include_restricted=True)
        chain = requests.verify_chain(request_id)
        return {"request": shown, "events": events,
                "chain": {"ok": chain["ok"]} if view == "owner" else chain}

    payload = await asyncio.to_thread(read)
    if payload is None:
        raise auth.LcError("not_found", "unknown request")
    return JSONResponse(payload)


@router.get("/api/sos/preview")
async def api_sos_preview(incident_type: str = Query("weapon"), level: int = Query(3, ge=3, le=5),
                          evidence_state: str = Query("confirmed"),
                          _principal_: auth.LcPrincipal = Depends(auth.require_lc())
                          ) -> JSONResponse:
    """Every text a request would send right now, from the current profile
    (works with SOS off too: it is how a household checks its SOS text)."""
    from sentrix.alerting import guard
    from sentrix.link import profile as profile_module
    from sentrix.sos import policy, templates

    if incident_type not in _PREVIEW_TYPES:
        raise auth.LcError("invalid_args", f"incident_type must be one of {_PREVIEW_TYPES}")
    if evidence_state not in ("confirmed", "corroborated"):
        raise auth.LcError("invalid_args", "evidence_state must be confirmed or corroborated")

    def render() -> dict[str, Any]:
        profile = profile_module.effective()
        test_mode = guard.test_mode()
        window = policy.window_seconds(profile, settings.sos, test_mode=test_mode)
        out = templates.preview(profile, incident_type=incident_type, level=level,
                                evidence_state=evidence_state, test_mode=test_mode,
                                window_seconds=window)
        out["window_seconds"] = window
        return out

    return JSONResponse(await asyncio.to_thread(render))


# ── answers ───────────────────────────────────────────────────────────────────
@router.post("/api/sos/requests/{request_id}/approve")
async def api_sos_approve(request: Request, request_id: str,
                          authority: str | None = Body(None, embed=True),
                          _p: auth.LcPrincipal = Depends(auth.require_lc())) -> JSONResponse:
    """"Alert my contacts and the SENTRIX desk" (Police / Fire / Medical)."""
    return await _respond(request, request_id, "approve", authority=authority)


@router.post("/api/sos/requests/{request_id}/decline")
async def api_sos_decline(request: Request, request_id: str,
                          pin: str | None = Body(None, embed=True),
                          _p: auth.LcPrincipal = Depends(auth.require_lc())) -> JSONResponse:
    """"False alarm (PIN)"."""
    return await _respond(request, request_id, "decline", pin=pin)


@router.post("/api/sos/requests/{request_id}/checking")
async def api_sos_checking(request: Request, request_id: str,
                           _p: auth.LcPrincipal = Depends(auth.require_lc())) -> JSONResponse:
    """"I'm checking": pauses the reminders, never the window or the safety timers."""
    return await _respond(request, request_id, "checking")


@router.post("/api/sos/requests/{request_id}/safe")
async def api_sos_safe(request: Request, request_id: str,
                       pin: str | None = Body(None, embed=True),
                       _p: auth.LcPrincipal = Depends(auth.require_lc())) -> JSONResponse:
    """"I'm safe now (PIN)": on the help path the Desk is told; only the Desk
    or a contact closes the request."""
    return await _respond(request, request_id, "safe", pin=pin)


@router.post("/api/sos/requests/{request_id}/help-now")
async def api_sos_help_now(request: Request, request_id: str,
                           authority: str | None = Body(None, embed=True),
                           _p: auth.LcPrincipal = Depends(auth.require_lc())) -> JSONResponse:
    """"Request help now" on an unanswered, declined or cancelled request:
    a new request linked to it, approved at once."""
    return await _respond(request, request_id, "help_now", authority=authority)


# ── demo controls (test mode only) ────────────────────────────────────────────
@router.post("/api/sos/demo/close-incident")
async def api_sos_demo_close(request: Request,
                             principal: auth.LcPrincipal = Depends(auth.require_lc())
                             ) -> JSONResponse:
    _require_csrf(request)
    result = await asyncio.to_thread(current_service().demo_close_incident,
                                     actor=principal.to_actor())
    return _result(result)


@router.post("/api/sos/demo/trigger")
async def api_sos_demo_trigger(request: Request,
                               incident_type: str = Body("weapon", embed=True),
                               principal: auth.LcPrincipal = Depends(
                                   auth.require_lc("device_admin"))
                               ) -> JSONResponse:
    """A disclosed scripted L3 ("SIMULATED DETECTION"), test mode only, for
    the device admin only (the demo's backup when a clip does not trigger;
    design §6). The SOS text and the audit say SIMULATED DETECTION."""
    _require_csrf(request)
    result = await asyncio.to_thread(current_service().demo_trigger, incident_type,
                                     actor=principal.to_actor())
    return _result(result)


__all__ = ["router"]
