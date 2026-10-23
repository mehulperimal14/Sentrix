"""Inbound provider callbacks: ``POST /hooks/telegram`` (docs/7 §8.4, §11)
and ``GET``/``POST /hooks/whatsapp`` (D17). Owner: track T-CH.

Unauthenticated by session (gate P2 lists it): the proof is Telegram's
``X-Telegram-Bot-Api-Secret-Token`` header, compared in constant time with
``TELEGRAM_WEBHOOK_SECRET`` (the value given to ``setWebhook``). Without a
configured secret the route does not exist (404), so a Portal in polling mode
accepts no forged updates. Everything else — private chats only, the sender
must be the linked chat, single-use nonces — is
``services.notify.handle_update``'s.

The answer is 200 for every authenticated update, handled or not, because
Telegram re-sends anything else for a day; re-deliveries are recognised by
``update_id``.

WhatsApp (Meta's Cloud API): ``GET`` is the subscription handshake
(``hub.verify_token`` must equal ``WHATSAPP_VERIFY_TOKEN``; the answer is
``hub.challenge`` as plain text); ``POST`` must carry ``X-Hub-Signature-256``,
an HMAC-SHA256 of the raw body with ``WHATSAPP_APP_SECRET``. Without the
secret (or the verify token) the routes do not exist (404). Every signed body
gets 200 (Meta retries anything else); messages are recognised by their id.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from sentrix.alerting.channels import telegram, whatsapp
from sentrix_portal.services import notify

log = logging.getLogger("sentrix_portal.hooks")

router = APIRouter()

#: Telegram updates are small; anything larger is not one.
MAX_UPDATE_BYTES = 256 * 1024


def _error(code: str, detail: str, status: int) -> JSONResponse:
    return JSONResponse({"detail": detail, "code": code}, status_code=status)


@router.post("/hooks/telegram", include_in_schema=False)
async def telegram_webhook(request: Request) -> JSONResponse:
    if not telegram.webhook_secret():
        return _error("not_found", "not found", 404)
    if not telegram.webhook_secret_ok(request.headers.get(telegram.SECRET_HEADER)):
        log.warning("telegram webhook: wrong secret token from %s",
                    request.client.host if request.client else "?")
        return _error("unauthorised", "invalid secret token", 401)
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > MAX_UPDATE_BYTES:
        return _error("too_large", "update too large", 413)
    body = await request.body()
    if len(body) > MAX_UPDATE_BYTES:
        return _error("too_large", "update too large", 413)
    try:
        update = json.loads(body or b"{}")
    except ValueError:
        return _error("invalid_args", "the body must be JSON", 400)
    if not isinstance(update, dict):
        return _error("invalid_args", "an update is a JSON object", 400)
    try:
        outcome = await notify.handle_update(request.app, update)
    except Exception as exc:                  # never make Telegram retry a crash loop
        log.error("telegram update %s failed: %s", update.get("update_id"), exc,
                  exc_info=True)
        outcome = {"handled": "error"}
    return JSONResponse({"ok": True, "handled": outcome.get("handled")})


@router.get("/hooks/whatsapp", include_in_schema=False)
async def whatsapp_verify(request: Request):
    if not whatsapp.verify_token():
        return _error("not_found", "not found", 404)
    query = request.query_params
    challenge = whatsapp.verify_subscription(query.get("hub.mode"),
                                             query.get("hub.verify_token"),
                                             query.get("hub.challenge"))
    if challenge is None:
        return _error("forbidden_role", "verification failed", 403)
    return PlainTextResponse(challenge[:200])


@router.post("/hooks/whatsapp", include_in_schema=False)
async def whatsapp_webhook(request: Request) -> JSONResponse:
    if not whatsapp.app_secret():
        return _error("not_found", "not found", 404)
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > MAX_UPDATE_BYTES:
        return _error("too_large", "body too large", 413)
    body = await request.body()
    if len(body) > MAX_UPDATE_BYTES:
        return _error("too_large", "body too large", 413)
    if not whatsapp.signature_ok(body, request.headers.get(whatsapp.SIGNATURE_HEADER)):
        log.warning("whatsapp webhook: bad signature from %s",
                    request.client.host if request.client else "?")
        return _error("unauthorised", "invalid signature", 401)
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        return _error("invalid_args", "the body must be JSON", 400)
    if not isinstance(payload, dict):
        return _error("invalid_args", "a webhook body is a JSON object", 400)
    try:
        outcomes = await notify.handle_whatsapp(request.app, payload)
    except Exception as exc:                  # never make Meta retry a crash loop
        log.error("whatsapp webhook failed: %s", exc, exc_info=True)
        outcomes = [{"handled": "error"}]
    return JSONResponse({"ok": True, "handled": [o.get("handled") for o in outcomes]})


__all__ = ["router", "MAX_UPDATE_BYTES"]
