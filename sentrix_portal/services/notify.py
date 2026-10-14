"""The Portal's channel router: Telegram, e-mail, its outbox and worker (docs/7 §8).

Owner: track T-CH. Foundation stub with the frozen signatures: nothing is
queued and nothing is sent until T-CH lands.

* ``router(db)`` -> a ``channels.base.ChannelRouter`` over the Portal's
  ``notifications`` table (``meta`` round-trips buttons, HTML, urgency and the
  recipient's name and role), with the guard built by
  ``guard.SendPolicy.from_env(test_mode_env="PORTAL_TEST_MODE")``.
* ``enqueue(db, recipient, message)`` -> outbox id, or None (duplicate
  ``request|recipient|channel|step`` or no such channel).
* ``start(app)`` runs the delivery worker and, in ``SENTRIX_TELEGRAM_MODE=
  polling``, ``getUpdates`` long polling; inbound callbacks go to
  ``services.sos.respond`` (``/hooks/telegram`` in webhook mode).
* Status changes are reported to listeners (``ChannelsService``), which
  T-LINK uses for ``delivered`` / ``undelivered``. SUPPRESSED (dry run) is
  never delivered; the Desk queue is (``sos.update delivered``).
"""

from __future__ import annotations

from typing import Any


def router(db) -> Any:
    raise NotImplementedError("services.notify.router is track T-CH's")


def enqueue(db, recipient, message) -> str | None:
    return None


def start(app):
    return lambda: None


__all__ = ["router", "enqueue", "start"]
