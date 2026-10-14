"""Inbound provider callbacks: POST /hooks/telegram (X-Telegram-Bot-Api-Secret-
Token checked; private chats only; callback_data = request id + nonce; sender
must be the linked chat). csrf=False: the secret header is the proof.

Owner: track T-CH. Included by ``sentrix_portal.app.create_app``; empty in
the foundation.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()

__all__ = ["router"]
